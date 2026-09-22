from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from riftcoach.config import Settings
from riftcoach.core.errors import RiotKeyExpired, RiotKeyInvalid, RiotNotFound
from riftcoach.riot import cache as ck
from riftcoach.riot.cache import RiotCache
from riftcoach.riot.client import RiotClient

ROUTING = "americas"
BASE = f"https://{ROUTING}.api.riotgames.com"


@pytest.fixture
def cfg() -> Settings:
    return Settings(riot_api_key="RGAPI-teste", riot_platform="br1")


@pytest.fixture
async def cache(tmp_path: Path) -> RiotCache:
    async with RiotCache(tmp_path / "t.sqlite") as c:
        yield c


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------


async def test_cache_roundtrip(cache: RiotCache) -> None:
    await cache.put("match:americas:BR1_1", {"info": {"gameDuration": 1800}})
    assert await cache.get("match:americas:BR1_1") == {"info": {"gameDuration": 1800}}
    assert await cache.has("match:americas:BR1_1")
    assert await cache.get("inexistente") is None


async def test_cache_compresses_timeline_payloads(cache: RiotCache) -> None:
    """Timelines sao extremamente compressiveis — e essa e a premissa do
    cache permanente (docs/02-data-pipeline.md, 2.6)."""
    frames = [
        {
            "timestamp": i * 60000,
            "participantFrames": {
                str(p): {
                    "totalGold": 500 + i * 300,
                    "xp": 100 + i * 250,
                    "level": 1 + i // 3,
                    "minionsKilled": i * 7,
                    "jungleMinionsKilled": 0,
                    "position": {"x": 7000 + p * 100, "y": 7000 + p * 100},
                }
                for p in range(1, 11)
            },
            "events": [],
        }
        for i in range(32)
    ]
    payload = {"info": {"frames": frames}}
    await cache.put("timeline:americas:BR1_1", payload)

    st = await cache.stats()
    assert st.entries == 1
    assert await cache.get("timeline:americas:BR1_1") == payload
    # JSON repetitivo deve comprimir pelo menos 5x.
    assert st.ratio > 5.0


async def test_cache_invalidates_on_schema_bump(
    cache: RiotCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    await cache.put("match:americas:BR1_1", {"a": 1})
    monkeypatch.setattr("riftcoach.riot.cache.SCHEMA_VERSION", 99)
    # Entrada de versao antiga e tratada como ausente, nunca devolvida errada.
    assert await cache.get("match:americas:BR1_1") is None


def test_cache_keys_are_namespaced_by_routing() -> None:
    assert ck.key_match("americas", "BR1_1") != ck.key_match("europe", "BR1_1")
    assert ck.key_match("americas", "BR1_1") != ck.key_timeline("americas", "BR1_1")
    # Riot ID e case-insensitive -> a chave precisa normalizar.
    assert ck.key_account("americas", "Faker", "KR1") == ck.key_account("americas", "faker", "kr1")


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


@respx.mock
async def test_match_is_cached_after_first_fetch(cfg: Settings, cache: RiotCache) -> None:
    """A segunda chamada NAO pode tocar a rede. Esse e o ponto do cache."""
    route = respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(
        return_value=httpx.Response(
            200,
            json={"info": {"gameDuration": 1800}},
            headers={"X-App-Rate-Limit": "20:1,100:120"},
        )
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        a = await rc.match("BR1_1")
        b = await rc.match("BR1_1")

    assert a == b
    assert route.call_count == 1


@respx.mock
async def test_match_ids_are_not_cached(cfg: Settings, cache: RiotCache) -> None:
    """A lista de partidas e a unica rota mutavel — precisa ir na rede sempre."""
    route = respx.get(url__startswith=f"{BASE}/lol/match/v5/matches/by-puuid/").mock(
        return_value=httpx.Response(200, json=["BR1_1", "BR1_2"])
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        await rc.match_ids("p" * 78, count=2)
        await rc.match_ids("p" * 78, count=2)
    assert route.call_count == 2


@respx.mock
async def test_401_raises_key_invalid_with_hint(cfg: Settings, cache: RiotCache) -> None:
    respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(return_value=httpx.Response(401, json={}))
    async with RiotClient(config=cfg, cache=cache) as rc:
        with pytest.raises(RiotKeyInvalid) as e:
            await rc.match("BR1_1")
    assert e.value.hint and "developer.riotgames.com" in e.value.hint


@respx.mock
async def test_403_is_reported_as_expired_dev_key(cfg: Settings, cache: RiotCache) -> None:
    """403 nas nossas 4 rotas e quase sempre chave de dev expirada.

    Dar a dica util em vez da generica e a diferenca entre o usuario resolver
    em 30 segundos ou abrir uma issue.
    """
    respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(return_value=httpx.Response(403, json={}))
    async with RiotClient(config=cfg, cache=cache) as rc:
        with pytest.raises(RiotKeyExpired) as e:
            await rc.match("BR1_1")
    assert e.value.hint and "24 horas" in e.value.hint


@respx.mock
async def test_404_raises_not_found(cfg: Settings, cache: RiotCache) -> None:
    respx.get(url__startswith=f"{BASE}/riot/account/v1/").mock(
        return_value=httpx.Response(404, json={})
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        with pytest.raises(RiotNotFound):
            await rc.account_by_riot_id("NaoExiste", "0000")


@respx.mock
async def test_429_then_success(cfg: Settings, cache: RiotCache) -> None:
    """Um 429 nao pode virar erro: espera o Retry-After e tenta de novo."""
    route = respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}, json={}),
            httpx.Response(200, json={"info": {"gameDuration": 600}}),
        ]
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        data = await rc.match("BR1_1")
    assert data["info"]["gameDuration"] == 600
    assert route.call_count == 2


@respx.mock
async def test_5xx_is_retried(cfg: Settings, cache: RiotCache) -> None:
    route = respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(
        side_effect=[
            httpx.Response(503, json={}),
            httpx.Response(200, json={"info": {}}),
        ]
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        await rc.match("BR1_1")
    assert route.call_count == 2


@respx.mock
async def test_rate_limit_header_is_adopted(cfg: Settings, cache: RiotCache) -> None:
    respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(
        return_value=httpx.Response(200, json={}, headers={"X-App-Rate-Limit": "500:10,30000:600"})
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        await rc.match("BR1_1")
        windows = {(w.limit, w.seconds) for w in rc._limiter._windows}
    assert windows == {(500, 10), (30000, 600)}


async def test_missing_key_fails_fast_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O cofre do SO precisa ser neutralizado aqui.

    Sem o monkeypatch este teste passa numa maquina sem chave guardada e falha
    na de quem rodou `riftcoach auth` — um teste que depende do ambiente do
    desenvolvedor nao vale nada.
    """
    monkeypatch.setattr("riftcoach.config.read_key_from_keyring", lambda _: None)
    cfg = Settings(riot_api_key=None)
    # Nao pode nem construir o cliente sem chave, e a mensagem precisa dizer o que fazer.
    with pytest.raises(RiotKeyInvalid) as e:
        RiotClient(config=cfg)
    assert e.value.hint and "riftcoach auth" in e.value.hint


def test_platform_maps_to_routing() -> None:
    assert Settings(riot_api_key="x", riot_platform="br1").routing == "americas"
    assert Settings(riot_api_key="x", riot_platform="euw1").routing == "europe"
    assert Settings(riot_api_key="x", riot_platform="kr").routing == "asia"
    # Plataforma desconhecida nao pode explodir; cai no padrao.
    assert Settings(riot_api_key="x", riot_platform="zz9").routing == "americas"


async def test_timeline_and_match_use_separate_cache_slots(
    cache: RiotCache,
) -> None:
    await cache.put(ck.key_match(ROUTING, "BR1_1"), {"tipo": "match"})
    await cache.put(ck.key_timeline(ROUTING, "BR1_1"), {"tipo": "timeline"})
    m = await cache.get(ck.key_match(ROUTING, "BR1_1"))
    t = await cache.get(ck.key_timeline(ROUTING, "BR1_1"))
    assert m is not None and t is not None
    assert m["tipo"] == "match"
    assert t["tipo"] == "timeline"


async def test_cache_survives_reopen(tmp_path: Path) -> None:
    """Cache permanente precisa mesmo persistir entre execucoes."""
    p = tmp_path / "persist.sqlite"
    async with RiotCache(p) as c:
        await c.put("match:americas:BR1_9", {"ok": True})
    async with RiotCache(p) as c:
        assert await c.get("match:americas:BR1_9") == {"ok": True}
        assert json.dumps(await c.keys("match:")) == '["match:americas:BR1_9"]'


@respx.mock
async def test_riot_message_is_surfaced(cfg: Settings, cache: RiotCache) -> None:
    """A mensagem da propria Riot e o sinal de diagnostico mais valioso.

    'Unknown apikey' (chave inexistente) e 'Forbidden' (expirada/sem permissao)
    exigem acoes diferentes do usuario. Engolir o corpo da resposta transforma
    os dois no mesmo erro inutil.
    """
    respx.get(f"{BASE}/lol/match/v5/matches/BR1_1").mock(
        return_value=httpx.Response(
            401, json={"status": {"message": "Unknown apikey", "status_code": 401}}
        )
    )
    async with RiotClient(config=cfg, cache=cache) as rc:
        with pytest.raises(RiotKeyInvalid) as e:
            await rc.match("BR1_1")
    assert "Unknown apikey" in e.value.message


@respx.mock
async def test_401_and_403_give_different_advice(cfg: Settings, cache: RiotCache) -> None:
    """401 = chave substituida -> pegue a atual no dashboard.
    403 = chave conhecida, porem expirada -> gere uma nova.
    Dar a mesma dica nos dois casos manda o usuario para o caminho errado.
    """
    respx.get(f"{BASE}/lol/match/v5/matches/A").mock(return_value=httpx.Response(401, json={}))
    respx.get(f"{BASE}/lol/match/v5/matches/B").mock(return_value=httpx.Response(403, json={}))
    async with RiotClient(config=cfg, cache=cache) as rc:
        with pytest.raises(RiotKeyInvalid) as a:
            await rc.match("A")
        with pytest.raises(RiotKeyExpired) as b:
            await rc.match("B")
    assert a.value.hint != b.value.hint
    assert "Unknown apikey" in (a.value.hint or "")
    assert "24 horas" in (b.value.hint or "")


def test_platform_and_regional_hosts_are_distinct(cfg: Settings) -> None:
    """Trocar um host pelo outro devolve 404 de um jeito confuso.

    MATCH-V5/ACCOUNT-V1 vao no host regional; LEAGUE-V4/STATUS-V4 no host da
    plataforma. Esse par de asserts documenta a regra no codigo.
    """
    rc = RiotClient(config=cfg)
    assert rc._url("/x") == "https://americas.api.riotgames.com/x"
    assert rc._platform_url("/x") == "https://br1.api.riotgames.com/x"
