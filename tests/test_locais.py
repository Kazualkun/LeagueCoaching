"""De onde vem o lugar de cada marcacao.

A distincao que este arquivo protege: `local_do_evento` e EXATO, porque sai do
proprio evento da timeline; `local_do_jogador` e APROXIMADO, porque a Riot so
entrega uma posicao por minuto. Trata-los como se fossem a mesma coisa foi o
erro da primeira versao do minimapa — um anel que dizia "voce esta aqui" e
discordava visivelmente do icone que o jogo desenha.
"""

from __future__ import annotations

from typing import Any

from riftcoach.overlay.locais import JANELA_MS, local_do_evento, local_do_jogador


def timeline(*eventos: dict[str, Any]) -> dict[str, Any]:
    return {"info": {"frames": [{"timestamp": 0, "events": list(eventos)}]}}


def evento(tipo: str, t_ms: int, x: float, y: float) -> dict[str, Any]:
    return {"type": tipo, "timestamp": t_ms, "position": {"x": x, "y": y}}


# --------------------------------------------------------------------------
# O lugar exato da jogada
# --------------------------------------------------------------------------


def test_acha_a_morte_no_instante_da_marcacao() -> None:
    tl = timeline(evento("CHAMPION_KILL", 900_000, 5000, 10000))
    assert local_do_evento(tl, 900_000) == (5000.0, 10000.0)


def test_aceita_uma_folga_entre_a_decisao_e_o_desfecho() -> None:
    """A marcacao aponta para a decisao; o evento registra a consequencia. Elas
    nao caem no mesmo milissegundo."""
    tl = timeline(evento("CHAMPION_KILL", 903_000, 5000, 10000))
    assert local_do_evento(tl, 900_000) == (5000.0, 10000.0)


def test_nao_pesca_a_teamfight_seguinte() -> None:
    """A janela existe para nao atribuir a uma marcacao o lugar de outro
    acontecimento — que seria apontar para o ponto errado com toda a
    confianca."""
    tl = timeline(evento("CHAMPION_KILL", 900_000 + JANELA_MS + 1, 5000, 10000))
    assert local_do_evento(tl, 900_000) is None


def test_entre_dois_eventos_vence_o_mais_proximo() -> None:
    tl = timeline(
        evento("CHAMPION_KILL", 898_000, 1000, 1000),
        evento("ELITE_MONSTER_KILL", 900_500, 9000, 9000),
    )
    assert local_do_evento(tl, 900_000) == (9000.0, 9000.0)


def test_evento_sem_posicao_e_ignorado() -> None:
    """Nem todo evento da timeline carrega coordenada — ITEM_PURCHASED, por
    exemplo. Ler `position` sem conferir estouraria no meio da revisao."""
    tl = timeline({"type": "CHAMPION_KILL", "timestamp": 900_000})
    assert local_do_evento(tl, 900_000) is None


def test_tipo_que_nao_acontece_no_mapa_e_ignorado() -> None:
    tl = timeline(evento("ITEM_PURCHASED", 900_000, 5000, 10000))
    assert local_do_evento(tl, 900_000) is None


def test_timeline_vazia_nao_estoura() -> None:
    assert local_do_evento({}, 900_000) is None
    assert local_do_evento({"info": {"frames": []}}, 900_000) is None


# --------------------------------------------------------------------------
# O lugar aproximado do jogador
# --------------------------------------------------------------------------


def test_interpola_entre_frames() -> None:
    """Sem interpolar, a posicao pularia de lugar uma vez por minuto."""
    trilha = [(0, 0.0, 0.0), (60_000, 12000.0, 6000.0)]
    assert local_do_jogador(trilha, 30_000) == (6000.0, 3000.0)


def test_antes_do_primeiro_e_depois_do_ultimo_frame() -> None:
    """Extrapolar seria inventar. Prende-se nas pontas."""
    trilha = [(60_000, 1000.0, 1000.0), (120_000, 2000.0, 2000.0)]
    assert local_do_jogador(trilha, 0) == (1000.0, 1000.0)
    assert local_do_jogador(trilha, 999_000) == (2000.0, 2000.0)


def test_sem_trilha_devolve_nada() -> None:
    assert local_do_jogador([], 900_000) is None


def test_frames_no_mesmo_instante_nao_dividem_por_zero() -> None:
    """Dado corrompido existe, e uma divisao por zero aqui derrubaria a revisao
    inteira por causa de um frame duplicado."""
    trilha = [(60_000, 1000.0, 1000.0), (60_000, 5000.0, 5000.0)]
    assert local_do_jogador(trilha, 60_000) == (1000.0, 1000.0)
