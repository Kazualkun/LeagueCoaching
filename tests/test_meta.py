"""Estatisticas do patch (knowledge/meta.py), a coleta e a comparacao de build.

O que estes testes prendem e o que tornaria os numeros MENTIROSOS sem ninguem
perceber: taxa sem amostra a vista, remake contado como partida, oponente de
rota errado, e achado de "build fora do padrao" apoiado em meia duzia de jogos.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from riftcoach.analysis.build import analisar, para_pagina, texto_para_ia
from riftcoach.knowledge.coleta import coletar
from riftcoach.knowledge.meta import AMOSTRA_MINIMA, MetaDB, Taxa, patch_anterior
from riftcoach.knowledge.sync import PatchDB
from riftcoach.parse.distill import distill
from tests.test_distill import load


@pytest.fixture
def meta(tmp_path: Path) -> MetaDB:
    return MetaDB(tmp_path / "meta.sqlite")


def _ranqueada(match: dict[str, Any], mid: str, versao: str = "16.19.1.1") -> dict[str, Any]:
    m = copy.deepcopy(match)
    m["metadata"]["matchId"] = mid
    m["info"]["queueId"] = 420
    m["info"]["gameVersion"] = versao
    return m


# --------------------------------------------------------------------------
# Taxa
# --------------------------------------------------------------------------


def test_taxa_pequena_e_puxada_para_50() -> None:
    """4 vitorias em 5 nao e 80%: e ruido. A suavizacao diz isso no numero."""
    pouca = Taxa(5, 4)
    assert 0.5 < pouca.suavizada < 0.65
    muita = Taxa(500, 400)
    assert muita.suavizada == pytest.approx(0.8, abs=0.01)
    assert pouca.margem > muita.margem
    assert not pouca.suficiente and muita.suficiente
    assert "5 partidas" in pouca.texto()


def test_patch_anterior() -> None:
    assert patch_anterior("16.19") == "16.18"
    assert patch_anterior("17.1") is None


# --------------------------------------------------------------------------
# Ingestao
# --------------------------------------------------------------------------


def test_uma_partida_vira_dez_linhas_com_o_oponente_certo(meta: MetaDB) -> None:
    match, _ = load("sr_ranked_35min")
    assert meta.ingerir(_ranqueada(match, "BR1_1"), fonte="teste")
    assert meta.partidas("16.19") == 1
    # Ingerir de novo nao duplica.
    meta.ingerir(_ranqueada(match, "BR1_1"), fonte="teste")
    assert meta.partidas() == 1

    por_papel = {
        (p["teamId"], p["teamPosition"]): p["championName"] for p in match["info"]["participants"]
    }
    for p in match["info"]["participants"]:
        oponente = por_papel[(300 - p["teamId"], p["teamPosition"])]
        taxa, _ = meta.matchup(p["championName"], p["teamPosition"], oponente, ("16.19",))
        assert taxa.jogos == 1
        assert taxa.vitorias == int(p["win"])


def test_remake_e_fila_normal_ficam_de_fora(meta: MetaDB) -> None:
    remake, _ = load("sr_remake")
    assert not meta.ingerir(_ranqueada(remake, "BR1_R"), fonte="teste")
    normal = _ranqueada(load("sr_ranked_35min")[0], "BR1_N")
    normal["info"]["queueId"] = 400
    assert not meta.ingerir(normal, fonte="teste")
    assert meta.partidas() == 0


def test_runas_e_itens_contam_escolha_e_vitoria(meta: MetaDB) -> None:
    match, _ = load("sr_ranked_35min")
    for i in range(3):
        meta.ingerir(_ranqueada(match, f"BR1_{i}"), fonte="teste")
    p = match["info"]["participants"][0]
    runas = meta.runas(p["championName"], p["teamPosition"], ("16.19",))
    assert runas and runas[0].escolha == 1.0 and runas[0].taxa.jogos == 3
    itens = meta.itens(p["championName"], p["teamPosition"], ("16.19",))
    assert itens and all(o.taxa.jogos == 3 for o in itens)


# --------------------------------------------------------------------------
# Coleta (cliente falso: nenhuma rede)
# --------------------------------------------------------------------------


class _RiotFalso:
    def __init__(self, partidas: dict[str, dict[str, Any]]) -> None:
        self.partidas = partidas
        self.pedidas: list[str] = []

    async def apex_league(self, tier: str) -> list[dict[str, Any]]:
        return [{"puuid": "p1"}] if tier == "challenger" else []

    async def match_ids(self, puuid: str, count: int = 20, queue: int | None = None) -> list[str]:
        return list(self.partidas)

    async def match(self, mid: str, *, guardar: bool = True) -> dict[str, Any]:
        assert guardar is False, "a coleta nao pode encher o cache das partidas da pessoa"
        self.pedidas.append(mid)
        return self.partidas[mid]


async def test_a_coleta_para_ao_chegar_no_patch_anterior(meta: MetaDB) -> None:
    """Historico e cronologico: achou patch velho, o resto tambem e velho."""
    match, _ = load("sr_ranked_35min")
    partidas = {
        "BR1_A": _ranqueada(match, "BR1_A", "16.20.1.1"),  # mais novo: pula
        "BR1_B": _ranqueada(match, "BR1_B", "16.19.1.1"),
        "BR1_C": _ranqueada(match, "BR1_C", "16.18.1.1"),  # velho: para aqui
        "BR1_D": _ranqueada(match, "BR1_D", "16.19.1.1"),
    }
    falso = _RiotFalso(partidas)
    n = await coletar(falso, meta, alvo=10, patch="16.19", log=lambda _m: None)  # type: ignore[arg-type]
    assert n == 1
    assert falso.pedidas == ["BR1_A", "BR1_B", "BR1_C"]
    assert meta.partidas("16.19") == 1 and meta.partidas("16.18") == 0


# --------------------------------------------------------------------------
# Comparacao de build
# --------------------------------------------------------------------------


def test_sem_amostra_a_comparacao_nao_acusa_nada(tmp_path: Path, meta: MetaDB) -> None:
    """Sem partidas na amostra, nenhum achado de runa ou item — so composicao."""
    match, tl = load("sr_ranked_35min")
    f = distill(match, tl, match["info"]["participants"][0]["puuid"])
    b = analisar(f, PatchDB(tmp_path / "patch.sqlite"), meta)
    assert not b.tem_amostra
    assert not any("runas" in a.claim.lower() or "build" in a.claim.lower() for a in b.achados)
    assert "sem partidas" in texto_para_ia(b)
    pagina = para_pagina(b)
    assert pagina["champion_games"] == 0
    assert b.composicao, "a composicao sai da propria partida, com ou sem amostra"


def test_amostra_pequena_nunca_vira_achado(tmp_path: Path, meta: MetaDB) -> None:
    match, tl = load("sr_ranked_35min")
    versao = match["info"]["gameVersion"]
    for i in range(AMOSTRA_MINIMA - 1):
        meta.ingerir(_ranqueada(match, f"BR1_{i}", versao), fonte="teste")
    f = distill(match, tl, match["info"]["participants"][0]["puuid"])
    b = analisar(f, PatchDB(tmp_path / "patch.sqlite"), meta)
    assert b.campeao_taxa.jogos == AMOSTRA_MINIMA - 1
    assert not any(a.category == "itemization" and "raras" in a.claim for a in b.achados)
