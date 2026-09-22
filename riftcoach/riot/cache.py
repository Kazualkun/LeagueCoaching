"""Cache permanente em SQLite + zstd para recursos imutaveis da API da Riot.

Esta e a decisao de engenharia de maior alavancagem do projeto
(ver docs/02-data-pipeline.md, 2.6). Dados de partida concluida NUNCA mudam,
entao guardamos para sempre:

  - reanalise fica instantanea
  - desenvolver o parser custa zero chamadas de API
  - o harness de avaliacao roda offline
  - o limite de 20 req/s da chave de desenvolvimento para de importar

Uma timeline de 2,5 MB comprime para ~180 KB. 500 partidas ~ 90 MB.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import zstandard as zstd

from riftcoach.config import data_dir

# Incrementar apenas se o FORMATO ARMAZENADO mudar (nao o parser — o
# MatchFacts destilado tem a propria chave de versao).
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    key        TEXT PRIMARY KEY,
    payload    BLOB NOT NULL,
    raw_bytes  INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    schema_v   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_fetched ON raw(fetched_at);
"""

_COMPRESSOR = zstd.ZstdCompressor(level=10)
_DECOMPRESSOR = zstd.ZstdDecompressor()


@dataclass(frozen=True)
class CacheStats:
    entries: int
    stored_bytes: int
    raw_bytes: int

    @property
    def ratio(self) -> float:
        return self.raw_bytes / self.stored_bytes if self.stored_bytes else 1.0


class RiotCache:
    """Cache assincrono. Use como context manager assincrono."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "cache.sqlite")
        self._db: aiosqlite.Connection | None = None

    async def __aenter__(self) -> RiotCache:
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(_SCHEMA)
        # WAL: leituras concorrentes enquanto o fetch em lote grava.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("RiotCache usado fora do 'async with'")
        return self._db

    async def get(self, key: str) -> Any | None:
        async with self.db.execute(
            "SELECT payload, schema_v FROM raw WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        payload, schema_v = row
        if schema_v != SCHEMA_VERSION:
            return None
        return json.loads(_DECOMPRESSOR.decompress(payload))

    async def put(self, key: str, value: Any) -> None:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        blob = _COMPRESSOR.compress(raw)
        await self.db.execute(
            "INSERT OR REPLACE INTO raw (key, payload, raw_bytes, fetched_at, schema_v)"
            " VALUES (?, ?, ?, ?, ?)",
            (key, blob, len(raw), int(time.time()), SCHEMA_VERSION),
        )
        await self.db.commit()

    async def has(self, key: str) -> bool:
        async with self.db.execute("SELECT 1 FROM raw WHERE key = ?", (key,)) as cur:
            return await cur.fetchone() is not None

    async def stats(self) -> CacheStats:
        async with self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(payload)),0), COALESCE(SUM(raw_bytes),0) FROM raw"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        return CacheStats(entries=row[0], stored_bytes=row[1], raw_bytes=row[2])

    async def keys(self, prefix: str = "") -> list[str]:
        async with self.db.execute(
            "SELECT key FROM raw WHERE key LIKE ? ORDER BY key", (f"{prefix}%",)
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


# --------------------------------------------------------------------------
# Chaves de cache
# --------------------------------------------------------------------------
# Formato: <recurso>:<roteamento>:<id>
# O roteamento entra na chave porque o mesmo match id pode existir em shards
# diferentes ao longo do tempo, e porque facilita inspecao manual.


def key_account(routing: str, game_name: str, tag_line: str) -> str:
    return f"account:{routing}:{game_name.lower()}#{tag_line.lower()}"


def key_match(routing: str, match_id: str) -> str:
    return f"match:{routing}:{match_id}"


def key_timeline(routing: str, match_id: str) -> str:
    return f"timeline:{routing}:{match_id}"
