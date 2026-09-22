"""Testes da destilacao contra partidas REAIS (anonimizadas).

Fixtures em tests/fixtures/*.json.gz, exportadas por tools/export_fixture.py.
Offline, deterministicos, sem chave de API.
"""

from __future__ import annotations

import gzip
import json
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from riftcoach.core.errors import ParseError, PlayerNotInMatch
from riftcoach.parse.distill import distill, mmss, normalize_patch
from riftcoach.parse.facts import MatchFacts

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rb") as f:
        data = json.load(f)
    return data["match"], data["timeline"]


@pytest.fixture(scope="module")
def ranked() -> tuple[dict[str, Any], dict[str, Any]]:
    return load("sr_ranked_35min")


@pytest.fixture(scope="module")
def remake() -> tuple[dict[str, Any], dict[str, Any]]:
    return load("sr_remake")


@pytest.fixture(scope="module")
def flex() -> tuple[dict[str, Any], dict[str, Any]]:
    return load("sr_flex_41min")


def every_player(match: dict[str, Any], tl: dict[str, Any]) -> list[MatchFacts]:
    return [distill(match, tl, p["puuid"]) for p in match["info"]["participants"]]


# --------------------------------------------------------------------------
# Utilitarios
# --------------------------------------------------------------------------


def test_normalize_patch_handles_four_components() -> None:
    """gameVersion real tem QUATRO componentes. O DataDragon so conhece
    major.minor — passar a string crua nao acha nada."""
    assert normalize_patch("16.9.772.8292") == "16.9"
    assert normalize_patch("15.18.1") == "15.18"
    assert normalize_patch("16.9") == "16.9"
    assert normalize_patch("esquisito") == "esquisito"


def test_mmss() -> None:
    assert mmss(862_000) == "14:22"
    assert mmss(0) == "0:00"
    assert mmss(-500) == "0:00"  # nunca negativo


# --------------------------------------------------------------------------
# Robustez: TODO jogador de TODA fixture precisa destilar
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min", "sr_remake"])
def test_every_participant_distills(fx: str) -> None:
    """160 destilacoes reais rodaram sem falha; estas travam a regressao.

    Um parser que funciona para o jogador em foco mas quebra para o support
    inimigo e um parser quebrado — o mesmo codigo roda para os 10.
    """
    match, tl = load(fx)
    facts = every_player(match, tl)
    assert len(facts) == 10
    assert {f.focus.participant_id for f in facts} == set(range(1, 11))


def test_unknown_player_is_rejected(ranked: tuple[dict[str, Any], ...]) -> None:
    with pytest.raises(PlayerNotInMatch):
        distill(ranked[0], ranked[1], "puuid-que-nao-jogou")


def test_non_summoners_rift_is_rejected_at_the_door(
    ranked: tuple[dict[str, Any], Any],
) -> None:
    """Arena (mapId 30) traz os challenges zerados e nao tem objetivos de SR.

    Analisar produziria cs@10=0 e killParticipation=0 como se fossem medicoes,
    em vez de campos inaplicaveis. Rejeitar na entrada e a unica saida honesta.
    """
    match = json.loads(json.dumps(ranked[0]))
    match["info"]["mapId"] = 30
    with pytest.raises(ParseError, match="Summoner's Rift"):
        distill(match, ranked[1], match["info"]["participants"][0]["puuid"])


# --------------------------------------------------------------------------
# Correcao dos numeros
# --------------------------------------------------------------------------


def test_headline_metrics_are_sane(ranked: tuple[dict[str, Any], Any]) -> None:
    for f in every_player(*ranked):
        assert 0 <= f.cs_at_10 <= 130, f.focus.champion
        assert f.cs_at_10 <= f.cs_at_14, "CS nao pode diminuir"
        assert 0 <= f.damage_share <= 1
        assert 0 <= f.kill_participation <= 1
        assert 0 <= f.time_dead_pct <= 100
        assert f.dpm >= 0
        assert f.patch == "16.9"
        assert f.map_id == 11


def test_deaths_match_the_scoreboard(ranked: tuple[dict[str, Any], Any]) -> None:
    """A contagem de mortes derivada da timeline precisa bater com o placar do
    match-v5. Se divergir, o mapeamento participantId->jogador esta errado."""
    for f in every_player(*ranked):
        assert len(f.deaths) == f.focus.deaths, f.focus.champion


def test_kill_participation_events_match_scoreboard(
    ranked: tuple[dict[str, Any], Any],
) -> None:
    for f in every_player(*ranked):
        assert len(f.kills) == f.focus.kills + f.focus.assists, f.focus.champion


def test_deaths_are_ordered_and_numbered(ranked: tuple[dict[str, Any], Any]) -> None:
    for f in every_player(*ranked):
        times = [d.t_ms for d in f.deaths]
        assert times == sorted(times)
        assert [d.n for d in f.deaths] == list(range(1, len(f.deaths) + 1))


def test_death_zones_are_side_relative(ranked: tuple[dict[str, Any], Any]) -> None:
    """O modelo nunca deve precisar lembrar de que lado o jogador esta.

    Todo rotulo de zona precisa cair em uma das tres categorias, sem excecao:
      OWN_*/ENEMY_*  territorio com dono
      NEUTRAL_*      o terco central de uma rota (entre as torres externas)
      RIVER / PIT    neutro por natureza

    Um rotulo cru como "TOP_MID" vazando ate aqui seria um bug: le como se
    fosse uma sexta rota e obriga o modelo a adivinhar de quem e o lado.
    """
    for f in every_player(*ranked):
        for d in f.deaths:
            assert (
                d.zone.startswith(("OWN_", "ENEMY_", "NEUTRAL_"))
                or "RIVER" in d.zone
                or "PIT" in d.zone
            ), d.zone


def test_no_raw_zone_labels_leak(ranked: tuple[dict[str, Any], Any]) -> None:
    """Nenhum rotulo cru de terco de rota pode escapar para a evidencia."""
    crus = {"TOP_MID", "MID_MID", "BOT_MID", "TOP_BLUE", "MID_RED"}
    for f in every_player(*ranked):
        rotulos = (
            [d.zone for d in f.deaths]
            + [k.zone for k in f.kills]
            + [s.zone for s in f.lane_series]
            + [o.focus_player_zone for o in f.objectives]
        )
        assert not (set(rotulos) & crus), set(rotulos) & crus


def test_lane_series_thins_after_minute_18(ranked: tuple[dict[str, Any], Any]) -> None:
    """Decisoes de rota sao de escala de minuto; late game e de escala de
    evento e ja esta coberto por deaths/objectives."""
    f = every_player(*ranked)[0]
    minutes = [s.minute for s in f.lane_series]
    assert minutes[:19] == list(range(19))
    assert all(m % 3 == 0 for m in minutes if m > 18)


def test_gold_diff_series_is_zero_sum(ranked: tuple[dict[str, Any], Any]) -> None:
    """A serie de um time e o negativo da do outro (a menos do arredondamento
    de 50). Se nao for, o particionamento por time esta errado."""
    blue = every_player(*ranked)[0]
    red = next(f for f in every_player(*ranked) if f.focus.team_id != blue.focus.team_id)
    for a, b in zip(blue.team_gold_diff_series, red.team_gold_diff_series, strict=True):
        assert abs(a + b) <= 100


def test_build_path_is_chronological(ranked: tuple[dict[str, Any], Any]) -> None:
    for f in every_player(*ranked):
        assert all(i > 0 for _, i in f.build_path), "itemId 0 nao e item"


def test_skill_order_uses_qwer(ranked: tuple[dict[str, Any], Any]) -> None:
    for f in every_player(*ranked):
        if f.skill_order:
            assert set(f.skill_order.split(">")) <= {"Q", "W", "E", "R"}


# --------------------------------------------------------------------------
# Recalls: sinal inferido (T2)
# --------------------------------------------------------------------------


def test_recall_count_is_plausible(
    ranked: tuple[dict[str, Any], Any], flex: tuple[dict[str, Any], Any]
) -> None:
    """Regressao da subcontagem grave.

    A deteccao so por posicao achava 1-4 recalls numa partida de 35 minutos,
    porque frames sao de 60s e um recall de ida e volta dentro de um minuto era
    invisivel. Agrupamento de compras PROVA presenca na loja com precisao de
    milissegundo. Uma partida longa tem pelo menos ~6 recalls por jogador.
    """
    for fixture in (ranked, flex):
        for f in every_player(*fixture):
            minutos = f.duration_s / 60
            assert len(f.recalls) >= 6, f"{f.focus.champion}: {len(f.recalls)}"
            # Teto de sanidade: mais de 1 recall por minuto e ruido, nao recall.
            assert len(f.recalls) <= minutos, f"{f.focus.champion}: {len(f.recalls)}"


def test_recalls_are_ordered_and_deduped(ranked: tuple[dict[str, Any], Any]) -> None:
    for f in every_player(*ranked):
        times = [r.t_ms for r in f.recalls]
        assert times == sorted(times)
        assert all(b - a >= 45_000 for a, b in pairwise(times))


def test_starting_purchase_is_not_a_recall(ranked: tuple[dict[str, Any], Any]) -> None:
    """A compra inicial acontece em t=0, na base, antes de sair.

    Conta-la como recall poluiria toda analise de economia da fase de rota.
    """
    for f in every_player(*ranked):
        assert all(r.t_ms > 0 for r in f.recalls)


# --------------------------------------------------------------------------
# Caso de borda: remake
# --------------------------------------------------------------------------


def test_remake_degrades_without_crashing(remake: tuple[dict[str, Any], Any]) -> None:
    """Partida de 1:10. Todo agregado precisa existir e ser vazio/zero, nunca
    um crash nem um numero inventado."""
    for f in every_player(*remake):
        assert f.duration_s < 120
        assert f.deaths == []
        assert f.recalls == []
        assert f.objectives == []
        assert f.cs_at_10 == 0
        # Sem frame do minuto 10, a diferenca e DESCONHECIDA — nao zero.
        assert f.gd_at_10 is None
        assert f.gd_at_14 is None


def test_unknown_is_none_not_zero(remake: tuple[dict[str, Any], Any]) -> None:
    """Distincao central de honestidade: 0 e uma medicao ('empatados'),
    None e a ausencia dela ('a partida nao chegou la'). Colapsar os dois faria
    o coach afirmar um empate que nunca foi medido."""
    f = every_player(*remake)[0]
    assert f.gd_at_10 is None and f.xpd_at_10 is None


# --------------------------------------------------------------------------
# Reducao de tokens
# --------------------------------------------------------------------------


def test_distillation_actually_reduces(ranked: tuple[dict[str, Any], Any]) -> None:
    match, tl = ranked
    raw = len(json.dumps(match).encode()) + len(json.dumps(tl).encode())
    f = distill(match, tl, match["info"]["participants"][0]["puuid"])
    out = len(f.model_dump_json().encode())
    assert raw / out > 40, f"reducao apenas {raw / out:.0f}x"
