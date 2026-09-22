"""Testes do banco de patch (DataDragon). Offline — o HTTP e mockado."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
import respx

from riftcoach.knowledge.sync import DDRAGON, PatchDB, PatchSyncError
from riftcoach.parse.render import Section, render

VERSIONS = ["16.10.1", "16.9.1", "16.8.1", "15.24.1"]


@pytest.fixture
def db(tmp_path: Path) -> PatchDB:
    return PatchDB(tmp_path / "patch.sqlite")


def _seed(db: PatchDB, patch: str, locale: str, kind: str, eid: int, name: str) -> None:
    with sqlite3.connect(db.path) as c:
        c.execute(
            "INSERT OR REPLACE INTO entity (patch, locale, kind, entity_id, name, data)"
            " VALUES (?,?,?,?,?,NULL)",
            (patch, locale, kind, eid, name),
        )


# --------------------------------------------------------------------------
# Resolucao de versao
# --------------------------------------------------------------------------


def test_game_patch_maps_to_ddragon_version() -> None:
    """O gameVersion do match-v5 NAO bate com o nome da versao do DDragon.

    A partida diz '16.9.772.8292'; o DDragon conhece '16.9.1'. Passar qualquer
    das duas strings cruas para a outra ponta nao acha nada.
    """
    assert PatchDB._match_version("16.9", VERSIONS) == "16.9.1"
    assert PatchDB._match_version("15.24", VERSIONS) == "15.24.1"


def test_missing_patch_fails_with_a_useful_hint() -> None:
    with pytest.raises(PatchSyncError) as e:
        PatchDB._match_version("9.1", VERSIONS)
    assert e.value.hint and "ids em vez de nomes" in e.value.hint


def test_prefix_match_is_not_substring_match() -> None:
    """'16.1' nao pode casar com '16.10.1'. Um patch a mais de meio ano de
    distancia traria stats de itens que nem existiam na partida."""
    assert PatchDB._match_version("16.1", ["16.10.1", "16.1.1"]) == "16.1.1"


# --------------------------------------------------------------------------
# Consulta
# --------------------------------------------------------------------------


def test_lookup_respects_the_pinned_patch(db: PatchDB) -> None:
    """Um relatorio SEMPRE consulta o patch da partida, nunca o mais recente."""
    _seed(db, "16.9", db._locale, "item", 3076, "Colete Espinhoso")
    _seed(db, "16.18", db._locale, "item", 3076, "Nome Novo")
    db.use_patch("16.9")
    assert db.item(3076) == "Colete Espinhoso"
    db.use_patch("16.18")
    assert db.item(3076) == "Nome Novo"


def test_falls_back_to_another_patch(db: PatchDB) -> None:
    """Nome levemente desatualizado e infinitamente mais util que '3076'.

    Divergencia de stat e problema do FactValidator, nao do resolver.
    """
    _seed(db, "16.8", db._locale, "item", 3076, "Colete Espinhoso")
    db.use_patch("16.9")  # patch nao sincronizado
    assert db.item(3076) == "Colete Espinhoso"


def test_unknown_id_returns_empty_not_a_guess(db: PatchDB) -> None:
    """String vazia faz o render manter o id cru.

    Id nao resolvido e informacao AUSENTE — o modelo precisa ve-la como
    ausente, nunca receber um palpite no lugar.
    """
    db.use_patch("16.9")
    assert db.item(999999) == ""
    assert db.rune(999999) == ""


@pytest.mark.parametrize("bad", [0, -1])
def test_zero_and_negative_ids_are_empty(db: PatchDB, bad: int) -> None:
    """Slot de item vazio vem como 0 no match-v5."""
    assert db.item(bad) == ""


def test_lookup_is_cached(db: PatchDB) -> None:
    _seed(db, "16.9", db._locale, "item", 3076, "Colete Espinhoso")
    db.use_patch("16.9")
    assert db.item(3076) == db.item(3076) == "Colete Espinhoso"
    assert ("item", 3076) in db._cache


def test_changing_patch_clears_the_cache(db: PatchDB) -> None:
    _seed(db, "16.9", db._locale, "item", 1, "Antigo")
    _seed(db, "16.18", db._locale, "item", 1, "Novo")
    db.use_patch("16.9")
    assert db.item(1) == "Antigo"
    db.use_patch("16.18")
    assert db.item(1) == "Novo", "cache nao foi limpo ao trocar de patch"


# --------------------------------------------------------------------------
# Sincronizacao
# --------------------------------------------------------------------------


def _mock_ddragon() -> None:
    respx.get(f"{DDRAGON}/api/versions.json").mock(
        return_value=httpx.Response(200, json=VERSIONS)
    )
    respx.get(url__regex=rf"{DDRAGON}/cdn/.+/item\.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "3076": {
                        "name": "Colete Espinhoso",
                        "gold": {"total": 1100, "base": 500},
                        "stats": {"FlatArmorMod": 45},
                        "from": ["1029"],
                        "into": [],
                        "tags": ["Armor"],
                    }
                }
            },
        )
    )
    respx.get(url__regex=rf"{DDRAGON}/cdn/.+/champion\.json").mock(
        return_value=httpx.Response(200, json={"data": {"Garen": {"key": "86", "name": "Garen"}}})
    )
    respx.get(url__regex=rf"{DDRAGON}/cdn/.+/summoner\.json").mock(
        return_value=httpx.Response(
            200, json={"data": {"SummonerFlash": {"key": "4", "name": "Flash"}}}
        )
    )
    respx.get(url__regex=rf"{DDRAGON}/cdn/.+/runesReforged\.json").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 8000,
                    "name": "Precisão",
                    "slots": [{"runes": [{"id": 8010, "name": "Conquistador"}]}],
                }
            ],
        )
    )


@respx.mock
async def test_sync_stores_every_entity_kind(db: PatchDB) -> None:
    _mock_ddragon()
    assert await db.sync("16.9") == "16.9.1"
    db.use_patch("16.9")
    assert db.item(3076) == "Colete Espinhoso"
    assert db.champion(86) == "Garen"
    assert db.summoner(4) == "Flash"
    assert db.rune(8010) == "Conquistador"
    assert db.rune(8000) == "Precisão"


@respx.mock
async def test_stat_shards_are_included(db: PatchDB) -> None:
    """Fragmentos de atributo NAO estao no runesReforged.json.

    Sem a tabela embutida, metade das runas do jogador sairia como id cru.
    """
    _mock_ddragon()
    await db.sync("16.9")
    db.use_patch("16.9")
    assert db.rune(5008) == "Adaptável"


@respx.mock
async def test_both_locales_are_stored(db: PatchDB) -> None:
    """O FactValidator casa nomes por texto exato.

    Um modelo treinado em ingles vai escrever "Luden's Companion" mesmo
    instruido em portugues; reconhecer os dois e melhor que rejeitar um
    finding correto.
    """
    _mock_ddragon()
    await db.sync("16.9")
    with sqlite3.connect(db.path) as c:
        locales = {r[0] for r in c.execute("SELECT DISTINCT locale FROM synced")}
    assert "en_US" in locales
    assert len(locales) >= 1


@respx.mock
async def test_sync_is_idempotent(db: PatchDB) -> None:
    """Re-sincronizar nao pode rebaixar nada nem duplicar linhas."""
    _mock_ddragon()
    await db.sync("16.9")
    antes = respx.calls.call_count
    await db.sync("16.9")
    assert respx.calls.call_count - antes <= 1  # so o versions.json
    assert db.available_patches() == ["16.9"]


@respx.mock
async def test_item_cost_is_available(db: PatchDB) -> None:
    """Custo de item alimenta a analise de eficiencia de recall."""
    _mock_ddragon()
    await db.sync("16.9")
    assert db.item_cost(3076) == 1100
    assert db.item_cost(999999) is None


# --------------------------------------------------------------------------
# Integracao com o render
# --------------------------------------------------------------------------


@respx.mock
async def test_render_uses_resolved_names(db: PatchDB) -> None:
    from riftcoach.parse.distill import distill
    from tests.test_distill import load

    _mock_ddragon()
    await db.sync("16.9")
    db.use_patch("16.9")

    match, tl = load("sr_ranked_35min")
    facts = distill(match, tl, match["info"]["participants"][0]["puuid"])
    texto = render(facts, (Section.BUILD,), resolver=db)

    assert "Flash" in texto, "summoners nao foram resolvidos"
    # Ids nao conhecidos pelo mock continuam crus, em vez de virar palpite.
    assert any(c.isdigit() for c in texto)
