"""Posicao entre frames e revisao de objetivos por papel.

Os dois modulos existem por causa de reclamacoes concretas de quem usou:
"voce estava longe" do Barao em que o jogador tinha assistencia, e "nao
tinha Smite" / "longe das Vastilarvas" para uma ADC. Os testes prendem as
propriedades que impedem as duas de voltar.
"""

from __future__ import annotations

import copy
import math
import statistics
from itertools import pairwise
from typing import Any

import pytest

from riftcoach.analysis.objetivos import papel_no_objetivo, revisar
from riftcoach.parse.distill import distill
from riftcoach.parse.posicoes import Posicoes, fator_de_tempo, tempo_de_morte_s
from tests.test_distill import load


def _times(match: dict[str, Any]) -> dict[int, int]:
    return {p["participantId"]: p["teamId"] for p in match["info"]["participants"]}


# --------------------------------------------------------------------------
# Tempo de morte
# --------------------------------------------------------------------------


def test_o_tempo_de_morte_cresce_com_nivel_e_com_a_partida() -> None:
    assert tempo_de_morte_s(1, 0) == 6
    assert tempo_de_morte_s(18, 0) == 52.5
    assert fator_de_tempo(14 * 60_000) == 0
    assert tempo_de_morte_s(10, 40 * 60_000) > tempo_de_morte_s(10, 20 * 60_000)
    assert fator_de_tempo(90 * 60_000) == 0.5  # teto


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min"])
def test_o_tempo_de_morte_bate_com_o_total_da_riot(fx: str) -> None:
    """A soma das mortes estimadas tem de bater com `totalTimeSpentDead`.

    E a unica medida direta que a Riot da; se a formula desandar (patch mudou
    a tabela), este teste e o que avisa — antes de os "vivos" do checklist de
    objetivos virarem ficcao.
    """
    match, tl = load(fx)
    pos = Posicoes(tl, _times(match))
    fim = match["info"]["gameDuration"] * 1000
    razoes = []
    for p in match["info"]["participants"]:
        real = p.get("totalTimeSpentDead", 0)
        estimado = sum(min(b, fim) - a for a, b in pos.mortes(p["participantId"])) / 1000
        if real > 20 and estimado > 0:
            razoes.append(real / estimado)
    assert razoes
    assert 0.85 <= statistics.median(razoes) <= 1.15


# --------------------------------------------------------------------------
# Posicao
# --------------------------------------------------------------------------


def test_a_vitima_esta_morta_logo_depois_do_abate() -> None:
    match, tl = load("sr_ranked_35min")
    pos = Posicoes(tl, _times(match))
    abates = [e for f in tl["info"]["frames"] for e in f["events"] if e["type"] == "CHAMPION_KILL"]
    for e in abates[:20]:
        est = pos.onde(e["victimId"], e["timestamp"] + 2_000)
        assert est is not None and est.morto
        assert est.renasce_em_s is not None and est.renasce_em_s > 0


def test_quem_mata_o_objetivo_participou() -> None:
    match, tl = load("sr_ranked_35min")
    pos = Posicoes(tl, _times(match))
    for f in tl["info"]["frames"]:
        for e in f["events"]:
            if e["type"] == "ELITE_MONSTER_KILL" and 1 <= e.get("killerId", 0) <= 10:
                assert pos.participou(e["killerId"], e)


def test_o_raio_de_duvida_e_honesto() -> None:
    """Esconde um frame e pede a estimativa aos outros sinais.

    O frame escondido e a verdade. O raio declarado precisa cobrir o erro real
    na grande maioria das vezes — e o que autoriza o checklist a dizer "longe"
    so quando ate o melhor caso e longe.
    """
    match, tl = load("sr_ranked_35min")
    times = _times(match)
    frames = tl["info"]["frames"]
    base = Posicoes(tl, times)
    cobertos = total = 0
    for idx in range(3, len(frames) - 1, 4):
        t = frames[idx]["timestamp"]
        sem = copy.deepcopy(tl)
        for pf in sem["info"]["frames"][idx]["participantFrames"].values():
            pf.pop("position", None)
        pos = Posicoes(sem, times)
        for chave, pf in frames[idx]["participantFrames"].items():
            pid = int(chave)
            # frame de quem pode estar morto e cadaver, nao posicao
            if any(a <= t < b + 0.25 * (b - a) for a, b in base.mortes(pid)):
                continue
            est = pos.onde(pid, t)
            if est is None or est.morto:
                continue
            real = math.hypot(est.x - pf["position"]["x"], est.y - pf["position"]["y"])
            total += 1
            cobertos += real <= est.erro_u
    assert total > 50
    assert cobertos / total >= 0.8


# --------------------------------------------------------------------------
# Objetivos por papel
# --------------------------------------------------------------------------


def test_de_quem_e_cada_objetivo() -> None:
    minuto = 60_000
    # A ADC nao responde pelo lado de cima na fase de rotas.
    assert papel_no_objetivo("BOTTOM", "HORDE", 8 * minuto) == "fora"
    assert papel_no_objetivo("BOTTOM", "RIFTHERALD", 10 * minuto) == "fora"
    assert papel_no_objetivo("BOTTOM", "DRAGON", 8 * minuto) == "principal"
    # O topo nao responde pelo dragao da fase de rotas.
    assert papel_no_objetivo("TOP", "DRAGON", 8 * minuto) == "fora"
    assert papel_no_objetivo("TOP", "HORDE", 8 * minuto) == "principal"
    # O cacador responde por tudo; o Barao e de todo mundo.
    assert papel_no_objetivo("JUNGLE", "HORDE", 8 * minuto) == "principal"
    for role in ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"):
        assert papel_no_objetivo(role, "BARON_NASHOR", 25 * minuto) == "principal"


@pytest.mark.parametrize("fx", ["sr_ranked_35min", "sr_flex_41min"])
def test_a_revisao_de_objetivos_fala_portugues_e_agrupa_as_larvas(fx: str) -> None:
    match, tl = load(fx)
    for p in match["info"]["participants"]:
        f = distill(match, tl, p["puuid"])
        revisao = revisar(f)
        assert revisao
        nomes = [r.nome for r in revisao]
        # Vastilarvas seguidas viram uma entrada so.
        for a, b in pairwise(revisao):
            assert not (
                a.nome.startswith("Vastilarvas")
                and b.nome.startswith("Vastilarvas")
                and b.t_ms - a.t_ms < 120_000
            )
        texto = " ".join(nomes + [i.texto for r in revisao for i in r.itens])
        for cru in ("HORDE", "HOLDING_MID", "Smite", "BARON_NASHOR", "OWN_", "NEUTRAL_"):
            assert cru not in texto
        # Nao-cacador nunca le sobre Smite, e ninguem le "+N ouro para seu time".
        assert "ouro para seu time" not in texto


def test_desvantagem_numerica_nao_vira_culpa() -> None:
    """Perder o objetivo com 1 contra 5 nao e erro de chegada: lutar era pior."""
    match, tl = load("sr_ranked_35min")
    f = distill(match, tl, match["info"]["participants"][0]["puuid"])
    for r in revisar(f):
        vivos = next((i for i in r.itens if i.rotulo.startswith("Vivos")), None)
        if vivos is None or r.seu_time:
            continue
        nossos, _, deles = (
            vivos.texto.removeprefix("seu time ").removesuffix(" inimigo").partition(" x ")
        )
        if int(nossos) <= int(deles) - 2:
            assert "desvantagem numérica" in r.veredito
            assert r.tom != "ruim"
