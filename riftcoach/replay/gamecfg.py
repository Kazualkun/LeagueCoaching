"""Leitura do `game.cfg` do League — a configuracao do JOGO, nao do RiftCoach.

Existe por causa de uma descoberta que so apareceu contra o client de verdade:

    A Replay API vem DESLIGADA de fabrica.

A documentacao da Riot e explicita: "By default the Replay API is disabled. To
start using the Replay API, enable the Replay API in the game client config".
Falta uma linha `EnableReplayApi=1` na secao `[General]`.

Sem isso, `/replay/playback` devolve 404 — e o `ReplayGuard` interpretava esse
404 como "pode haver partida ao vivo", porque e assim que uma partida ao vivo
se parece. O diagnostico ficava alarmante e errado, para TODO usuario que ainda
nao mexeu no arquivo, que e todo usuario novo.

O guard continua falhando fechado no 404. O que muda e a explicacao: quando
sabemos que a API esta desligada, dizemos isso, em vez de acusar partida ao
vivo.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REPLAY_API_KEY = "EnableReplayApi"

# Caminhos padrao por sistema. A instalacao pode estar em outro disco, entao
# isto e um palpite ordenado, nao uma certeza — `find_game_cfg` tambem aceita
# um caminho explicito por variavel de ambiente.
_CANDIDATES = (
    r"C:\Riot Games\League of Legends\Config\game.cfg",
    r"D:\Riot Games\League of Legends\Config\game.cfg",
    r"E:\Riot Games\League of Legends\Config\game.cfg",
    "/Applications/League of Legends.app/Contents/LoL/Config/game.cfg",
)

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_ENTRY_RE = re.compile(r"^\s*(?P<key>[A-Za-z0-9_]+)\s*=\s*(?P<value>.*?)\s*$")


def find_game_cfg() -> Path | None:
    """Localiza o game.cfg. `RIFTCOACH_GAME_CFG` tem precedencia."""
    if override := os.environ.get("RIFTCOACH_GAME_CFG"):
        caminho = Path(override)
        return caminho if caminho.exists() else None
    for c in _CANDIDATES:
        caminho = Path(c)
        if caminho.exists():
            return caminho
    return None


@dataclass(frozen=True)
class GameConfig:
    path: Path
    sections: dict[str, dict[str, str]] = field(default_factory=dict)

    def get(self, section: str, key: str) -> str | None:
        return self.sections.get(section, {}).get(key)

    @property
    def replay_api_enabled(self) -> bool:
        valor = self.get("General", REPLAY_API_KEY)
        return valor is not None and valor.strip() not in ("0", "", "false", "False")

    @property
    def client_version(self) -> str | None:
        return self.get("General", "CfgVersion")

    @property
    def resolution(self) -> tuple[int, int] | None:
        w, h = self.get("General", "Width"), self.get("General", "Height")
        try:
            return (int(w), int(h)) if w and h else None
        except ValueError:
            return None

    @property
    def hud_scale_raw(self) -> float | None:
        """`GlobalScale` cru, como o jogo grava.

        Deliberadamente NAO convertido para um multiplicador. O valor e
        normalizado pelo slider da interface e a faixa que ele representa nao
        esta documentada — inventar uma formula produziria ROIs errados com
        aparencia de precisao, que e o modo de falha que esta camada inteira
        existe para evitar. Serve como pista para a calibracao, nao como
        resposta.
        """
        try:
            valor = self.get("HUD", "GlobalScale")
            return float(valor) if valor else None
        except ValueError:
            return None

    @property
    def minimap_scale_raw(self) -> float | None:
        try:
            valor = self.get("HUD", "MinimapScale")
            return float(valor) if valor else None
        except ValueError:
            return None


def parse(path: Path) -> GameConfig:
    sections: dict[str, dict[str, str]] = {}
    atual = "General"
    texto = path.read_text(encoding="utf-8", errors="replace")
    for linha in texto.splitlines():
        if (m := _SECTION_RE.match(linha)) is not None:
            atual = m.group("name")
            sections.setdefault(atual, {})
            continue
        if linha.lstrip().startswith(("#", ";")):
            continue
        if (m := _ENTRY_RE.match(linha)) is not None:
            sections.setdefault(atual, {})[m.group("key")] = m.group("value")
    return GameConfig(path=path, sections=sections)


def load() -> GameConfig | None:
    caminho = find_game_cfg()
    return parse(caminho) if caminho else None


def replay_api_status() -> tuple[bool | None, str]:
    """(habilitada?, explicacao). `None` quando nao achamos o game.cfg.

    A distincao entre `False` e `None` importa: "esta desligada, ligue assim" e
    acionavel; "nao achei seu game.cfg" pede outra coisa do usuario.
    """
    cfg = load()
    if cfg is None:
        return None, (
            "nao foi possivel localizar o game.cfg do League. Defina "
            "RIFTCOACH_GAME_CFG com o caminho completo do arquivo."
        )
    if cfg.replay_api_enabled:
        return True, f"{REPLAY_API_KEY}=1 presente em {cfg.path}"
    return False, (
        f"a Replay API do League esta DESLIGADA — ela vem assim de fabrica.\n"
        f"Adicione a linha abaixo na secao [General] de:\n"
        f"  {cfg.path}\n\n"
        f"  {REPLAY_API_KEY}=1\n\n"
        f"Depois reabra o replay. Ou rode: riftcoach enable-replay-api"
    )


def installed_patch() -> str | None:
    """O patch do jogo instalado, em major.minor ("16.19"). None se nao der.

    Lido de `Game/compat-version-metadata.json`, que o instalador da Riot
    mantem ao lado da pasta `Config` do game.cfg. `CfgVersion`, dentro do
    proprio game.cfg, NAO serve: ele guarda a versao em que o arquivo foi
    gravado pela ultima vez, que fica para tras depois de uma atualizacao.
    """
    cfg = find_game_cfg()
    if cfg is None:
        return None
    arquivo = cfg.parent.parent / "Game" / "compat-version-metadata.json"
    try:
        versao = json.loads(arquivo.read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # "16.19.8207193+branch.releases-16-19..." -> "16.19"
    partes = str(versao).split("+")[0].split(".")
    if len(partes) < 2 or not (partes[0].isdigit() and partes[1].isdigit()):
        return None
    return f"{int(partes[0])}.{int(partes[1])}"


def replay_patch_mismatch(match_patch: str) -> str | None:
    """Por que o replay desta partida nao abre, ou None se nada impede.

    O client da Riot recusa replay de outro patch — o log dele diz "Replay is
    incompatible due to major-minor version mismatch" e o botao de assistir
    some. Nao ha contorno: depois de uma atualizacao, os replays do patch
    anterior deixam de abrir. Sem este aviso, quem analisou uma partida da
    semana passada fica esperando um replay que nunca vai rodar, com uma
    mensagem mandando "baixar o replay e dar play".

    None tambem quando nao da para saber — sem o arquivo de versao, um aviso
    chutado seria pior que nenhum.
    """
    instalado = installed_patch()
    if instalado is None or not match_patch or instalado == match_patch:
        return None
    return (
        f"esta partida e do patch {match_patch} e o seu League ja esta no "
        f"{instalado}. O client da Riot nao abre replay de outro patch — nao ha "
        "como assistir este. Escolha uma partida jogada depois da atualizacao."
    )


def enable_replay_api(cfg: GameConfig) -> Path:
    """Acrescenta `EnableReplayApi=1` em `[General]`, com backup.

    Backup sempre: e o arquivo de configuracao do jogo do usuario, nao nosso.
    """
    backup = cfg.path.with_suffix(cfg.path.suffix + ".riftcoach-bak")
    if not backup.exists():
        backup.write_bytes(cfg.path.read_bytes())

    linhas = cfg.path.read_text(encoding="utf-8", errors="replace").splitlines()
    saida: list[str] = []
    inserido = False
    for linha in linhas:
        saida.append(linha)
        if not inserido and _SECTION_RE.match(linha) and "General" in linha:
            saida.append(f"{REPLAY_API_KEY}=1")
            inserido = True
    if not inserido:
        # Sem secao [General]: criamos uma no fim, que e valido no formato.
        saida.extend(["", "[General]", f"{REPLAY_API_KEY}=1"])

    cfg.path.write_text("\n".join(saida) + "\n", encoding="utf-8")
    return backup
