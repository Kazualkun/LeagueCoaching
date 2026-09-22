"""Calibra os limiares de gravidade contra a distribuicao real de erros.

Limiar chutado produz ou "tudo e critico" ou "nada e critico" — os dois
inuteis. Aqui medimos a distribuicao de perda de probabilidade de vitoria em
todas as partidas em cache e escolhemos percentis.

Alvo de projeto: 1-3 criticos por partida por jogador. Mais que isso e a
marcacao perde sentido; menos e o jogador nunca ve um.

    uv run python tools/calibrate_severity.py
"""

from __future__ import annotations

import asyncio
import statistics

from riftcoach.analysis.advantage import SEVERITY_THRESHOLDS, find_blunders
from riftcoach.core.errors import RiftCoachError
from riftcoach.parse.distill import distill
from riftcoach.riot.cache import RiotCache

SUMMONERS_RIFT = 11


async def main() -> None:
    perdas: list[float] = []
    por_jogador: list[int] = []
    n_partidas = 0

    async with RiotCache() as cache:
        for tkey in await cache.keys("timeline:"):
            routing, match_id = tkey.split(":")[1], tkey.split(":")[2]
            match = await cache.get(f"match:{routing}:{match_id}")
            tl = await cache.get(tkey)
            if match is None or tl is None:
                continue
            if match["info"].get("mapId") != SUMMONERS_RIFT:
                continue
            n_partidas += 1
            for p in match["info"]["participants"]:
                try:
                    facts = distill(match, tl, p["puuid"])
                except RiftCoachError:
                    continue
                bl = find_blunders(facts)
                perdas.extend(b.wp_loss for b in bl)
                por_jogador.append(len(bl))

    if not perdas:
        print("Nenhum erro medido em cache.")
        return

    perdas.sort()
    print(f"=== {n_partidas} partidas · {len(por_jogador)} jogadores ===")
    print(f"erros por jogador: mediana={statistics.median(por_jogador):.0f}")
    print(f"perdas medidas: n={len(perdas)}\n")

    print("percentil  perda (pp)")
    for q in (50, 70, 80, 90, 95, 98, 99):
        idx = min(len(perdas) - 1, int(len(perdas) * q / 100))
        print(f"   p{q:<7}{perdas[idx]:>6.1f}")
    print(f"   maximo  {perdas[-1]:>6.1f}")

    print("\n--- com os limiares ATUAIS ---")
    for limiar, sev in SEVERITY_THRESHOLDS:
        n = sum(1 for p in perdas if p >= limiar)
        print(
            f"  sev{sev} (>={limiar:>4.1f}pp): {n:>4} erros "
            f"= {n / len(por_jogador):.1f} por jogador"
        )


asyncio.run(main())
