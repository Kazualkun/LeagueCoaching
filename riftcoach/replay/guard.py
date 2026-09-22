"""O intertravamento de conformidade. LEIA ISTO ANTES DE MEXER.

Este e o UNICO modulo do projeto autorizado a abrir conexao com
`127.0.0.1:2999`. Nao e uma convencao de organizacao — e o que torna a promessa
do README ("isso da ban? nao") verificavel em vez de declarada.

A regra que ele impoe: o RiftCoach NUNCA fala com o client do League fora do
modo replay. Antes de CADA requisicao, dispara:

    GET https://127.0.0.1:2999/replay/playback

Essa rota existe SOMENTE enquanto um replay esta rodando. Durante uma partida
ao vivo ela devolve 404 enquanto `/liveclientdata/*` continua respondendo — ou
seja, um 404 aqui e um SINAL POSITIVO de que pode haver partida ao vivo em
andamento, e recusamos incondicionalmente.

Tres propriedades tornam isso confiavel, e as tres sao testadas:

  1. FALHA FECHADO. Erro de conexao, 404, timeout, corpo inesperado, JSON
     malformado, campo faltando — todos levantam `LiveGameRefused`. Nao existe
     caminho de codigo em que um resultado ambiguo siga adiante.

  2. REVALIDA ANTES DE CADA REQUISICAO, nao uma vez por sessao. O usuario pode
     sair do replay por alt-tab e cair na selecao de campeoes no meio da
     analise; um job de cinco minutos precisa perceber isso. Cachear a
     verificacao seria exatamente o bug que este modulo existe para impedir.

  3. E GARANTIDO PELA ARQUITETURA, NAO PELA DISCIPLINA. A verificacao esta na
     camada de transporte. Um contribuidor nao consegue burlar acidentalmente
     escrevendo uma funcionalidade nova, porque nao existe outro cliente — e o
     teste `test_no_module_outside_replay_talks_to_the_client` quebra o build
     se alguem criar um.

TLS: pinning contra o `riotgames.pem` publicado pela Riot. NUNCA `verify=False`.
Nao custa nada e impede que um MITM local alimente o app com estado fabricado.

Ver COMPLIANCE.md e docs/03-vod-review.md, 3.4.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from riftcoach.core.errors import LiveGameRefused

HOST = "127.0.0.1"
PORT = 2999
BASE = f"https://{HOST}:{PORT}"

# Curto de proposito. O client local responde em milissegundos quando esta
# em replay; um timeout longo aqui so serviria para atrasar a recusa.
PROBE_TIMEOUT_S = 2.0
REQUEST_TIMEOUT_S = 5.0

CERT_NAME = "riotgames.pem"

_HINT_REPLAY = (
    "Abra o client do League, va no seu historico de partidas, baixe o replay "
    "e de play. O RiftCoach so conversa com o client enquanto um replay esta "
    "rodando — nunca durante uma partida."
)


def cert_path() -> Path:
    """O certificado publicado pela Riot, versionado em certs/."""
    return Path(__file__).resolve().parents[2] / "certs" / CERT_NAME


def _ssl_context() -> ssl.SSLContext:
    """Contexto TLS fixado no certificado da Riot.

    Se o arquivo sumir, levantamos em vez de cair para `verify=False`. Essa
    degradacao seria silenciosa e derrubaria justamente a protecao contra MITM
    local — e um app que promete nao vazar dados nao pode ter esse caminho.
    """
    caminho = cert_path()
    if not caminho.exists():
        raise LiveGameRefused(
            f"certificado {CERT_NAME} nao encontrado em {caminho}",
            hint=(
                "O RiftCoach fixa o certificado publicado pela Riot em vez de "
                "desabilitar a verificacao TLS. Reinstale o pacote ou restaure "
                "o arquivo certs/riotgames.pem."
            ),
        )
    return ssl.create_default_context(cafile=str(caminho))


@dataclass(frozen=True)
class PlaybackState:
    """O que `/replay/playback` devolve. `time` esta em SEGUNDOS, float."""

    time: float
    length: float
    paused: bool
    seeking: bool
    speed: float

    @classmethod
    def parse(cls, dados: Any) -> PlaybackState:
        """Constroi a partir do corpo cru, recusando qualquer coisa estranha.

        Validar a FORMA da resposta, e nao so o status, e parte do
        intertravamento: um 200 com corpo vazio, ou com HTML de um proxy, nao
        prova que existe um replay rodando. So a forma certa prova.
        """
        if not isinstance(dados, dict):
            raise LiveGameRefused(
                "resposta inesperada de /replay/playback (nao e um objeto JSON)",
                hint=_HINT_REPLAY,
            )
        try:
            return cls(
                time=float(dados["time"]),
                length=float(dados.get("length", 0.0)),
                paused=bool(dados.get("paused", False)),
                seeking=bool(dados.get("seeking", False)),
                speed=float(dados.get("speed", 1.0)),
            )
        except (KeyError, TypeError, ValueError) as e:
            raise LiveGameRefused(
                f"corpo de /replay/playback nao tem a forma esperada: {e}",
                hint=_HINT_REPLAY,
            ) from None


class ReplayGuard:
    """Falha fechado. Toda ambiguidade vira recusa.

    Nao tem estado de "ja verifiquei": cada chamada a `assert_replay_mode`
    pergunta de novo, de propósito.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def assert_replay_mode(self) -> PlaybackState:
        """Devolve o estado do replay, ou levanta `LiveGameRefused`.

        Toda ramificacao abaixo termina em recusa, menos uma: 200 com corpo na
        forma certa. Essa assimetria e o ponto do modulo.
        """
        try:
            resp = await self._client.get(
                f"{BASE}/replay/playback", timeout=PROBE_TIMEOUT_S
            )
        except httpx.ConnectError as e:
            # Client fechado, ou rodando sem a API local. Nao da para provar
            # que nao ha partida ao vivo, entao recusamos.
            raise LiveGameRefused(
                "o client do League nao esta respondendo em 127.0.0.1:2999",
                hint=_HINT_REPLAY,
            ) from e
        except httpx.TimeoutException as e:
            # A mensagem precisa nomear o endereco e o modo replay igual as
            # outras ramificacoes. "nao respondeu a tempo", sozinho, nao diz
            # QUAL client nem O QUE fazer — e recusa que o usuario nao sabe
            # resolver e indistinguivel de bug.
            raise LiveGameRefused(
                f"o client do League nao respondeu a tempo em {HOST}:{PORT}"
                " (/replay/playback)",
                hint=(
                    "Resultado ambiguo e tratado como recusa. " + _HINT_REPLAY
                ),
            ) from e
        except httpx.HTTPError as e:
            raise LiveGameRefused(
                f"falha ao falar com o client do League: {e}", hint=_HINT_REPLAY
            ) from e

        if resp.status_code == 404:
            # O caso mais importante: /replay/* some durante partida ao vivo,
            # mas /liveclientdata/* continua respondendo. 404 aqui e indicio
            # POSITIVO de partida em andamento.
            raise LiveGameRefused(
                "o client respondeu 404 em /replay/playback — nao ha replay rodando",
                hint=(
                    "Essa rota some durante uma partida ao vivo. O RiftCoach se "
                    "recusa a continuar: ele nunca roda durante uma partida. "
                    + _HINT_REPLAY
                ),
            )

        if not resp.is_success:
            raise LiveGameRefused(
                f"o client respondeu {resp.status_code} em /replay/playback",
                hint=_HINT_REPLAY,
            )

        try:
            corpo = resp.json()
        except ValueError as e:
            raise LiveGameRefused(
                "o client respondeu algo que nao e JSON em /replay/playback",
                hint=_HINT_REPLAY,
            ) from e

        return PlaybackState.parse(corpo)

    # ------------------------------------------------------------------
    # O transporte. Nao existe outro.
    # ------------------------------------------------------------------

    async def get(self, path: str) -> Any:
        """GET numa rota do client, SEMPRE precedido da verificacao."""
        await self.assert_replay_mode()
        return await self._send("GET", path, None)

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST numa rota do client, SEMPRE precedido da verificacao."""
        await self.assert_replay_mode()
        return await self._send("POST", path, payload)

    async def _send(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        url = f"{BASE}/{path.lstrip('/')}"
        try:
            resp = await self._client.request(
                method, url, json=payload, timeout=REQUEST_TIMEOUT_S
            )
        except httpx.HTTPError as e:
            # Perder a conexao ENTRE a verificacao e a requisicao tambem e
            # ambiguo: o replay pode ter sido fechado nesse meio tempo.
            raise LiveGameRefused(
                f"a conexao com o client caiu durante {method} {path}: {e}",
                hint=_HINT_REPLAY,
            ) from e

        if not resp.is_success:
            raise LiveGameRefused(
                f"o client respondeu {resp.status_code} em {method} {path}",
                hint=_HINT_REPLAY,
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            # Algumas rotas respondem 200 com corpo vazio ou nao-JSON. Isso e
            # aceitavel DEPOIS da verificacao ter passado — ela ja provou que
            # ha um replay rodando.
            return None


async def open_guard() -> tuple[httpx.AsyncClient, ReplayGuard]:
    """Abre o unico cliente autorizado a falar com o client do League.

    Quem chama e dono do `AsyncClient` e precisa fecha-lo. A funcao devolve os
    dois de propósito: esconder o cliente dentro do guard tentaria contribuintes
    a criar outro para "so um GET rapido".
    """
    client = httpx.AsyncClient(verify=_ssl_context(), timeout=REQUEST_TIMEOUT_S)
    return client, ReplayGuard(client)


async def is_replay_running() -> bool:
    """Ha um replay rodando agora?

    Para a UI decidir se mostra o botao de pular, sem provocar excecao. Este e
    o unico lugar do projeto onde a recusa nao propaga — e ela vira `False`,
    que e o valor conservador.
    """
    try:
        client, guard = await open_guard()
    except LiveGameRefused:
        return False
    try:
        await guard.assert_replay_mode()
        return True
    except LiveGameRefused:
        return False
    finally:
        await client.aclose()
