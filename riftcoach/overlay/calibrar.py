"""Onde o minimapa esta NESTA tela, medido pelas torres.

A caixa fixa de `geometry.MINIMAP_MAP_AREA` foi medida num print, e acerta so
enquanto a pessoa usa a mesma resolucao, a mesma escala de HUD de replay e o
mesmo tamanho de minimapa de espectador. Qualquer um dos tres mudando e o
"VOCE" do minimapa sai no lugar errado — sem erro nenhum, so errado.

A medicao antiga tinha ainda um erro de metodo: achava a borda da TEXTURA do
mapa (por saturacao). Mas os limites do mundo (-120..14870) caem na caixa
preta de fora, alguns pixels alem da textura. Resultado: tudo projetado ~9%
encolhido para o centro.

O jeito certo e o que um topografo faria: pontos de controle. As 22 torres
tem posicao de mundo conhecida e aparecem no minimapa como icones vermelhos e
azuis inconfundiveis. Acha-se o centro de cada icone, casa-se com a torre
mais proxima da projecao atual e ajusta-se escala + deslocamento por minimos
quadrados. No replay de referencia o residuo mediano e menor que 1 px.

Funcao pura sobre uma imagem PIL: testavel com um print, sem jogo aberto.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from riftcoach.overlay.geometry import MAP_MAX_X, MAP_MAX_Y, MAP_MIN_X, MAP_MIN_Y, Rect

# Posicoes de mundo das torres, lidas de BUILDING_KILL em partidas reais
# (tests/fixtures). Estaveis ha muitos patches: o mapa nao muda de lugar.
TORRES_AZUIS: tuple[tuple[float, float], ...] = (
    (981, 10441),
    (1512, 6699),
    (1169, 4287),  # top
    (5846, 6396),
    (5048, 4812),
    (3651, 3696),  # mid
    (10504, 1029),
    (6919, 1483),
    (4281, 1253),  # bot
    (1748, 2270),  # nexo (a outra fica colada e vira o mesmo icone)
)
TORRES_VERMELHAS: tuple[tuple[float, float], ...] = (
    (4318, 13875),
    (7943, 13411),
    (10481, 13650),
    (8955, 8510),
    (9767, 10113),
    (11134, 11207),
    (13866, 4505),
    (13327, 8226),
    (13624, 10572),
    (12611, 13084),
)


# Pixels que contam como icone. Medidos no replay: vermelho ~#e13d3d/#af302f/
# #9b2a2a, azul ~#389bba/#48c7ef. Os limiares deixam de fora o terreno (verde
# acinzentado) e os contornos dos campeoes (mais claros e menos saturados).
def _vermelho(r: int, g: int, b: int) -> bool:
    return r > 140 and g < 70 and b < 70


def _azul(r: int, g: int, b: int) -> bool:
    return b > 150 and r < 90 and g > 110


# Um ajuste so vale com torres suficientes E residuo pequeno. Abaixo disso a
# caixa fixa e mais confiavel que um ajuste em cima de icones errados.
MIN_TORRES = 8
MAX_RESIDUO_PX = 3.0


@dataclass(frozen=True)
class Ajuste:
    rect: Rect
    torres: int
    residuo_px: float


def _blobs(px: Any, caixa: tuple[int, int, int, int], cond: Any) -> list[tuple[float, float]]:
    """Centros de icone. O numero dentro do escudo corta o icone em pedacos,
    entao o agrupamento tolera buracos de ate 3 px."""
    x0, y0, x1, y1 = caixa
    pontos = {(x, y) for y in range(y0, y1) for x in range(x0, x1) if cond(*px[x, y][:3])}
    vistos: set[tuple[int, int]] = set()
    centros: list[tuple[float, float]] = []
    for p in pontos:
        if p in vistos:
            continue
        pilha = [p]
        vistos.add(p)
        grupo: list[tuple[int, int]] = []
        while pilha:
            a, b = pilha.pop()
            grupo.append((a, b))
            for dx in range(-3, 4):
                for dy in range(-3, 4):
                    n = (a + dx, b + dy)
                    if n in pontos and n not in vistos:
                        vistos.add(n)
                        pilha.append(n)
        # Icone de torre tem dezenas de pixels; menos que isso e ruido
        # (bordas de campeao, particulas).
        if len(grupo) >= 12:
            xs = [q[0] for q in grupo]
            ys = [q[1] for q in grupo]
            centros.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return centros


def _reta(u: list[float], v: list[float]) -> tuple[float, float]:
    n = len(u)
    mu, mv = sum(u) / n, sum(v) / n
    den = sum((x - mu) ** 2 for x in u)
    a = sum((x - mu) * (y - mv) for x, y in zip(u, v, strict=True)) / den if den else 0.0
    return a, mv - a * mu


def ajustar(imagem: Any, chute: Rect) -> Ajuste | None:
    """Ajusta a caixa do minimapa a partir de um print da tela do jogo.

    `chute` e a caixa atual (a fixa, ou a ultima medida): serve para casar
    cada torre com o icone certo. Devolve None quando a imagem nao sustenta um
    ajuste confiavel — minimapa coberto, poucas torres vivas, outro jogo na
    tela. None e resposta legitima; quem chama continua com o chute.
    """
    img = imagem.convert("RGB")
    largura, altura = img.size
    px = img.load()
    margem = max(8, int(chute.w * 0.12))
    caixa = (
        max(0, int(chute.x) - margem),
        max(0, int(chute.y) - margem),
        min(largura, int(chute.right) + margem),
        min(altura, int(chute.bottom) + margem),
    )
    icones = {100: _blobs(px, caixa, _azul), 200: _blobs(px, caixa, _vermelho)}
    if not icones[100] or not icones[200]:
        return None

    ax = chute.w / (MAP_MAX_X - MAP_MIN_X)
    bx = chute.x - MAP_MIN_X * ax
    ay = -chute.h / (MAP_MAX_Y - MAP_MIN_Y)
    by = chute.bottom - MAP_MIN_Y * ay

    residuos: list[float] = []
    pares: list[tuple[float, float, float, float]] = []
    for _ in range(4):
        pares = []
        for time, torres in ((100, TORRES_AZUIS), (200, TORRES_VERMELHAS)):
            for wx, wy in torres:
                ex, ey = ax * wx + bx, ay * wy + by
                cx, cy = min(icones[time], key=lambda c: (c[0] - ex) ** 2 + (c[1] - ey) ** 2)
                pares.append((wx, wy, cx, cy))
        # Torre destruida casa com o icone vizinho e vira ponto fora da reta.
        # Uma rodada de corte robusto (3x a mediana) tira esses.
        if len(pares) >= MIN_TORRES:
            ax, bx = _reta([p[0] for p in pares], [p[2] for p in pares])
            ay, by = _reta([p[1] for p in pares], [p[3] for p in pares])
            residuos = [
                ((p[2] - (ax * p[0] + bx)) ** 2 + (p[3] - (ay * p[1] + by)) ** 2) ** 0.5
                for p in pares
            ]
            corte = max(2.0, 3 * statistics.median(residuos))
            bons = [p for p, r in zip(pares, residuos, strict=True) if r <= corte]
            if len(bons) >= MIN_TORRES:
                ax, bx = _reta([p[0] for p in bons], [p[2] for p in bons])
                ay, by = _reta([p[1] for p in bons], [p[3] for p in bons])
                pares = bons

    if len(pares) < MIN_TORRES or ax <= 0 or ay >= 0:
        return None
    residuos = [
        ((p[2] - (ax * p[0] + bx)) ** 2 + (p[3] - (ay * p[1] + by)) ** 2) ** 0.5 for p in pares
    ]
    mediano = statistics.median(residuos)
    if mediano > MAX_RESIDUO_PX:
        return None

    esquerda = ax * MAP_MIN_X + bx
    direita = ax * MAP_MAX_X + bx
    base = ay * MAP_MIN_Y + by
    topo = ay * MAP_MAX_Y + by
    rect = Rect(esquerda, topo, direita - esquerda, base - topo)
    # Minimapa e quadrado (a menos de 1%). Um ajuste torto e sinal de casamento
    # errado, nao de minimapa torto.
    if abs(rect.w - rect.h) > 0.05 * rect.w:
        return None
    return Ajuste(rect=rect, torres=len(pares), residuo_px=round(mediano, 2))
