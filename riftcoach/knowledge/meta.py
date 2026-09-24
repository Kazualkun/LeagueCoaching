"""Estatisticas do patch — matchup, runas e itens — geradas NESTA maquina.

O pedido era "treinar a IA para entender o jogo": matchup, composicao, como
jogar contra tal campeao, itens e taxas de vitoria, runa e build escolhidas
contra as recomendadas. Tres caminhos possiveis, e dois estao fechados:

  - fine-tune: docs/04-knowledge-base.md, 4.2 — fatos mudam a cada patch e
    pertencem ao contexto, nao aos pesos;
  - raspar op.gg/u.gg: o mesmo documento proibe (viola os termos deles e
    quebra a cada redesenho);
  - GERAR os numeros a partir da API da Riot, com a chave da propria pessoa.
    E o que este modulo faz: amostra partidas ranqueadas do servidor (dos
    melhores jogadores para baixo), guarda UMA linha por jogador — campeao,
    papel, oponente de rota, runas, itens finais, resultado — e agrega.

Os numeros viram EVIDENCIA para o relatorio e para o modelo, com a amostra
declarada. Win rate de 60% em 5 partidas nao e informacao, e ruido com cara
de fato; por isso toda taxa sai suavizada em direcao a 50% e com intervalo, e
quem mostra decide se a amostra sustenta a frase.

So agregados: nenhum PUUID e gravado, nenhuma linha identifica ninguem.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from riftcoach.config import data_dir

# Filas que entram na estatistica. Normal e ARAM ficam de fora: outro jogo.
FILAS_RANQUEADAS = frozenset({420, 440})

# Partida mais curta que isto e remake ou abandono — o resultado nao diz nada
# sobre campeao, runa ou item.
DURACAO_MINIMA_S = 15 * 60

# Suavizacao bayesiana: toda taxa comeca "emprestando" K partidas a 50%. Com
# 5 partidas reais, 4 vitorias viram 60% e nao 80%; com 500, o emprestimo
# some. E o jeito honesto de mostrar taxa com amostra pequena.
K_SUAVIZACAO = 10.0

# Abaixo disto a taxa e mostrada, mas nunca vira argumento de achado.
AMOSTRA_MINIMA = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS partida (
    match_id    TEXT PRIMARY KEY,
    patch       TEXT NOT NULL,
    fila        INTEGER NOT NULL,
    fonte       TEXT NOT NULL,
    duracao_s   INTEGER NOT NULL,
    coletada_em INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS linha (
    match_id      TEXT NOT NULL,
    pid           INTEGER NOT NULL,
    patch         TEXT NOT NULL,
    campeao       TEXT NOT NULL,   -- identificador do match-v5 ("MonkeyKing")
    campeao_id    INTEGER NOT NULL,
    papel         TEXT NOT NULL,   -- TOP / JUNGLE / MIDDLE / BOTTOM / UTILITY
    vitoria       INTEGER NOT NULL,
    oponente      TEXT,            -- campeao inimigo do mesmo papel
    pedra         INTEGER,         -- runa-chave
    estilo        INTEGER,
    sub_estilo    INTEGER,
    runas         TEXT,            -- JSON: as seis runas, na ordem
    feiticos      TEXT,            -- JSON: [menor, maior]
    itens         TEXT,            -- JSON: inventario final (sem trinket)
    venceu_rota   INTEGER,         -- challenges.laningPhaseGoldExpAdvantage
    PRIMARY KEY (match_id, pid)
);
CREATE INDEX IF NOT EXISTS linha_campeao ON linha (patch, campeao, papel);
CREATE INDEX IF NOT EXISTS linha_matchup ON linha (patch, campeao, papel, oponente);
"""


def meta_path() -> Path:
    return data_dir() / "meta.sqlite"


def patch_de(game_version: str) -> str:
    partes = game_version.split(".")
    return ".".join(partes[:2]) if len(partes) >= 2 else game_version


def patch_anterior(patch: str) -> str | None:
    """'16.19' -> '16.18'. '17.1' -> None (virada de temporada nao compara)."""
    try:
        maior, menor = (int(x) for x in patch.split("."))
    except ValueError:
        return None
    return f"{maior}.{menor - 1}" if menor > 1 else None


@dataclass(frozen=True)
class Taxa:
    """Uma taxa de vitoria com a amostra a mostra."""

    jogos: int
    vitorias: int

    @property
    def bruta(self) -> float:
        return self.vitorias / self.jogos if self.jogos else 0.5

    @property
    def suavizada(self) -> float:
        return (self.vitorias + K_SUAVIZACAO * 0.5) / (self.jogos + K_SUAVIZACAO)

    @property
    def margem(self) -> float:
        """Meia-largura do intervalo de Wilson a 90%, em fracao."""
        n = self.jogos
        if n == 0:
            return 0.5
        z = 1.645
        p = self.bruta
        den = 1 + z * z / n
        return z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den

    @property
    def suficiente(self) -> bool:
        return self.jogos >= AMOSTRA_MINIMA

    def texto(self) -> str:
        """'52% (±6, 180 partidas)' — a amostra sempre junto do numero."""
        if not self.jogos:
            return "sem partidas na amostra"
        return (
            f"{100 * self.suavizada:.0f}% (±{100 * self.margem:.0f}, "
            f"{self.jogos} partida{'s' if self.jogos != 1 else ''})"
        )


@dataclass(frozen=True)
class Opcao:
    """Uma escolha (runa-chave, item) com quanto e escolhida e quanto vence."""

    chave: tuple[int, ...]
    taxa: Taxa
    escolha: float  # fracao das partidas do campeao/papel que usam isto


class MetaDB:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or meta_path()
        with self._con() as db:
            db.executescript(_SCHEMA)

    def _con(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    # ------------------------------------------------------------------
    # Ingestao
    # ------------------------------------------------------------------

    def conhece(self, match_id: str) -> bool:
        with self._con() as db:
            return (
                db.execute("SELECT 1 FROM partida WHERE match_id=?", (match_id,)).fetchone()
                is not None
            )

    def ingerir(self, match: dict[str, Any], fonte: str) -> bool:
        """Grava uma partida. False quando ela nao serve para estatistica."""
        info = match.get("info", {})
        mid = match.get("metadata", {}).get("matchId")
        if not mid or info.get("mapId") != 11 or info.get("queueId") not in FILAS_RANQUEADAS:
            return False
        if int(info.get("gameDuration", 0)) < DURACAO_MINIMA_S:
            return False
        participantes = info.get("participants", [])
        if len(participantes) != 10:
            return False
        patch = patch_de(str(info.get("gameVersion", "")))
        por_papel: dict[tuple[int, str], str] = {
            (int(p["teamId"]), str(p.get("teamPosition") or "")): str(p.get("championName", ""))
            for p in participantes
        }
        linhas = []
        for p in participantes:
            papel = str(p.get("teamPosition") or "")
            if not papel:
                return False  # sem papel nao ha matchup nem comparacao possivel
            estilos = p.get("perks", {}).get("styles", [])
            runas = [s.get("perk") for st in estilos for s in st.get("selections", [])]
            itens = sorted(i for i in (p.get(f"item{k}", 0) for k in range(6)) if i)
            linhas.append(
                (
                    mid,
                    int(p["participantId"]),
                    patch,
                    str(p.get("championName", "")),
                    int(p.get("championId", 0)),
                    papel,
                    int(bool(p.get("win"))),
                    por_papel.get((300 - int(p["teamId"]), papel)),
                    runas[0] if runas else None,
                    estilos[0].get("style") if estilos else None,
                    estilos[1].get("style") if len(estilos) > 1 else None,
                    json.dumps(runas),
                    json.dumps(sorted([p.get("summoner1Id", 0), p.get("summoner2Id", 0)])),
                    json.dumps(itens),
                    p.get("challenges", {}).get("laningPhaseGoldExpAdvantage"),
                )
            )
        with self._con() as db:
            db.execute(
                "INSERT OR IGNORE INTO partida VALUES (?, ?, ?, ?, ?, ?)",
                (
                    mid,
                    patch,
                    int(info["queueId"]),
                    fonte,
                    int(info["gameDuration"]),
                    int(time.time()),
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO linha VALUES (" + ",".join("?" * 15) + ")", linhas
            )
        return True

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------

    def partidas(self, patch: str | None = None) -> int:
        with self._con() as db:
            if patch is None:
                return int(db.execute("SELECT COUNT(*) FROM partida").fetchone()[0])
            return int(
                db.execute("SELECT COUNT(*) FROM partida WHERE patch=?", (patch,)).fetchone()[0]
            )

    def por_patch(self) -> list[tuple[str, int]]:
        with self._con() as db:
            return [
                (str(p), int(n))
                for p, n in db.execute(
                    "SELECT patch, COUNT(*) FROM partida GROUP BY patch ORDER BY COUNT(*) DESC"
                )
            ]

    def patches_para(self, patch: str, campeao: str, papel: str) -> tuple[str, ...]:
        """O patch da partida — e o anterior junto, se a amostra for pouca.

        Campeao pouco jogado quase nunca junta amostra num patch so; somar o
        anterior e melhor do que nao dizer nada, desde que o texto avise.
        """
        atual = self.campeao(campeao, papel, (patch,))
        anterior = patch_anterior(patch)
        if atual.jogos >= AMOSTRA_MINIMA * 3 or anterior is None:
            return (patch,)
        return (patch, anterior)

    @staticmethod
    def _em(patches: Iterable[str]) -> tuple[str, list[str]]:
        lista = list(patches)
        return f"patch IN ({','.join('?' * len(lista))})", lista

    def campeao(self, campeao: str, papel: str, patches: Iterable[str]) -> Taxa:
        onde, args = self._em(patches)
        with self._con() as db:
            n, v = db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(vitoria),0) FROM linha "
                f"WHERE {onde} AND campeao=? AND papel=?",
                [*args, campeao, papel],
            ).fetchone()
        return Taxa(int(n), int(v))

    def matchup(
        self, campeao: str, papel: str, oponente: str, patches: Iterable[str]
    ) -> tuple[Taxa, float | None]:
        """(taxa de vitoria do campeao CONTRA o oponente, fracao que venceu a rota)."""
        onde, args = self._em(patches)
        with self._con() as db:
            n, v, rota = db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(vitoria),0), AVG(venceu_rota) FROM linha "
                f"WHERE {onde} AND campeao=? AND papel=? AND oponente=?",
                [*args, campeao, papel, oponente],
            ).fetchone()
        return Taxa(int(n), int(v)), (float(rota) if rota is not None and n else None)

    def runas(self, campeao: str, papel: str, patches: Iterable[str], top: int = 4) -> list[Opcao]:
        """Runa-chave + arvore secundaria, das mais escolhidas."""
        onde, args = self._em(patches)
        with self._con() as db:
            total = self.campeao(campeao, papel, args).jogos
            linhas = db.execute(
                f"SELECT pedra, sub_estilo, COUNT(*), SUM(vitoria) FROM linha "
                f"WHERE {onde} AND campeao=? AND papel=? AND pedra IS NOT NULL "
                f"GROUP BY pedra, sub_estilo ORDER BY COUNT(*) DESC LIMIT ?",
                [*args, campeao, papel, top],
            ).fetchall()
        return [
            Opcao((int(p), int(s or 0)), Taxa(int(n), int(v)), n / total if total else 0.0)
            for p, s, n, v in linhas
        ]

    def itens(
        self,
        campeao: str,
        papel: str,
        patches: Iterable[str],
        validos: set[int] | None = None,
        top: int = 10,
    ) -> list[Opcao]:
        """Itens do inventario final, dos mais presentes. `validos` filtra
        (so lendarios, so botas) — quem decide o que e lendario e o PatchDB."""
        onde, args = self._em(patches)
        with self._con() as db:
            linhas = db.execute(
                f"SELECT itens, vitoria FROM linha WHERE {onde} AND campeao=? AND papel=?",
                [*args, campeao, papel],
            ).fetchall()
        total = len(linhas)
        contagem: dict[int, list[int]] = {}
        for itens_json, vitoria in linhas:
            for item in set(json.loads(itens_json or "[]")):
                if validos is not None and item not in validos:
                    continue
                par = contagem.setdefault(int(item), [0, 0])
                par[0] += 1
                par[1] += int(vitoria)
        ordem = sorted(contagem.items(), key=lambda kv: -kv[1][0])[:top]
        return [Opcao((item,), Taxa(n, v), n / total if total else 0.0) for item, (n, v) in ordem]

    def feiticos(self, campeao: str, papel: str, patches: Iterable[str]) -> list[Opcao]:
        onde, args = self._em(patches)
        with self._con() as db:
            total = self.campeao(campeao, papel, args).jogos
            linhas = db.execute(
                f"SELECT feiticos, COUNT(*), SUM(vitoria) FROM linha "
                f"WHERE {onde} AND campeao=? AND papel=? GROUP BY feiticos "
                f"ORDER BY COUNT(*) DESC LIMIT 3",
                [*args, campeao, papel],
            ).fetchall()
        return [
            Opcao(tuple(json.loads(f)), Taxa(int(n), int(v)), n / total if total else 0.0)
            for f, n, v in linhas
        ]
