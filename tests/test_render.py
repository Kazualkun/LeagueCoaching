"""Testes da renderizacao compacta."""

from __future__ import annotations

import json
from typing import Any

import pytest

from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts
from riftcoach.parse.render import (
    ANALYST_SECTIONS,
    Section,
    estimate_tokens,
    render,
    render_for_analyst,
)
from tests.test_distill import load


@pytest.fixture(scope="module")
def facts() -> MatchFacts:
    match, tl = load("sr_ranked_35min")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


@pytest.fixture(scope="module")
def remake_facts() -> MatchFacts:
    match, tl = load("sr_remake")
    return distill(match, tl, match["info"]["participants"][0]["puuid"])


class FakeResolver:
    def item(self, item_id: int) -> str:
        return {3076: "Placa Espinhosa", 3009: "Botas de Rapidez"}.get(item_id, f"item{item_id}")

    def rune(self, perk_id: int) -> str:
        return f"runa{perk_id}"

    def summoner(self, spell_id: int) -> str:
        return {4: "Flash", 12: "Teleporte"}.get(spell_id, f"spell{spell_id}")


# --------------------------------------------------------------------------
# Economia de tokens
# --------------------------------------------------------------------------


def test_text_beats_json(facts: MatchFacts) -> None:
    """A razao de ser deste modulo.

    JSON paga chaves, aspas e nomes de campo repetidos em TODA linha. Se o
    texto nao for sensivelmente menor, e melhor mandar o JSON e apagar isto.
    """
    texto = len(render(facts))
    js = len(facts.model_dump_json())
    assert js / texto > 2.5, f"ganho apenas {js / texto:.1f}x"


def test_full_render_fits_the_budget(facts: MatchFacts) -> None:
    """Partida de 35 min, 9 mortes, 12 recalls, 28 objetivos.

    O orcamento do blueprint e ~1.100-1.600 tokens para a partida inteira. Se
    isto estourar, algum filtro parou de filtrar — foi exatamente assim que o
    bug do `contested` apareceu (578 tokens so de objetivos).
    """
    assert estimate_tokens(render(facts)) < 2000


@pytest.mark.parametrize("analyst", list(ANALYST_SECTIONS))
def test_analyst_slice_is_small(facts: MatchFacts, analyst: str) -> None:
    """E isto que torna modelos de 8B viaveis: cada passe ve uma fatia e
    responde uma pergunta. Prompt longo multiobjetivo e onde eles desabam."""
    assert estimate_tokens(render_for_analyst(facts, analyst)) < 1200


def test_analyst_slices_are_smaller_than_full(facts: MatchFacts) -> None:
    cheio = estimate_tokens(render(facts))
    for analyst in ANALYST_SECTIONS:
        assert estimate_tokens(render_for_analyst(facts, analyst)) < cheio


def test_sections_are_selectable(facts: MatchFacts) -> None:
    so_mortes = render(facts, (Section.DEATHS,))
    assert "MORTES" in so_mortes
    assert "RECALLS" not in so_mortes
    assert "OBJETIVOS" not in so_mortes


# --------------------------------------------------------------------------
# Honestidade do conteudo
# --------------------------------------------------------------------------


def test_no_puuid_leaks_into_the_prompt(facts: MatchFacts) -> None:
    """PUUIDs sao identificadores pessoais e nao ajudam o modelo em nada.

    Tudo que e renderizado pode ir para um provedor na nuvem — identificador
    nao tem motivo para sair da maquina.
    """
    texto = render(facts)
    assert facts.focus.puuid not in texto
    for p in facts.team + facts.enemy:
        assert p.puuid not in texto


def test_unknown_renders_as_nd_not_zero(remake_facts: MatchFacts) -> None:
    """Remake: a partida nao chegou ao minuto 10.

    Renderizar 'gd@10=0' afirmaria um empate que nunca foi medido. Precisa
    aparecer como N/D.
    """
    texto = render(remake_facts)
    assert "gd@10=0" not in texto
    if "gd@10" in texto:
        assert "gd@10=N/D" in texto


def test_no_raw_zone_labels_leak(facts: MatchFacts) -> None:
    """Rotulos crus obrigariam o modelo a adivinhar de quem e o lado."""
    texto = render(facts)
    for cru in (" TOP_MID ", " MID_MID ", " BOT_MID ", " TOP_BLUE ", " MID_RED "):
        assert cru not in texto, cru


def test_vision_states_its_own_limitation(facts: MatchFacts) -> None:
    """A telemetria nao da posicao de ward.

    O modelo precisa ler isso no contexto, senao inventa 'voce nao wardou o
    pit' a partir de uma contagem que nao sabe onde as wards foram.
    """
    texto = render(facts, (Section.VISION,))
    assert "nao informa ONDE" in texto


def test_recalls_are_marked_as_inferred(facts: MatchFacts) -> None:
    """Recalls sao T2. O rotulo precisa viajar junto com o dado."""
    assert "T2" in render(facts, (Section.RECALLS,))


def test_contested_is_not_always_true(facts: MatchFacts) -> None:
    """Regressao: sem a dimensao espacial, 26 de 28 objetivos apareciam como
    disputados e o marcador virava ruido constante."""
    disputados = sum(1 for o in facts.objectives if o.contested)
    assert 0 < disputados < len(facts.objectives) * 0.5


# --------------------------------------------------------------------------
# Resolucao de nomes (camada de conhecimento, etapa 2)
# --------------------------------------------------------------------------


def test_ids_render_raw_without_a_resolver(facts: MatchFacts) -> None:
    """Sem resolver, ids crus. Nunca inventamos um nome — id nao resolvido e
    informacao AUSENTE, e o modelo precisa ve-la como ausente."""
    texto = render(facts, (Section.BUILD,))
    assert any(c.isdigit() for c in texto)


def test_resolver_replaces_ids_with_names(facts: MatchFacts) -> None:
    texto = render(facts, (Section.BUILD,), resolver=FakeResolver())
    assert "Placa Espinhosa" in texto or "item" in texto
    assert "3076" not in texto


def test_resolver_is_used_in_recalls_too(facts: MatchFacts) -> None:
    texto = render(facts, (Section.RECALLS,), resolver=FakeResolver())
    assert "comprou=[3076]" not in texto


# --------------------------------------------------------------------------
# Robustez
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min", "sr_remake"])
def test_every_player_renders(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        texto = render(f)
        assert texto.strip()
        assert texto.endswith("\n")


def test_remake_renders_without_crashing(remake_facts: MatchFacts) -> None:
    texto = render(remake_facts)
    assert "MORTES (0)" in texto
    assert "RECALLS (0)" in texto


def test_is_deterministic(facts: MatchFacts) -> None:
    """Snapshots e testes de regressao dependem disto."""
    assert render(facts) == render(facts)


def test_reduction_versus_raw_json(facts: MatchFacts) -> None:
    match, tl = load("sr_ranked_35min")
    bruto = len(json.dumps(match).encode()) + len(json.dumps(tl).encode())
    assert bruto / len(render(facts)) > 100


def _sections_of(texto: str) -> list[str]:
    return [ln for ln in texto.splitlines() if ln and ln[0].isupper() and "=" not in ln]


def test_header_is_always_present(facts: MatchFacts) -> None:
    """Todo analista precisa saber campeao, rota e resultado — sem isso o
    conselho sai descolado da partida."""
    for analyst in ANALYST_SECTIONS:
        assert "VOCE" in render_for_analyst(facts, analyst)


def test_unknown_analyst_falls_back_to_full(facts: MatchFacts) -> None:
    """Nome errado nao pode devolver texto vazio em silencio: um analista sem
    contexto nenhum produziria conselho inventado."""
    texto = render_for_analyst(facts, "nao-existe")
    assert "VOCE" in texto
    assert len(texto) > 500


def test_estimate_tokens_is_monotonic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("a" * 800) > estimate_tokens("a" * 400)


def test_analyst_sections_cover_every_section() -> None:
    """Nenhuma secao pode ficar orfa: se ninguem le, ou e morta ou alguem
    esqueceu de incluir."""
    usadas: set[Any] = set()
    for secs in ANALYST_SECTIONS.values():
        usadas.update(secs)
    orfas = set(Section) - usadas
    assert not orfas, f"secoes que nenhum analista le: {orfas}"
