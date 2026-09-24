"""Da partida para o estado do overlay.

Separado de `run.py` por um motivo pratico: montar o estado precisa de rede,
cache e analise; rodar o laco precisa do jogo aberto. Quem esta mexendo na
aparencia do overlay quer montar um estado e olhar o desenho sem esperar a
Riot responder, e quem esta mexendo no laco quer um estado pronto sem refazer
a analise a cada tentativa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from riftcoach.analysis.report import analyze, analyze_with_ai
from riftcoach.core.errors import RiftCoachError
from riftcoach.core.review import load_session, open_session, save_session
from riftcoach.core.schema import CoachingReport, ReviewSession
from riftcoach.knowledge.benchmarks import Benchmark, BenchmarkTable
from riftcoach.knowledge.sync import PatchDB
from riftcoach.overlay.locais import local_do_evento
from riftcoach.overlay.run import focus_track
from riftcoach.overlay.scene import OverlayState
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from riftcoach.riot.client import RiotClient


@dataclass
class Preparado:
    state: OverlayState
    session: ReviewSession
    facts: MatchFacts
    report: CoachingReport
    # Calculados aqui, e nao so no caminho da CLI, para que o relatorio aberto
    # pela janela tenha o MESMO conteudo. Dois caminhos que produzem
    # relatorios diferentes e um bug esperando acontecer.
    benchmarks: list[Benchmark] = field(default_factory=list)


async def preparar(
    riot_id: str,
    *,
    match_id: str | None = None,
    last: int = 1,
    use_ai: bool = True,
    hud_scale: float = 1.0,
    minimap_rotated: bool = False,
    reanalisar: bool = True,
) -> Preparado:
    """Busca, analisa, e devolve tudo o que o overlay precisa.

    `reanalisar=False` aproveita as marcacoes ja gravadas da revisao, quando
    existem, e pula a analise inteira. E o caminho que a janela usa ao abrir o
    overlay logo depois de ter analisado: refazer a chamada de IA ali gastaria
    minutos e cota para chegar exatamente no mesmo resultado.

    A resolucao entra como 1600x900 provisoria: o laco sobrescreve com o
    tamanho real da janela do League no primeiro quadro. Chutar aqui evitaria
    que o estado existisse antes de o jogo abrir, e e justamente antes de o
    jogo abrir que a gente quer conseguir montar o estado.
    """
    nome, _, tag = riot_id.partition("#")
    async with RiotClient() as rc:
        conta = await rc.account_by_riot_id(nome.strip(), tag.strip())
        puuid: str = conta["puuid"]
        alvo = match_id
        if alvo is None:
            ids = await rc.match_ids(puuid, count=max(1, last))
            if not ids:
                raise RiftCoachError(
                    "nenhuma partida encontrada",
                    hint="Jogue uma partida de Summoner's Rift e tente de novo.",
                )
            alvo = ids[min(last, len(ids)) - 1]
        match: dict[str, Any] = await rc.match(alvo)
        timeline: dict[str, Any] = await rc.timeline(alvo)

    facts = distill(match, timeline, puuid)
    db = PatchDB()
    db.use_patch(facts.patch)

    guardada = None if reanalisar else load_session(facts.match_id, puuid)
    if guardada is not None and guardada.marks:
        # Ja existe revisao com marcacoes: elas SAO o resultado da analise.
        # Recalcular so para chegar no mesmo lugar gastaria minutos e cota.
        report, _txt = analyze(facts, resolver=db)
        sessao = guardada
    else:
        if use_ai:
            from riftcoach.llm.router import ModelRouter

            router = await ModelRouter.create()
            try:
                report, _txt, _tr = await analyze_with_ai(facts, router, resolver=db, patch_db=db)
            finally:
                await router.aclose()
        else:
            report, _txt = analyze(facts, resolver=db)
        sessao = open_session(facts.match_id, puuid, report, facts)

    trilha = focus_track(timeline, facts.focus.participant_id)
    times = {j.participant_id: j.team_id for j in (*facts.team, *facts.enemy)}
    _dar_lugar(sessao, timeline, facts.focus.participant_id, times)
    marcas_de_referencia = BenchmarkTable(patch=facts.patch).evaluate(facts, None)

    st = OverlayState(
        width=1600,
        height=900,
        duration_ms=facts.duration_s * 1000,
        marks=sessao.timeline(),
        hud_scale=hud_scale,
        minimap_rotated=minimap_rotated,
        focus_track=trilha,
        focus_champion=facts.focus.champion,
        match_id=facts.match_id,
    )
    return Preparado(
        state=st,
        session=sessao,
        facts=facts,
        report=report,
        benchmarks=marcas_de_referencia,
    )


def _dar_lugar(
    sessao: ReviewSession,
    timeline: dict[str, Any],
    participant_id: int,
    times: dict[int, int],
) -> None:
    """Preenche `where` e `you` das marcacoes que ainda nao tem.

    Feito aqui, e nao em `marks_from_report`, por um motivo pratico: a timeline
    crua e grande e so existe neste ponto do caminho. Guardar ela inteira na
    revisao para calcular isto depois custaria megabytes por partida.

    Idempotente: marcacao que ja tem lugar nao e tocada. Assim a revisao
    guardada em disco nao perde posicao quando o formato da timeline mudar.
    """
    from riftcoach.parse.posicoes import Posicoes

    pos = Posicoes(timeline, times)
    mudou = False
    for m in sessao.marks:
        if m.where is None:
            m.where = local_do_evento(timeline, m.t_ms)
            mudou = mudou or m.where is not None
        # `you` e SEMPRE recalculado. A versao anterior interpolava entre
        # frames de minuto e gravava o resultado; manter esses valores velhos
        # manteria o "VOCE" no lugar errado para sempre nas revisoes antigas.
        est = pos.onde(participant_id, m.t_ms)
        novo = (est.x, est.y) if est is not None and est.confiavel else None
        erro = round(est.erro_u) if est is not None and est.confiavel else None
        if (m.you, m.you_err_u) != (novo, erro):
            m.you, m.you_err_u = novo, erro
            mudou = True
    if mudou:
        save_session(sessao)
