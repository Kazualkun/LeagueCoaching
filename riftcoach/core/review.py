"""As marcacoes do replay, e a memoria delas entre sessoes.

Isto fecha um pedido que estava aberto desde o inicio do projeto: marcar
SEMPRE os erros e os erros criticos, deixar o jogador marcar os dele, e
retomar a revisao com tudo no lugar.

DUAS FONTES, UMA LINHA DO TEMPO:

  ai    vem do motor de vantagem. Gravidade MEDIDA em pontos de probabilidade
        de vitoria, nao opinada por modelo nenhum.
  user  vem da pessoa, durante o replay, com o texto que ela quiser.

Elas ficam na mesma lista de proposito. O que ensina nao e nenhuma das duas
sozinha: e onde a IA marcou critico e a pessoa nao sentiu nada, e onde a
pessoa sentiu que errou e a medicao nao viu. O primeiro caso e ponto cego; o
segundo, quase sempre, e um erro real que a telemetria da Riot nao consegue
enxergar — troca de dano, combo, posicionamento fino.

A GRAVACAO E ATOMICA. A pessoa vai fechar a janela no meio da revisao; se a
escrita for direta no arquivo final, uma hora ela fecha exatamente durante o
flush e perde as anotacoes da sessao inteira. Escreve-se ao lado e troca-se o
nome, que no mesmo volume o Windows garante ser atomico.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from riftcoach.analysis.advantage import find_blunders
from riftcoach.config import data_dir
from riftcoach.core.schema import (
    CoachingReport,
    Mark,
    MarkKind,
    ReviewSession,
)
from riftcoach.parse.facts import MatchFacts


def sessions_dir() -> Path:
    d = data_dir() / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _caminho(match_id: str, puuid: str) -> Path:
    # O puuid e longo demais para nome de arquivo e nao acrescenta nada depois
    # dos primeiros caracteres: o par (partida, inicio do puuid) ja e unico,
    # porque dois jogadores da mesma partida diferem bem antes do 12o.
    return sessions_dir() / f"{match_id}__{puuid[:12]}.json"


# --------------------------------------------------------------------------
# Da analise para as marcacoes
# --------------------------------------------------------------------------


def marks_from_report(
    report: CoachingReport, facts: MatchFacts, *, max_marks: int = 12
) -> list[Mark]:
    """As marcacoes da IA, ja ordenadas no tempo.

    O texto de cada marcacao e o `claim`, nao o `fix`. No overlay a pessoa
    esta OLHANDO o momento acontecer: ela precisa saber o que observar, e a
    correcao vem em seguida, quando ela ja viu. Inverter isso faz ler a
    resposta antes de entender a pergunta.

    `wp_loss` vem do motor de vantagem, casado por proximidade no tempo. Sem
    blunder medido por perto o campo fica vazio — preencher com estimativa
    transformaria em numero o que e so ordenacao.
    """
    blunders = find_blunders(facts)
    marcas: list[Mark] = []

    for f in report.ranked()[:max_marks]:
        wp = None
        if blunders:
            perto = min(blunders, key=lambda b: abs(b.t_ms - f.timestamp_ms))
            # 45 s e a mesma janela que `find_blunders` usa para atribuir causa.
            # Fora dela, o blunder e outro evento e emprestar o numero dele
            # seria inventar evidencia.
            if abs(perto.t_ms - f.timestamp_ms) <= 45_000:
                wp = round(perto.wp_loss, 2)

        kind: MarkKind = "critical" if f.severity >= 4 else "error"
        marcas.append(
            Mark(
                t_ms=f.timestamp_ms,
                author="ai",
                kind=kind,
                text=f.claim,
                category=f.category,
                severity=f.severity,
                wp_loss=wp,
            )
        )
    return sorted(marcas, key=lambda m: m.t_ms)


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------


def load_session(match_id: str, puuid: str) -> ReviewSession | None:
    p = _caminho(match_id, puuid)
    if not p.exists():
        return None
    try:
        return ReviewSession.model_validate_json(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Arquivo corrompido nao pode derrubar a revisao. Perder as anotacoes e
        # ruim; nao conseguir mais abrir a partida seria pior.
        return None


def save_session(rs: ReviewSession) -> Path:
    p = _caminho(rs.match_id, rs.puuid)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(rs.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def open_session(
    match_id: str,
    puuid: str,
    report: CoachingReport | None = None,
    facts: MatchFacts | None = None,
) -> ReviewSession:
    """Abre a revisao, criando ou ATUALIZANDO as marcacoes da IA.

    Reanalisar a mesma partida com um modelo melhor deve trocar as marcacoes
    da IA — e NAO pode encostar nas do jogador. Por isso a substituicao e
    seletiva em vez de recriar o arquivo: as anotacoes dele sao o unico dado
    aqui que nao da para recalcular.
    """
    rs = load_session(match_id, puuid) or ReviewSession(match_id=match_id, puuid=puuid)
    if report is not None and facts is not None:
        do_usuario = [m for m in rs.marks if m.author == "user"]
        rs.marks = sorted(marks_from_report(report, facts) + do_usuario, key=lambda m: m.t_ms)
        save_session(rs)
    return rs


def add_user_mark(rs: ReviewSession, t_ms: int, kind: MarkKind = "note", text: str = "") -> Mark:
    """Coloca uma marcacao do jogador e grava na hora.

    Gravar a cada marcacao, e nao ao sair, porque nao existe "ao sair": a
    pessoa fecha o replay quando termina de assistir, nao quando termina de
    anotar.
    """
    m = Mark(
        t_ms=max(0, t_ms),
        author="user",
        kind=kind,
        text=text or f"marcado em {t_ms // 60_000}:{(t_ms // 1000) % 60:02d}",
    )
    rs.add(m)
    rs.last_position_ms = m.t_ms
    save_session(rs)
    return m


def export_marks(rs: ReviewSession) -> str:
    """As marcacoes em texto, para colar num Discord de time.

    Existe porque coach de time nao quer abrir programa nenhum: quer a lista
    de minutos para conferir no replay junto com o jogador.
    """
    linhas = [f"# Revisao {rs.match_id}", ""]
    for m in rs.timeline():
        t = f"{m.t_ms // 60_000}:{(m.t_ms // 1000) % 60:02d}"
        quem = "IA" if m.author == "ai" else "voce"
        grav = f" [gravidade {m.severity}]" if m.severity else ""
        custo = f" (-{m.wp_loss:.1f} pts)" if m.wp_loss else ""
        linhas.append(f"{t}  ({quem}){grav}{custo}  {m.text}")
    return "\n".join(linhas)


def json_marks(rs: ReviewSession) -> str:
    return json.dumps(
        {
            "match_id": rs.match_id,
            "exported_at": int(time.time()),
            "marks": [m.model_dump() for m in rs.timeline()],
        },
        ensure_ascii=False,
        indent=2,
    )
