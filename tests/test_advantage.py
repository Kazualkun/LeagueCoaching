"""Testes do motor de vantagem contra partidas reais."""

from __future__ import annotations

import pytest

from riftcoach.analysis.advantage import (
    SEVERITY_THRESHOLDS,
    advantage_series,
    evaluate,
    find_blunders,
    summarize,
    win_probability,
)
from riftcoach.core.schema import Mark, ReviewSession
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from tests.test_distill import load


@pytest.fixture(scope="module")
def loser() -> MatchFacts:
    """Garen TOP, derrota de 35 min — a curva precisa terminar bem baixa."""
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


@pytest.fixture(scope="module")
def winner(loser: MatchFacts) -> MatchFacts:
    match, tl = load("sr_ranked_35min")
    return next(
        distill(match, tl, p["puuid"])
        for p in match["info"]["participants"]
        if p["teamId"] != loser.focus.team_id
    )


@pytest.fixture(scope="module")
def remake() -> MatchFacts:
    match, tl = load("sr_remake")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


# --------------------------------------------------------------------------
# Probabilidade de vitoria
# --------------------------------------------------------------------------


def test_zero_advantage_is_a_coin_flip() -> None:
    assert win_probability(0, 0) == pytest.approx(0.5)
    assert win_probability(0, 30 * 60_000) == pytest.approx(0.5)


def test_advantage_moves_probability_the_right_way() -> None:
    t = 20 * 60_000
    assert win_probability(5000, t) > 0.5
    assert win_probability(-5000, t) < 0.5
    assert win_probability(10_000, t) > win_probability(5000, t)


def test_same_lead_matters_less_late() -> None:
    """5k aos 15 min e uma fracao muito maior da partida que 5k aos 40.

    Se a escala nao crescesse com o tempo, o motor trataria uma vantagem
    tardia como decisiva demais e inflaria a gravidade de tudo no late game.
    """
    cedo = win_probability(5000, 15 * 60_000)
    tarde = win_probability(5000, 40 * 60_000)
    assert cedo > tarde


def test_probability_stays_in_range() -> None:
    for adv in (-100_000, -5000, 0, 5000, 100_000):
        for minuto in (0, 10, 45):
            p = win_probability(adv, minuto * 60_000)
            assert 0.0 <= p <= 1.0


# --------------------------------------------------------------------------
# Avaliacao
# --------------------------------------------------------------------------


def test_evaluation_is_explainable(loser: MatchFacts) -> None:
    """Uma engine de xadrez pode dizer so '+1.5'. Um coach precisa dizer por
    que — senao a avaliacao nao vira evidencia citavel."""
    e = evaluate(loser, 20 * 60_000)
    assert e.terms
    nomes = {t.name for t in e.terms}
    assert "ouro" in nomes
    assert e.advantage == pytest.approx(sum(t.contribution for t in e.terms))
    assert "wp=" in e.explain()


def test_evaluation_is_zero_sum_between_teams(
    loser: MatchFacts, winner: MatchFacts
) -> None:
    """A vantagem de um time e o negativo da do outro. Se nao for, algum termo
    esta sendo contado do mesmo lado para os dois."""
    for minuto in (5, 10, 20, 30):
        a = evaluate(loser, minuto * 60_000).advantage
        b = evaluate(winner, minuto * 60_000).advantage
        assert a + b == pytest.approx(0, abs=250)


def test_losing_team_ends_with_low_probability(loser: MatchFacts) -> None:
    serie = advantage_series(loser)
    assert serie[0].win_probability == pytest.approx(0.5, abs=0.05)
    assert serie[-1].win_probability < 0.2, "quem perdeu deve terminar baixo"


def test_winning_team_ends_high(winner: MatchFacts) -> None:
    assert advantage_series(winner)[-1].win_probability > 0.8


def test_series_covers_the_whole_game(loser: MatchFacts) -> None:
    assert len(advantage_series(loser)) == len(loser.team_gold_diff_series)


def test_remake_is_evaluated_as_even(remake: MatchFacts) -> None:
    """Partida de 1:10: nada aconteceu, entao nada pode ser afirmado."""
    for e in advantage_series(remake):
        assert e.win_probability == pytest.approx(0.5, abs=0.1)


# --------------------------------------------------------------------------
# Erros medidos
# --------------------------------------------------------------------------


def test_blunders_only_contain_losses(loser: MatchFacts) -> None:
    """Ganhos entram na curva, nao na lista de erros. Um coach que lista 40
    'erros' nao e usado duas vezes."""
    for b in find_blunders(loser):
        assert b.wp_loss > 0


def test_blunders_are_sorted_by_cost(loser: MatchFacts) -> None:
    perdas = [b.wp_loss for b in find_blunders(loser)]
    assert perdas == sorted(perdas, reverse=True)


def test_severity_comes_from_measurement_not_opinion(loser: MatchFacts) -> None:
    """Este e o ponto do motor inteiro: gravidade e medida, nao opinada."""
    for b in find_blunders(loser):
        esperado = 1
        for limiar, sev in SEVERITY_THRESHOLDS:
            if b.wp_loss >= limiar:
                esperado = sev
        assert b.severity == esperado
        assert b.is_critical == (b.severity >= 4)


def test_deaths_are_attributed_directly(loser: MatchFacts) -> None:
    mortes = [b for b in find_blunders(loser) if b.kind == "morte"]
    assert mortes
    assert all(b.involvement == "direct" for b in mortes)


def test_objectives_lost_far_away_are_positional(loser: MatchFacts) -> None:
    """'Voce morreu' e 'seu time perdeu o barao enquanto voce empurrava a top'
    pedem correcoes completamente diferentes."""
    objetivos = [b for b in find_blunders(loser) if b.kind.startswith("perdeu")]
    assert objetivos
    assert all(b.involvement in ("positional", "team") for b in objetivos)


def test_blunder_detail_is_specific(loser: MatchFacts) -> None:
    """Sem detalhe concreto, a marcacao vira 'voce errou aqui' — inutil."""
    for b in find_blunders(loser)[:5]:
        assert len(b.detail) > 20
        assert any(c.isdigit() for c in b.detail) or "voce estava" in b.detail


def test_critical_count_matches_design_target(loser: MatchFacts) -> None:
    """Alvo calibrado: ~1,5 criticos por jogador. Zero e a marcacao nunca
    aparece; dez e ela perde o sentido."""
    criticos = [b for b in find_blunders(loser) if b.is_critical]
    assert 1 <= len(criticos) <= 6


def test_remake_has_no_blunders(remake: MatchFacts) -> None:
    assert find_blunders(remake) == []


def test_summary_is_rendered_for_the_l5_report(loser: MatchFacts) -> None:
    texto = summarize(loser)
    assert "VANTAGEM" in texto
    assert "ERROS POR CUSTO MEDIDO" in texto
    assert "CRITICO" in texto


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min", "sr_remake"])
def test_every_player_evaluates(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        assert advantage_series(f)
        find_blunders(f)
        summarize(f)


# --------------------------------------------------------------------------
# Marcacoes e sessao de revisao
# --------------------------------------------------------------------------


def _mark(**kw: object) -> Mark:
    base: dict[str, object] = {
        "t_ms": 862_000,
        "author": "user",
        "kind": "note",
        "text": "aqui eu devia ter recuado",
    }
    base.update(kw)
    return Mark(**base)  # type: ignore[arg-type]


def test_mark_seek_lands_before_the_moment() -> None:
    assert _mark(t_ms=862_000).seek_ms == 854_000
    assert _mark(t_ms=2_000).seek_ms == 0


def test_session_merges_ai_and_user_marks_on_one_timeline() -> None:
    """O valor da revisao esta na comparacao: onde a IA marcou critico e o
    jogador nao viu nada, e onde o jogador sentiu erro e a metrica nao viu."""
    s = ReviewSession(match_id="BR1_1", puuid="p")
    s.add(_mark(t_ms=500_000, author="ai", kind="critical", severity=4, wp_loss=12.2))
    s.add(_mark(t_ms=120_000, author="user", kind="question"))
    s.add(_mark(t_ms=300_000, author="ai", kind="error", severity=2, wp_loss=4.0))
    assert [m.t_ms for m in s.timeline()] == [120_000, 300_000, 500_000]
    assert len(s.criticals()) == 1


def test_session_remembers_where_you_stopped() -> None:
    s = ReviewSession(match_id="BR1_1", puuid="p", last_position_ms=742_000)
    voltou = ReviewSession.model_validate_json(s.model_dump_json())
    assert voltou.last_position_ms == 742_000
    assert not voltou.completed


def test_user_marks_need_no_measurement() -> None:
    """O jogador marca o que sentiu; so a IA mede."""
    m = _mark(author="user", kind="error")
    assert m.wp_loss is None and m.severity is None


def test_blunders_convert_to_marks(loser: MatchFacts) -> None:
    """Ponte entre o motor e a linha do tempo da UI."""
    s = ReviewSession(match_id=loser.match_id, puuid=loser.focus.puuid)
    for b in find_blunders(loser):
        s.add(
            Mark(
                t_ms=b.t_ms,
                author="ai",
                kind="critical" if b.is_critical else "error",
                text=b.detail,
                severity=b.severity,
                wp_loss=b.wp_loss,
            )
        )
    assert s.marks
    assert all(m.author == "ai" and m.wp_loss is not None for m in s.marks)
    assert s.criticals()
