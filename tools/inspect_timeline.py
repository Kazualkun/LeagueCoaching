"""Inspeciona uma timeline real em cache: formas de campo, custo em bytes, marcos.

Roda ANTES de escrever o parser. Escrever a destilacao contra o schema
documentado e so depois descobrir que a realidade e diferente custa muito mais
caro do que gastar cinco minutos olhando os dados.

    uv run python tools/inspect_timeline.py [match_id]
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from typing import Any

from riftcoach.riot.cache import RiotCache


def size_of(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":")).encode())


def human(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1_048_576 else f"{n / 1_048_576:.2f} MB"


async def main() -> None:
    async with RiotCache() as cache:
        tl_keys = await cache.keys("timeline:")
        if not tl_keys:
            print("Nenhuma timeline em cache. Rode: riftcoach fetch \"Nome#TAG\"")
            return

        wanted = sys.argv[1] if len(sys.argv) > 1 else None
        key = next((k for k in tl_keys if wanted and wanted in k), tl_keys[0])
        match_id = key.split(":")[-1]
        tl = await cache.get(key)
        match = await cache.get(f"match:{key.split(':')[1]}:{match_id}")
        assert tl is not None and match is not None

        raw = size_of(tl)
        print(f"=== {match_id} ===")
        print(f"timeline bruta: {human(raw)}  (~{raw // 4:,} tokens)")
        print(f"match bruto:    {human(size_of(match))}")

        info = tl["info"]
        frames = info["frames"]
        print(f"frames: {len(frames)}  intervalo: {info.get('frameInterval')} ms")

        # ---- eventos: quantidade E custo em bytes -------------------------
        counts: Counter[str] = Counter()
        bytes_by_type: Counter[str] = Counter()
        for f in frames:
            for e in f["events"]:
                t = e["type"]
                counts[t] += 1
                bytes_by_type[t] += size_of(e)

        total_ev_bytes = sum(bytes_by_type.values())
        print(f"\n--- eventos ({sum(counts.values())} no total, "
              f"{human(total_ev_bytes)}) ---")
        print(f"{'tipo':<32}{'qtd':>6}{'bytes':>12}{'%':>7}")
        for t, b in bytes_by_type.most_common():
            print(f"{t:<32}{counts[t]:>6}{human(b):>12}{100 * b / total_ev_bytes:>6.1f}%")

        # ---- participantFrames: onde esta o peso --------------------------
        pf_total = sum(size_of(f["participantFrames"]) for f in frames)
        print(f"\n--- participantFrames: {human(pf_total)} "
              f"({100 * pf_total / raw:.0f}% da timeline) ---")
        sample = frames[len(frames) // 2]["participantFrames"]["1"]
        sub: dict[str, int] = {k: size_of(v) for k, v in sample.items()}
        for k, v in sorted(sub.items(), key=lambda kv: -kv[1]):
            share = v / size_of(sample)
            print(f"  {k:<28}{v:>6} B  {100 * share:>5.1f}% do frame de 1 jogador")

        # ---- CHAMPION_KILL: o vilao suspeito ------------------------------
        kills = [e for f in frames for e in f["events"] if e["type"] == "CHAMPION_KILL"]
        if kills:
            k0 = kills[0]
            dmg_recv = size_of(k0.get("victimDamageReceived", []))
            dmg_dealt = size_of(k0.get("victimDamageDealt", []))
            print(f"\n--- CHAMPION_KILL ({len(kills)} abates) ---")
            print(f"  evento inteiro:          {size_of(k0):>6} B")
            print(f"  victimDamageReceived:    {dmg_recv:>6} B "
                  f"({len(k0.get('victimDamageReceived', []))} entradas)")
            print(f"  victimDamageDealt:       {dmg_dealt:>6} B")
            print(f"  restante (o que importa):{size_of(k0) - dmg_recv - dmg_dealt:>6} B")
            print(f"  chaves: {sorted(k0.keys())}")

        # ---- marcos reais para calibrar zones.py --------------------------
        print("\n--- MARCOS REAIS (calibracao de zones.py) ---")
        monsters: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for f in frames:
            for e in f["events"]:
                if e["type"] == "ELITE_MONSTER_KILL" and "position" in e:
                    name = e.get("monsterType", "?")
                    if sub_t := e.get("monsterSubType"):
                        name = f"{name}/{sub_t}"
                    monsters[name].append((e["position"]["x"], e["position"]["y"]))
        for name, pts in sorted(monsters.items()):
            ax = sum(p[0] for p in pts) // len(pts)
            ay = sum(p[1] for p in pts) // len(pts)
            print(f"  {name:<28} n={len(pts):<3} centro=({ax}, {ay})")

        buildings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for f in frames:
            for e in f["events"]:
                if e["type"] == "BUILDING_KILL" and "position" in e:
                    lane = e.get("laneType", "?")
                    tier = e.get("towerType") or e.get("buildingType", "?")
                    buildings[f"{lane}/{tier}"].append(
                        (e["position"]["x"], e["position"]["y"])
                    )
        for name, pts in sorted(buildings.items()):
            print(f"  {name:<28} n={len(pts):<3} {pts[0]}")

        xs = [
            pf["position"]["x"]
            for f in frames
            for pf in f["participantFrames"].values()
            if "position" in pf
        ]
        ys = [
            pf["position"]["y"]
            for f in frames
            for pf in f["participantFrames"].values()
            if "position" in pf
        ]
        print(f"  extensao do mapa: x [{min(xs)}, {max(xs)}]  y [{min(ys)}, {max(ys)}]")

        # ---- challenges disponiveis em match-v5 ---------------------------
        parts = match["info"]["participants"]
        ch = parts[0].get("challenges", {})
        print("\n--- match-v5 ---")
        print(f"  patch: {match['info'].get('gameVersion')}")
        print(f"  queueId: {match['info'].get('queueId')}")
        print(f"  campos por participante: {len(parts[0])}")
        print(f"  challenges disponiveis: {len(ch)}")
        wanted_ch = [
            "laneMinionsFirst10Minutes", "maxCsAdvantageOnLaneOpponent",
            "earlyLaningPhaseGoldExpAdvantage", "laningPhaseGoldExpAdvantage",
            "killParticipation", "teamDamagePercentage", "visionScorePerMinute",
            "controlWardsPlaced", "turretPlatesTaken", "soloKills",
            "goldPerMinute", "damagePerMinute", "maxLevelLeadLaneOpponent",
        ]
        for name in wanted_ch:
            mark = "ok " if name in ch else "AUSENTE"
            print(f"    {mark} {name}" + (f" = {ch[name]}" if name in ch else ""))


asyncio.run(main())
