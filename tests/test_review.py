"""Marcacoes: nascer da analise, sobreviver ao fechamento da janela.

O teste que mais importa aqui e
`test_reanalisar_troca_as_marcacoes_da_ia_e_preserva_as_suas`. As marcacoes da
IA sao recalculaveis a qualquer momento; as anotacoes da pessoa, nao. Perde-las
numa reanalise seria o unico dano irreversivel que este modulo pode causar.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riftcoach.analysis.report import analyze
from riftcoach.core.review import (
    add_user_mark,
    export_marks,
    json_marks,
    load_session,
    marks_from_report,
    open_session,
    save_session,
)
from riftcoach.core.schema import CoachingReport, Mark, ReviewSession
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from tests.test_distill import load


@pytest.fixture(autouse=True)
def _dados_em_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """As revisoes vao para uma pasta temporaria.

    Sem isto o teste gravaria na pasta de dados de quem esta rodando — e o pior
    e que na segunda execucao ele leria a revisao que ele mesmo deixou, e
    passaria por motivo errado.
    """
    monkeypatch.setattr("riftcoach.core.review.data_dir", lambda: tmp_path)


@pytest.fixture(scope="module")
def facts() -> MatchFacts:
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


@pytest.fixture(scope="module")
def report(facts: MatchFacts) -> CoachingReport:
    r, _ = analyze(facts)
    return r


# --------------------------------------------------------------------------
# Da analise para as marcacoes
# --------------------------------------------------------------------------


def test_as_marcacoes_saem_em_ordem_cronologica(report: CoachingReport, facts: MatchFacts) -> None:
    """A regua desenha na ordem do tempo; o relatorio ordena por gravidade.
    Converter sem reordenar faria a regua sair embaralhada."""
    marcas = marks_from_report(report, facts)
    assert marcas
    assert [m.t_ms for m in marcas] == sorted(m.t_ms for m in marcas)


def test_gravidade_alta_vira_marcacao_critica(report: CoachingReport, facts: MatchFacts) -> None:
    for m in marks_from_report(report, facts):
        assert m.author == "ai"
        assert (m.kind == "critical") == ((m.severity or 0) >= 4)


def test_o_texto_da_marcacao_e_o_que_observar_nao_a_correcao(
    report: CoachingReport, facts: MatchFacts
) -> None:
    """No overlay a pessoa esta OLHANDO o momento. Mostrar a correcao antes de
    ela ver o que aconteceu e dar a resposta antes da pergunta."""
    marcas = marks_from_report(report, facts)
    claims = {f.claim for f in report.findings}
    fixes = {f.fix for f in report.findings}
    assert all(m.text in claims for m in marcas)
    assert not any(m.text in fixes for m in marcas if m.text not in claims)


def test_custo_so_aparece_quando_ha_blunder_medido_por_perto(
    report: CoachingReport, facts: MatchFacts
) -> None:
    """`wp_loss` e medicao, nao enfeite. Emprestar o numero de um blunder a 3
    minutos de distancia seria inventar evidencia com cara de dado."""
    from riftcoach.analysis.advantage import find_blunders

    blunders = find_blunders(facts)
    for m in marks_from_report(report, facts):
        if m.wp_loss is None:
            continue
        assert any(abs(b.t_ms - m.t_ms) <= 45_000 for b in blunders)


def test_o_limite_de_marcacoes_e_respeitado(report: CoachingReport, facts: MatchFacts) -> None:
    """Uma regua com 40 marcacoes nao e uma regua, e uma listra."""
    assert len(marks_from_report(report, facts, max_marks=3)) <= 3


# --------------------------------------------------------------------------
# Persistencia
# --------------------------------------------------------------------------


def test_retomar_traz_tudo_de_volta() -> None:
    rs = ReviewSession(match_id="BR1_1", puuid="p" * 78)
    rs.add(Mark(t_ms=60_000, author="user", kind="note", text="olhar de novo"))
    rs.last_position_ms = 60_000
    save_session(rs)

    de_volta = load_session("BR1_1", "p" * 78)
    assert de_volta is not None
    assert de_volta.last_position_ms == 60_000
    assert [m.text for m in de_volta.marks] == ["olhar de novo"]


def test_partida_nunca_revisada_devolve_nada() -> None:
    assert load_session("BR1_jamais", "x" * 78) is None


def test_arquivo_corrompido_nao_impede_de_abrir(tmp_path: Path) -> None:
    """Perder as anotacoes e ruim. Nao conseguir mais abrir a partida por causa
    de um byte torto seria pior."""
    rs = ReviewSession(match_id="BR1_2", puuid="q" * 78)
    caminho = save_session(rs)
    caminho.write_text("{isto nao e json", encoding="utf-8")
    assert load_session("BR1_2", "q" * 78) is None


def test_reanalisar_troca_as_marcacoes_da_ia_e_preserva_as_suas(
    report: CoachingReport, facts: MatchFacts
) -> None:
    """O unico dado aqui que nao da para recalcular e a anotacao da pessoa."""
    mid, puuid = facts.match_id, facts.focus.puuid
    rs = open_session(mid, puuid, report, facts)
    da_ia_antes = len([m for m in rs.marks if m.author == "ai"])
    assert da_ia_antes > 0

    add_user_mark(rs, 123_000, "note", "aqui eu travei")

    de_novo = open_session(mid, puuid, report, facts)
    minhas = [m for m in de_novo.marks if m.author == "user"]
    assert [m.text for m in minhas] == ["aqui eu travei"]
    # E as da IA nao duplicaram.
    assert len([m for m in de_novo.marks if m.author == "ai"]) == da_ia_antes


def test_a_marcacao_do_usuario_grava_na_hora() -> None:
    """Nao existe "ao sair": a pessoa fecha o replay quando termina de
    assistir, nao quando termina de anotar."""
    rs = ReviewSession(match_id="BR1_3", puuid="r" * 78)
    add_user_mark(rs, 300_000, "error", "ward errada")

    do_disco = load_session("BR1_3", "r" * 78)
    assert do_disco is not None
    assert len(do_disco.marks) == 1
    assert do_disco.last_position_ms == 300_000


def test_marcacao_em_tempo_negativo_e_presa_no_zero() -> None:
    rs = ReviewSession(match_id="BR1_4", puuid="s" * 78)
    assert add_user_mark(rs, -5_000).t_ms == 0


# --------------------------------------------------------------------------
# Exportar
# --------------------------------------------------------------------------


def test_exportar_em_texto_para_colar_no_discord(report: CoachingReport, facts: MatchFacts) -> None:
    rs = open_session(facts.match_id, facts.focus.puuid, report, facts)
    add_user_mark(rs, 900_000, "question", "por que recuei?")
    texto = export_marks(rs)

    assert facts.match_id in texto
    assert "15:00" in texto
    assert "(voce)" in texto and "(IA)" in texto
    # Uma linha por marcacao, e em ordem de tempo.
    linhas = [ln for ln in texto.splitlines() if ln and ln[0].isdigit()]
    assert len(linhas) == len(rs.marks)


def test_exportar_em_json_e_lido_de_volta() -> None:
    import json

    rs = ReviewSession(match_id="BR1_5", puuid="t" * 78)
    add_user_mark(rs, 61_000, "good", "bom freeze")
    dados = json.loads(json_marks(rs))
    assert dados["match_id"] == "BR1_5"
    assert dados["marks"][0]["text"] == "bom freeze"
