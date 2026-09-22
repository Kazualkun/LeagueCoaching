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

import hashlib
import socket
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
# Impressoes digitais aceitas alem da do pem embarcado. Preenchido pelo
# usuario via `riftcoach pin-cert`, depois de ele confirmar que o replay e
# dele. Nao vem preenchido: fixar cego seria o mesmo que nao verificar.
PINNED_NAME = "pinned-sha256.txt"

_HINT_REPLAY = (
    "Abra o client do League, va no seu historico de partidas, baixe o replay "
    "e de play. O RiftCoach so conversa com o client enquanto um replay esta "
    "rodando — nunca durante uma partida."
)


def cert_path() -> Path:
    """O certificado publicado pela Riot, versionado em certs/."""
    return Path(__file__).resolve().parents[2] / "certs" / CERT_NAME


def pinned_path() -> Path:
    """Impressoes digitais aceitas, uma por linha."""
    return Path(__file__).resolve().parents[2] / "certs" / PINNED_NAME


def fingerprint_of_pem(caminho: Path) -> str | None:
    try:
        return hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(caminho.read_text(encoding="utf-8"))
        ).hexdigest()
    except Exception:
        return None


def pinned_fingerprints() -> set[str]:
    """Tudo que aceitamos: o pem embarcado mais o que o usuario fixou."""
    out: set[str] = set()
    if (fp := fingerprint_of_pem(cert_path())) is not None:
        out.add(fp)
    arquivo = pinned_path()
    if arquivo.exists():
        for linha in arquivo.read_text(encoding="utf-8").splitlines():
            limpa = linha.split("#")[0].strip().lower().replace(":", "")
            if len(limpa) == 64:
                out.add(limpa)
    return out


def peer_fingerprint(timeout_s: float = PROBE_TIMEOUT_S) -> str:
    """SHA-256 do certificado que o client apresenta AGORA."""
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    contexto.check_hostname = False
    contexto.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((HOST, PORT), timeout=timeout_s) as sock,
        contexto.wrap_socket(sock, server_hostname=HOST) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    if not der:
        raise LiveGameRefused(
            "o client nao apresentou certificado TLS",
            hint=_HINT_REPLAY,
        )
    return hashlib.sha256(der).hexdigest()


def assert_pinned_certificate() -> str:
    """Confere a impressao digital do client contra as aceitas.

    POR QUE IMPRESSAO DIGITAL E NAO CADEIA DE CONFIANCA. A versao anterior
    passava `certs/riotgames.pem` como CA para o OpenSSL. Isso NUNCA funcionou
    em Python moderno, por dois motivos somados:

      - o OpenSSL 3.x recusa esse certificado com "Missing Authority Key
        Identifier", porque ele nao traz as extensoes que a construcao de
        cadeia passou a exigir;
      - o certificado apresentado pelo client local nem e o mesmo do arquivo.

    O resultado era o guard recusando para TODO usuario, com a mensagem
    "o client nao esta respondendo" — enganosa, porque ele estava respondendo.
    O modo replay inteiro ficava inutilizavel e o erro apontava para o lugar
    errado.

    Para um unico endpoint conhecido em localhost, comparar a impressao digital
    e mais forte que validar cadeia: nao depende de CA nenhuma e nao tem como
    ser contornado por um certificado assinado por outra autoridade.
    """
    apresentado = peer_fingerprint()
    aceitos = pinned_fingerprints()
    if apresentado in aceitos:
        return apresentado
    raise LiveGameRefused(
        f"o certificado do client nao confere com nenhum fixado "
        f"(apresentado sha256:{apresentado[:16]}...)",
        hint=(
            "O RiftCoach fixa o certificado em vez de desabilitar a verificacao "
            "TLS. Se este e o seu client do League, rode `riftcoach pin-cert` "
            "para conferir e registrar esta impressao digital. Se voce NAO "
            "iniciou um replay agora, nao fixe: outra coisa esta atendendo na "
            f"porta {PORT}."
        ),
    )


def _ssl_context() -> ssl.SSLContext:
    """Contexto usado DEPOIS de a impressao digital ja ter sido conferida.

    A verificacao de cadeia fica desligada de proposito, e nao por desleixo: o
    certificado e autoassinado e a garantia vem de `assert_pinned_certificate`,
    que e mais forte aqui. Ligar as duas so faria o handshake falhar sempre.

    Limitacao declarada: a conferencia e uma pre-checagem, entao existe uma
    janela entre ela e as requisicoes seguintes. Em `127.0.0.1`, explorar essa
    janela exige execucao de codigo local — e nesse cenario o TLS ja nao e a
    defesa que importa.
    """
    contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    contexto.check_hostname = False
    contexto.verify_mode = ssl.CERT_NONE
    return contexto


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

    A impressao digital do certificado e conferida ANTES de qualquer
    requisicao. Quem chama e dono do `AsyncClient` e precisa fecha-lo. A funcao
    devolve os dois de propósito: esconder o cliente dentro do guard tentaria
    contribuintes a criar outro para "so um GET rapido".
    """
    try:
        assert_pinned_certificate()
    except (TimeoutError, OSError) as e:
        raise LiveGameRefused(
            f"o client do League nao esta respondendo em {HOST}:{PORT}",
            hint=_HINT_REPLAY,
        ) from e
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
