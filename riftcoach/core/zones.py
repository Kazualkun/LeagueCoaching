"""Coordenada bruta -> zona nomeada do mapa.

Esta e a maior melhoria isolada de precisao do pipeline, e nao e principalmente
sobre tokens. Um LLM NAO consegue raciocinar sobre `(7432, 8891)`. Ele raciocina
muito bem sobre `"ENEMY_JUNGLE_TOPSIDE"`. Ver docs/02-data-pipeline.md, Transf. 1.

Geometria do Summoner's Rift, em coordenadas do match-v5:

    - base azul no canto inferior esquerdo (x e y baixos)
    - base vermelha no canto superior direito (x e y altos)
    - a rota do meio e a diagonal principal          ->  x ~= y
    - o rio e a antidiagonal, perpendicular ao meio  ->  x + y ~= 14800
    - lado do time azul                              ->  x + y <  14800
    - lado de cima (baron) vs lado de baixo (dragao) ->  y > x  vs  x > y

Usamos distancia ate a polilinha de cada rota em vez de uma grade quadrada: as
rotas superior e inferior sao faixas em "L" e uma grade rotula rio/selva errado
o tempo todo.

CALIBRADO contra telemetria real (8 partidas de SR, `tools/calibrate_zones.py`).
O sinal de verdade sao os `ELITE_MONSTER_KILL`: um Baron morre, por definicao,
no pit do Baron. Medianas medidas:

    BARON_NASHOR  (5007, 10471)   n=6     -- Baron, Arauto e Grubs
    RIFTHERALD    (4776, 10000)   n=7        nascem todos no mesmo pit
    HORDE         (4790, 10272)   n=21
    DRAGON/*      (9844,  4420)   n=23
    extensao real  x [130, 14589]  y [135, 14673]

Rode a calibracao de novo se a Riot mexer na geometria do mapa.
"""

from __future__ import annotations

import math
from enum import StrEnum

# Extensao do mundo no SR usada pelas posicoes do match-v5.
MAP_MAX = 14870
# A linha do rio: x + y = RIVER_AXIS
RIVER_AXIS = 14800

BLUE_TEAM = 100
RED_TEAM = 200

Point = tuple[float, float]


class MapZone(StrEnum):
    BLUE_BASE = "BLUE_BASE"
    RED_BASE = "RED_BASE"

    TOP_LANE_BLUE = "TOP_BLUE"
    TOP_LANE_MID = "TOP_MID"
    TOP_LANE_RED = "TOP_RED"
    MID_LANE_BLUE = "MID_BLUE"
    MID_LANE_MID = "MID_MID"
    MID_LANE_RED = "MID_RED"
    BOT_LANE_BLUE = "BOT_BLUE"
    BOT_LANE_MID = "BOT_MID"
    BOT_LANE_RED = "BOT_RED"

    TOP_RIVER = "TOP_RIVER"
    BOT_RIVER = "BOT_RIVER"
    BARON_PIT = "BARON_PIT"
    DRAGON_PIT = "DRAGON_PIT"

    BLUE_TOP_JUNGLE = "BLUE_TOPJG"
    BLUE_BOT_JUNGLE = "BLUE_BOTJG"
    RED_TOP_JUNGLE = "RED_TOPJG"
    RED_BOT_JUNGLE = "RED_BOTJG"

    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------
# Marcos (medidos — ver CALIBRACAO no topo)
# --------------------------------------------------------------------------

BLUE_NEXUS: Point = (1550.0, 1550.0)
RED_NEXUS: Point = (13250.0, 13250.0)
# Centro do pit do Baron: ponderado entre Baron, Arauto e Grubs, que nascem
# todos ali. Nao e a posicao exata do Baron sozinho.
BARON: Point = (4900.0, 10350.0)
DRAGON: Point = (9844.0, 4420.0)

# BASE_RADIUS foi reduzido de 2300 para 1600 apos a calibracao: com 2300 as
# torres de INIBIDOR (a ~1950u do nexus) eram engolidas pela base, e inibidor e
# estrutura de ROTA. As torres do nexus ficam a ~680u, entao 1600 separa as duas
# coisas com folga dos dois lados.
BASE_RADIUS = 1600.0
PIT_RADIUS = 1250.0
LANE_HALF_WIDTH = 1300.0
RIVER_HALF_WIDTH = 1400.0

# Polilinhas das rotas, da base azul ate a base vermelha.
MID_LANE: list[Point] = [(1900.0, 1900.0), (13000.0, 13000.0)]
TOP_LANE: list[Point] = [
    (1300.0, 2200.0),
    (1450.0, 11500.0),
    (2600.0, 13200.0),
    (12400.0, 13550.0),
]
BOT_LANE: list[Point] = [
    (2200.0, 1300.0),
    (11500.0, 1450.0),
    (13200.0, 2600.0),
    (13550.0, 12400.0),
]


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_to_segment(p: Point, a: Point, b: Point) -> tuple[float, float]:
    """Retorna (distancia, t) onde t e a posicao projetada em [0, 1]."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0.0:
        return _dist(p, a), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / denom
    t = max(0.0, min(1.0, t))
    proj = (ax + t * dx, ay + t * dy)
    return _dist(p, proj), t


def _polyline_distance(p: Point, line: list[Point]) -> tuple[float, float]:
    """Menor distancia ate a polilinha e a progressao normalizada ao longo dela.

    A progressao e ponderada por comprimento de segmento, para que 0.5 fique de
    fato no meio da rota e nao no meio da lista de vertices.
    """
    seg_lengths = [_dist(line[i], line[i + 1]) for i in range(len(line) - 1)]
    total = sum(seg_lengths)
    best_d = math.inf
    best_prog = 0.0
    travelled = 0.0
    for i, seg_len in enumerate(seg_lengths):
        d, t = _point_to_segment(p, line[i], line[i + 1])
        if d < best_d:
            best_d = d
            best_prog = (travelled + t * seg_len) / total if total else 0.0
        travelled += seg_len
    return best_d, best_prog


def _lane_third(progress: float, lane: str) -> MapZone:
    """Terco azul / meio / terco vermelho ao longo da rota."""
    if progress < 0.34:
        band = "BLUE"
    elif progress < 0.66:
        band = "MID"
    else:
        band = "RED"
    return MapZone[f"{lane}_LANE_{band}"]


def to_zone(x: float, y: float) -> MapZone:
    """Classifica uma posicao do match-v5 em uma zona nomeada.

    A ordem das checagens importa: os pits vencem o rio, o rio vence a selva, e
    as bases vencem tudo (uma morte na base nao e uma morte na rota do meio, mesmo
    a base ficando em cima da diagonal do meio).
    """
    p: Point = (float(x), float(y))

    if _dist(p, BLUE_NEXUS) < BASE_RADIUS:
        return MapZone.BLUE_BASE
    if _dist(p, RED_NEXUS) < BASE_RADIUS:
        return MapZone.RED_BASE

    if _dist(p, BARON) < PIT_RADIUS:
        return MapZone.BARON_PIT
    if _dist(p, DRAGON) < PIT_RADIUS:
        return MapZone.DRAGON_PIT

    candidates: list[tuple[float, MapZone]] = []
    for name, line in (("MID", MID_LANE), ("TOP", TOP_LANE), ("BOT", BOT_LANE)):
        d, prog = _polyline_distance(p, line)
        if d < LANE_HALF_WIDTH:
            candidates.append((d, _lane_third(prog, name)))
    if candidates:
        return min(candidates)[1]

    # Rio: faixa em torno da antidiagonal, ja descontados os pits e as rotas.
    if abs(p[0] + p[1] - RIVER_AXIS) < RIVER_HALF_WIDTH:
        return MapZone.TOP_RIVER if p[1] > p[0] else MapZone.BOT_RIVER

    blue_side = p[0] + p[1] < RIVER_AXIS
    topside = p[1] > p[0]
    if blue_side:
        return MapZone.BLUE_TOP_JUNGLE if topside else MapZone.BLUE_BOT_JUNGLE
    return MapZone.RED_TOP_JUNGLE if topside else MapZone.RED_BOT_JUNGLE


# --------------------------------------------------------------------------
# Perspectiva
# --------------------------------------------------------------------------

_SIDE_PREFIXES = {
    MapZone.BLUE_BASE: ("BLUE", "BASE"),
    MapZone.RED_BASE: ("RED", "BASE"),
    MapZone.TOP_LANE_BLUE: ("BLUE", "TOP_LANE"),
    MapZone.TOP_LANE_RED: ("RED", "TOP_LANE"),
    MapZone.MID_LANE_BLUE: ("BLUE", "MID_LANE"),
    MapZone.MID_LANE_RED: ("RED", "MID_LANE"),
    MapZone.BOT_LANE_BLUE: ("BLUE", "BOT_LANE"),
    MapZone.BOT_LANE_RED: ("RED", "BOT_LANE"),
    MapZone.BLUE_TOP_JUNGLE: ("BLUE", "JUNGLE_TOPSIDE"),
    MapZone.BLUE_BOT_JUNGLE: ("BLUE", "JUNGLE_BOTSIDE"),
    MapZone.RED_TOP_JUNGLE: ("RED", "JUNGLE_TOPSIDE"),
    MapZone.RED_BOT_JUNGLE: ("RED", "JUNGLE_BOTSIDE"),
}

# O terco central de uma rota nao pertence a ninguem — e a faixa entre as duas
# torres externas. Recebe rotulo proprio em vez de cair no nome cru da zona:
# "TOP_MID" le como se fosse uma sexta rota, enquanto "NEUTRAL_TOP_LANE" diz
# exatamente o que e e combina com o esquema OWN_/ENEMY_.
_NEUTRAL_LANE_THIRDS = {
    MapZone.TOP_LANE_MID: "NEUTRAL_TOP_LANE",
    MapZone.MID_LANE_MID: "NEUTRAL_MID_LANE",
    MapZone.BOT_LANE_MID: "NEUTRAL_BOT_LANE",
}


def side_relative(zone: MapZone, team_id: int) -> str:
    """Traduz uma zona para a perspectiva do jogador: OWN_* / ENEMY_*.

    SEMPRE apresente isto ao modelo em vez de BLUE/RED. Todo erro que um LLM
    comete sobre "voce se sobre-estendeu" se origina de ele perder a nocao de
    qual metade do mapa e perigosa. Pre-resolver elimina o modo de falha.
    """
    if (neutral := _NEUTRAL_LANE_THIRDS.get(zone)) is not None:
        return neutral
    mapped = _SIDE_PREFIXES.get(zone)
    if mapped is None:
        # Rio e pits sao neutros — nao tem dono.
        return zone.value
    side, rest = mapped
    own = "OWN" if (side == "BLUE") == (team_id == BLUE_TEAM) else "ENEMY"
    return f"{own}_{rest}"


def is_enemy_territory(zone: MapZone, team_id: int) -> bool:
    """Atalho para heuristicas de sobre-extensao."""
    return side_relative(zone, team_id).startswith("ENEMY_")
