"""O kit dos campeoes da rota — para o modelo falar de matchup sem inventar.

"Como jogar contra tal campeao" depende do kit: recarga, alcance, o que a
habilidade faz. O modelo tem isso de memoria, mas de um patch antigo, e o
prompt proibe (com razao) nomear habilidade que nao esteja no contexto. A
saida e por o kit OFICIAL do patch no contexto: o DataDragon publica, por
campeao, nome, recarga, alcance e descricao de cada habilidade.

So o campeao do jogador e o oponente de rota — o kit dos dez gastaria o
contexto inteiro. Cacheado em disco por versao: um arquivo por campeao, uma
vez por patch.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from riftcoach.config import data_dir, settings
from riftcoach.knowledge.sync import DDRAGON, PatchDB

TECLAS = ("Q", "W", "E", "R")


def _versao_ddragon(patch: str) -> str | None:
    """A versao exata do DataDragon ja usada para este patch (tabela synced)."""
    db = PatchDB()
    with sqlite3.connect(db.path) as con:
        row = con.execute("SELECT ddragon FROM synced WHERE patch=? LIMIT 1", (patch,)).fetchone()
    return str(row[0]) if row else None


def _pasta(ddv: str, locale: str) -> Path:
    p = data_dir() / "kits" / ddv / locale
    p.mkdir(parents=True, exist_ok=True)
    return p


async def kit(nome: str, patch: str, locale: str | None = None) -> dict[str, Any] | None:
    """O JSON do campeao no DataDragon, ou None (sem rede, patch nao sincronizado).

    `nome` e o identificador do match-v5 ("KogMaw", "MonkeyKing"), que e o
    mesmo nome de arquivo do DataDragon.
    """
    if not re.fullmatch(r"[A-Za-z0-9]+", nome or ""):
        return None
    ddv = _versao_ddragon(patch)
    if ddv is None:
        return None
    loc = locale or settings.locale
    arquivo = _pasta(ddv, loc) / f"{nome}.json"
    if arquivo.exists():
        try:
            dado: dict[str, Any] = json.loads(arquivo.read_text(encoding="utf-8"))
            return dado
        except (OSError, ValueError):
            pass
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(f"{DDRAGON}/cdn/{ddv}/data/{loc}/champion/{nome}.json")
            r.raise_for_status()
            dado = r.json()["data"][nome]
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    arquivo.write_text(json.dumps(dado, ensure_ascii=False), encoding="utf-8")
    return dado


def _limpo(html: str, limite: int = 160) -> str:
    """Descricao do DataDragon sem as tags de formatacao, e curta."""
    texto = re.sub(r"<[^>]+>", " ", html or "")
    texto = re.sub(r"%[^%\s]*%", "", texto)  # marcadores internos do jogo
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto if len(texto) <= limite else texto[: limite - 1].rsplit(" ", 1)[0] + "…"


def em_texto(dado: dict[str, Any], *, papel: str) -> str:
    """Kit compacto: passiva + Q/W/E/R com recarga, alcance e o que faz."""
    linhas = [f"KIT DE {dado.get('name', '?').upper()} ({papel}, dados oficiais do patch):"]
    passiva = dado.get("passive") or {}
    if passiva:
        linhas.append(
            f"  Passiva — {passiva.get('name', '?')}: {_limpo(passiva.get('description', ''))}"
        )
    for tecla, s in zip(TECLAS, dado.get("spells", []), strict=False):
        recarga = s.get("cooldownBurn", "?")
        alcance = s.get("rangeBurn", "?")
        linhas.append(
            f"  {tecla} — {s.get('name', '?')} (recarga {recarga}s, alcance {alcance}): "
            f"{_limpo(s.get('description', ''))}"
        )
    return "\n".join(linhas)


async def texto_dos_kits(foco: str, oponente: str | None, patch: str) -> str:
    """O kit do jogador e o do oponente de rota, prontos para o contexto."""
    partes = []
    meu = await kit(foco, patch)
    if meu:
        partes.append(em_texto(meu, papel="o campeao do jogador"))
    if oponente:
        dele = await kit(oponente, patch)
        if dele:
            partes.append(em_texto(dele, papel="o oponente de rota"))
    if not partes:
        return ""
    partes.append(
        "Para matchup, use estas recargas e alcances: janela de troca e quando a habilidade "
        "principal do oponente esta em recarga. Nao cite numero que nao esteja aqui."
    )
    return "\n".join(partes)
