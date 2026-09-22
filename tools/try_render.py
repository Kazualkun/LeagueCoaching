"""Renderiza uma partida em cache e mede o custo em tokens por secao e por analista.

    uv run python tools/try_render.py [match_id] [indice_do_jogador]
"""

from __future__ import annotations

import asyncio
import json
import sys

from riftcoach.parse.distill import distill
from riftcoach.parse.render import (
    ANALYST_SECTIONS,
    Section,
    estimate_tokens,
    render,
    render_for_analyst,
)
from riftcoach.riot.cache import RiotCache

SUMMONERS_RIFT = 11


async def main() -> None:
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    who = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    async with RiotCache() as cache:
        keys = await cache.keys("timeline:")
        key = next((k for k in keys if wanted and wanted in k), keys[0])
        routing, match_id = key.split(":")[1], key.split(":")[2]
        match = await cache.get(f"match:{routing}:{match_id}")
        tl = await cache.get(key)
    assert match is not None and tl is not None
    if match["info"].get("mapId") != SUMMONERS_RIFT:
        print(f"{match_id} nao e Summoner's Rift.")
        return

    puuid = match["info"]["participants"][who]["puuid"]
    facts = distill(match, tl, puuid)
    full = render(facts)

    print(full)
    print("=" * 78)

    raw = len(json.dumps(match).encode()) + len(json.dumps(tl).encode())
    ir = len(facts.model_dump_json().encode())
    print(f"{'bruto (match+timeline)':<28}{raw // 1024:>6} KB  ~{raw // 4:>8,} tokens")
    print(f"{'MatchFacts (JSON)':<28}{ir // 1024:>6} KB  ~{ir // 4:>8,} tokens")
    print(
        f"{'renderizado (texto)':<28}{len(full) // 1024:>6} KB  "
        f"~{estimate_tokens(full):>8,} tokens"
    )
    print(f"{'reducao total':<28}{raw / len(full):>6.0f}x")
    print(f"{'ganho do texto sobre JSON':<28}{ir / len(full):>6.1f}x")

    print("\n--- por secao ---")
    for s in Section:
        t = estimate_tokens(render(facts, (s,)))
        print(f"  {s.value:<12}{t:>6} tokens")

    print("\n--- por analista (o que cada passe realmente ve) ---")
    total = 0
    for name in ANALYST_SECTIONS:
        t = estimate_tokens(render_for_analyst(facts, name))
        total += t
        print(f"  {name:<12}{t:>6} tokens")
    print(f"  {'SOMA':<12}{total:>6} tokens (5 passes)")


asyncio.run(main())
