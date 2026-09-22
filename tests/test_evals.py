"""Testes do harness de avaliacao.

Um harness que pontua errado e pior que nenhum harness: ele da a um PR ruim a
autoridade de um numero. Entao os eixos da rubrica sao testados um a um, e a
propriedade que mais importa e a de que a fundamentacao ELIMINA — um finding
bem escrito sobre premissa nao declarada precisa valer zero, nao 0,8.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from evals.run import BASELINE, load_corpus, run_baseline  # noqa: E402
from evals.scoring import (  # noqa: E402
    ANCHOR_EXACT_MS,
    ANCHOR_NEAR_MS,
    Golden,
    GoldenFinding,
    ProviderScore,
    score_actionability,
    score_anchor,
    score_grounding,
    score_report,
)
from riftcoach.core.schema import (  # noqa: E402
    CoachingReport,
    Evidence,
    EvidenceTier,
    Finding,
)

GOLDEN_DIR = RAIZ / "evals" / "golden"


def _ev(tier: EvidenceTier, assumption: str | None = None) -> Evidence:
    return Evidence(
        tier=tier,
        timestamp_ms=600_000,
        statement="alguma coisa medida",
        source="timeline",
        assumption=assumption,
    )


def _finding(
    category: str = "macro",
    t_ms: int = 600_000,
    fix: str = "Atravesse no recall antes dos 13:00, com a wave crashada na torre",
    evidence: list[Evidence] | None = None,
) -> Finding:
    return Finding(
        category=category,  # type: ignore[arg-type]
        phase="mid",
        severity=4,
        timestamp_ms=t_ms,
        claim="voce estava do lado errado do mapa",
        evidence=evidence or [_ev(EvidenceTier.T1_MEASURED)],
        fix=fix,
        confidence=0.8,
    )


def _report(findings: list[Finding]) -> CoachingReport:
    return CoachingReport(
        match_id="BR1_1",
        patch="16.9",
        puuid="p",
        parser_version=1,
        findings=findings,
        top_three=list(range(min(3, len(findings)))),
    )


def _golden(*findings: GoldenFinding) -> Golden:
    return Golden(match_id="BR1_1", puuid="p", findings=list(findings))


def _gf(category: str = "macro", t_ms: int = 600_000, **kw: object) -> GoldenFinding:
    base: dict[str, object] = {
        "category": category,
        "timestamp_ms": t_ms,
        "severity": 4,
        "summary": "morreu do lado errado do mapa",
    }
    base.update(kw)
    return GoldenFinding(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Eixo 1 — fundamentacao, eliminatoria
# --------------------------------------------------------------------------


def test_measured_evidence_needs_no_assumption() -> None:
    nota, _ = score_grounding(_finding())
    assert nota == 1.0


def test_the_schema_refuses_a_derived_claim_without_an_assumption() -> None:
    """A primeira linha de defesa, e ela e mais forte do que parece."""
    with pytest.raises(ValidationError):
        _ev(EvidenceTier.T2_DERIVED, None)


def test_the_schema_refuses_it_even_nested_inside_a_finding() -> None:
    """E aqui esta o motivo de a premissa vazia nao ser um risco real: o
    pydantic REVALIDA o Evidence aninhado ao montar o Finding, entao nem
    construir a evidencia crua por fora contorna a regra.

    Isto e o que torna o eixo 1 uma verificacao de ultimo recurso no harness,
    e nao a defesa principal — a defesa principal e o tipo.
    """
    cru = Evidence.model_construct(
        tier=EvidenceTier.T2_DERIVED,
        timestamp_ms=600_000,
        statement="a wave estava empurrando",
        source="timeline",
        assumption=None,
    )
    with pytest.raises(ValidationError):
        _finding(evidence=[cru])


def test_grounding_still_checks_for_a_missing_assumption() -> None:
    """Defesa em profundidade, alcancavel so ignorando o schema inteiro.

    Vale manter: se um dia a validacao do schema for relaxada — por
    desempenho, ou porque alguem precisou carregar relatorio antigo sem
    revalidar — o harness continua reprovando em vez de passar a dar nota a
    findings sem premissa.
    """
    sem_validar = Finding.model_construct(
        category="macro",
        phase="mid",
        severity=4,
        timestamp_ms=600_000,
        claim="voce estava do lado errado do mapa",
        evidence=[
            Evidence.model_construct(
                tier=EvidenceTier.T2_DERIVED,
                timestamp_ms=600_000,
                statement="a wave estava empurrando",
                source="timeline",
                assumption=None,
            )
        ],
        fix="faca diferente",
        drill=None,
        confidence=0.8,
    )
    nota, motivo = score_grounding(sem_validar)
    assert nota == 0.0
    assert "premissa" in motivo


def test_a_generic_assumption_fails() -> None:
    """Um T2 com 'derivado dos dados' e formalmente valido e materialmente
    inutil: o usuario nao consegue discordar de uma premissa que nao foi dita."""
    nota, motivo = score_grounding(
        _finding(evidence=[_ev(EvidenceTier.T2_DERIVED, "derivado dos dados")])
    )
    assert nota == 0.0
    assert "generica" in motivo


def test_a_real_assumption_passes() -> None:
    nota, _ = score_grounding(
        _finding(
            evidence=[
                _ev(
                    EvidenceTier.T2_DERIVED,
                    "a API da Riot nao expoe estado de wave; isto vem do ritmo de CS",
                )
            ]
        )
    )
    assert nota == 1.0


def test_grounding_failure_zeroes_the_whole_finding() -> None:
    """A propriedade mais importante do modulo.

    Um finding perfeito nos outros tres eixos — ancora exata, categoria certa,
    correcao concreta — precisa valer ZERO se a premissa nao se sustenta. Um
    conselho bem escrito que o usuario nao consegue conferir e pior que nenhum
    conselho, porque ele nao tem como saber que precisa desconfiar.
    """
    ruim = _finding(
        fix=(
            "Com o dragao a menos de um minuto, atravesse no recall antes dos 13:00 "
            "e estabeleca visao no river de baixo em vez de empurrar a wave"
        ),
        evidence=[_ev(EvidenceTier.T2_DERIVED, "derivado dos dados")],
    )
    r = score_report(_report([ruim]), _golden(_gf()))
    s = r.scores[0]
    assert s.anchor == 1.0 and s.match == 1.0 and s.actionability == 1.0
    assert s.grounding == 0.0
    assert s.total == 0.0, "os outros tres eixos nao podem salvar a fundamentacao"


# --------------------------------------------------------------------------
# Eixo 2 — ancoragem
# --------------------------------------------------------------------------


def test_anchor_scoring_has_three_bands() -> None:
    assert score_anchor(600_000, 600_000) == 1.0
    assert score_anchor(600_000, 600_000 + ANCHOR_EXACT_MS - 1) == 1.0
    assert score_anchor(600_000, 600_000 + ANCHOR_EXACT_MS + 1) == 0.5
    assert score_anchor(600_000, 600_000 + ANCHOR_NEAR_MS + 1) == 0.0


def test_anchor_is_symmetric() -> None:
    """Adiantar e atrasar por igual precisa custar por igual — senao a metrica
    premiaria sistematicamente ancorar cedo demais."""
    assert score_anchor(600_000, 620_000) == score_anchor(620_000, 600_000)


# --------------------------------------------------------------------------
# Eixo 3 — categoria
# --------------------------------------------------------------------------


def test_the_same_category_scores_full() -> None:
    r = score_report(_report([_finding("macro")]), _golden(_gf("macro")))
    assert r.scores[0].match == 1.0


def test_neighbouring_categories_score_half() -> None:
    """As fronteiras entre macro, positioning e decision sao genuinamente
    borradas, e o jogador nao liga para a etiqueta."""
    r = score_report(_report([_finding("positioning")]), _golden(_gf("macro")))
    assert r.scores[0].match == 0.5


def test_an_unrelated_category_scores_zero() -> None:
    r = score_report(_report([_finding("vision")]), _golden(_gf("macro")))
    assert r.scores[0].match == 0.0


# --------------------------------------------------------------------------
# Eixo 4 — utilidade
# --------------------------------------------------------------------------


def test_a_truism_is_worthless() -> None:
    """O teste operacional: se a fix serviria para qualquer partida de qualquer
    jogador, ela vale zero."""
    assert score_actionability(_finding(fix="Melhore seu posicionamento.")) == 0.0
    assert score_actionability(_finding(fix="Nao morra tanto.")) == 0.0


def test_a_concrete_fix_scores_well() -> None:
    nota = score_actionability(
        _finding(
            fix=(
                "Com o dragao a menos de um minuto, atravesse no recall antes dos 13:00 "
                "e estabeleca visao no river de baixo em vez de empurrar a wave"
            )
        )
    )
    assert nota == 1.0


def test_a_manual_annotation_beats_the_heuristic() -> None:
    """A heuristica e grosseira e nao entende a frase. Quando um humano julga,
    o julgamento dele vence."""
    truismo = _finding(fix="Melhore seu posicionamento.")
    assert score_actionability(truismo, manual=1.0) == 1.0
    assert score_actionability(truismo, manual=None) == 0.0


# --------------------------------------------------------------------------
# Casamento
# --------------------------------------------------------------------------


def test_each_golden_is_consumed_at_most_once() -> None:
    """Sem isso, tres findings quase iguais sobre a mesma morte casariam todos
    com a mesma anotacao e a precisao ficaria inflada exatamente no caso que
    ela deveria punir."""
    tres_iguais = [_finding(t_ms=600_000) for _ in range(3)]
    r = score_report(_report(tres_iguais), _golden(_gf(t_ms=600_000)))
    assert r.matched == 1
    assert r.noise == 2


def test_matching_is_greedy_from_the_best() -> None:
    """O finding que casa melhor com uma anotacao precisa ficar com ela, e nao
    perde-la para um pior que apareceu antes na lista."""
    pior = _finding("vision", t_ms=620_000)
    melhor = _finding("macro", t_ms=600_000)
    r = score_report(_report([pior, melhor]), _golden(_gf("macro", 600_000)))
    assert r.scores[1].matched_golden == 0
    assert r.scores[0].matched_golden is None


def test_unmatched_reports_count_as_noise() -> None:
    r = score_report(_report([_finding(t_ms=100_000)]), _golden(_gf(t_ms=1_500_000)))
    assert r.noise == 1
    assert r.matched == 0
    assert r.precision == 0.0


def test_missed_golden_findings_are_listed() -> None:
    """Saber O QUE o sistema nao viu vale mais que saber quantos."""
    r = score_report(_report([]), _golden(_gf(t_ms=1_095_000)))
    assert r.recall == 0.0
    assert len(r.missed) == 1
    assert "18:15" in r.missed[0]


def test_an_empty_report_scores_zero_not_one() -> None:
    """Nao reportar nada precisa ser ruim. Uma metrica em que o silencio ganha
    premiaria exatamente o comportamento inutil."""
    r = score_report(_report([]), _golden(_gf(), _gf(t_ms=900_000)))
    assert r.precision == 0.0
    assert r.recall == 0.0
    assert r.f_beta(1.0) == 0.0


# --------------------------------------------------------------------------
# Agregacao
# --------------------------------------------------------------------------


def test_f05_weighs_precision_more_than_f1() -> None:
    """Um finding errado custa mais credibilidade do que dez certos constroem,
    e o relatorio L5 ja garante um piso de cobertura."""
    muitos_ruins = [_finding(t_ms=100_000 * i) for i in range(1, 9)]
    r = score_report(_report(muitos_ruins), _golden(_gf(t_ms=100_000)))
    assert r.precision < r.recall
    assert r.f_beta(0.5) < r.f_beta(1.0), "F0.5 precisa punir a precisao baixa"


def test_a_provider_with_no_runs_does_not_divide_by_zero() -> None:
    vazio = ProviderScore(provider="ninguem")
    assert vazio.f1 == 0.0
    assert vazio.precision == 0.0
    assert vazio.drop_rate == 0.0


# --------------------------------------------------------------------------
# O corpus de verdade
# --------------------------------------------------------------------------


def test_every_golden_file_parses() -> None:
    corpus = load_corpus()
    assert corpus, "o corpus nao pode estar vazio"
    for g in corpus:
        assert g.match_id and g.puuid
        assert g.findings, f"{g.match_id} sem findings anotados"


def test_golden_annotations_stay_inside_the_rubric_limit() -> None:
    """Tres a seis por partida. Uma anotacao com vinte entradas transforma a
    cobertura em ruido e pune modelos por nao listarem coisas que nenhum
    jogador quereria ler."""
    for g in load_corpus():
        assert 1 <= len(g.findings) <= 6, f"{g.match_id}: {len(g.findings)} findings"


def test_golden_files_carry_only_a_puuid_prefix() -> None:
    """PUUID completo identifica a pessoa fora da partida. O prefixo basta para
    acha-la dentro dela."""
    for caminho in GOLDEN_DIR.glob("*.json"):
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        assert len(dados["puuid"]) <= 24, f"{caminho.name}: puuid longo demais"


def test_golden_timestamps_are_plausible() -> None:
    for g in load_corpus():
        for f in g.findings:
            assert 0 <= f.timestamp_ms <= 90 * 60_000
            assert 1 <= f.severity <= 5


def test_the_baseline_runs_over_the_real_corpus() -> None:
    """O teste de ponta a ponta do harness: ele precisa rodar sem rede, sem
    modelo e sem chave — senao nao serve de linha de base."""
    score = run_baseline(load_corpus())
    assert score.provider == BASELINE
    assert score.matches
    assert score.failures == 0, "nenhuma fixture pode estar faltando"
    assert score.recall > 0.0, "o motor deterministico precisa achar alguma coisa"


def test_the_baseline_finds_most_of_what_was_annotated() -> None:
    """Trava de regressao. Se uma mudanca no motor de regras derrubar isto, o
    PR precisa explicar por que — e nao descobrir depois pelo usuario."""
    score = run_baseline(load_corpus())
    assert score.recall >= 0.6, f"cobertura caiu para {score.recall:.2f}"
