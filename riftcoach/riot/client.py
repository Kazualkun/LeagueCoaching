"""Cliente da API da Riot.

Proprio, sobre httpx, em vez de um wrapper de terceiros — porque o cache
permanente e a decisao de maior alavancagem do projeto e wrappers atrapalham
camadas de cache (ver docs/05-stack-and-repo.md, 5.4).

A superficie que usamos sao 4 endpoints. Todos em recursos IMUTAVEIS, exceto a
lista de match ids.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

import httpx

from riftcoach.config import PLATFORM_TO_ROUTING, Settings, normalize_platform
from riftcoach.config import settings as default_settings
from riftcoach.core.errors import (
    RiotApiError,
    RiotKeyExpired,
    RiotKeyInvalid,
    RiotNotFound,
    RiotRateLimited,
)
from riftcoach.riot import cache as ck
from riftcoach.riot.cache import RiotCache
from riftcoach.riot.limiter import RiotLimiter

USER_AGENT = "RiftCoach/0.1 (+https://github.com/riftcoach-ai)"

PORTAL = "https://developer.riotgames.com"

# 401 e 403 tem causas DIFERENTES e a dica precisa refletir isso.
# A Riot devolve 401 "Unknown apikey" quando a string nao existe no sistema dela
# — o caso mais comum e o usuario ter copiado uma chave que o dashboard ja
# substituiu. 403 e a chave conhecida, porem sem permissao/expirada.
UNKNOWN_KEY_HINT = (
    "A Riot respondeu 'Unknown apikey': essa chave nao existe no sistema dela.\n"
    f"O dashboard ({PORTAL}) substitui a chave de desenvolvimento sempre que gera\n"
    "uma nova — a anterior morre na hora. Copie a chave que esta la AGORA\n"
    "(botao REGENERATE API KEY) e rode: riftcoach auth"
)

EXPIRED_KEY_HINT = (
    "Chaves de desenvolvimento da Riot expiram a cada 24 horas.\n"
    f"Gere uma nova em {PORTAL} e rode: riftcoach auth\n"
    "Para uso continuo, solicite uma Personal API Key (nao expira)."
)


class RiotClient:
    """Use como context manager assincrono.

    O cache e consultado ANTES do limitador: um acerto de cache nao consome
    orcamento de taxa nem espera.
    """

    def __init__(
        self,
        api_key: str | None = None,
        config: Settings | None = None,
        cache: RiotCache | None = None,
    ) -> None:
        self.settings = config or default_settings
        self.api_key = api_key or self.settings.resolve_api_key()
        if not self.api_key:
            raise RiotKeyInvalid(
                "Nenhuma chave da API da Riot configurada.",
                status=401,
                hint="Rode: riftcoach auth  (ou defina RIOT_API_KEY no ambiente)",
            )
        # Normalizado aqui, uma vez so, para que 'br' (em vez de 'br1') nunca
        # chegue a virar host de URL: rejeitado na entrada, nao 400/403 minutos
        # depois num endpoint por-puuid, longe de onde a plataforma foi digitada.
        self.platform = normalize_platform(self.settings.riot_platform)
        self.routing = PLATFORM_TO_ROUTING.get(self.platform, "americas")
        self._cache = cache
        self._owns_cache = cache is None
        self._limiter = RiotLimiter()
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> RiotClient:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            headers={"X-Riot-Token": self.api_key or "", "User-Agent": USER_AGENT},
            follow_redirects=False,
        )
        if self._cache is None:
            self._cache = RiotCache()
        if self._owns_cache:
            await self._cache.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_cache and self._cache is not None:
            await self._cache.__aexit__(exc_type, exc, tb)

    @property
    def cache(self) -> RiotCache:
        assert self._cache is not None
        return self._cache

    # ------------------------------------------------------------------
    # Transporte
    # ------------------------------------------------------------------

    async def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        assert self._http is not None
        # Uma retentativa por motivo: 429 (portao) e 5xx transitorio.
        for attempt in range(3):
            await self._limiter.acquire()
            resp = await self._http.get(url, params=params)

            if (hdr := resp.headers.get("X-App-Rate-Limit")) is not None:
                self._limiter.adopt(hdr)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "10"))
                self._limiter.penalize(retry_after)
                if attempt == 2:
                    raise RiotRateLimited(f"Limite de taxa persistente em {url}", retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                if attempt == 2:
                    raise RiotApiError(
                        f"A API da Riot devolveu {resp.status_code}",
                        status=resp.status_code,
                        hint="Servico da Riot instavel. Tente de novo em alguns minutos.",
                    )
                self._limiter.penalize(1.5 * (attempt + 1))
                continue

            self._raise_for_status(resp, url)

        raise RiotApiError("Requisicao esgotou as tentativas", status=0)

    @staticmethod
    def riot_message(resp: httpx.Response) -> str:
        """A mensagem que a propria Riot mandou, quando ela manda uma.

        Vale ouro no diagnostico: 'Unknown apikey' e 'Forbidden' apontam para
        problemas completamente diferentes e a UI precisa saber qual e.
        """
        try:
            body = resp.json()
            msg = body.get("status", {}).get("message")
            return str(msg) if msg else ""
        except Exception:
            return ""

    @classmethod
    def _raise_for_status(cls, resp: httpx.Response, url: str) -> None:
        code = resp.status_code
        msg = cls.riot_message(resp)
        if code == 401:
            raise RiotKeyInvalid(
                f"A Riot nao reconheceu a chave (401{f': {msg}' if msg else ''}).",
                status=401,
                hint=UNKNOWN_KEY_HINT,
            )
        if code == 403:
            raise RiotKeyExpired(
                f"A Riot recusou a chave (403{f': {msg}' if msg else ''}) "
                "— expirada ou sem permissao.",
                status=403,
                hint=EXPIRED_KEY_HINT,
            )
        if code == 404:
            raise RiotNotFound(
                f"Nao encontrado: {url}",
                status=404,
                hint="Confira o Riot ID (Nome#TAG) e a regiao configurada.",
            )
        raise RiotApiError(f"HTTP {code} em {url}", status=code)

    def _url(self, path: str) -> str:
        """Host de ROTEAMENTO REGIONAL: americas / europe / asia / sea.

        Usado por ACCOUNT-V1 e MATCH-V5.
        """
        return f"https://{self.routing}.api.riotgames.com{path}"

    def _platform_url(self, path: str) -> str:
        """Host de PLATAFORMA: br1 / na1 / euw1 / kr ...

        Usado por LEAGUE-V4, STATUS-V4, CHAMPION-MASTERY-V4. Trocar um host pelo
        outro devolve 404 de um jeito confuso, entao os dois sao explicitos.
        """
        return f"https://{self.platform}.api.riotgames.com{path}"

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    # "Estavel", nao imutavel: nome/tag podem ser liberados e reclamados por
    # outra conta, e a Riot ja migrou puuids de contas antigas no passado —
    # um puuid que parou de decriptar do lado da Riot e o sintoma. 30 dias
    # limita ha quanto tempo uma entrada envenenada pode ficar respondendo
    # errado antes de se corrigir sozinha, sem abrir mao do cache no dia a dia.
    ACCOUNT_CACHE_MAX_AGE_S = 30 * 24 * 3600.0

    async def account_by_riot_id(self, game_name: str, tag_line: str) -> dict[str, Any]:
        """account-v1. Cacheado com prazo — ver ACCOUNT_CACHE_MAX_AGE_S."""
        key = ck.key_account(self.routing, game_name, tag_line)
        if (hit := await self.cache.get(key, max_age_s=self.ACCOUNT_CACHE_MAX_AGE_S)) is not None:
            return dict(hit)
        data = await self._get(
            self._url(f"/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}")
        )
        await self.cache.put(key, data)
        return dict(data)

    async def match_ids(
        self, puuid: str, count: int = 20, start: int = 0, queue: int | None = None
    ) -> list[str]:
        """match-v5 ids. NAO cacheado: e a unica rota mutavel que usamos."""
        params: dict[str, Any] = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        data = await self._get(
            self._url(f"/lol/match/v5/matches/by-puuid/{puuid}/ids"), params=params
        )
        return list(data)

    async def player_replays(self, puuid: str) -> Any:
        """match-v5 /replays — quais partidas tem replay disponivel.

        Relevante para o Modo B (Replay Sincronizado, docs/03-vod-review.md):
        hoje o usuario descobre que um replay nao esta mais disponivel so depois
        de tentar abrir. Com isto da para marcar na UI quais partidas ainda podem
        ser revisadas com o client antes de ele escolher.

        ATENCAO: o formato da resposta ainda NAO foi verificado contra dados
        reais. Devolvemos o payload cru de proposito — so vamos tipar depois de
        ver o que a Riot realmente manda. Nao cacheado: disponibilidade de replay
        expira com o tempo e com a troca de patch.
        """
        return await self._get(self._url(f"/lol/match/v5/matches/by-puuid/{puuid}/replays"))

    async def match(self, match_id: str) -> dict[str, Any]:
        """match-v5. Imutavel -> cache permanente."""
        key = ck.key_match(self.routing, match_id)
        if (hit := await self.cache.get(key)) is not None:
            return dict(hit)
        data = await self._get(self._url(f"/lol/match/v5/matches/{match_id}"))
        await self.cache.put(key, data)
        return dict(data)

    async def platform_status(self) -> dict[str, Any]:
        """lol-status-v4. Nao exige parametro nenhum — e o teste mais limpo de
        'a chave e valida?', porque nao ha como confundir com puuid errado."""
        return dict(await self._get(self._platform_url("/lol/status/v4/platform-data")))

    async def league_entries(self, puuid: str) -> list[dict[str, Any]]:
        """league-v4 por puuid. Da o elo do jogador, necessario para escolher a
        faixa de benchmarks correta (docs/04-knowledge-base.md, L3).

        NAO cacheado: o elo muda a cada partida.
        """
        data = await self._get(self._platform_url(f"/lol/league/v4/entries/by-puuid/{puuid}"))
        return list(data)

    async def timeline(self, match_id: str) -> dict[str, Any]:
        """match-v5 timeline. Imutavel -> cache permanente. ~2,5 MB -> ~180 KB."""
        key = ck.key_timeline(self.routing, match_id)
        if (hit := await self.cache.get(key)) is not None:
            return dict(hit)
        data = await self._get(self._url(f"/lol/match/v5/matches/{match_id}/timeline"))
        await self.cache.put(key, data)
        return dict(data)
