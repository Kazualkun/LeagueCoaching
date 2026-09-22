"""Mostra a curva de vantagem e os erros medidos de uma partida.

    uv run python tools/try_advantage.py [match_id] [indice_do_jogador]
"""

from __future__ import annotations

import asyncio
import sys

from riftcoach.analysis.advantage import advantage_series, evaluate, find_blunders
from riftcoach.parse.distill import distill
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

    facts = distill(match, tl, match["info"]["participants"][who]["puuid"])
    serie = advantage_series(facts)

    print(
        f"=== {facts.match_id} · {facts.focus.champion} {facts.focus.position} · "
        f"{'VITORIA' if facts.focus.win else 'DERROTA'} ===\n"
    )

    print("min   ouro-eq    wp   grafico")
    for e in serie:
        barra = round(e.win_probability * 40)
        linha = "-" * barra + "|" + "-" * (40 - barra)
        print(
            f"{e.t_ms // 60000:>3}  {e.advantage:>+8.0f}  {100 * e.win_probability:>3.0f}%  {linha}"
        )

    meio = evaluate(facts, min(20, len(serie) - 1) * 60_000)
    print(f"\ndecomposicao aos {meio.t_ms // 60000}min:")
    for t in meio.terms:
        if abs(t.contribution) >= 1:
            print(f"   {t.render()}")

    blunders = find_blunders(facts)
    criticos = [b for b in blunders if b.is_critical]
    print(f"\n=== ERROS MEDIDOS ({len(blunders)}, {len(criticos)} criticos) ===")
    for b in blunders[:12]:
        marca = "CRITICO" if b.is_critical else f"sev{b.severity}"
        print(
            f"{b.t_ms // 60000:>3}:{(b.t_ms // 1000) % 60:02d} [{marca:>7}] "
            f"-{b.wp_loss:>4.1f}pp  {b.involvement:<10} {b.kind}\n"
            f"            {b.detail}"
        )


asyncio.run(main())
