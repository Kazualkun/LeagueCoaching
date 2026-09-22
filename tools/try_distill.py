"""Roda a destilacao em toda partida de SR em cache e mede a reducao real.

    uv run python tools/try_distill.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from riftcoach.core.errors import RiftCoachError
from riftcoach.parse.distill import distill
from riftcoach.parse.render import render
from riftcoach.riot.cache import RiotCache

SUMMONERS_RIFT = 11


def size_of(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), default=str).encode())


async def main() -> None:
    total_raw = 0
    total_out = 0
    total_txt = 0
    ok = 0
    async with RiotCache() as cache:
        for tkey in await cache.keys("timeline:"):
            routing, match_id = tkey.split(":")[1], tkey.split(":")[2]
            match = await cache.get(f"match:{routing}:{match_id}")
            tl = await cache.get(tkey)
            if match is None or tl is None:
                continue
            if match["info"].get("mapId") != SUMMONERS_RIFT:
                continue

            for p in match["info"]["participants"]:
                try:
                    facts = distill(match, tl, p["puuid"])
                except RiftCoachError as e:
                    print(f"  {match_id} {p.get('championName'):<14} FALHOU: {e.message}")
                    continue

                raw = size_of(match) + size_of(tl)
                out = size_of(facts.model_dump())
                total_raw += raw
                txt = len(render(facts).encode())
                total_out += out
                total_txt += txt
                ok += 1

                if p["puuid"] == match["info"]["participants"][0]["puuid"]:
                    print(
                        f"{match_id} {facts.focus.champion:<14}"
                        f"{facts.focus.position:<9}"
                        f"cs@10={facts.cs_at_10:<4}"
                        f"gd@10={facts.gd_at_10!s:<7}"
                        f"mortes={len(facts.deaths):<3}"
                        f"recalls={len(facts.recalls):<3}"
                        f"obj={len(facts.objectives):<3}"
                        f"opp={facts.opponent_source:<14}"
                        f"{raw // 1024}KB -> {out // 1024}KB"
                    )

    if ok:
        print(
            f"\n{ok} destilacoes ok\n"
            f"bruto  : {total_raw / ok / 1024:>8.0f} KB/partida "
            f"(~{total_raw / ok / 4:>9,.0f} tokens)\n"
            f"saida  : {total_out / ok / 1024:>8.0f} KB/partida "
            f"(~{total_out / ok / 4:>9,.0f} tokens)\n"
            f"texto  : {total_txt / ok / 1024:>8.1f} KB/partida "
            f"(~{total_txt / ok / 4:>9,.0f} tokens)\n"
            f"reducao: {total_raw / total_txt:>8.0f}x  (bruto -> texto)"
        )


asyncio.run(main())
