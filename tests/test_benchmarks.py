"""Testes dos benchmarks (L3).

O que precisa ser verdade aqui nao e "o numero esta certo" — o modelo embarcado
e assumidamente nao calibrado. E que a ORIENTACAO esteja certa (mais CS e
melhor, mais mortes e pior), que a fonte do percentil seja sempre declarada, e
que o caso sem dado nenhum degrade em vez de mentir.
"""

from __future__ import annotations

import pytest

from riftcoach.knowledge.benchmarks import (
    DEFAULT_TIER,
    MIN_HISTORY,
    ROLES,
    TIERS,
    Benchmark,
    BenchmarkTable,
    Distribution,
    metrics_of,
    model_distribution,
    normalize_role,
    normalize_tier,
    summarize,
)
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from tests.test_distill import load


@pytest.fixture(scope="module")
def garen() -> MatchFacts:
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


@pytest.fixture(scope="module")
def remake() -> MatchFacts:
    match, tl = load("sr_remake")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


# --------------------------------------------------------------------------
# Orientacao: percentil alto SEMPRE significa "bom"
# --------------------------------------------------------------------------


def test_more_cs_is_a_better_percentile() -> None:
    d = model_distribution("MIDDLE", "GOLD", "cs_at_10")
    assert d.percentile_of(90) > d.percentile_of(68) > d.percentile_of(40)


def test_more_deaths_is_a_WORSE_percentile() -> None:
    """A inversao mais facil de errar do modulo inteiro.

    Se `higher_is_better` fosse ignorado, morrer doze vezes daria percentil 95
    e o relatorio parabenizaria o jogador pelo pior numero da partida.
    """
    d = model_distribution("TOP", "GOLD", "deaths")
    assert d.percentile_of(2) > d.percentile_of(5) > d.percentile_of(12)


def test_less_time_dead_is_a_better_percentile() -> None:
    d = model_distribution("TOP", "GOLD", "time_dead_pct")
    assert d.percentile_of(4) > d.percentile_of(25)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("tier", TIERS)
def test_percentile_stays_in_range(role: str, tier: str) -> None:
    for metric in ("cs_at_10", "dpm", "vision_per_min", "deaths", "time_dead_pct"):
        d = model_distribution(role, tier, metric)  # type: ignore[arg-type]
        for v in (0, 0.001, 1, 50, 10_000):
            assert 0.0 <= d.percentile_of(v) <= 100.0


def test_zero_is_the_bottom_not_an_error() -> None:
    """Zero CS acontece (remake, morte no minuto 1). Log de zero nao existe, e
    o caminho precisa devolver o fundo da distribuicao em vez de estourar."""
    d = model_distribution("MIDDLE", "GOLD", "cs_at_10")
    assert d.percentile_of(0) == 0.0
    morte = model_distribution("MIDDLE", "GOLD", "deaths")
    assert morte.percentile_of(0) == 100.0


def test_percentile_and_at_percentile_are_inverses() -> None:
    for metric in ("cs_at_10", "dpm", "deaths", "gd_at_10"):
        d = model_distribution("MIDDLE", "EMERALD", metric)  # type: ignore[arg-type]
        for p in (10.0, 30.0, 50.0, 75.0, 90.0):
            assert d.percentile_of(d.at_percentile(p)) == pytest.approx(p, abs=0.5)


# --------------------------------------------------------------------------
# Diferenca de ouro: simetrica em zero por construcao
# --------------------------------------------------------------------------


def test_gold_diff_is_a_coin_flip_at_zero() -> None:
    """A sua vantagem e exatamente a desvantagem do oponente direto. Se a
    mediana nao fosse zero, a tabela estaria afirmando que uma rota media
    termina o minuto 10 ganhando."""
    d = model_distribution("TOP", "GOLD", "gd_at_10")
    assert d.percentile_of(0) == pytest.approx(50.0)


def test_gold_diff_is_symmetric() -> None:
    d = model_distribution("MIDDLE", "PLATINUM", "gd_at_10")
    assert d.percentile_of(500) + d.percentile_of(-500) == pytest.approx(100.0)


def test_gold_diff_never_uses_log_scale() -> None:
    """Metade dos valores sao negativos; log seria invalido, nao so impreciso."""
    assert not model_distribution("TOP", "GOLD", "gd_at_10").log_scale


# --------------------------------------------------------------------------
# O exemplo da documentacao
# --------------------------------------------------------------------------


def test_the_example_from_the_docs_reproduces() -> None:
    """docs/04-knowledge-base.md promete, textualmente:

        "61 CS@10 te coloca no percentil 28 para mid Esmeralda, onde a mediana
        e 68"

    Se este teste quebrar, ou o modelo mudou ou a documentacao mentiu. Os dois
    casos exigem decisao humana, entao ele falha alto de proposito.
    """
    d = model_distribution("MIDDLE", "EMERALD", "cs_at_10")
    assert d.median == pytest.approx(68, abs=0.5)
    assert d.percentile_of(61) == pytest.approx(28, abs=4)


# --------------------------------------------------------------------------
# Normalizacao de entrada
# --------------------------------------------------------------------------


@pytest.mark.parametrize("apex", ["MASTER", "GRANDMASTER", "CHALLENGER"])
def test_apex_tiers_collapse(apex: str) -> None:
    """Amostra pequena demais para sustentar percentis separados, e a diferenca
    entre eles nao muda nenhuma recomendacao de coaching."""
    assert normalize_tier(apex) == "MASTER+"


def test_unranked_falls_back_instead_of_failing() -> None:
    """Fora de ranked a league-v4 nao devolve tier nenhum. Esse e o caso comum
    em normal/quickplay/ARAM, nao a excecao."""
    assert normalize_tier(None) == DEFAULT_TIER
    assert normalize_tier("") == DEFAULT_TIER
    assert normalize_tier("MADEIRA") == DEFAULT_TIER


def test_tier_is_case_insensitive() -> None:
    assert normalize_tier("emerald") == "EMERALD"
    assert normalize_tier(" Gold ") == "GOLD"


def test_unknown_role_falls_back() -> None:
    assert normalize_role("INVENTADA") == "MIDDLE"
    assert normalize_role(None) == "MIDDLE"
    assert normalize_role("utility") == "UTILITY"


# --------------------------------------------------------------------------
# Extracao a partir de MatchFacts
# --------------------------------------------------------------------------


def test_metrics_come_from_the_facts(garen: MatchFacts) -> None:
    m = metrics_of(garen)
    assert m["cs_at_10"] == garen.cs_at_10
    assert m["deaths"] == garen.focus.deaths
    assert m["dpm"] == pytest.approx(garen.dpm)


def test_gold_diff_is_absent_when_there_is_no_opponent() -> None:
    """Comparar a sua diferenca de ouro com a de ninguem nao significa nada.
    Ausente e honesto; zero afirmaria um empate que nunca foi medido."""
    match, tl = load("sr_ranked_35min")
    f = distill(match, tl, match["info"]["participants"][0]["puuid"])
    sem_adv = f.model_copy(update={"gd_at_10": None})
    assert "gd_at_10" not in metrics_of(sem_adv)


# --------------------------------------------------------------------------
# A tabela e a procedencia
# --------------------------------------------------------------------------


def test_without_parquet_or_history_it_says_so(garen: MatchFacts) -> None:
    """O ponto do modulo inteiro: um percentil do modelo embarcado e um chute
    educado, e o relatorio precisa dizer isso em vez de fingir medicao."""
    marks = BenchmarkTable(patch=garen.patch).evaluate(garen, "EMERALD")
    assert marks
    assert all(b.source == "model" for b in marks)
    assert all("nao calibrado" in b.source_label for b in marks)
    assert "NAO e calibrado" in summarize(marks)


def test_history_needs_a_real_sample(garen: MatchFacts) -> None:
    """Com tres partidas, um jogo ruim move o percentil dezenas de pontos. A
    tabela precisa recusar a amostra em vez de produzir um numero convincente
    e errado."""
    poucas = [garen] * (MIN_HISTORY - 1)
    t = BenchmarkTable(patch=garen.patch, history=poucas)
    assert t.lookup("cs_at_10", 50, "TOP", "GOLD").source == "model"


def test_history_wins_once_there_is_enough_of_it(garen: MatchFacts) -> None:
    bastante = [garen] * MIN_HISTORY
    t = BenchmarkTable(patch=garen.patch, history=bastante)
    b = t.lookup("cs_at_10", garen.cs_at_10, "TOP", "GOLD")
    assert b.source == "history"
    assert b.sample == MIN_HISTORY
    assert "seu proprio historico" in b.source_label


def test_history_is_kept_per_role(garen: MatchFacts) -> None:
    """Percentis de TOP nao descrevem um suporte. Misturar as rotas numa
    amostra so daria a comparacao errada para as duas."""
    t = BenchmarkTable(patch=garen.patch, history=[garen] * MIN_HISTORY)
    assert t.lookup("cs_at_10", 50, "TOP", "GOLD").source == "history"
    assert t.lookup("cs_at_10", 50, "UTILITY", "GOLD").source == "model"


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min"])
def test_evaluate_covers_every_player_of_a_real_match(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        marks = BenchmarkTable(patch=f.patch).evaluate(f)
        assert marks
        assert all(0.0 <= b.percentile <= 100.0 for b in marks)
        assert all(b.render() for b in marks)


# --------------------------------------------------------------------------
# Partidas curtas demais para comparar
# --------------------------------------------------------------------------


def test_a_remake_produces_no_metrics_at_all(remake: MatchFacts) -> None:
    """Partida de 1:10. Ninguem jogou o suficiente para ter um numero que
    descreva o jogo, e um percentil calculado sobre nada nao e um percentil
    baixo — e uma medicao que nunca aconteceu."""
    assert remake.duration_s < 8 * 60
    assert metrics_of(remake) == {}
    assert BenchmarkTable(patch=remake.patch).evaluate(remake) == []


def test_zero_cs_at_ten_never_becomes_percentile_zero(remake: MatchFacts) -> None:
    """A regressao concreta que motivou o corte: o remake gerava
    '0 CS aos 10 minutos — percentil 0', gravidade 4, ancorado num minuto que a
    partida nunca alcancou."""
    assert "cs_at_10" not in metrics_of(remake)


def test_lane_metrics_need_the_match_to_reach_minute_ten(garen: MatchFacts) -> None:
    curta = garen.model_copy(update={"duration_s": 9 * 60})
    m = metrics_of(curta)
    assert m, "nove minutos ainda rende taxas"
    assert "cs_at_10" not in m
    assert "gd_at_10" not in m
    assert "dpm" in m


def test_summarize_is_empty_without_marks() -> None:
    assert summarize([]) == ""


def test_weak_and_strong_are_opposite_ends() -> None:
    fraco = Benchmark("cs_at_10", 20, 8.0, 68, "MIDDLE", "GOLD", "model")
    forte = Benchmark("cs_at_10", 95, 92.0, 68, "MIDDLE", "GOLD", "model")
    assert fraco.is_weak and not fraco.is_strong
    assert forte.is_strong and not forte.is_weak


def test_distribution_is_frozen() -> None:
    """Os pesos sao constantes de projeto discutidas em PR, nao estado mutavel
    que uma chamada possa ajustar por baixo."""
    d = Distribution(60, 0.2)
    with pytest.raises((AttributeError, TypeError)):
        d.median = 99  # type: ignore[misc]
