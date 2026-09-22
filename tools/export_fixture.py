"""Exporta uma partida em cache como fixture de teste, com PUUIDs anonimizados.

Fixtures versionadas deixam a suite de testes offline, deterministica e
independente da chave da API — e sao o unico jeito de travar regressao do
parser contra dados reais.

    uv run python tools/export_fixture.py BR1_3239179616 nome_do_arquivo
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from riftcoach.riot.cache import RiotCache

OUT = Path("tests/fixtures")


def fake_puuid(real: str) -> str:
    """PUUID falso, deterministico e do mesmo tamanho.

    Deterministico para que a fixture nao mude a cada export (senao todo
    snapshot quebraria sem motivo).
    """
    h = hashlib.sha256(real.encode()).hexdigest()
    return (h * 3)[:78]


def scrub(obj: Any, mapping: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        return {k: scrub(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, mapping) for v in obj]
    if isinstance(obj, str):
        if obj in mapping:
            return mapping[obj]
        # Nomes de invocador e Riot IDs tambem sao dados pessoais.
        return obj
    return obj


async def main() -> None:
    match_id, name = sys.argv[1], sys.argv[2]
    OUT.mkdir(parents=True, exist_ok=True)

    async with RiotCache() as cache:
        key = next(k for k in await cache.keys("match:") if match_id in k)
        routing = key.split(":")[1]
        match = await cache.get(key)
        tl = await cache.get(f"timeline:{routing}:{match_id}")
    assert match is not None and tl is not None

    mapping = {
        p["puuid"]: fake_puuid(p["puuid"]) for p in match["info"]["participants"]
    }
    for p in match["info"]["participants"]:
        p["riotIdGameName"] = f"Jogador{p['participantId']}"
        p["riotIdTagline"] = "TEST"
        p["summonerName"] = f"Jogador{p['participantId']}"
        p["summonerId"] = f"anon-{p['participantId']}"
        p["puuid"] = mapping[p["puuid"]]
    match["metadata"]["participants"] = [
        mapping.get(x, x) for x in match["metadata"].get("participants", [])
    ]
    tl["metadata"]["participants"] = [
        mapping.get(x, x) for x in tl["metadata"].get("participants", [])
    ]
    for pt in tl["info"].get("participants", []):
        if "puuid" in pt:
            pt["puuid"] = mapping.get(pt["puuid"], pt["puuid"])

    payload = {"match": scrub(match, mapping), "timeline": scrub(tl, mapping)}
    path = OUT / f"{name}.json.gz"
    raw = json.dumps(payload, separators=(",", ":")).encode()
    with gzip.open(path, "wb", compresslevel=9) as f:
        f.write(raw)

    print(
        f"{path}  {len(raw) / 1024:.0f} KB -> {path.stat().st_size / 1024:.0f} KB "
        f"comprimido\n  puuid do jogador 1: {mapping[next(iter(mapping))][:16]}..."
    )


asyncio.run(main())
