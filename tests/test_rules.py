"""Testes do motor de regras e do relatorio L5.

O criterio aqui nao e "achou o erro certo" — isso e julgamento de LoL e mora em
evals/. O criterio e que TODO finding produzido seja verificavel pelo usuario:
gravidade que vem de medicao, evidencia que cita o numero, e premissa declarada
sempre que a afirmacao nao for medida.

Se este arquivo passar, o relatorio pode estar incompleto, mas nao pode estar
afirmando coisas que nao consegue sustentar.
"""

from __future__ import annotations

import pytest

from riftcoach.analysis.merge import (
    DEDUPE_WINDOW_MS,
    GENERIC,
    MAX_FINDINGS,
    build_report,
    dedupe,
    merge,
    pick_top_three,
    rank,
)
from riftcoach.analysis.report import analyze, render_finding
from riftcoach.analysis.rules import (
    GOLD_HOARD_AT_DEATH,
    TRIGGERS,
    RuleContext,
    evaluate,
    phase_of,
    rule_lane_economy,
    triggers_of,
)
from riftcoach.core.schema import Evidence, EvidenceTier, Finding
from riftcoach.knowledge.benchmarks import BenchmarkTable
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from tests.test_distill import load

FIXTURES = ("sr_ranked_35min", "sr_flex_41min", "sr_remake")


@pytest.fixture(scope="module")
def garen() -> MatchFacts:
    """Garen TOP, 4/9/1, derrota de 35 min. Partida com erro para achar."""
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


@pytest.fixture(scope="module")
def nami() -> MatchFacts:
    """Nami UTILITY — a rota em que as metricas de farm nao se aplicam."""
    match, tl = load("sr_ranked_35min")
    return next(
        distill(match, tl, p["puuid"])
        for p in match["info"]["participants"]
        if p["teamPosition"] == "UTILITY"
    )


@pytest.fixture(scope="module")
def remake() -> MatchFacts:
    match, tl = load("sr_remake")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


def _all_findings(facts: MatchFacts) -> list[Finding]:
    return evaluate(facts)


# --------------------------------------------------------------------------
# A invariante central: nada afirmado sem lastro
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fx", FIXTURES)
def test_every_finding_cites_its_evidence(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        for finding in _all_findings(f):
            assert finding.evidence, f"{finding.claim} nao cita nada"
            assert finding.fix.strip()
            assert 1 <= finding.severity <= 5
            assert 0.0 <= finding.confidence <= 1.0


@pytest.mark.parametrize("fx", FIXTURES)
def test_derived_evidence_always_declares_its_assumption(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        for finding in _all_findings(f):
            for e in finding.evidence:
                if e.tier is not EvidenceTier.T1_MEASURED:
                    assert e.assumption, f"{e.statement} e {e.tier} sem premissa"
                    assert len(e.assumption) > 20, "premissa vaga nao deixa discordar"


def test_derived_evidence_is_actually_produced(garen: MatchFacts) -> None:
    """O schema ja impoe a premissa na construcao — mas so para quem constroi
    um T2. Sem este teste, as regras poderiam nunca produzir evidencia
    derivada e a invariante acima passaria a vazio, para sempre."""
    tiers = {e.tier for f in _all_findings(garen) for e in f.evidence}
    assert EvidenceTier.T2_DERIVED in tiers
    assert EvidenceTier.T1_MEASURED in tiers


@pytest.mark.parametrize("fx", FIXTURES)
def test_every_finding_is_anchored_in_the_match(fx: str) -> None:
    """timestamp_ms e a chave de juncao com o replay. Um finding fora da
    duracao da partida nao pode ser clicado nem conferido."""
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        limite = f.duration_s * 1000 + 60_000
        for finding in _all_findings(f):
            assert 0 <= finding.timestamp_ms <= limite


def test_seek_lands_before_the_moment(garen: MatchFacts) -> None:
    """O erro e a decisao, nao o desfecho."""
    for finding in _all_findings(garen):
        assert finding.seek_ms <= finding.timestamp_ms
        assert finding.timestamp_ms - finding.seek_ms <= 8_000


# --------------------------------------------------------------------------
# Gravidade e medida, nao opinada
# --------------------------------------------------------------------------


def test_blunder_findings_quote_the_measured_cost(garen: MatchFacts) -> None:
    medidos = [f for f in _all_findings(garen) if "custou" in f.claim]
    assert medidos, "o motor de vantagem nao produziu nenhum finding"
    for f in medidos:
        assert "pp" in f.claim
        assert any("probabilidade de vitoria" in e.statement for e in f.evidence)


def test_benchmark_findings_never_claim_to_have_cost_the_game(garen: MatchFacts) -> None:
    """Gravidade 5 e uma afirmacao sobre um MOMENTO. Uma tendencia da partida
    inteira nao tem um instante em que custou o jogo, entao o teto e 4."""
    for f in _all_findings(garen):
        if any(e.source == "benchmark" for e in f.evidence):
            assert f.severity <= 4


def test_severity_and_claim_agree_after_merging(garen: MatchFacts) -> None:
    """Um finding marcado gravidade 4 cuja frase so descreve contexto deixa o
    leitor sem saber de onde veio o 4."""
    for f in merge(_all_findings(garen)):
        if f.severity >= 4:
            tem_numero = any(c.isdigit() for c in f.claim)
            assert tem_numero, f"gravidade {f.severity} sem numero na claim: {f.claim}"


# --------------------------------------------------------------------------
# Regras que precisam ficar CALADAS
# --------------------------------------------------------------------------


def test_support_is_never_told_to_farm_more(nami: MatchFacts) -> None:
    """O exemplo classico de metrica aplicada na rota errada. O suporte nao
    farma de proposito — apontar CS baixo para ele e um bug, nao um conselho."""
    assert nami.focus.position == "UTILITY"
    for f in _all_findings(nami):
        assert "CS aos 10" not in f.claim


def test_lane_economy_is_silent_for_support(nami: MatchFacts) -> None:
    tabela = BenchmarkTable(patch=nami.patch)
    ctx = RuleContext(facts=nami, benchmarks=tabela.evaluate(nami))
    assert rule_lane_economy(ctx) == []


def test_a_remake_produces_almost_nothing(remake: MatchFacts) -> None:
    """Partida de 1:10. Nada aconteceu, entao nada pode ser afirmado — e um
    relatorio que inventa oito findings aqui perde a confianca do usuario para
    sempre."""
    achados = _all_findings(remake)
    assert len(achados) <= 2


def test_report_says_so_when_it_found_nothing(remake: MatchFacts) -> None:
    _, texto = analyze(remake)
    assert "Nenhum erro acima do limiar" in texto or "OS 1 PRINCIPAIS" in texto


def test_gold_hoarding_needs_a_habit_not_bad_luck(garen: MatchFacts) -> None:
    """Uma morte cara e azar. Duas e um habito de recall — e so o habito da
    para treinar."""
    hoard = [f for f in _all_findings(garen) if f.category == "recall"]
    if hoard:
        caros = [d for d in garen.deaths if d.gold_at_death >= GOLD_HOARD_AT_DEATH]
        assert len(caros) >= 2


# --------------------------------------------------------------------------
# Fases
# --------------------------------------------------------------------------


def test_phases_are_ordered() -> None:
    assert phase_of(0) == "early"
    assert phase_of(10 * 60_000) == "early"
    assert phase_of(20 * 60_000) == "mid"
    assert phase_of(40 * 60_000) == "late"


# --------------------------------------------------------------------------
# Gatilhos — contrato publico com knowledge/principles/
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fx", FIXTURES)
def test_triggers_stay_inside_the_published_vocabulary(fx: str) -> None:
    """Contribuidores marcam os arquivos de principio com estes nomes
    (CONTRIBUTING.md). Emitir um gatilho fora da lista significa que nenhum
    arquivo jamais vai casar com ele."""
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        for t in triggers_of(f, _all_findings(f)):
            assert t in TRIGGERS or t.startswith(("role=", "phase="))


def test_triggers_always_identify_the_role(garen: MatchFacts) -> None:
    assert "role=TOP" in triggers_of(garen, _all_findings(garen))


# --------------------------------------------------------------------------
# Determinismo
# --------------------------------------------------------------------------


def test_the_same_match_gives_the_same_report_twice(garen: MatchFacts) -> None:
    """Sem IA significa determinista. Se duas execucoes divergirem, um dos
    caminhos depende de ordem de dicionario ou de relogio."""
    a, texto_a = analyze(garen)
    b, texto_b = analyze(garen)
    assert texto_a == texto_b
    assert [f.claim for f in a.ranked()] == [f.claim for f in b.ranked()]


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def _finding(**kw: object) -> Finding:
    base: dict[str, object] = {
        "category": "decision",
        "phase": "mid",
        "severity": 3,
        "timestamp_ms": 600_000,
        "claim": "algo aconteceu",
        "evidence": [
            Evidence(
                tier=EvidenceTier.T1_MEASURED,
                timestamp_ms=600_000,
                statement="medido",
                source="timeline",
            )
        ],
        "fix": "faca diferente",
        "confidence": 0.8,
    }
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


def test_same_category_same_moment_collapses() -> None:
    a = _finding(claim="morte A")
    b = _finding(claim="morte B", timestamp_ms=600_000 + DEDUPE_WINDOW_MS - 1)
    assert len(dedupe([a, b])) == 1


def test_same_category_far_apart_survives() -> None:
    a = _finding(claim="morte A")
    b = _finding(claim="morte B", timestamp_ms=600_000 + DEDUPE_WINDOW_MS + 1)
    assert len(dedupe([a, b])) == 2


def test_a_generic_merges_into_the_macro_finding_that_explains_it() -> None:
    """Um lado traz o custo medido, o outro traz o porque. Descartar qualquer
    um dos dois perde informacao que o usuario precisa."""
    generico = _finding(category="decision", severity=4, claim="Morte em 10:00 custou 16pp.")
    macro = _finding(
        category="macro",
        severity=3,
        claim="Voce morreu em 10:00 com DRAGON_EM_30s.",
        evidence=[
            Evidence(
                tier=EvidenceTier.T1_MEASURED,
                timestamp_ms=600_000,
                statement="janela de objetivo: DRAGON_EM_30s",
                source="timeline",
            )
        ],
    )
    resultado = dedupe([generico, macro])
    assert len(resultado) == 1
    fundido = resultado[0]
    assert fundido.category == "macro"
    assert fundido.severity == 4, "a gravidade medida nao pode ser rebaixada"
    assert "16pp" in fundido.claim, "a claim precisa sustentar a gravidade 4"
    assert "DRAGON_EM_30s" in fundido.claim


def test_a_tendency_finding_never_swallows_a_death() -> None:
    """Uma observacao de checklist ancorada por acaso perto de uma morte
    critica nao pode engoli-la. Isto ja aconteceu em teste antes da restricao
    de EXPLAINS_A_MOMENT existir."""
    morte = _finding(category="decision", severity=4, claim="Morte em 10:00 custou 21pp.")
    checklist = _finding(
        category="vision", severity=2, claim="Sentinela de Controle em 1 de 12 recalls."
    )
    resultado = dedupe([morte, checklist])
    assert len(resultado) == 2
    assert any(f.severity == 4 for f in resultado)


def test_generic_findings_are_kept_when_nothing_explains_them() -> None:
    solto = _finding(category="decision", severity=4)
    assert dedupe([solto]) == [solto]
    assert solto.category in GENERIC


def test_ranking_puts_the_worst_first(garen: MatchFacts) -> None:
    ordenados = rank(_all_findings(garen))
    assert [f.severity for f in ordenados] == sorted(
        (f.severity for f in ordenados), reverse=True
    )


def test_the_report_is_capped(garen: MatchFacts) -> None:
    """Um relatorio com vinte findings nao e quatro vezes mais util que um com
    cinco. Ele e menos util, porque ninguem le ate o fim."""
    assert len(merge(_all_findings(garen))) <= MAX_FINDINGS


def test_top_three_prefers_distinct_categories() -> None:
    achados = [
        _finding(category="decision", severity=5, timestamp_ms=1),
        _finding(category="decision", severity=4, timestamp_ms=200_000),
        _finding(category="vision", severity=3, timestamp_ms=400_000),
        _finding(category="wave", severity=2, timestamp_ms=600_000),
    ]
    escolhidos = pick_top_three(achados)
    categorias = {achados[i].category for i in escolhidos}
    assert len(categorias) == 3, "tres findings sobre mortes ensinam uma coisa so"


def test_top_three_falls_back_when_categories_repeat() -> None:
    achados = [_finding(timestamp_ms=i * 200_000) for i in range(4)]
    assert len(pick_top_three(achados)) == 3


def test_top_three_never_points_past_the_end() -> None:
    """O CoachingReport valida isso e levanta — entao um bug aqui derruba o
    relatorio inteiro, nao so a secao do topo."""
    assert pick_top_three([]) == []
    assert pick_top_three([_finding()]) == [0]


@pytest.mark.parametrize("fx", FIXTURES)
def test_the_report_validates_for_every_player(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        r = build_report(f, _all_findings(f))
        assert r.match_id == f.match_id
        assert r.puuid == f.focus.puuid
        assert len(r.top_three) == min(3, len(r.findings))
        assert all(0 <= i < len(r.findings) for i in r.top_three)


# --------------------------------------------------------------------------
# Renderizacao
# --------------------------------------------------------------------------


def test_rendered_finding_shows_every_tier_and_assumption(garen: MatchFacts) -> None:
    """Sem isso o usuario nao consegue discordar de forma util — e 'essa
    premissa nao vale no meu caso' e o melhor relato de bug que existe."""
    r, _ = analyze(garen)
    for i, f in enumerate(r.ranked(), 1):
        texto = "\n".join(render_finding(f, i))
        for e in f.evidence:
            assert e.statement in texto
            if e.assumption:
                assert e.assumption in texto
                assert "premissa:" in texto


def test_the_l5_report_declares_that_no_model_touched_it(garen: MatchFacts) -> None:
    """Quando a etapa 3 entrar, o mesmo campo vai listar os modelos usados —
    entao a ausencia deles precisa ser visivel, nao silenciosa."""
    r, texto = analyze(garen)
    assert r.model_trace == {}
    assert "SEM IA" in texto
    assert "nenhum dado saiu desta maquina" in texto


def test_the_report_carries_the_patch_and_parser_version(garen: MatchFacts) -> None:
    """Sem isso o usuario nao consegue explicar por que o relatorio de ontem
    difere do de hoje."""
    _, texto = analyze(garen)
    assert f"patch {garen.patch}" in texto
    assert f"parser v{garen.parser_version}" in texto


@pytest.mark.parametrize("fx", FIXTURES)
def test_the_report_renders_for_every_player(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        _, texto = analyze(f)
        assert texto.strip()
        assert f.match_id in texto
