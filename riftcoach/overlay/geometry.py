"""Do mundo do jogo para os pixels da tela.

TUDO AQUI FOI MEDIDO, NAO CHUTADO — e a medicao esta reproduzida em
`tests/test_overlay_geometry.py` contra `tests/fixtures/hud-spectator-1600x900.png`,
um print de replay de verdade.

Como a caixa do minimapa foi encontrada
---------------------------------------
Procurar borda por LUMINANCIA nao funciona: o interior do minimapa e cheio de
preto puro (as paredes de terreno), entao o detector encontra "borda" no meio
do mapa. Por SATURACAO funciona de primeira, porque a moldura da HUD e cinza
e o mapa e colorido. Em 1600x900 a saturacao media desaba para ~3 nas colunas
1378-1385 e 1584-1591, e nas linhas 678-683 e 884-889. Sobra:

    x  1385 .. 1584   (199 px)
    y   684 ..  884   (200 px)

Quadrado, com 16 px de folga ate a borda direita e ate a inferior. A simetria
e o sinal de que a medicao esta certa.

Isto NAO e o mesmo que `SPEC_MINIMAP` em vision/rois.py, e os dois precisam
existir: aquele e o widget inteiro COM moldura, usado para recortar imagem;
este e so a area desenhavel, que e onde o mapa realmente esta. Usar o primeiro
para projetar empurraria tudo uns 8 px para fora.

Precisao
--------
Projetando as 22 torres do Summoner's Rift sobre o print, os circulos caem
dentro dos icones de torre com desvio da ordem de 5 px em 199 — cerca de 370
unidades de mundo. Isso e MENOR que o icone de campeao no minimapa, que cobre
uns 1000 u. Ou seja: da para circular um jogador com honestidade, e nao da
para afirmar "ele estava 200 u fora de posicao" a partir daqui.
"""

from __future__ import annotations

from dataclasses import dataclass

from riftcoach.vision.rois import Anchor, Roi

# --------------------------------------------------------------------------
# O mapa
# --------------------------------------------------------------------------

# Limites do Summoner's Rift, como a propria Riot publica no DataDragon.
# Nao sao simetricos (o eixo y vai 110 u mais longe) e nao vale "arredondar
# para ficar bonito": a assimetria e real e some no arredondamento.
MAP_MIN_X = -120.0
MAP_MIN_Y = -120.0
MAP_MAX_X = 14870.0
MAP_MAX_Y = 14980.0

# Alcances que a gente desenha como circulo. Em unidades de mundo.
WARD_SIGHT_U = 1100.0  # visao de uma ward comum
CONTROL_WARD_U = 900.0
TURRET_RANGE_U = 775.0

# --------------------------------------------------------------------------
# A caixa na tela
# --------------------------------------------------------------------------

# Em fracao da altura, no mesmo formato dos outros ROIs, para acompanhar
# qualquer resolucao. Conferencia em 1600x900: dx=dy=16 px, 199x200 px.
MINIMAP_MAP_AREA = Roi(
    "minimap_map_area",
    Anchor.BOTTOM_RIGHT,
    dx=16 / 900,
    dy=16 / 900,
    w=199 / 900,
    h=200 / 900,
)


@dataclass(frozen=True)
class Rect:
    """Retangulo em pixels. `x`/`y` sao o canto superior esquerdo."""

    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.bottom

    def inset(self, d: float) -> Rect:
        return Rect(self.x + d, self.y + d, max(1.0, self.w - 2 * d), max(1.0, self.h - 2 * d))


def minimap_rect(width: int, height: int, hud_scale: float = 1.0) -> Rect:
    """Onde o mapa e desenhado, numa janela de `width` x `height`."""
    x0, y0, x1, y1 = MINIMAP_MAP_AREA.pixels(width, height, hud_scale)
    return Rect(float(x0), float(y0), float(x1 - x0), float(y1 - y0))


# --------------------------------------------------------------------------
# A projecao
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MinimapProjector:
    """Converte coordenada de mundo em pixel dentro do minimapa.

    O eixo y e INVERTIDO, e esquecer isso e o erro classico: no mundo o y
    cresce em direcao a base vermelha, que no minimapa fica em CIMA, enquanto
    o y de tela cresce para baixo. Sem a inversao tudo aparece espelhado na
    diagonal — e o pior e que continua parecendo um mapa plausivel.
    """

    rect: Rect
    rotated: bool = False

    def to_px(self, world_x: float, world_y: float) -> tuple[float, float]:
        fx = (world_x - MAP_MIN_X) / (MAP_MAX_X - MAP_MIN_X)
        fy = (world_y - MAP_MIN_Y) / (MAP_MAX_Y - MAP_MIN_Y)
        if self.rotated:
            # A opcao "girar minimapa" do jogo vira o mapa 180 graus para quem
            # joga no lado vermelho. Em replay o padrao e NAO girado, mas quem
            # deixou ligado no perfil veria tudo de cabeca para baixo, e sem
            # esta opcao nao haveria nem como explicar o que aconteceu.
            fx, fy = 1.0 - fx, 1.0 - fy
        return self.rect.x + fx * self.rect.w, self.rect.bottom - fy * self.rect.h

    def to_world(self, px: float, py: float) -> tuple[float, float]:
        """A volta. Serve para traduzir um clique do usuario em posicao."""
        fx = (px - self.rect.x) / self.rect.w
        fy = (self.rect.bottom - py) / self.rect.h
        if self.rotated:
            fx, fy = 1.0 - fx, 1.0 - fy
        return (
            MAP_MIN_X + fx * (MAP_MAX_X - MAP_MIN_X),
            MAP_MIN_Y + fy * (MAP_MAX_Y - MAP_MIN_Y),
        )

    def units_to_px(self, units: float) -> float:
        """Raio de mundo para raio de tela.

        Usa a media dos dois eixos. Eles diferem em 0,7% por causa da
        assimetria do mapa; um circulo perfeito e mais honesto que uma elipse
        que ninguem consegue justificar.
        """
        sx = self.rect.w / (MAP_MAX_X - MAP_MIN_X)
        sy = self.rect.h / (MAP_MAX_Y - MAP_MIN_Y)
        return units * (sx + sy) / 2
