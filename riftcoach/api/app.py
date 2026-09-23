"""A interface web: FastAPI servindo a SPA e controlando o replay.

ESCOPO DELIBERADO: o servidor escuta SO em 127.0.0.1. Nao ha autenticacao
porque nao ha superficie remota — e se algum dia houver, a autenticacao precisa
vir antes do bind, nao depois.

A ponte com o client do League passa inteira pelo `ReplayGuard`. A rota /seek
nao fala com a porta 2999; ela pede ao controller, que pede ao guard, que
verifica antes de cada requisicao. Um 404 do client vira 409 aqui, com a
explicacao — nao um 500 generico.
"""

from __future__ import annotations

import webbrowser
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from riftcoach.analysis.pergunta import LIMITE_DA_PERGUNTA, responder
from riftcoach.analysis.report import AiTrace, analyze, analyze_with_ai, render_finding
from riftcoach.core.errors import LiveGameRefused, RiftCoachError
from riftcoach.core.schema import CoachingReport
from riftcoach.knowledge.benchmarks import Benchmark, BenchmarkTable
from riftcoach.knowledge.sync import PatchDB
from riftcoach.parse.distill import distill, mmss
from riftcoach.parse.facts import MatchFacts
from riftcoach.replay.controller import ReplayController
from riftcoach.replay.guard import open_guard
from riftcoach.replay.rofl import find_replay
from riftcoach.riot.client import RiotClient

STATIC_DIR = Path(__file__).parent / "static"
HOST = "127.0.0.1"


@dataclass
class Session:
    """O relatorio aberto agora. Um de cada vez, em memoria.

    Nao ha banco de sessao porque nao ha multiplos usuarios: e um app local que
    uma pessoa abre para revisar as proprias partidas. Persistir isso seria
    complexidade sem beneficio.
    """

    facts: MatchFacts | None = None
    report: CoachingReport | None = None
    benchmarks: list[Benchmark] = field(default_factory=list)
    trace: AiTrace | None = None
    controller: ReplayController | None = None
    replay_path: Path | None = None

    @property
    def ready(self) -> bool:
        return self.facts is not None and self.report is not None


SESSION = Session()


class _RoteadorGuardado:
    """Um ModelRouter reaproveitado entre perguntas.

    Criar um custa 2,4s medidos — ele sonda os provedores, inclusive um Ollama
    que pode nem estar de pe. Pagar isso a cada pergunta dobraria a espera de
    um recurso que e interativo por natureza: quem digitou e olha para a tela
    tolera muito menos que um passe de analise que roda sozinho.

    O lock existe porque duas perguntas podem chegar juntas (a pagina e o
    overlay, por exemplo) e sem ele as duas criariam roteadores em paralelo —
    dois baldes de cota separados, cada um achando que tem os 8.000 tokens
    inteiros, e as duas chamadas levando 429.
    """

    def __init__(self) -> None:
        self._router: Any = None
        self._lock: Any = None

    async def obter(self) -> Any:
        import asyncio

        from riftcoach.llm.router import ModelRouter

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._router is None:
                self._router = await ModelRouter.create()
            return self._router

    async def fechar(self) -> None:
        if self._router is not None:
            await self._router.aclose()
            self._router = None


ROTEADOR = _RoteadorGuardado()


class Pergunta(BaseModel):
    """Corpo de /api/perguntar. O limite e o mesmo do motor, de proposito:
    validar aqui devolve 422 com a explicacao antes de gastar cota."""

    pergunta: str = Field(min_length=1, max_length=LIMITE_DA_PERGUNTA)
    finding: int | None = None


def _report_payload(s: Session) -> dict[str, Any]:
    """O relatorio, no formato que a SPA consome."""
    assert s.facts is not None and s.report is not None
    f, r = s.facts, s.report

    return {
        "match_id": f.match_id,
        "patch": f.patch,
        "duration_s": f.duration_s,
        "win": f.focus.win,
        "champion": f.focus.champion,
        "position": f.focus.position,
        "kda": f"{f.focus.kills}/{f.focus.deaths}/{f.focus.assists}",
        "opponent": f.opponent.champion if f.opponent else None,
        "used_ai": bool(s.trace and s.trace.used_ai),
        "model_trace": r.model_trace,
        "validation": s.trace.validation if s.trace else "",
        "has_replay": s.replay_path is not None,
        "top_three": r.top_three,
        "findings": [
            {
                "index": i,
                "category": fi.category,
                "phase": fi.phase,
                "severity": fi.severity,
                "timestamp_ms": fi.timestamp_ms,
                "seek_ms": fi.seek_ms,
                "at": mmss(fi.timestamp_ms),
                "seek_at": mmss(fi.seek_ms),
                "claim": fi.claim,
                "fix": fi.fix,
                "drill": fi.drill,
                "confidence": fi.confidence,
                "tier": fi.best_tier.value,
                "evidence": [
                    {
                        "tier": e.tier.value,
                        "at": mmss(e.timestamp_ms),
                        "statement": e.statement,
                        "assumption": e.assumption,
                        "source": e.source,
                    }
                    for e in fi.evidence
                ],
                "text": "\n".join(render_finding(fi, i + 1)),
            }
            for i, fi in enumerate(r.ranked())
        ],
        "benchmarks": [
            {
                "metric": b.metric,
                "value": round(b.value, 2),
                "percentile": round(b.percentile, 1),
                "median": round(b.median, 2),
                "source": b.source,
                "source_label": b.source_label,
                "weak": b.is_weak,
                "strong": b.is_strong,
                "label": b.render(),
            }
            for b in s.benchmarks
        ],
        # A curva alimenta o grafico da linha do tempo. Uma amostra por minuto
        # basta para ver a forma; por segundo seria ruido e peso.
        "advantage": _advantage_series(f),
    }


def _advantage_series(f: MatchFacts) -> list[dict[str, float]]:
    from riftcoach.analysis.advantage import advantage_series

    return [
        {"minute": i, "wp": round(100 * e.win_probability, 1)}
        for i, e in enumerate(advantage_series(f))
    ]


def create_app() -> Any:
    """Monta o app. Import tardio do FastAPI: ele e um extra opcional.

    Quem instalou so o essencial precisa continuar usando a CLI sem tropecar
    num ImportError de uma dependencia que nunca vai usar.
    """
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise RiftCoachError(
            "a interface web nao esta instalada",
            hint="Rode: uv sync --extra web",
        ) from e

    app = FastAPI(title="RiftCoach AI", docs_url=None, redoc_url=None)

    @app.get("/api/report")
    async def get_report() -> Any:
        if not SESSION.ready:
            raise HTTPException(status_code=404, detail="nenhuma partida analisada ainda")
        return _report_payload(SESSION)

    @app.get("/api/replay/status")
    async def replay_status() -> Any:
        """A UI pergunta isto para decidir se mostra o botao de pular.

        Recusa NAO e erro nesta rota: "nao ha replay rodando" e o estado
        normal, e transformar isso em 4xx faria a UI piscar erro o tempo todo.
        """
        if SESSION.replay_path is None:
            return {"available": False, "reason": "nenhum arquivo .rofl encontrado"}
        try:
            client, guard = await open_guard()
        except LiveGameRefused as e:
            return {"available": False, "reason": e.message}
        try:
            estado = await guard.assert_replay_mode()
            return {
                "available": True,
                "time": estado.time,
                "length": estado.length,
                "paused": estado.paused,
            }
        except LiveGameRefused as e:
            return {"available": False, "reason": e.message, "hint": e.hint}
        finally:
            await client.aclose()

    @app.post("/api/seek/{index}")
    async def seek(index: int) -> Any:
        """Pula o replay para um finding.

        409, e nao 500, quando o client recusa: nao ha replay rodando e essa e
        uma condicao esperada que o usuario resolve abrindo um. A mensagem do
        guard vai junto, inteira.
        """
        if not SESSION.ready or SESSION.report is None:
            raise HTTPException(status_code=404, detail="nenhuma partida analisada")
        ranqueados = SESSION.report.ranked()
        if not 0 <= index < len(ranqueados):
            raise HTTPException(status_code=404, detail=f"finding {index} nao existe")

        finding = ranqueados[index]

        # `open_guard` FICA DENTRO do try, e isso ja foi bug uma vez.
        #
        # Quando ele so montava o cliente, nao levantava nada, e deixa-lo fora
        # era inofensivo. Depois passou a conferir a impressao digital do
        # certificado — e a recusa escapava do except e virava 500, que e
        # exatamente o que a docstring desta rota promete nunca acontecer.
        try:
            client, guard = await open_guard()
        except LiveGameRefused as e:
            return JSONResponse(status_code=409, content={"error": e.message, "hint": e.hint})

        try:
            controller = ReplayController(guard)
            alvo = await controller.seek_to_ms(finding.timestamp_ms)
            await controller.focus_camera(SESSION.facts.focus.champion if SESSION.facts else None)
            return {"ok": True, "replay_time_s": round(alvo, 2), "at": mmss(finding.seek_ms)}
        except LiveGameRefused as e:
            return JSONResponse(status_code=409, content={"error": e.message, "hint": e.hint})
        finally:
            await client.aclose()

    @app.post("/api/perguntar")
    async def perguntar(corpo: Pergunta) -> Any:
        """Pergunta em texto sobre a partida aberta.

        `finding` opcional ancora a pergunta num momento: sem ele o modelo
        recebe a partida inteira e nao sabe de qual dos erros o jogador fala
        quando pergunta so "por que isso foi ruim?".

        Erro de cota vira 429 com a mensagem do roteador, e nao 500: o teto
        de tokens por minuto e uma condicao NORMAL do tier gratuito, e a
        pagina precisa poder dizer "espere um pouco" em vez de "deu erro".
        """
        if not SESSION.ready or SESSION.facts is None or SESSION.report is None:
            raise HTTPException(status_code=404, detail="nenhuma partida analisada ainda")

        alvo = None
        if corpo.finding is not None:
            ranqueados = SESSION.report.ranked()
            if not 0 <= corpo.finding < len(ranqueados):
                raise HTTPException(
                    status_code=404, detail=f"finding {corpo.finding} nao existe"
                )
            alvo = ranqueados[corpo.finding]

        db = PatchDB()
        db.use_patch(SESSION.facts.patch)
        try:
            router = await ROTEADOR.obter()
            resposta = await responder(
                router, SESSION.facts, corpo.pergunta, finding=alvo, resolver=db
            )
        except RiftCoachError as e:
            codigo = 429 if "cota" in e.message.lower() else 400
            return JSONResponse(
                status_code=codigo, content={"error": e.message, "hint": e.hint or ""}
            )
        return {"resposta": resposta.texto, "provedor": resposta.provedor}

    @app.get("/")
    async def index() -> Any:
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


# --------------------------------------------------------------------------
# Preparacao da sessao
# --------------------------------------------------------------------------


async def prepare_session(
    riot_id: str,
    match_id: str | None = None,
    last: int = 1,
    use_ai: bool = True,
    history: list[MatchFacts] | None = None,
    tier: str | None = None,
) -> Session:
    """Busca, destila e analisa — o mesmo caminho da CLI.

    A web e uma casca sobre as mesmas funcoes, de proposito: uma segunda
    implementacao do pipeline divergiria da primeira em semanas.
    """
    from riftcoach.llm.router import ModelRouter

    nome, _, tag = riot_id.partition("#")
    async with RiotClient() as rc:
        conta = await rc.account_by_riot_id(nome.strip(), tag.strip())
        puuid = conta["puuid"]
        alvo = match_id
        if alvo is None:
            ids = await rc.match_ids(puuid, count=max(1, last))
            if not ids:
                raise RiftCoachError(
                    "nenhuma partida encontrada", hint="Jogue uma partida, ou passe --match."
                )
            alvo = ids[min(last, len(ids)) - 1]
        match = await rc.match(alvo)
        timeline = await rc.timeline(alvo)

    facts = distill(match, timeline, puuid)
    db = PatchDB()
    db.use_patch(facts.patch)

    tabela = BenchmarkTable(patch=facts.patch, history=history)
    marks = tabela.evaluate(facts, tier)

    if use_ai:
        router = await ModelRouter.create()
        try:
            report, _texto, trace = await analyze_with_ai(
                facts, router, tier=tier, history=history, resolver=db, patch_db=db
            )
        finally:
            await router.aclose()
    else:
        report, _texto = analyze(facts, tier=tier, history=history, resolver=db)
        trace = AiTrace(note="--no-ai")

    SESSION.facts = facts
    SESSION.report = report
    SESSION.benchmarks = marks
    SESSION.trace = trace
    SESSION.replay_path = find_replay(facts.match_id)
    return SESSION


def adopt(
    facts: MatchFacts,
    report: CoachingReport,
    benchmarks: list[Benchmark] | None = None,
    trace: AiTrace | None = None,
) -> Session:
    """Entrega ao servidor uma analise que JA foi feita em outro lugar.

    Existe por causa de um bug que chegou ate o usuario: a janela analisa pelo
    caminho de `overlay/prepare.py` e o servidor le de `SESSION`, que so era
    preenchida por `prepare_session`. Resultado — a pagina abria (200), a SPA
    pedia /api/report, levava 404, e quem clicou em "Ler relatorio" via
    "pagina nao encontrada" com a analise pronta na memoria do processo ao
    lado.

    A licao vale alem deste bug: onde ha dois caminhos para produzir a mesma
    coisa, tem de haver UM lugar onde ela e entregue. Este e o lugar.
    """
    SESSION.facts = facts
    SESSION.report = report
    SESSION.benchmarks = benchmarks or []
    SESSION.trace = trace
    SESSION.replay_path = find_replay(facts.match_id)
    return SESSION


def esta_no_ar(port: int, host: str = HOST) -> bool:
    """O servidor ja aceita conexao nesta porta?

    Quem abre o navegador precisa disto: esperar um tempo fixo e chutar, e o
    chute erra na maquina lenta — que e justamente onde o servidor demora mais
    a subir.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def serve(port: int = 8770, open_browser: bool = True) -> None:
    """Sobe o servidor. SO em 127.0.0.1 — ver o escopo no topo do modulo."""
    import sys

    import uvicorn

    app = create_app()
    url = f"http://{HOST}:{port}/"
    if open_browser:
        webbrowser.open(url)
    # Sem console (pythonw), sys.stdout e None e o formatter do uvicorn morre
    # em `sys.stdout.isatty()` — antes de ligar na porta. Sem stream nao ha
    # log para configurar, entao a configuracao inteira sai de cena. Quem
    # chama daqui ja costuma arrumar os streams; esta funcao nao depende
    # disso porque quem quebrava era ela.
    sem_saida = sys.stdout is None or sys.stderr is None
    uvicorn.run(
        app,
        host=HOST,
        port=port,
        log_level="warning",
        log_config=None if sem_saida else uvicorn.config.LOGGING_CONFIG,
    )


def session_as_dict() -> dict[str, Any]:
    return asdict(SESSION.trace) if SESSION.trace else {}
