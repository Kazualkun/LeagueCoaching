"""O pincel, testado sem abrir janela.

A logica do pincel mora longe do mouse justamente para isto: o comportamento
inteiro — diluicao de pontos, clique sem arrasto, desfazer, limpar — da para
afirmar aqui, e o que sobra em `render.py` e so traduzir evento em fracao.
"""

from __future__ import annotations

import pytest

from riftcoach.core.schema import Stroke
from riftcoach.overlay.desenho import (
    CORES,
    PASSO_MINIMO,
    Prancheta,
    apagar_ultimo,
    limpar_instante,
)

# --------------------------------------------------------------------------
# O traco em andamento
# --------------------------------------------------------------------------


def test_um_arrasto_vira_traco() -> None:
    p = Prancheta()
    p.comecar(0.1, 0.1)
    p.mover(0.2, 0.2)
    p.mover(0.3, 0.3)
    traco = p.terminar(900_000)
    assert traco is not None
    assert len(traco.points) == 3
    assert traco.t_ms == 900_000


def test_clique_sem_arrastar_nao_vira_nada() -> None:
    """Um ponto nao e um traco. Devolver isso encheria a revisao de rabiscos
    invisiveis a cada clique errado."""
    p = Prancheta()
    p.comecar(0.5, 0.5)
    assert p.terminar(900_000) is None


def test_pontos_colados_sao_descartados() -> None:
    """O mouse gera dezenas de eventos por segundo e quase todos caem em cima
    do anterior. Guardar todos engorda a revisao sem mudar um pixel."""
    p = Prancheta()
    p.comecar(0.5, 0.5)
    assert not p.mover(0.5 + PASSO_MINIMO / 4, 0.5), "perto demais, nao entra"
    assert p.mover(0.5 + PASSO_MINIMO * 2, 0.5), "longe o bastante, entra"
    traco = p.terminar(0)
    assert traco is not None
    assert len(traco.points) == 2


def test_arrastar_para_fora_da_tela_nao_perde_o_desenho() -> None:
    """O evento chega com coordenada negativa quando o ponteiro sai da janela.
    Guardar isso quebraria a validacao do `Stroke` — ou seja, a pessoa perderia
    o desenho por ter passado do canto."""
    p = Prancheta()
    p.comecar(0.5, 0.5)
    p.mover(-0.4, 1.9)
    traco = p.terminar(0)
    assert traco is not None
    assert traco.points[-1] == (0.0, 1.0)


def test_mover_sem_ter_comecado_nao_estoura() -> None:
    assert Prancheta().mover(0.3, 0.3) is False


def test_descartar_joga_fora_o_traco_em_andamento() -> None:
    p = Prancheta()
    p.comecar(0.1, 0.1)
    p.mover(0.4, 0.4)
    p.descartar()
    assert p.terminar(0) is None


# --------------------------------------------------------------------------
# Cor e espessura
# --------------------------------------------------------------------------


def test_escolher_cor_pelo_numero() -> None:
    p = Prancheta()
    assert p.usar_cor(2)
    assert p.cor == CORES[2][0]
    assert not p.usar_cor(99), "numero fora da paleta nao muda nada"
    assert p.cor == CORES[2][0]


def test_a_espessura_gira_em_ciclo() -> None:
    p = Prancheta()
    vistas = {p.espessura}
    for _ in range(4):
        vistas.add(p.proxima_espessura())
    assert len(vistas) == 3, "tres espessuras, e o ciclo volta ao inicio"


def test_a_cor_escolhida_vai_para_o_traco() -> None:
    p = Prancheta()
    p.usar_cor(1)
    p.proxima_espessura()
    p.comecar(0.1, 0.1)
    p.mover(0.5, 0.5)
    traco = p.terminar(0)
    assert traco is not None
    assert traco.color == CORES[1][0]
    assert traco.width == p.espessura


# --------------------------------------------------------------------------
# Desfazer e limpar
# --------------------------------------------------------------------------


def _traco(t_ms: int, x: float = 0.1) -> Stroke:
    return Stroke(t_ms=t_ms, points=[(x, 0.1), (x, 0.5)])


def test_desfazer_tira_o_mais_recente_deste_instante() -> None:
    tracos = [_traco(900_000, 0.1), _traco(900_000, 0.2)]
    saiu = apagar_ultimo(tracos, 900_000)
    assert saiu is not None and saiu.points[0][0] == 0.2
    assert len(tracos) == 1


def test_desfazer_nao_alcanca_outro_momento_da_partida() -> None:
    """Desfazer nao pode apagar um desenho que a pessoa nem esta vendo."""
    tracos = [_traco(60_000)]
    assert apagar_ultimo(tracos, 900_000) is None
    assert len(tracos) == 1


def test_limpar_so_apaga_o_instante_atual() -> None:
    tracos = [_traco(900_000), _traco(900_000), _traco(60_000)]
    assert limpar_instante(tracos, 900_000) == 2
    assert [t.t_ms for t in tracos] == [60_000]


def test_limpar_sem_nada_para_apagar_devolve_zero() -> None:
    tracos = [_traco(60_000)]
    assert limpar_instante(tracos, 900_000) == 0
    assert len(tracos) == 1


# --------------------------------------------------------------------------
# O modelo guardado
# --------------------------------------------------------------------------


def test_ponto_fora_da_tela_e_recusado_no_schema() -> None:
    """Fracao fora de [0,1] so aparece se alguem editou o arquivo a mao ou se
    a conversao errou — e um traco invisivel fora da tela nao da sinal
    nenhum."""
    with pytest.raises(ValueError, match="fora da tela"):
        Stroke(t_ms=0, points=[(0.5, 0.5), (1.4, 0.2)])


def test_um_traco_precisa_de_pelo_menos_dois_pontos() -> None:
    with pytest.raises(ValueError):
        Stroke(t_ms=0, points=[(0.5, 0.5)])


def test_a_revisao_devolve_os_tracos_do_instante() -> None:
    from riftcoach.core.schema import ReviewSession

    rs = ReviewSession(match_id="BR1_1", puuid="p" * 78)
    rs.strokes = [_traco(900_000), _traco(60_000)]
    assert len(rs.strokes_em(900_000)) == 1
    assert rs.strokes_em(500_000) == []
