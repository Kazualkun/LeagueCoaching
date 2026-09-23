"""O motor de perguntas: o que entra no prompt, e o que nem chega a sair.

Nada aqui toca a rede. O que importa testar e o CONTRATO do prompt — se o
momento em foco aparece, se a partida inteira vai junto — e a recusa de
entrada invalida ANTES de gastar cota, que e o recurso escasso aqui.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from riftcoach.analysis.pergunta import (
    LIMITE_DA_PERGUNTA,
    montar_prompt,
    responder,
)
from riftcoach.analysis.report import analyze
from riftcoach.core.errors import RiftCoachError
from riftcoach.parse.distill import distill
from riftcoach.parse.facts import MatchFacts

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def garen() -> MatchFacts:
    with gzip.open(FIXTURES / "sr_ranked_35min.json.gz", "rb") as f:
        d = json.load(f)
    return distill(d["match"], d["timeline"], d["match"]["info"]["participants"][0]["puuid"])


def test_o_prompt_carrega_a_partida_e_a_pergunta(garen: MatchFacts) -> None:
    p = montar_prompt(garen, "por que eu morri tanto?")
    assert "=== DADOS DA PARTIDA ===" in p
    assert "=== PERGUNTA DO JOGADOR ===" in p
    assert "por que eu morri tanto?" in p
    # Sem finding, nao ha momento em foco — e a secao nao pode aparecer vazia.
    assert "MOMENTO EM FOCO" not in p


def test_o_momento_em_foco_entra_quando_a_pergunta_vem_ancorada(garen: MatchFacts) -> None:
    """Sem isto o modelo recebe a partida inteira e nao sabe de qual erro o
    jogador fala quando pergunta so "por que isso foi ruim?"."""
    relatorio, _ = analyze(garen)
    f = relatorio.ranked()[0]

    p = montar_prompt(garen, "por que isso foi ruim?", finding=f)
    assert "MOMENTO EM FOCO" in p
    assert f.claim in p
    assert f.fix in p


def test_do_overlay_a_pergunta_leva_o_instante_do_replay(garen: MatchFacts) -> None:
    """No overlay nao ha finding: so o relogio do replay parado. Sem ele, "por
    que eu morri aqui?" nao diz qual das mortes."""
    p = montar_prompt(garen, "por que eu morri aqui?", momento_ms=14 * 60_000 + 22_000)
    assert "MOMENTO EM FOCO" in p
    assert "14:22" in p


def test_o_finding_prevalece_sobre_o_instante(garen: MatchFacts) -> None:
    """Com os dois, o finding diz mais: ja traz o instante E o que foi apontado."""
    relatorio, _ = analyze(garen)
    f = relatorio.ranked()[0]
    p = montar_prompt(garen, "por que?", finding=f, momento_ms=1)
    assert f.claim in p
    assert "replay parado" not in p


def test_o_prompt_cabe_na_cota(garen: MatchFacts) -> None:
    """A conta que justifica mandar a partida INTEIRA em vez de uma fatia.

    Contra o teto de 8.000 tokens/min do Groq, uma pergunta precisa caber com
    folga para a resposta. Se este teste quebrar, ou o render cresceu ou
    alguem passou a mandar contexto demais — e as duas coisas viram 429 na
    mao de quem esta esperando uma resposta na tela.
    """
    relatorio, _ = analyze(garen)
    p = montar_prompt(garen, "o que eu faco diferente?", finding=relatorio.ranked()[0])
    assert len(p) // 4 < 2_500


async def test_pergunta_vazia_nao_chega_ao_provedor(garen: MatchFacts) -> None:
    with pytest.raises(RiftCoachError):
        await responder(None, garen, "   ")  # type: ignore[arg-type]


async def test_pergunta_longa_demais_nao_chega_ao_provedor(garen: MatchFacts) -> None:
    """O roteador nem e tocado: `None` como router prova que a recusa
    acontece antes de qualquer chamada."""
    with pytest.raises(RiftCoachError) as e:
        await responder(None, garen, "x" * (LIMITE_DA_PERGUNTA + 1))  # type: ignore[arg-type]
    assert str(LIMITE_DA_PERGUNTA) in e.value.message
