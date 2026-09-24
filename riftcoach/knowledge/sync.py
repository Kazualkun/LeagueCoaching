"""Sincronizacao do DataDragon: ids -> nomes, stats fixados no patch.

O DataDragon e a CDN publica e VERSIONADA da Riot. Nao exige chave de API, nao
tem limite de taxa relevante e guarda todas as versoes historicas — o que
permite gerar o changelog de qualquer patch sob demanda (ver patchdiff.py).

LOCALE E UMA DECISAO COM CONSEQUENCIA, NAO COSMETICA. O `FactValidator` casa
nomes de entidade por texto exato contra esta tabela. Se o banco for carregado
em en_US e o modelo escrever "Companheiro de Luden", TODO item vira
HALLUCINATED_ENTITY e o relatorio inteiro e descartado. Por isso guardamos os
DOIS locales: renderizamos no idioma do usuario e reconhecemos os dois.
Ver docs/04-knowledge-base.md, secao "Locale".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from riftcoach.config import data_dir, settings
from riftcoach.core.errors import RiftCoachError

DDRAGON = "https://ddragon.leagueoflegends.com"
# Sempre guardamos ingles junto com o idioma do usuario: modelos treinados
# majoritariamente em ingles vao escrever "Luden's Companion" mesmo instruidos
# em portugues, e reconhecer isso e corrigir e muito melhor do que rejeitar um
# finding correto.
FALLBACK_LOCALE = "en_US"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity (
    patch     TEXT NOT NULL,
    locale    TEXT NOT NULL,
    kind      TEXT NOT NULL,   -- item | rune | summoner | champion
    entity_id INTEGER NOT NULL,
    name      TEXT NOT NULL,
    data      TEXT,            -- JSON com stats/custo, so para item
    PRIMARY KEY (patch, locale, kind, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_entity_name ON entity(patch, locale, kind, name);

CREATE TABLE IF NOT EXISTS synced (
    patch     TEXT NOT NULL,
    locale    TEXT NOT NULL,
    ddragon   TEXT NOT NULL,   -- versao exata do DDragon usada
    synced_at INTEGER NOT NULL,
    PRIMARY KEY (patch, locale)
);
"""

# Fragmentos de atributo (statPerks) NAO estao no runesReforged.json. Sao
# poucos e estaveis, entao ficam aqui em vez de virarem um id cru no relatorio.
STAT_SHARDS = {
    5001: "Saúde",
    5002: "Armadura",
    5003: "Resistência Mágica",
    5005: "Velocidade de Ataque",
    5007: "Aceleração de Habilidade",
    5008: "Adaptável",
    5010: "Velocidade de Movimento",
    5011: "Saúde (escala)",
    5013: "Vigor",
}


class PatchSyncError(RiftCoachError):
    pass


def normalize_patch(game_version: str) -> str:
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


class PatchDB:
    """Banco de patch local. Implementa o protocolo `NameResolver`."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_dir() / "patch.sqlite")
        self._cache: dict[tuple[str, int], str] = {}
        self._patch: str | None = None
        self._locale = settings.locale
        self._init()

    def _init(self) -> None:
        import sqlite3

        with sqlite3.connect(self.path) as db:
            db.executescript(_SCHEMA)

    # ------------------------------------------------------------------
    # Sincronizacao
    # ------------------------------------------------------------------

    @staticmethod
    async def ddragon_versions(client: httpx.AsyncClient) -> list[str]:
        r = await client.get(f"{DDRAGON}/api/versions.json", timeout=20.0)
        r.raise_for_status()
        return list(r.json())

    @staticmethod
    def _match_version(patch: str, versions: list[str]) -> str:
        """'16.9' -> a versao do DDragon que comeca com '16.9.'.

        O `gameVersion` do match-v5 tem quatro componentes e NAO bate com o
        nome da versao do DDragon. Passar a string crua nao acha nada.
        """
        for v in versions:
            if v.startswith(f"{patch}."):
                return v
        raise PatchSyncError(
            f"Patch {patch} nao existe no DataDragon.",
            hint=(
                "Partidas muito antigas podem ter saido da CDN. O relatorio "
                "ainda funciona, mas com ids em vez de nomes."
            ),
        )

    async def sync(self, patch: str | None = None, locales: list[str] | None = None) -> str:
        """Baixa e grava os dados de um patch. Retorna a versao do DDragon usada.

        Idempotente: se o patch ja estiver gravado, nao baixa de novo.
        """
        import json
        import sqlite3
        import time

        wanted = locales or sorted({self._locale, FALLBACK_LOCALE})

        async with httpx.AsyncClient(timeout=30.0) as client:
            versions = await self.ddragon_versions(client)
            patch = patch or normalize_patch(versions[0])
            ddv = self._match_version(patch, versions)

            for locale in wanted:
                if self._is_synced(patch, locale):
                    continue
                rows: list[tuple[str, str, str, int, str, str | None]] = []

                data = await self._fetch(client, ddv, locale, "item.json")
                for raw_id, it in data["data"].items():
                    gold = it.get("gold", {})
                    rows.append(
                        (
                            patch,
                            locale,
                            "item",
                            int(raw_id),
                            it["name"],
                            json.dumps(
                                {
                                    "gold_total": gold.get("total", 0),
                                    "gold_base": gold.get("base", 0),
                                    "stats": it.get("stats", {}),
                                    "from": it.get("from", []),
                                    "into": it.get("into", []),
                                    "tags": it.get("tags", []),
                                },
                                ensure_ascii=False,
                            ),
                        )
                    )

                champs = await self._fetch(client, ddv, locale, "champion.json")
                for c in champs["data"].values():
                    # 'key' e o id numerico, mas vem como STRING no DDragon.
                    rows.append((patch, locale, "champion", int(c["key"]), c["name"], None))

                spells = await self._fetch(client, ddv, locale, "summoner.json")
                for s in spells["data"].values():
                    rows.append((patch, locale, "summoner", int(s["key"]), s["name"], None))

                trees = await self._fetch(client, ddv, locale, "runesReforged.json")
                for tree in trees:
                    rows.append((patch, locale, "rune", int(tree["id"]), tree["name"], None))
                    for slot in tree.get("slots", []):
                        for rune in slot.get("runes", []):
                            rows.append(
                                (patch, locale, "rune", int(rune["id"]), rune["name"], None)
                            )
                for sid, name in STAT_SHARDS.items():
                    rows.append((patch, locale, "rune", sid, name, None))

                with sqlite3.connect(self.path) as db:
                    db.executemany(
                        "INSERT OR REPLACE INTO entity "
                        "(patch, locale, kind, entity_id, name, data) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO synced (patch, locale, ddragon, synced_at)"
                        " VALUES (?, ?, ?, ?)",
                        (patch, locale, ddv, int(time.time())),
                    )

        self._patch = patch
        self._cache.clear()
        return ddv

    @staticmethod
    async def _fetch(client: httpx.AsyncClient, ddv: str, locale: str, file: str) -> Any:
        r = await client.get(f"{DDRAGON}/cdn/{ddv}/data/{locale}/{file}")
        r.raise_for_status()
        return r.json()

    def _is_synced(self, patch: str, locale: str) -> bool:
        import sqlite3

        with sqlite3.connect(self.path) as db:
            cur = db.execute("SELECT 1 FROM synced WHERE patch = ? AND locale = ?", (patch, locale))
            return cur.fetchone() is not None

    def available_patches(self) -> list[str]:
        import sqlite3

        with sqlite3.connect(self.path) as db:
            cur = db.execute("SELECT DISTINCT patch FROM synced ORDER BY patch")
            return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------------
    # Enumeracao — alimenta o FactValidator (docs/04-knowledge-base.md, 4.5)
    # ------------------------------------------------------------------

    def entity_names(self, patch: str | None = None) -> dict[str, str]:
        """{nome em minusculas: tipo}, de um patch ou de todos.

        Todos os locales de uma vez, de proposito. Um modelo treinado
        majoritariamente em ingles vai escrever "Luden's Companion" mesmo
        instruido em portugues, e reconhecer o nome em ingles para depois
        corrigir e muito melhor do que rejeitar um finding correto.
        """
        import sqlite3

        sql = "SELECT name, kind FROM entity"
        params: tuple[str, ...] = ()
        if patch is not None:
            sql += " WHERE patch = ?"
            params = (patch,)

        with sqlite3.connect(self.path) as db:
            linhas = db.execute(sql, params).fetchall()
        # Nomes de uma letra ou dois caracteres casariam com qualquer texto e
        # so produziriam ruido no scanner.
        return {str(n).lower(): str(k) for n, k in linhas if n and len(str(n)) > 2}

    def knows_patch(self, patch: str) -> bool:
        """Ha dados deste patch gravados?

        O validador precisa saber: sem dados do patch da partida, ele nao pode
        distinguir "item removido" de "item que nunca sincronizamos", e
        acusar o segundo como se fosse o primeiro descartaria findings bons.
        """
        import sqlite3

        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT 1 FROM synced WHERE patch = ? LIMIT 1", (patch,)).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Consulta (protocolo NameResolver)
    # ------------------------------------------------------------------

    def use_patch(self, patch: str) -> None:
        """Fixa o patch das consultas. Um relatorio SEMPRE consulta o patch em
        que a partida foi jogada, nunca o mais recente."""
        self._patch = patch
        self._cache.clear()

    def _lookup(self, kind: str, entity_id: int) -> str:
        import sqlite3

        if entity_id <= 0:
            return ""
        key = (kind, entity_id)
        if (hit := self._cache.get(key)) is not None:
            return hit

        with sqlite3.connect(self.path) as db:
            row = None
            if self._patch:
                row = db.execute(
                    "SELECT name FROM entity WHERE patch=? AND locale=? AND kind=? AND entity_id=?",
                    (self._patch, self._locale, kind, entity_id),
                ).fetchone()
            if row is None:
                # Qualquer patch serve como plano B: um nome levemente
                # desatualizado ainda e infinitamente mais util que "3076",
                # e o FactValidator pega divergencia de stat depois.
                row = db.execute(
                    "SELECT name FROM entity WHERE locale=? AND kind=? AND entity_id=?"
                    " ORDER BY patch DESC LIMIT 1",
                    (self._locale, kind, entity_id),
                ).fetchone()
        name = str(row[0]) if row else ""
        self._cache[key] = name
        return name

    # Nunca inventamos um nome: string vazia faz o render manter o id cru, e o
    # modelo ve "informacao ausente" em vez de receber um palpite.
    def item(self, item_id: int) -> str:
        return self._lookup("item", item_id)

    def rune(self, perk_id: int) -> str:
        return self._lookup("rune", perk_id)

    def summoner(self, spell_id: int) -> str:
        return self._lookup("summoner", spell_id)

    def champion(self, key: int) -> str:
        return self._lookup("champion", key)

    def item_cost(self, item_id: int) -> int | None:
        import json
        import sqlite3

        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT data FROM entity WHERE locale=? AND kind='item' AND entity_id=?"
                " ORDER BY patch DESC LIMIT 1",
                (self._locale, item_id),
            ).fetchone()
        if not row or not row[0]:
            return None
        return int(json.loads(row[0]).get("gold_total", 0)) or None


# --------------------------------------------------------------------------
# Classes de item — para comparar build com build
# --------------------------------------------------------------------------


def classes_de_itens(db: PatchDB, patch: str) -> dict[str, set[int]]:
    """{'lendario': {...}, 'botas': {...}} a partir do item.json do patch.

    Comparar a build da pessoa com a dos melhores so faz sentido item COMPLETO
    contra item completo: componente e consumivel mudam de partida para
    partida e nao dizem nada sobre escolha. O DataDragon nao marca "lendario";
    o criterio e o que o jogo pratica — nao vira mais nada, custa pelo menos
    2.000 e nao e consumivel nem acessorio. Botas a parte, porque toda build
    tem uma.
    """
    import json
    import sqlite3

    with sqlite3.connect(db.path) as con:
        linhas = con.execute(
            "SELECT entity_id, data FROM entity WHERE kind='item' AND patch=? AND locale=?",
            (patch, FALLBACK_LOCALE),
        ).fetchall()
        if not linhas:
            linhas = con.execute(
                "SELECT entity_id, data FROM entity WHERE kind='item' AND locale=? AND patch="
                "(SELECT MAX(patch) FROM synced)",
                (FALLBACK_LOCALE,),
            ).fetchall()
    out: dict[str, set[int]] = {"lendario": set(), "botas": set()}
    for item_id, dados in linhas:
        if not dados:
            continue
        d = json.loads(dados)
        tags = set(d.get("tags", []))
        if "Consumable" in tags or "Trinket" in tags:
            continue
        if "Boots" in tags and d.get("gold_total", 0) >= 900:
            out["botas"].add(int(item_id))
        elif not d.get("into") and d.get("gold_total", 0) >= 2000:
            out["lendario"].add(int(item_id))
    return out


def stats_do_item(db: PatchDB, item_id: int, patch: str) -> dict[str, float]:
    """Os atributos do item no patch (armadura, resistencia magica...), do DataDragon."""
    import json
    import sqlite3

    with sqlite3.connect(db.path) as con:
        row = con.execute(
            "SELECT data FROM entity WHERE kind='item' AND entity_id=? AND locale=?"
            " ORDER BY (patch = ?) DESC, patch DESC LIMIT 1",
            (item_id, FALLBACK_LOCALE, patch),
        ).fetchone()
    if not row or not row[0]:
        return {}
    return {k: float(v) for k, v in json.loads(row[0]).get("stats", {}).items()}
