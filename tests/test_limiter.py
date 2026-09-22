from __future__ import annotations

import asyncio
import time

import pytest

from riftcoach.riot.limiter import RiotLimiter, parse_limit_header


def test_parse_limit_header() -> None:
    assert parse_limit_header("20:1,100:120") == [(20, 1), (100, 120)]
    assert parse_limit_header("500:10,30000:600") == [(500, 10), (30000, 600)]
    assert parse_limit_header("") == []
    assert parse_limit_header("lixo") == []
    # Entradas malformadas sao descartadas, as validas sobrevivem.
    assert parse_limit_header("20:1,quebrado,100:120") == [(20, 1), (100, 120)]


async def test_allows_burst_up_to_limit() -> None:
    lim = RiotLimiter([(5, 60)])
    start = time.monotonic()
    for _ in range(5):
        await lim.acquire()
    # As 5 primeiras nao devem esperar nada.
    assert time.monotonic() - start < 0.1


async def test_blocks_when_window_full() -> None:
    lim = RiotLimiter([(2, 1)])
    await lim.acquire()
    await lim.acquire()
    start = time.monotonic()
    await lim.acquire()  # precisa esperar a janela de 1s abrir
    assert time.monotonic() - start >= 0.9


async def test_429_gate_is_shared_by_all_coroutines() -> None:
    """Um 429 precisa parar TODO mundo, nao so quem levou o 429.

    Sem isso, as outras corrotinas continuam mandando requisicao e levam 429
    de novo, o que pode escalar para um bloqueio mais longo.
    """
    lim = RiotLimiter([(100, 1)])
    lim.penalize(0.5)
    start = time.monotonic()
    await asyncio.gather(*(lim.acquire() for _ in range(4)))
    elapsed = time.monotonic() - start
    assert elapsed >= 0.45
    # Esperaram em paralelo, nao em serie (4 x 0,5s seria 2s).
    assert elapsed < 1.0


async def test_adopt_switches_windows() -> None:
    """Ao subir de chave de dev para producao, o limitador se adapta sozinho."""
    lim = RiotLimiter([(20, 1), (100, 120)])
    lim.adopt("500:10,30000:600")
    assert {(w.limit, w.seconds) for w in lim._windows} == {(500, 10), (30000, 600)}


async def test_adopt_preserves_history() -> None:
    """Trocar de janela nao pode zerar a contagem.

    Se zerasse, a rajada seguinte estouraria o limite real do servidor na hora.
    """
    lim = RiotLimiter([(10, 60)])
    for _ in range(6):
        await lim.acquire()
    lim.adopt("20:60")
    assert len(lim._windows[0]._hits) == 6


async def test_adopt_ignores_identical_spec() -> None:
    lim = RiotLimiter([(20, 1)])
    await lim.acquire()
    lim.adopt("20:1")
    assert len(lim._windows[0]._hits) == 1


@pytest.mark.parametrize("bad", ["", "   ", "nao-e-um-header"])
async def test_adopt_ignores_garbage(bad: str) -> None:
    lim = RiotLimiter([(20, 1)])
    lim.adopt(bad)
    assert {(w.limit, w.seconds) for w in lim._windows} == {(20, 1)}
