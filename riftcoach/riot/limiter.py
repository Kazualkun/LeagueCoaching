"""Limitador de taxa de duas janelas para a API da Riot.

Chaves de desenvolvimento: 20 req/s e 100 req/2min, POR valor de roteamento.
Chaves de producao: 500 req/10s e 30000 req/10min.

Em vez de fixar isso em codigo, lemos o header `X-App-Rate-Limit` da resposta
("20:1,100:120") e nos adaptamos. Assim, quando o usuario sobe de chave de
desenvolvimento para Personal API Key, o limitador se ajusta sozinho.

Um 429 fecha um portao unico: TODAS as corrotinas esperam o mesmo `Retry-After`,
em vez de cada uma tentar de novo e levar 429 outra vez.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class _Window:
    """Janela deslizante de contagem de requisicoes."""

    __slots__ = ("_hits", "limit", "seconds")

    def __init__(self, limit: int, seconds: int) -> None:
        self.limit = limit
        self.seconds = seconds
        self._hits: deque[float] = deque()

    def _evict(self, now: float) -> None:
        cutoff = now - self.seconds
        while self._hits and self._hits[0] <= cutoff:
            self._hits.popleft()

    def wait_time(self, now: float) -> float:
        self._evict(now)
        if len(self._hits) < self.limit:
            return 0.0
        # Espera ate a requisicao mais antiga sair da janela (+ folga).
        return max(0.0, self._hits[0] + self.seconds - now) + 0.01

    def record(self, now: float) -> None:
        self._hits.append(now)


def parse_limit_header(value: str) -> list[tuple[int, int]]:
    """'20:1,100:120' -> [(20, 1), (100, 120)]"""
    out: list[tuple[int, int]] = []
    for part in value.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        limit_s, secs_s = part.split(":", 1)
        try:
            out.append((int(limit_s), int(secs_s)))
        except ValueError:
            continue
    return out


class RiotLimiter:
    """Um limitador por (chave, roteamento)."""

    def __init__(self, windows: list[tuple[int, int]] | None = None) -> None:
        # Padrao conservador de chave de desenvolvimento ate o primeiro header chegar.
        spec = windows or [(20, 1), (100, 120)]
        self._windows = [_Window(limit, secs) for limit, secs in spec]
        self._lock = asyncio.Lock()
        self._gate_until = 0.0  # portao global apos um 429

    def adopt(self, header_value: str) -> None:
        """Reconfigura a partir do header X-App-Rate-Limit, se ele mudou."""
        spec = parse_limit_header(header_value)
        if not spec:
            return
        current = {(w.limit, w.seconds) for w in self._windows}
        if current == set(spec):
            return
        # Mantem o historico de hits ao trocar de janelas: descartar zeraria a
        # contagem e a proxima rajada levaria 429 na hora.
        old_hits = [list(w._hits) for w in self._windows]
        self._windows = [_Window(limit, secs) for limit, secs in spec]
        if old_hits:
            newest = max(old_hits, key=len)
            for w in self._windows:
                w._hits = deque(newest[-w.limit :])

    def penalize(self, retry_after: float) -> None:
        """Fecha o portao global. Todas as corrotinas aguardam o mesmo instante."""
        self._gate_until = max(self._gate_until, time.monotonic() + retry_after)

    async def acquire(self) -> None:
        """Bloqueia ate ser seguro enviar uma requisicao."""
        while True:
            async with self._lock:
                now = time.monotonic()
                gate = max(0.0, self._gate_until - now)
                if gate <= 0:
                    wait = max((w.wait_time(now) for w in self._windows), default=0.0)
                    if wait <= 0:
                        for w in self._windows:
                            w.record(now)
                        return
                else:
                    wait = gate
            # Dorme FORA do lock, senao serializamos todo mundo.
            await asyncio.sleep(wait)
