"""Calibra os marcos de `riftcoach/core/zones.py` contra telemetria real.

O sinal de verdade: um `ELITE_MONSTER_KILL` de Baron acontece, por definicao,
no pit do Baron. Agregando varias partidas e tirando a MEDIANA (nao a media),
ficamos robustos a outliers — monstros podem ser puxados alguns metros para
fora do pit antes de morrer.

    uv run python tools/calibrate_zones.py
"""

from __future__ import annotations

import asyncio
import statistics
from collections import defaultdict

from riftcoach.core.zones import BARON, DRAGON, MapZone, to_zone
from riftcoach.riot.cache import RiotCache

SUMMONERS_RIFT = 11
Point = tuple[int, int]


def median_point(pts: list[Point]) -> Point:
    return (
        int(statistics.median(p[0] for p in pts)),
        int(statistics.median(p[1] for p in pts)),
    )


async def main() -> None:
    monsters: dict[str, list[Point]] = defaultdict(list)
    buildings: dict[str, list[Point]] = defaultdict(list)
    xs: list[int] = []
    ys: list[int] = []
    n_matches = 0

    async with RiotCache() as cache:
        for tkey in await cache.keys("timeline:"):
            routing, match_id = tkey.split(":")[1], tkey.split(":")[2]
            match = await cache.get(f"match:{routing}:{match_id}")
            if match is None or match["info"].get("mapId") != SUMMONERS_RIFT:
                continue
            tl = await cache.get(tkey)
            if tl is None:
                continue
            n_matches += 1

            for f in tl["info"]["frames"]:
                for pf in f["participantFrames"].values():
                    if pos := pf.get("position"):
                        xs.append(pos["x"])
                        ys.append(pos["y"])
                for e in f["events"]:
                    pos = e.get("position")
                    if not pos:
                        continue
                    p: Point = (pos["x"], pos["y"])
                    if e["type"] == "ELITE_MONSTER_KILL":
                        name = e.get("monsterType", "?")
                        if sub := e.get("monsterSubType"):
                            name = f"{name}/{sub}"
                        monsters[name].append(p)
                    elif e["type"] == "BUILDING_KILL":
                        lane = e.get("laneType", "?")
                        kind = e.get("towerType") or e.get("buildingType", "?")
                        buildings[f"{lane}/{kind}"].append(p)

    if not n_matches:
        print("Nenhuma partida de Summoner's Rift em cache (mapId 11).")
        return

    print(f"=== calibracao sobre {n_matches} partidas de SR ===")
    print(f"extensao real: x [{min(xs)}, {max(xs)}]  y [{min(ys)}, {max(ys)}]\n")

    print("--- monstros epicos (mediana) ---")
    for name, pts in sorted(monsters.items()):
        c = median_point(pts)
        z = to_zone(*c)
        print(f"  {name:<30} n={len(pts):<3} {c!s:<16} -> {z.value}")

    # Baron, Arauto e Grubs nascem TODOS no pit do Baron.
    pit_baron = [
        p
        for name, pts in monsters.items()
        for p in pts
        if name.startswith(("BARON", "RIFTHERALD", "HORDE"))
    ]
    pit_dragon = [
        p for name, pts in monsters.items() for p in pts if name.startswith("DRAGON")
    ]

    print("\n--- marcos sugeridos para zones.py ---")
    for label, pts, atual in (
        ("BARON", pit_baron, BARON),
        ("DRAGON", pit_dragon, DRAGON),
    ):
        if not pts:
            continue
        c = median_point(pts)
        dx = c[0] - atual[0]
        dy = c[1] - atual[1]
        erro = (dx**2 + dy**2) ** 0.5
        print(
            f"  {label}: {c}  (atual {tuple(int(v) for v in atual)}, "
            f"erro {erro:.0f}u, n={len(pts)})"
        )

    print("\n--- construcoes (mediana, ajuda a conferir as rotas) ---")
    for name, pts in sorted(buildings.items()):
        c = median_point(pts)
        z = to_zone(*c)
        flag = "" if name.split("/")[0].replace("_LANE", "") in z.value else "  <-- ?"
        print(f"  {name:<32} n={len(pts):<3} {c!s:<16} -> {z.value}{flag}")

    print("\n--- sanidade: todas as posicoes caem em alguma zona? ---")
    zone_hist: dict[MapZone, int] = defaultdict(int)
    for x, y in zip(xs, ys, strict=True):
        zone_hist[to_zone(x, y)] += 1
    total = sum(zone_hist.values())
    for z, n in sorted(zone_hist.items(), key=lambda kv: -kv[1]):
        print(f"  {z.value:<16}{n:>7}  {100 * n / total:>5.1f}%")


asyncio.run(main())
