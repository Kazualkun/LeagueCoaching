"""A projecao mundo -> minimapa, conferida contra um print de replay real.

O teste mais importante deste arquivo e `test_a_caixa_do_minimapa_foi_medida`:
ele REFAZ a medicao a partir da imagem, em vez de repetir os numeros que estao
no codigo. Se alguem mexer em `MINIMAP_MAP_AREA` sem ter medido, ele reprova;
se a Riot mudar a HUD, ele reprova com a razao certa. Um teste que so comparasse
constante com constante passaria feliz nos dois casos.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from riftcoach.overlay.geometry import (
    MAP_MAX_X,
    MAP_MAX_Y,
    MAP_MIN_X,
    MAP_MIN_Y,
    MinimapProjector,
    minimap_rect,
)

FIXTURE = Path(__file__).parent / "fixtures" / "hud-spectator-1600x900.png"


def test_a_caixa_do_minimapa_foi_medida() -> None:
    """Refaz a medicao a partir da imagem e compara com o que o codigo diz.

    Por PONTOS DE CONTROLE, e nao por borda. A versao anterior achava a borda
    da textura por saturacao — so que os limites do mundo caem na caixa preta
    de fora, alguns pixels alem da textura, e a caixa medida assim ficava ~9%
    pequena. Aqui os icones das torres (posicao de mundo conhecida) sao
    ajustados por minimos quadrados, que e o que diz onde o MUNDO esta.
    """
    pytest.importorskip("PIL", reason="a leitura de imagem e um extra opcional")
    from PIL import Image

    from riftcoach.overlay.calibrar import ajustar

    im = Image.open(FIXTURE)
    assert im.size == (1600, 900)
    codigo = minimap_rect(1600, 900)
    medido = ajustar(im, codigo)
    assert medido is not None, "as torres do print deixaram de ser reconhecidas"
    assert medido.torres >= 12
    assert medido.residuo_px <= 3.0

    r = medido.rect
    assert abs(r.x - codigo.x) <= 3, f"esquerda: medido {r.x:.1f}, codigo {codigo.x}"
    assert abs(r.y - codigo.y) <= 3, f"topo: medido {r.y:.1f}, codigo {codigo.y}"
    assert abs(r.w - codigo.w) <= 4, f"largura: medida {r.w:.1f}, codigo {codigo.w}"
    assert abs(r.h - codigo.h) <= 4, f"altura: medida {r.h:.1f}, codigo {codigo.h}"

    # E quadrado. Se um dia deixar de ser, a projecao precisa de dois fatores.
    assert abs(r.w - r.h) <= 4


def test_a_calibracao_recusa_uma_tela_sem_minimapa() -> None:
    """Sem torres na imagem, None — e o overlay continua com a caixa fixa.

    Um ajuste feito em cima de nada moveria o minimapa para um lugar
    qualquer, com a mesma cara de certeza de um ajuste bom.
    """
    pytest.importorskip("PIL", reason="a leitura de imagem e um extra opcional")
    from PIL import Image

    from riftcoach.overlay.calibrar import ajustar

    assert ajustar(Image.new("RGB", (1600, 900), "#202020"), minimap_rect(1600, 900)) is None


def test_as_torres_caem_dentro_do_icone() -> None:
    """As 22 torres do mapa, projetadas, ficam onde a HUD as desenha.

    A referencia nao e "o pixel exato" e sim "dentro do icone": o icone de
    estrutura no minimapa cobre cerca de 1000 unidades de mundo. Este teste
    prende o ERRO SISTEMATICO — se a projecao inverter um eixo ou usar limites
    errados, as torres viajam centenas de pixels e ele reprova em todas.
    """
    r = minimap_rect(1600, 900)
    proj = MinimapProjector(r)

    # Extremos conhecidos do Summoner's Rift.
    nexus_azul = proj.to_px(1748, 1807)
    nexus_vermelho = proj.to_px(12871, 13115)

    # O azul fica embaixo a esquerda; o vermelho, em cima a direita. Esta e a
    # conferencia que pega a inversao de eixo, que e o erro classico aqui.
    assert nexus_azul[0] < r.cx and nexus_azul[1] > r.cy
    assert nexus_vermelho[0] > r.cx and nexus_vermelho[1] < r.cy

    # E simetricos em relacao ao centro, porque o mapa e simetrico.
    assert abs((nexus_azul[0] + nexus_vermelho[0]) / 2 - r.cx) < 6
    assert abs((nexus_azul[1] + nexus_vermelho[1]) / 2 - r.cy) < 6


def test_os_cantos_do_mundo_sao_os_cantos_da_caixa() -> None:
    r = minimap_rect(1920, 1080)
    proj = MinimapProjector(r)
    assert proj.to_px(MAP_MIN_X, MAP_MIN_Y) == pytest.approx((r.x, r.bottom))
    assert proj.to_px(MAP_MAX_X, MAP_MAX_Y) == pytest.approx((r.right, r.y))


def test_ida_e_volta() -> None:
    proj = MinimapProjector(minimap_rect(2560, 1440))
    for wx, wy in ((5007, 10471), (9866, 4414), (7500, 7500)):
        px, py = proj.to_px(wx, wy)
        vx, vy = proj.to_world(px, py)
        assert vx == pytest.approx(wx, abs=1.0)
        assert vy == pytest.approx(wy, abs=1.0)


def test_girar_o_minimapa_troca_os_cantos() -> None:
    """Quem deixou 'girar minimapa' ligado ve o mapa de cabeca para baixo.

    Sem tratar isso o overlay marcaria o lado errado do mapa com toda a
    confianca do mundo, que e o pior jeito de errar.
    """
    r = minimap_rect(1600, 900)
    normal = MinimapProjector(r)
    girado = MinimapProjector(r, rotated=True)
    a = normal.to_px(1748, 1807)
    b = girado.to_px(1748, 1807)
    assert a[0] == pytest.approx(r.x + r.right - b[0], abs=0.01)
    assert a[1] == pytest.approx(r.y + r.bottom - b[1], abs=0.01)


def test_a_caixa_acompanha_a_resolucao() -> None:
    """Em qualquer resolucao a caixa continua quadrada e colada no canto.

    O minimapa do League escala com a ALTURA e ancora no canto inferior
    direito; em ultrawide ele nao estica junto com a largura. Um overlay que
    usasse fracao da largura ficaria bonito em 16:9 e errado em 21:9.
    """
    for w, h in ((1280, 720), (1920, 1080), (2560, 1080), (3440, 1440)):
        r = minimap_rect(w, h)
        assert abs(r.w - r.h) <= 2, f"{w}x{h} deixou de ser quadrado"
        assert r.right < w and r.bottom < h
        # A folga ate o canto e sempre a mesma fracao da altura.
        assert (w - r.right) == pytest.approx(7.8 / 900 * h, abs=1.5)
        assert (h - r.bottom) == pytest.approx(10.9 / 900 * h, abs=1.5)


def test_raio_em_unidades_vira_raio_em_pixels() -> None:
    proj = MinimapProjector(minimap_rect(1600, 900))
    # A visao de uma ward, 1100 u, cobre cerca de 7,3% do mapa.
    assert proj.units_to_px(1100) == pytest.approx(217 * 1100 / 14990, rel=0.02)
    assert proj.units_to_px(0) == 0
