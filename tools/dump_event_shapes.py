"""Mostra um exemplo real de cada tipo de evento e de participantFrame.

Nomes de campo da Riot sao inconsistentes de proposito (`killerId` mas
`creatorId`, `participantId` mas `victimId`) e nenhuma documentacao lista todos.
Escrever o parser contra nomes lembrados de cabeca e como se descobre isso do
jeito caro. Este dump e a fonte da verdade para `distill.py`.

    uv run python tools/dump_event_shapes.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from riftcoach.riot.cache import RiotCache

SUMMONERS_RIFT = 11


async def main() -> None:
    exemplos: dict[str, dict[str, Any]] = {}
    pf_exemplo: dict[str, Any] | None = None
    n = 0

    async with RiotCache() as cache:
        for tkey in await cache.keys("timeline:"):
            routing, match_id = tkey.split(":")[1], tkey.split(":")[2]
            match = await cache.get(f"match:{routing}:{match_id}")
            if match is None or match["info"].get("mapId") != SUMMONERS_RIFT:
                continue
            tl = await cache.get(tkey)
            if tl is None:
                continue
            n += 1
            for f in tl["info"]["frames"]:
                if pf_exemplo is None and f["participantFrames"]:
                    pf_exemplo = next(iter(f["participantFrames"].values()))
                for e in f["events"]:
                    t = e["type"]
                    # Guarda o exemplo com MAIS chaves: campos opcionais
                    # (monsterSubType, towerType, shutdownBounty) so aparecem
                    # em algumas ocorrencias.
                    if t not in exemplos or len(e) > len(exemplos[t]):
                        exemplos[t] = e

    print(f"=== {n} partidas de SR ===\n")

    if pf_exemplo:
        print("--- participantFrame ---")
        podado = {
            k: (f"<{len(v)} campos>" if isinstance(v, dict) and len(v) > 6 else v)
            for k, v in pf_exemplo.items()
        }
        print(json.dumps(podado, indent=2, ensure_ascii=False))

    print("\n--- eventos ---")
    for t in sorted(exemplos):
        e = dict(exemplos[t])
        for arr in (
            "victimDamageReceived",
            "victimDamageDealt",
            "victimTeamfightDamageReceived",
            "victimTeamfightDamageDealt",
        ):
            if arr in e:
                e[arr] = f"<{len(e[arr])} entradas — DESCARTADO>"
        print(f"\n{t}")
        print(json.dumps(e, indent=2, ensure_ascii=False))


asyncio.run(main())
