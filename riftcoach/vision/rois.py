"""Regioes de interesse da HUD, ancoradas em canto e escaladas por ALTURA.

Por que nao normalizar por largura, que e o reflexo obvio: a HUD do LoL nao
escala com a largura. Ela escala com a ALTURA e se ancora nas bordas. Num
monitor 21:9 o minimapa continua do mesmo tamanho e colado no canto inferior
direito — normalizar por largura o jogaria para o meio da tela e todo recorte
sairia errado, em silencio.

Entao cada ROI e descrita como:

    (canto de ancoragem, deslocamento, tamanho) — tudo em unidades de ALTURA

Isso vale para 16:9, 21:9, 16:10 e 4:3 sem caso especial.

CALIBRACAO: os valores abaixo sao medidos da HUD padrao em 1920x1080, escala
de interface 100%. Eles NAO foram conferidos contra captura real ainda — use
`tools/calibrate_rois.py`, que desenha os retangulos sobre um print para
conferencia visual. Jogadores com escala de HUD diferente de 100% vao precisar
de ajuste; ver `scale` em `Roi.pixels`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

Box = tuple[int, int, int, int]  # (x0, y0, x1, y1), estilo PIL


class Anchor(StrEnum):
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"
    TOP_CENTER = "top_center"
    BOTTOM_CENTER = "bottom_center"


@dataclass(frozen=True)
class Roi:
    """Um recorte da HUD.

    `dx`/`dy` sao o deslocamento a partir do canto ancorado, e `w`/`h` o
    tamanho — todos em fracao da ALTURA da tela. Deslocamento sempre cresce
    para DENTRO da tela, qualquer que seja o canto, para que a mesma descricao
    sirva aos quatro cantos sem inverter sinal na mao.
    """

    name: str
    anchor: Anchor
    dx: float
    dy: float
    w: float
    h: float

    def pixels(self, width: int, height: int, scale: float = 1.0) -> Box:
        """Converte para pixels numa resolucao concreta.

        `scale` acompanha a escala de interface do jogo (o slider de HUD).
        Em 100% e 1.0; quem joga em 80% passa 0.8.
        """
        if width <= 0 or height <= 0:
            raise ValueError(f"resolucao invalida: {width}x{height}")

        u = height * scale  # a unidade: altura da tela vezes a escala da HUD
        w = self.w * u
        h = self.h * u
        dx = self.dx * u
        dy = self.dy * u

        if self.anchor is Anchor.TOP_LEFT:
            x0, y0 = dx, dy
        elif self.anchor is Anchor.TOP_RIGHT:
            x0, y0 = width - dx - w, dy
        elif self.anchor is Anchor.BOTTOM_LEFT:
            x0, y0 = dx, height - dy - h
        elif self.anchor is Anchor.BOTTOM_RIGHT:
            x0, y0 = width - dx - w, height - dy - h
        elif self.anchor is Anchor.TOP_CENTER:
            x0, y0 = (width - w) / 2 + dx, dy
        else:  # BOTTOM_CENTER
            x0, y0 = (width - w) / 2 + dx, height - dy - h

        # Prender a tela: um recorte que vaza a borda estoura no PIL em vez de
        # devolver imagem menor, e a mensagem nao ajudaria em nada.
        x0 = max(0.0, min(x0, width - 1))
        y0 = max(0.0, min(y0, height - 1))
        x1 = max(x0 + 1, min(x0 + w, float(width)))
        y1 = max(y0 + 1, min(y0 + h, float(height)))
        return (round(x0), round(y0), round(x1), round(y1))


# --------------------------------------------------------------------------
# A HUD do Summoner's Rift
# --------------------------------------------------------------------------

# O relogio fica no canto superior direito, colado no minimapa do placar.
# E o ROI mais importante de todos: e por ele que o video e sincronizado com a
# timeline (docs/03-vod-review.md, 3.4). Se este recorte estiver errado, TODO
# finding do modo video sai deslocado no tempo.
GAME_CLOCK = Roi("game_clock", Anchor.TOP_RIGHT, dx=0.019, dy=0.004, w=0.047, h=0.024)

# Minimapa: quadrado no canto inferior direito. Usado para detectar icones de
# campeao (consciencia de mapa) — o unico ROI que vai para o VLM.
MINIMAP = Roi("minimap", Anchor.BOTTOM_RIGHT, dx=0.004, dy=0.004, w=0.258, h=0.258)

# Placar pessoal: KDA e CS ficam juntos, acima da barra de itens.
KDA = Roi("kda", Anchor.TOP_RIGHT, dx=0.078, dy=0.004, w=0.055, h=0.024)
CS = Roi("cs", Anchor.TOP_RIGHT, dx=0.140, dy=0.004, w=0.043, h=0.024)

# Ouro disponivel, embaixo no centro, ao lado dos slots de item.
GOLD = Roi("gold", Anchor.BOTTOM_CENTER, dx=0.243, dy=0.018, w=0.055, h=0.026)

# Barra de vida/mana do proprio campeao. E o que falta para analisar TROCA DE
# DANO — a telemetria so tem vida de minuto em minuto, inutil para uma troca
# de tres segundos (docs/06-advantage-engine.md, secao 8).
SELF_HEALTH = Roi("self_health", Anchor.BOTTOM_CENTER, dx=-0.118, dy=0.120, w=0.148, h=0.012)

# Slots de habilidade: cooldown e o que falta para analisar COMBO, ja que a
# timeline nao emite NENHUM evento de uso de habilidade.
ABILITIES = Roi("abilities", Anchor.BOTTOM_CENTER, dx=-0.036, dy=0.028, w=0.106, h=0.040)

ALL: tuple[Roi, ...] = (
    GAME_CLOCK,
    MINIMAP,
    KDA,
    CS,
    GOLD,
    SELF_HEALTH,
    ABILITIES,
)

BY_NAME = {r.name: r for r in ALL}

# Os unicos ROIs que valem OCR deterministico. Todo o resto ou vai para o VLM
# ou nao e lido — nunca se paga um VLM para ler um contador de quatro digitos.
OCR_ROIS: tuple[Roi, ...] = (GAME_CLOCK, CS, GOLD, KDA)


# --------------------------------------------------------------------------
# A HUD de ESPECTADOR (replay)
# --------------------------------------------------------------------------
#
# DESCOBERTA QUE INVALIDOU UMA PREMISSA: assistir um replay coloca voce em modo
# ESPECTADOR, cujo layout nao tem quase nada a ver com o do jogador.
#
#   jogador                          espectador
#   relogio no canto superior DIR    relogio no topo ao CENTRO
#   ouro e CS proprios embaixo       placar dos 10 jogadores embaixo
#   barra de habilidades             nao existe
#   vida do proprio campeao          nao existe
#
# Medido contra print real (1600x900, replay aos 14:39). Com o perfil de
# jogador, o ROI do relogio caia em cima do contador de FPS e lia "D"; o de KDA
# lia "FPS: | 31". Tudo errado, e errado em silencio.
#
# Isto importa mais do que parece: no Modo B (replay sincronizado) o usuario
# esta SEMPRE em espectador. Este e o perfil primario dali, e o de jogador so
# serve para o Modo C, com gravacao da propria tela em jogo.

SPEC_GAME_CLOCK = Roi(
    "game_clock", Anchor.TOP_CENTER, dx=0.004, dy=0.067, w=0.053, h=0.026
)
SPEC_TEAM_GOLD_LEFT = Roi(
    "team_gold_left", Anchor.TOP_CENTER, dx=-0.124, dy=0.014, w=0.060, h=0.021
)
SPEC_TEAM_GOLD_RIGHT = Roi(
    "team_gold_right", Anchor.TOP_CENTER, dx=0.164, dy=0.014, w=0.060, h=0.021
)
SPEC_MINIMAP = Roi(
    "minimap", Anchor.BOTTOM_RIGHT, dx=0.006, dy=0.011, w=0.239, h=0.239
)

SPECTATOR_ALL: tuple[Roi, ...] = (
    SPEC_GAME_CLOCK,
    SPEC_TEAM_GOLD_LEFT,
    SPEC_TEAM_GOLD_RIGHT,
    SPEC_MINIMAP,
)
SPECTATOR_OCR: tuple[Roi, ...] = (
    SPEC_GAME_CLOCK,
    SPEC_TEAM_GOLD_LEFT,
    SPEC_TEAM_GOLD_RIGHT,
)


@dataclass(frozen=True)
class HudProfile:
    """Um layout de HUD inteiro. O modo escolhe qual usar."""

    name: str
    rois: tuple[Roi, ...]
    ocr: tuple[Roi, ...]
    clock: Roi

    def by_name(self, nome: str) -> Roi | None:
        return next((r for r in self.rois if r.name == nome), None)


PLAYER = HudProfile("player", ALL, OCR_ROIS, GAME_CLOCK)
SPECTATOR = HudProfile("spectator", SPECTATOR_ALL, SPECTATOR_OCR, SPEC_GAME_CLOCK)
PROFILES = {p.name: p for p in (PLAYER, SPECTATOR)}


def aspect_ratio(width: int, height: int) -> float:
    return width / height


def is_ultrawide(width: int, height: int) -> bool:
    """Acima de ~2:1 a HUD ganha faixas laterais vazias.

    Nao muda o calculo — os ROIs sao ancorados nos cantos justamente para isso
    — mas serve de aviso na calibracao.
    """
    return aspect_ratio(width, height) > 2.0
