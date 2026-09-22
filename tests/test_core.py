from __future__ import annotations

import pytest
from pydantic import ValidationError

from riftcoach.core.schema import (
    CoachingReport,
    Evidence,
    EvidenceTier,
    Finding,
)
from riftcoach.core.zones import (
    BARON,
    BLUE_NEXUS,
    BLUE_TEAM,
    DRAGON,
    RED_NEXUS,
    RED_TEAM,
    MapZone,
    is_enemy_territory,
    side_relative,
    to_zone,
)

# --------------------------------------------------------------------------
# Zonas
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        (BLUE_NEXUS, MapZone.BLUE_BASE),
        (RED_NEXUS, MapZone.RED_BASE),
        (BARON, MapZone.BARON_PIT),
        (DRAGON, MapZone.DRAGON_PIT),
        ((7400, 7400), MapZone.MID_LANE_MID),
        ((3500, 3500), MapZone.MID_LANE_BLUE),
        ((11500, 11500), MapZone.MID_LANE_RED),
    ],
)
def test_landmarks_classify(point: tuple[float, float], expected: MapZone) -> None:
    assert to_zone(*point) == expected


def test_lanes_are_bands_not_grid_cells() -> None:
    """A rota superior e um 'L': sobe pela esquerda e vira para a direita.

    Uma grade quadrada erra os dois bracos. Esses dois pontos ficam em celulas
    de grade completamente diferentes e ainda assim sao a mesma rota.
    """
    assert to_zone(1400, 6000).name.startswith("TOP_LANE")  # braco vertical
    assert to_zone(7000, 13400).name.startswith("TOP_LANE")  # braco horizontal
    assert to_zone(6000, 1400).name.startswith("BOT_LANE")
    assert to_zone(13400, 7000).name.startswith("BOT_LANE")


def test_river_splits_top_and_bot() -> None:
    # Na antidiagonal (x + y ~ 14800), longe dos pits e das rotas.
    assert to_zone(6300, 8500) == MapZone.TOP_RIVER
    assert to_zone(8500, 6300) == MapZone.BOT_RIVER


def test_jungle_quadrants() -> None:
    assert to_zone(3300, 7600) == MapZone.BLUE_TOP_JUNGLE
    assert to_zone(7600, 3300) == MapZone.BLUE_BOT_JUNGLE
    assert to_zone(7300, 11200) == MapZone.RED_TOP_JUNGLE
    assert to_zone(11200, 7300) == MapZone.RED_BOT_JUNGLE


def test_base_beats_mid_lane() -> None:
    """As bases ficam em cima da diagonal do meio; morrer na base nao e
    morrer na rota do meio."""
    assert to_zone(1600, 1600) == MapZone.BLUE_BASE
    assert to_zone(13200, 13200) == MapZone.RED_BASE


def test_zone_is_total_over_the_map() -> None:
    """Nenhuma posicao valida pode cair em UNKNOWN — isso viraria evidencia
    silenciosamente vazia no prompt."""
    for x in range(200, 14800, 350):
        for y in range(200, 14800, 350):
            assert to_zone(x, y) is not MapZone.UNKNOWN


def test_side_relative_flips_with_team() -> None:
    z = MapZone.RED_TOP_JUNGLE
    assert side_relative(z, BLUE_TEAM) == "ENEMY_JUNGLE_TOPSIDE"
    assert side_relative(z, RED_TEAM) == "OWN_JUNGLE_TOPSIDE"


def test_side_relative_leaves_neutral_zones_alone() -> None:
    for z in (MapZone.BARON_PIT, MapZone.DRAGON_PIT, MapZone.TOP_RIVER):
        assert side_relative(z, BLUE_TEAM) == z.value
        assert side_relative(z, RED_TEAM) == z.value


def test_is_enemy_territory() -> None:
    assert is_enemy_territory(MapZone.RED_BOT_JUNGLE, BLUE_TEAM)
    assert not is_enemy_territory(MapZone.BLUE_BOT_JUNGLE, BLUE_TEAM)
    # Zona neutra nao e territorio inimigo.
    assert not is_enemy_territory(MapZone.DRAGON_PIT, BLUE_TEAM)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def _ev(tier: EvidenceTier = EvidenceTier.T1_MEASURED, **kw: object) -> Evidence:
    base: dict[str, object] = {
        "tier": tier,
        "timestamp_ms": 862_000,
        "statement": "Morreu em ENEMY_JUNGLE_TOPSIDE",
        "source": "timeline",
    }
    base.update(kw)
    return Evidence(**base)  # type: ignore[arg-type]


def test_derived_evidence_requires_an_assumption() -> None:
    """Esta e a invariante central: T2/T3 sem premissa declarada e justamente o
    modo de falha que o sistema de niveis existe para impedir."""
    with pytest.raises(ValidationError, match="assumption"):
        _ev(EvidenceTier.T2_DERIVED)
    with pytest.raises(ValidationError, match="assumption"):
        _ev(EvidenceTier.T3_INFERRED)
    # Com premissa, passa.
    assert _ev(EvidenceTier.T2_DERIVED, assumption="ritmo de CS + posicao")


def test_measured_evidence_needs_no_assumption() -> None:
    assert _ev(EvidenceTier.T1_MEASURED).assumption is None


def _finding(**kw: object) -> Finding:
    base: dict[str, object] = {
        "category": "positioning",
        "phase": "mid",
        "severity": 5,
        "timestamp_ms": 862_000,
        "claim": "Morreu na selva inimiga com o dragao nascendo",
        "evidence": [_ev()],
        "fix": "Atravesse o mapa no recall antes do objetivo",
        "confidence": 0.8,
    }
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


def test_finding_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        _finding(evidence=[])


def test_seek_target_is_before_the_anchor() -> None:
    """O erro e a decisao, nao o desfecho."""
    assert _finding(timestamp_ms=862_000).seek_ms == 854_000
    # Nao pode ficar negativo em mortes muito cedo.
    assert _finding(timestamp_ms=3_000).seek_ms == 0


def test_best_tier_picks_the_strongest_evidence() -> None:
    f = _finding(
        evidence=[
            _ev(EvidenceTier.T3_INFERRED, assumption="minimapa"),
            _ev(EvidenceTier.T1_MEASURED),
        ]
    )
    assert f.best_tier is EvidenceTier.T1_MEASURED


def test_top_three_must_index_real_findings() -> None:
    with pytest.raises(ValidationError, match="inexistentes"):
        CoachingReport(
            match_id="BR1_1",
            patch="15.18.1",
            puuid="p",
            parser_version=1,
            findings=[_finding()],
            top_three=[0, 7],
        )


def test_top_three_rejects_duplicates() -> None:
    with pytest.raises(ValidationError, match="repetidos"):
        CoachingReport(
            match_id="BR1_1",
            patch="15.18.1",
            puuid="p",
            parser_version=1,
            findings=[_finding(), _finding()],
            top_three=[0, 0],
        )


def test_report_ranking_prefers_severity_then_evidence_quality() -> None:
    weak = _finding(
        severity=5,
        confidence=0.9,
        evidence=[_ev(EvidenceTier.T3_INFERRED, assumption="visao")],
    )
    strong = _finding(severity=5, confidence=0.5, evidence=[_ev()])
    minor = _finding(severity=2)
    report = CoachingReport(
        match_id="BR1_1",
        patch="15.18.1",
        puuid="p",
        parser_version=1,
        findings=[weak, minor, strong],
    )
    order = report.ranked()
    # Mesma gravidade: evidencia medida vence inferida, mesmo com confianca menor.
    assert order[0] is strong
    assert order[1] is weak
    assert order[2] is minor


# --------------------------------------------------------------------------
# Calibracao contra telemetria REAL (tools/calibrate_zones.py, 8 partidas SR)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ponto", "esperado", "origem"),
    [
        ((5007, 10471), MapZone.BARON_PIT, "ELITE_MONSTER_KILL BARON_NASHOR"),
        ((4776, 10000), MapZone.BARON_PIT, "ELITE_MONSTER_KILL RIFTHERALD"),
        ((4790, 10272), MapZone.BARON_PIT, "ELITE_MONSTER_KILL HORDE (grubs)"),
        ((9836, 4397), MapZone.DRAGON_PIT, "ELITE_MONSTER_KILL WATER_DRAGON"),
        ((10111, 4543), MapZone.DRAGON_PIT, "ELITE_MONSTER_KILL AIR_DRAGON"),
        ((10142, 4689), MapZone.DRAGON_PIT, "ELITE_MONSTER_KILL EARTH_DRAGON"),
    ],
)
def test_pits_match_real_monster_kills(
    ponto: tuple[int, int], esperado: MapZone, origem: str
) -> None:
    """Arauto e Grubs nascem no pit do Baron — os tres precisam cair em
    BARON_PIT, senao a evidencia de objetivo sai com o local errado."""
    assert to_zone(*ponto) == esperado, origem


@pytest.mark.parametrize(
    ("ponto", "esperado", "origem"),
    [
        ((2177, 1807), MapZone.BLUE_BASE, "MID_LANE/NEXUS_TURRET azul"),
        ((12611, 12612), MapZone.RED_BASE, "MID_LANE/NEXUS_TURRET vermelha"),
        ((1169, 4287), MapZone.TOP_LANE_BLUE, "TOP_LANE/BASE_TURRET azul"),
        ((4281, 1253), MapZone.BOT_LANE_BLUE, "BOT_LANE/BASE_TURRET azul"),
        ((1512, 6699), MapZone.TOP_LANE_BLUE, "TOP_LANE/INNER_TURRET azul"),
        ((6919, 1483), MapZone.BOT_LANE_BLUE, "BOT_LANE/INNER_TURRET azul"),
        ((9767, 10113), MapZone.MID_LANE_RED, "MID_LANE/INNER_TURRET vermelha"),
        ((7943, 13411), MapZone.TOP_LANE_RED, "TOP_LANE/INNER_TURRET vermelha"),
        ((13327, 8226), MapZone.BOT_LANE_RED, "BOT_LANE/INNER_TURRET vermelha"),
    ],
)
def test_buildings_land_in_their_own_lane(
    ponto: tuple[int, int], esperado: MapZone, origem: str
) -> None:
    assert to_zone(*ponto) == esperado, origem


@pytest.mark.parametrize(
    ("ponto", "origem"),
    [
        ((3468, 1230), "BOT_LANE/INHIBITOR_BUILDING azul"),
        ((1172, 3583), "TOP_LANE/INHIBITOR_BUILDING azul"),
    ],
)
def test_inhibitor_towers_are_lane_not_base(
    ponto: tuple[int, int], origem: str
) -> None:
    """Regressao: com BASE_RADIUS=2300 as torres de inibidor eram engolidas pela
    base. Inibidor e estrutura de ROTA — classificar como base apagaria a
    diferenca entre 'perdeu o inibidor' e 'lutou na base'."""
    z = to_zone(*ponto)
    assert z not in (MapZone.BLUE_BASE, MapZone.RED_BASE), f"{origem} -> {z.value}"
    assert "LANE" in z.name, f"{origem} -> {z.value}"


def test_real_map_extent_is_covered() -> None:
    """Extensao medida em 8 partidas: x [130, 14589], y [135, 14673].
    Os cantos sao dentro da base, nao UNKNOWN."""
    for ponto in ((130, 135), (14589, 14673), (130, 14673), (14589, 135)):
        assert to_zone(*ponto) is not MapZone.UNKNOWN
