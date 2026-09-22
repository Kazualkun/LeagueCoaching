"""Leitura de `.rofl` — SOMENTE metadados.

O QUE ESTE MODULO NAO FAZ, e nao por falta de vontade: ele nunca decodifica os
chunks. O conteudo da partida dentro de um `.rofl` e criptografado e a chave
nao e recuperavel depois; quem afirma extrair posicoes de um `.rofl` cru esta
enganado ou esta falando de outra coisa.

Isso nao e limitacao, e uma escolha barata: usamos a timeline do Match-v5 para
os dados e o client do League para a reproducao, o que entrega estritamente
mais do que o arquivo teria.

Os offsets do cabecalho MUDARAM entre versoes maiores do client. Entao o
parsing e defensivo e, quando falha, cai para o nome do arquivo — os clients
gravam `<PLATAFORMA>-<GAMEID>.rofl`, que ja e tudo o que realmente precisamos
para casar o arquivo com uma partida.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAGIC = b"RIOT"
# Offset do bloco de cabecalho na versao atual. E um palpite versionado, nao um
# contrato — por isso `read_metadata` tolera o parsing falhar por inteiro.
HEADER_OFFSET = 262
# Teto de sanidade para o bloco de metadados. Um valor absurdo aqui significa
# que o offset mudou de novo, e ler 2 GB de um arquivo local por causa de um
# struct mal interpretado seria um jeito bobo de travar o app.
MAX_META_BYTES = 8 * 1024 * 1024

# Os clients gravam <PLATAFORMA>-<GAMEID>.rofl.
FILENAME_RE = re.compile(r"^(?P<platform>[A-Z0-9]+)-(?P<game_id>\d+)\.rofl$", re.IGNORECASE)


@dataclass(frozen=True)
class RoflMeta:
    """O que da para saber de um `.rofl` sem tocar no conteudo cifrado."""

    path: Path
    match_id: str | None = None
    game_length_ms: int | None = None
    # Stats por jogador, quando o cabecalho pode ser lido. Vem de `statsJson`,
    # que e uma STRING JSON dentro do JSON de metadados — detalhe facil de
    # errar e que ja quebrou implementacoes.
    stats: list[dict[str, Any]] = None  # type: ignore[assignment]
    source: str = "filename"  # "header" | "filename"

    def __post_init__(self) -> None:
        if self.stats is None:
            object.__setattr__(self, "stats", [])

    @property
    def duration_s(self) -> int | None:
        return self.game_length_ms // 1000 if self.game_length_ms else None


def match_id_from_filename(path: Path) -> str | None:
    """`BR1-3239179616.rofl` -> `BR1_3239179616`.

    O separador muda de `-` para `_` de proposito: e assim que o match-v5
    identifica a partida, e o objetivo aqui e casar o arquivo com o que ja
    esta no cache.
    """
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    return f"{m.group('platform').upper()}_{m.group('game_id')}"


def read_metadata(path: Path) -> RoflMeta:
    """Le o que der. Nunca levanta por arquivo estranho.

    Um `.rofl` de uma versao futura do client, truncado, ou de outro jogo
    precisa degradar para o nome do arquivo em vez de derrubar a analise: o
    replay e opcional no produto, e o relatorio nao depende dele.
    """
    do_nome = match_id_from_filename(path)

    try:
        with path.open("rb") as f:
            if f.read(4) != MAGIC:
                return RoflMeta(path=path, match_id=do_nome, source="filename")

            f.seek(HEADER_OFFSET)
            cabecalho = f.read(8)
            if len(cabecalho) < 8:
                return RoflMeta(path=path, match_id=do_nome, source="filename")

            meta_off, meta_len = struct.unpack("<II", cabecalho)
            if not 0 < meta_len <= MAX_META_BYTES or meta_off <= 0:
                return RoflMeta(path=path, match_id=do_nome, source="filename")

            f.seek(meta_off)
            bruto = f.read(meta_len)
            if len(bruto) < meta_len:
                return RoflMeta(path=path, match_id=do_nome, source="filename")

            meta = json.loads(bruto.decode("utf-8"))
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        # Toda falha de leitura cai no nome do arquivo. Nao ha caso em que
        # valha a pena propagar: o chamador nao pode fazer nada melhor.
        return RoflMeta(path=path, match_id=do_nome, source="filename")

    if not isinstance(meta, dict):
        return RoflMeta(path=path, match_id=do_nome, source="filename")

    return RoflMeta(
        path=path,
        match_id=do_nome,
        game_length_ms=_as_int(meta.get("gameLength")),
        stats=_parse_stats(meta.get("statsJson")),
        source="header",
    )


def _as_int(valor: object) -> int | None:
    """`gameLength` chega como int, float ou string, dependendo da versao."""
    if isinstance(valor, bool):
        # bool e subclasse de int em Python, e `True` viraria 1 silenciosamente.
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor)
    if isinstance(valor, str):
        try:
            return int(float(valor))
        except ValueError:
            return None
    return None


def _parse_stats(bruto: object) -> list[dict[str, Any]]:
    """`statsJson` e uma STRING de JSON dentro do JSON. Sim, de verdade."""
    if isinstance(bruto, list):
        return [x for x in bruto if isinstance(x, dict)]
    if not isinstance(bruto, str):
        return []
    try:
        dados = json.loads(bruto)
    except ValueError:
        return []
    return [x for x in dados if isinstance(x, dict)] if isinstance(dados, list) else []


def default_replay_dir() -> Path:
    """Onde o client do League grava os replays, por padrao no Windows."""
    return Path.home() / "Documents" / "League of Legends" / "Replays"


def find_replay(match_id: str, directory: Path | None = None) -> Path | None:
    """Acha o `.rofl` de uma partida pelo nome do arquivo.

    Casamento por nome, nao por cabecalho: ler o cabecalho de cada arquivo de
    uma pasta com centenas de replays custaria muito mais e o nome ja e
    autoritativo.
    """
    pasta = directory or default_replay_dir()
    if not pasta.is_dir():
        return None
    alvo = match_id.replace("_", "-").upper()
    for arquivo in pasta.glob("*.rofl"):
        if arquivo.stem.upper() == alvo:
            return arquivo
    return None
