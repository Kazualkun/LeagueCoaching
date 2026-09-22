"""Testes do FactValidator e do pipeline completo com IA.

O README promete que o modelo nunca e responsavel pelos fatos. Este arquivo
verifica a parte dessa promessa que e codigo:

  - um item que existiu mas foi removido nao passa      (STALE_ENTITY)
  - um item que nunca existiu nao passa                 (HALLUCINATED_ENTITY)
  - um finding que nao da para clicar no replay nao passa (BAD_ANCHOR)
  - e, acima de tudo: NADA disso pode derrubar o relatorio. O pior caso do
    caminho com IA e exatamente o relatorio sem IA.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from riftcoach.analysis.report import analyze, analyze_with_ai
from riftcoach.core.schema import CoachingReport, Evidence, EvidenceTier, Finding
from riftcoach.knowledge.sync import PatchDB
from riftcoach.knowledge.validator import (
    EntityScanner,
    ValidationResult,
    ViolationKind,
    validate,
    validate_findings,
)
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from tests.test_distill import load
from tests.test_llm import FakeProvider, _profile, _router

PATCH = "16.9"
ANTIGO = "15.1"


@pytest.fixture
def db(tmp_path: Path) -> PatchDB:
    """Banco de patch minimo, montado a mao.

    Escrever direto no sqlite em vez de sincronizar o DataDragon: os testes nao
    tocam a rede, e o que interessa aqui e a logica de comparacao entre
    patches, nao o download.
    """
    caminho = tmp_path / "patch.sqlite"
    p = PatchDB(caminho)
    linhas = [
        # (patch, locale, kind, id, nome)
        (PATCH, "pt_BR", "item", 3153, "Lamina do Rei Arruinado", None),
        (PATCH, "pt_BR", "item", 1001, "Botas", None),
        (PATCH, "pt_BR", "item", 2055, "Sentinela de Controle", None),
        (PATCH, "pt_BR", "champion", 86, "Garen", None),
        (PATCH, "en_US", "item", 3153, "Blade of the Ruined King", None),
        # So no patch antigo: removido desde entao.
        (ANTIGO, "pt_BR", "item", 3152, "Eco de Luden", None),
        (ANTIGO, "pt_BR", "item", 1001, "Botas", None),
    ]
    with sqlite3.connect(caminho) as con:
        con.executemany(
            "INSERT OR REPLACE INTO entity (patch, locale, kind, entity_id, name, data)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            linhas,
        )
        for patch in (PATCH, ANTIGO):
            for locale in ("pt_BR", "en_US"):
                con.execute(
                    "INSERT OR REPLACE INTO synced (patch, locale, ddragon, synced_at)"
                    " VALUES (?, ?, ?, ?)",
                    (patch, locale, f"{patch}.1", 0),
                )
    return p


def _finding(claim: str = "algo", t_ms: int = 600_000, fix: str = "faca diferente") -> Finding:
    return Finding(
        category="itemization",
        phase="mid",
        severity=3,
        timestamp_ms=t_ms,
        claim=claim,
        evidence=[
            Evidence(
                tier=EvidenceTier.T1_MEASURED,
                timestamp_ms=t_ms,
                statement="medido",
                source="timeline",
            )
        ],
        fix=fix,
        confidence=0.6,
    )


# --------------------------------------------------------------------------
# O automato
# --------------------------------------------------------------------------


def test_the_scanner_finds_overlapping_names() -> None:
    """'Botas' dentro de 'Botas de Velocidade' precisa aparecer tambem: sem
    herdar as saidas pelo link de falha, o padrao mais curto some."""
    s = EntityScanner({"botas": "item", "botas de velocidade": "item"})
    assert s.scan("compre Botas de Velocidade") == {"botas", "botas de velocidade"}


def test_the_scanner_ignores_case() -> None:
    """Os nomes vem do DataDragon com capitalizacao propria, e o modelo nao a
    respeita de forma confiavel."""
    s = EntityScanner({"garen": "champion"})
    assert s.scan("GAREN empurrou") == {"garen"}
    assert s.scan("garen empurrou") == {"garen"}


def test_the_scanner_finds_nothing_in_clean_text() -> None:
    s = EntityScanner({"garen": "champion", "botas": "item"})
    assert s.scan("voce morreu no river sem visao") == set()


def test_the_scanner_handles_an_empty_table() -> None:
    assert EntityScanner({}).scan("qualquer coisa") == set()


def test_very_short_names_are_excluded(db: PatchDB) -> None:
    """Um nome de dois caracteres casaria com qualquer texto e so produziria
    ruido."""
    nomes = db.entity_names()
    assert all(len(n) > 2 for n in nomes)


# --------------------------------------------------------------------------
# STALE_ENTITY — a captura que mais se paga
# --------------------------------------------------------------------------


def test_an_item_removed_from_this_patch_is_caught(db: PatchDB) -> None:
    """O modelo foi treinado antes do patch e recomenda o item com toda a
    confianca. O jogador abre a loja, o item nao existe, e nada mais que o
    relatorio disser importa."""
    r = validate_findings(
        [_finding(fix="Rushe Eco de Luden no segundo item")], PATCH, db, 2_000_000
    )
    assert not r.clean
    assert r.violations[0].kind is ViolationKind.STALE_ENTITY
    assert "Eco de Luden".lower() in r.violations[0].detail
    assert r.kept == []


def test_an_item_that_still_exists_passes(db: PatchDB) -> None:
    r = validate_findings(
        [_finding(fix="Compre Sentinela de Controle em todo recall")], PATCH, db, 2_000_000
    )
    assert r.clean
    assert len(r.kept) == 1


def test_the_english_name_is_recognized_too(db: PatchDB) -> None:
    """Um modelo treinado majoritariamente em ingles escreve 'Blade of the
    Ruined King' mesmo instruido em portugues. Reconhecer e melhor que
    rejeitar um finding correto."""
    r = validate_findings(
        [_finding(fix="Blade of the Ruined King resolve esse matchup")],
        PATCH,
        db,
        2_000_000,
    )
    assert r.clean


def test_entities_are_scanned_in_every_field_the_model_wrote(db: PatchDB) -> None:
    f = Finding(
        category="itemization",
        phase="mid",
        severity=3,
        timestamp_ms=600_000,
        claim="tudo certo aqui",
        evidence=[
            Evidence(
                tier=EvidenceTier.T2_DERIVED,
                timestamp_ms=600_000,
                statement="voce nao tinha Eco de Luden ainda",
                source="timeline",
                assumption="derivado do caminho de build",
            )
        ],
        fix="nada",
        confidence=0.6,
    )
    r = validate_findings([f], PATCH, db, 2_000_000)
    assert any(v.kind is ViolationKind.STALE_ENTITY for v in r.violations)


# --------------------------------------------------------------------------
# HALLUCINATED_ENTITY — precisa da declaracao do modelo
# --------------------------------------------------------------------------


def test_an_invented_item_is_caught_via_the_declaration(db: PatchDB) -> None:
    """Uma entidade inventada NAO casa com o automato, justamente por ser
    inventada. So a declaracao do modelo revela que ele achou que citou um
    item."""
    r = validate_findings(
        [_finding(fix="Compre Cajado do Vazio Eterno")],
        PATCH,
        db,
        2_000_000,
        declared_entities={0: ["Cajado do Vazio Eterno"]},
    )
    assert not r.clean
    assert r.violations[0].kind is ViolationKind.HALLUCINATED_ENTITY
    assert r.kept == []


def test_a_real_item_declared_is_fine(db: PatchDB) -> None:
    r = validate_findings(
        [_finding(fix="Compre Botas cedo")],
        PATCH,
        db,
        2_000_000,
        declared_entities={0: ["Botas"]},
    )
    assert r.clean


def test_declarations_are_matched_to_the_right_finding(db: PatchDB) -> None:
    """Se o alinhamento quebrar, o validador confere as entidades de um finding
    contra outro — e a saida continua plausivel, entao ninguem percebe."""
    r = validate_findings(
        [_finding(claim="primeiro"), _finding(claim="segundo")],
        PATCH,
        db,
        2_000_000,
        declared_entities={1: ["Item Que Nao Existe"]},
    )
    assert len(r.violations) == 1
    assert r.violations[0].finding_index == 1
    assert [f.claim for f in r.kept] == ["primeiro"]


# --------------------------------------------------------------------------
# BAD_ANCHOR
# --------------------------------------------------------------------------


def test_a_finding_outside_the_match_is_dropped(db: PatchDB) -> None:
    r = validate_findings([_finding(t_ms=9_000_000)], PATCH, db, 2_000_000)
    assert r.violations[0].kind is ViolationKind.BAD_ANCHOR
    assert r.kept == []


def test_a_finding_at_the_very_end_is_kept(db: PatchDB) -> None:
    r = validate_findings([_finding(t_ms=2_000_000)], PATCH, db, 2_000_000)
    assert r.clean


# --------------------------------------------------------------------------
# Patch desconhecido
# --------------------------------------------------------------------------


def test_an_unsynced_patch_skips_validation_and_says_so(db: PatchDB) -> None:
    """Sem dados do patch nao da para distinguir 'item removido' de 'item que
    nunca sincronizamos'. Acusar o segundo descartaria findings bons."""
    r = validate_findings([_finding(fix="Rushe Eco de Luden")], "99.9", db, 2_000_000)
    assert r.skipped
    assert len(r.kept) == 1
    assert "pulada" in r.summary()


def test_a_clean_report_says_it_was_validated(db: PatchDB) -> None:
    """'Nao validado' e uma informacao diferente de 'validado e limpo', e
    apresentar as duas do mesmo jeito seria mentir por omissao."""
    r = validate_findings([_finding()], PATCH, db, 2_000_000)
    assert "nenhuma violacao" in r.summary()
    assert not r.skipped


def test_validate_also_works_on_a_finished_report(db: PatchDB) -> None:
    rel = CoachingReport(
        match_id="BR1_1",
        patch=PATCH,
        puuid="p",
        parser_version=1,
        findings=[_finding(fix="Rushe Eco de Luden")],
        top_three=[0],
    )
    r = validate(rel, db, 2_000_000)
    assert not r.clean


def test_empty_input_is_clean(db: PatchDB) -> None:
    r = validate_findings([], PATCH, db, 2_000_000)
    assert r.clean and r.kept == [] and isinstance(r, ValidationResult)


# --------------------------------------------------------------------------
# O pipeline completo
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def garen() -> MatchFacts:
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


def _analyst_json(claim: str = "Voce empurrou sem visao", t_ms: int = 600_000) -> str:
    return (
        '{"findings": [{"category": "wave", "severity": 4, "timestamp_ms": '
        f"{t_ms}"
        ', "claim": "' + claim + '", "evidence": [{"tier": "T1", "timestamp_ms": '
        f"{t_ms}"
        ', "statement": "cs@10 abaixo da mediana", "assumption": ""}],'
        ' "fix": "resolva a rota antes do objetivo", "drill": "", "entities": []}]}'
    )


HEAD_COACH_JSON = (
    '{"keep": [{"ref": "laning:0", "severity": 4, "reason": "custou caro"}],'
    ' "top_three": ["laning:0"]}'
)


@pytest.mark.asyncio
async def test_the_ai_path_adds_findings_on_top_of_the_rules(garen: MatchFacts) -> None:
    p = FakeProvider(_profile("groq"), [_analyst_json()] * 4 + [HEAD_COACH_JSON])
    r = _router(p)
    rel, texto, trace = await analyze_with_ai(garen, r)

    assert trace.used_ai
    assert "modelos:" in texto
    assert "SEM IA" not in texto
    # As regras continuam presentes: elas sao a rede de seguranca, nao uma
    # alternativa ao modelo.
    assert any(f.confidence >= 0.7 for f in rel.findings)


@pytest.mark.asyncio
async def test_with_no_provider_the_ai_path_returns_exactly_the_l5(garen: MatchFacts) -> None:
    """A garantia que sustenta a escada de degradacao inteira."""
    vazio = _router()
    rel_ia, texto_ia, trace = await analyze_with_ai(garen, vazio)
    rel_l5, _ = analyze(garen)

    assert not trace.used_ai
    assert "SEM IA" in texto_ia
    assert [f.claim for f in rel_ia.ranked()] == [f.claim for f in rel_l5.ranked()]


@pytest.mark.asyncio
async def test_every_analyst_failing_still_produces_a_report(garen: MatchFacts) -> None:
    """Um modelo que nunca valida nao pode custar o relatorio ao usuario."""
    p = FakeProvider(_profile("groq"), ["nao sou json"] * 40)
    rel, texto, trace = await analyze_with_ai(garen, _router(p))

    assert rel.findings, "as regras precisam ter sobrevivido"
    assert not trace.used_ai
    assert "SEM IA" in texto


@pytest.mark.asyncio
async def test_one_analyst_failing_does_not_take_the_others_down(
    garen: MatchFacts,
) -> None:
    """`return_exceptions=True` no fan-out. Um relatorio com tres angulos e
    util; nenhum relatorio nao e.

    O provedor responde pelo conteudo do prompt, e nao por uma fila: com uma
    fila, as tres tentativas gastas pelo analista que falha QUEIMARIAM o
    provedor compartilhado e derrubariam os outros tres junto — que e o
    comportamento correto do breaker, mas nao e o que este teste mede.
    """
    p = FakeProvider(
        _profile("groq"),
        by_prompt={
            "Analista de lutas": "nao sou json",
            "Analista de rota": _analyst_json("rota"),
            "Analista de macro": _analyst_json("macro"),
            "Analista de economia": _analyst_json("economia"),
            "Head coach": HEAD_COACH_JSON,
        },
    )
    _rel, _texto, trace = await analyze_with_ai(garen, _router(p))

    assert sum(trace.by_analyst.values()) > 0, "os outros tres tinham que ter produzido"
    assert "fights" in trace.failures


@pytest.mark.asyncio
async def test_the_validator_drops_stale_items_from_the_ai_path(
    garen: MatchFacts, db: PatchDB
) -> None:
    """O caminho ponta a ponta da promessa principal do README."""
    obsoleto = (
        '{"findings": [{"category": "itemization", "severity": 4, "timestamp_ms": 600000,'
        ' "claim": "build errada", "evidence": [{"tier": "T1", "timestamp_ms": 600000,'
        ' "statement": "voce comprou tarde", "assumption": ""}],'
        ' "fix": "Rushe Eco de Luden", "drill": "", "entities": ["Eco de Luden"]}]}'
    )
    p = FakeProvider(_profile("groq"), [obsoleto] * 4 + [HEAD_COACH_JSON])
    facts = garen.model_copy(update={"patch": PATCH})
    rel, _texto, trace = await analyze_with_ai(facts, _router(p), patch_db=db)

    assert trace.violations, "o item removido tinha que ter sido pego"
    assert all("Eco de Luden" not in f.fix for f in rel.findings)


@pytest.mark.asyncio
async def test_a_head_coach_failure_falls_back_to_the_deterministic_merge(
    garen: MatchFacts,
) -> None:
    """O head coach melhora a selecao; ele nao e condicao para existir
    relatorio."""
    p = FakeProvider(_profile("groq"), [_analyst_json()] * 4 + ["nao sou json"] * 10)
    rel, texto, trace = await analyze_with_ai(garen, _router(p))
    assert trace.head_coach_error
    assert rel.findings
    assert "head coach falhou" in texto


@pytest.mark.asyncio
async def test_a_model_finding_outside_the_match_never_reaches_the_report(
    garen: MatchFacts,
) -> None:
    fora = _analyst_json(t_ms=99_000_000)
    p = FakeProvider(_profile("groq"), [fora] * 4 + [HEAD_COACH_JSON])
    rel, _texto, trace = await analyze_with_ai(garen, _router(p))
    limite = garen.duration_s * 1000
    assert all(f.timestamp_ms <= limite for f in rel.findings)
    assert trace.rejected


@pytest.mark.asyncio
async def test_a_model_finding_is_never_squeezed_out_by_the_rules(
    garen: MatchFacts,
) -> None:
    """A regressao que motivou `merge_sources`.

    O criterio de ranqueamento coloca medicao acima de interpretacao de
    proposito, e as regras sozinhas ja enchem o relatorio. Sem reserva, o
    modelo produzia um finding bom e ele era cortado — ou seja, ligar a IA nao
    mudava nada visivel na saida.
    """
    unico = (
        '{"findings": [{"category": "macro", "severity": 4, "timestamp_ms": 1050000,'
        ' "claim": "MARCADOR DO MODELO", "evidence": [{"tier": "T1",'
        ' "timestamp_ms": 1050000, "statement": "voce estava na selva de cima",'
        ' "assumption": ""}], "fix": "va para o river", "drill": "", "entities": []}]}'
    )
    p = FakeProvider(
        _profile("groq"),
        by_prompt={
            "Analista de macro": unico,
            "Head coach": '{"keep": [{"ref": "macro:0", "severity": 4}], "top_three": []}',
        },
    )
    rel, _texto, _trace = await analyze_with_ai(garen, _router(p))
    assert any("MARCADOR DO MODELO" in f.claim for f in rel.findings)


@pytest.mark.asyncio
async def test_the_measured_findings_still_come_first(garen: MatchFacts) -> None:
    """A reserva nao rebaixa o medido: ela so impede que ele zere o
    interpretado."""
    fraco = (
        '{"findings": [{"category": "vision", "severity": 1, "timestamp_ms": 600000,'
        ' "claim": "observacao fraca", "evidence": [{"tier": "T3",'
        ' "timestamp_ms": 600000, "statement": "chute", "assumption": "puro palpite meu"}],'
        ' "fix": "nada", "drill": "", "entities": []}]}'
    )
    p = FakeProvider(_profile("groq"), by_prompt={"Analista": fraco, "Head coach": "lixo"})
    rel, _texto, _trace = await analyze_with_ai(garen, _router(p))
    primeiro = rel.ranked()[0]
    assert primeiro.severity >= 4
    assert "observacao fraca" not in primeiro.claim


@pytest.mark.asyncio
async def test_the_footer_always_says_which_path_produced_the_report(
    garen: MatchFacts,
) -> None:
    """Um relatorio que usou IA e um que degradou precisam ser distinguiveis
    pelo leitor."""
    com = FakeProvider(_profile("groq"), [_analyst_json()] * 4 + [HEAD_COACH_JSON])
    _, texto_com, _ = await analyze_with_ai(garen, _router(com))
    _, texto_sem, _ = await analyze_with_ai(garen, _router())

    assert ("modelos:" in texto_com) != ("modelos:" in texto_sem)
    assert ("SEM IA" in texto_sem) and ("SEM IA" not in texto_com)
