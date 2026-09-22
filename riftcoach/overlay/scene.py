"""Estado + instante -> o que desenhar. Sem tkinter, sem win32, sem rede.

Este modulo e a parte que pode estar errada, entao e a parte que da para
testar sem abrir o jogo. `render.py` so sabe pintar retangulo, circulo, linha
e texto; toda a decisao de O QUE aparece e QUANDO mora aqui.

A decisao de UX que organiza o resto: O CARTAO APARECE ANTES DO ERRO, NAO
DEPOIS. O seek ja leva o replay para 8 s antes da decisao (controller.py,
LEAD_IN_S) — se o cartao so surgisse no instante marcado, a pessoa leria o
diagnostico depois de ja ter visto o desfecho, que e a ordem errada de
aprender. Entao ele entra junto com o seek, e uma barrinha mostra quanto falta
para o momento. A pessoa le, olha, e ve acontecer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise
from typing import Literal

from riftcoach.core.schema import Mark
from riftcoach.overlay.geometry import MinimapProjector, Rect, minimap_rect

# --------------------------------------------------------------------------
# Primitivas
# --------------------------------------------------------------------------

Color = str  # "#rrggbb"

# Os mesmos nomes que o Canvas do tkinter aceita, para que o renderizador nao
# precise traduzir nem validar nada.
Anchor = Literal["nw", "n", "ne", "w", "center", "e", "sw", "s", "se"]


@dataclass(frozen=True)
class Box:
    rect: Rect
    fill: Color | None = None
    outline: Color | None = None
    width: float = 1.0


@dataclass(frozen=True)
class Circle:
    x: float
    y: float
    r: float
    fill: Color | None = None
    outline: Color | None = None
    width: float = 1.0


@dataclass(frozen=True)
class Line:
    x1: float
    y1: float
    x2: float
    y2: float
    color: Color = "#ffffff"
    width: float = 1.0
    dash: bool = False


@dataclass(frozen=True)
class Label:
    x: float
    y: float
    text: str
    color: Color = "#ffffff"
    size: float = 12.0
    bold: bool = False
    anchor: Anchor = "nw"


Primitive = Box | Circle | Line | Label


@dataclass
class Scene:
    items: list[Primitive] = field(default_factory=list)

    def add(self, *p: Primitive) -> None:
        self.items.extend(p)


# --------------------------------------------------------------------------
# Paleta e tempos
# --------------------------------------------------------------------------

# Cores por gravidade. Vermelho so para o que realmente custou a partida: se
# tudo e vermelho, nada e vermelho, e a pessoa para de olhar.
COR_CRITICO = "#ff453a"
COR_ERRO = "#ff9f0a"
COR_LEVE = "#ffd60a"
COR_USUARIO = "#0a84ff"
COR_PERGUNTA = "#bf5af0"
COR_BOM = "#30d158"
COR_FUNDO = "#0d1117"
COR_TEXTO = "#e6edf3"
COR_FRACO = "#8b949e"

# A cor de fundo da janela inteira, que o Windows torna invisivel. Precisa ser
# uma cor que NAO apareca em nada que a gente desenhe — por isso este magenta
# absurdo, e nao preto: preto e cor legitima de sombra e de contorno.
COR_TRANSPARENTE = "#ff00ff"

# O cartao entra junto com o seek e sai um pouco depois do momento.
ANTES_MS = 8_000
DEPOIS_MS = 5_000

# A regua avisa antes mesmo do cartao, para quem esta assistindo corrido.
AVISO_MS = 15_000


def cor_da_marca(m: Mark) -> Color:
    if m.author == "user":
        return COR_PERGUNTA if m.kind == "question" else COR_USUARIO
    if m.kind == "good":
        return COR_BOM
    if m.kind == "critical" or (m.severity or 0) >= 4:
        return COR_CRITICO
    if (m.severity or 0) >= 3:
        return COR_ERRO
    return COR_LEVE


def mmss(ms: int) -> str:
    s = max(0, ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


# --------------------------------------------------------------------------
# Estado
# --------------------------------------------------------------------------


@dataclass
class OverlayState:
    """Tudo o que o desenho precisa saber, e nada mais.

    Nao guarda cliente, nem sessao, nem conexao: assim o teste monta um estado
    na mao e confere o desenho, sem jogo aberto e sem rede.
    """

    width: int
    height: int
    duration_ms: int
    marks: list[Mark] = field(default_factory=list)
    hud_scale: float = 1.0
    minimap_rotated: bool = False
    # Posicao do jogador em foco por instante da timeline, um ponto por frame
    # (a Riot so entrega um a cada 60 s). Vazio = sem anotacao de minimapa.
    focus_track: list[tuple[int, float, float]] = field(default_factory=list)
    focus_champion: str = ""
    # Ligar/desligar cada camada. A regua incomoda menos que o cartao, entao
    # quem quiser so o essencial desliga o cartao e fica com ela.
    show_ruler: bool = True
    show_card: bool = True
    show_minimap: bool = True

    @property
    def u(self) -> float:
        """A unidade de layout: altura da tela. Tudo escala junto com ela."""
        return float(self.height)


# --------------------------------------------------------------------------
# Camadas
# --------------------------------------------------------------------------


def _regua(st: OverlayState, now_ms: int) -> list[Primitive]:
    """A faixa de marcacoes colada no topo da tela.

    Vive nos primeiros pixels porque e a unica faixa larga que a HUD de
    espectador deixa livre de ponta a ponta. O placar comeca mais abaixo
    (SPEC_TEAM_GOLD esta em dy=0.014, ou seja 12 px em 900p).
    """
    if not st.show_ruler or st.duration_ms <= 0:
        return []
    u = st.u
    alt = max(4.0, 0.009 * u)
    out: list[Primitive] = [Box(Rect(0.0, 0.0, float(st.width), alt), fill="#161b22")]

    def x_de(t_ms: int) -> float:
        return st.width * min(1.0, max(0.0, t_ms / st.duration_ms))

    for m in st.marks:
        x = x_de(m.t_ms)
        grave = m.kind == "critical" or (m.severity or 0) >= 4
        # Marcacao critica ganha o dobro de largura. E a unica diferenca de
        # tamanho na regua; o resto e so cor, para nao virar enfeite.
        larg = max(2.0, 0.0035 * u) * (2 if grave else 1)
        out.append(Box(Rect(x - larg / 2, 0.0, larg, alt), fill=cor_da_marca(m)))

    xc = x_de(now_ms)
    out.append(Line(xc, 0.0, xc, alt * 1.9, color="#ffffff", width=max(1.5, 0.002 * u)))
    return out


def _proxima(st: OverlayState, now_ms: int) -> tuple[Mark, int] | None:
    """A proxima marcacao da IA e quanto falta. Marcacao do usuario nao avisa —
    ele mesmo colocou, ja sabe que esta la."""
    futuras = [m for m in st.marks if m.author == "ai" and m.t_ms > now_ms]
    if not futuras:
        return None
    m = min(futuras, key=lambda x: x.t_ms)
    return m, m.t_ms - now_ms


def ativa(st: OverlayState, now_ms: int) -> Mark | None:
    """A marcacao cujo cartao deve estar na tela agora.

    Quando duas janelas se sobrepoem — coisa comum numa teamfight — vence a
    mais grave, e no empate a mais proxima. Mostrar as duas empilhadas cobriria
    o jogo justamente no momento em que a pessoa precisa ver o jogo.
    """
    cands = [m for m in st.marks if m.t_ms - ANTES_MS <= now_ms <= m.t_ms + DEPOIS_MS]
    if not cands:
        return None
    return max(cands, key=lambda m: ((m.severity or 0), -abs(m.t_ms - now_ms)))


def _quebrar(texto: str, largura_ch: int) -> list[str]:
    """Quebra por palavra. Sem hifenizacao: palavra cortada em overlay fica
    ilegivel em movimento."""
    linhas: list[str] = []
    atual = ""
    for palavra in texto.split():
        cand = f"{atual} {palavra}".strip()
        if len(cand) <= largura_ch:
            atual = cand
        else:
            if atual:
                linhas.append(atual)
            atual = palavra
    if atual:
        linhas.append(atual)
    return linhas


def _cartao(st: OverlayState, now_ms: int, m: Mark, indice: int) -> list[Primitive]:
    u = st.u
    cor = cor_da_marca(m)
    fonte = 0.0165 * u
    pad = 0.014 * u
    larg = 0.30 * st.width
    ch = max(20, int(larg / (fonte * 0.52)))  # ~0,52 de largura media por caractere

    corpo = _quebrar(m.text, ch)
    if m.author == "ai":
        titulo = f"ERRO #{indice}" if m.kind != "good" else f"ACERTO #{indice}"
        sub = (m.category or "").upper()
    else:
        titulo = "SUA MARCAÇÃO"
        sub = m.kind.upper()

    linhas_h = fonte * 1.45
    # 3.1 linhas de folga: titulo, categoria, e o par contagem + barra no pe.
    # Medido contra a tela: com 2.6 a barra encostava na borda de baixo e
    # parecia corte de renderizacao, nao elemento.
    altura = pad * 2 + linhas_h * (len(corpo) + 3.1)
    if m.wp_loss:
        altura += linhas_h
    x0 = 0.105 * st.width  # a coluna de jogadores do espectador acaba em ~9%
    y0 = 0.13 * st.height
    caixa = Rect(x0, y0, larg, altura)

    out: list[Primitive] = [
        Box(caixa, fill=COR_FUNDO, outline=cor, width=max(1.5, 0.0022 * u)),
        # Faixa lateral na cor da gravidade: identifica o cartao pela cor antes
        # de qualquer leitura.
        Box(Rect(x0, y0, max(3.0, 0.005 * u), altura), fill=cor),
    ]
    tx = x0 + pad
    ty = y0 + pad
    out.append(Label(tx, ty, titulo, color=cor, size=fonte * 1.05, bold=True))
    out.append(Label(x0 + larg - pad, ty, mmss(m.t_ms), color=COR_FRACO, size=fonte, anchor="ne"))
    ty += linhas_h
    if sub:
        out.append(Label(tx, ty, sub, color=COR_FRACO, size=fonte * 0.82, bold=True))
    ty += linhas_h * 0.95

    for ln in corpo:
        out.append(Label(tx, ty, ln, color=COR_TEXTO, size=fonte))
        ty += linhas_h

    if m.wp_loss:
        out.append(
            Label(
                tx,
                ty,
                f"custou {m.wp_loss:.1f} pontos de vitória",
                color=cor,
                size=fonte * 0.88,
                bold=True,
            )
        )
        ty += linhas_h

    # A contagem ate o momento. Enquanto ela corre, a pessoa le; quando zera,
    # ela olha. Depois do instante vira "AGORA" e o cartao so termina de sair.
    falta = m.t_ms - now_ms
    barra_y = y0 + altura - pad * 1.15
    barra = Rect(tx, barra_y, larg - pad * 2, max(2.0, 0.003 * u))
    out.append(Box(barra, fill="#30363d"))
    if falta > 0:
        frac = 1.0 - falta / ANTES_MS
        out.append(Box(Rect(tx, barra_y, barra.w * max(0.0, frac), barra.h), fill=cor))
        out.append(
            Label(
                x0 + larg - pad,
                barra_y - fonte * 1.25,
                f"o momento em {falta / 1000:.0f}s",
                color=COR_FRACO,
                size=fonte * 0.8,
                anchor="ne",
            )
        )
    else:
        out.append(Box(barra, fill=cor))
        out.append(
            Label(
                x0 + larg - pad,
                barra_y - fonte * 1.25,
                "AGORA",
                color=cor,
                size=fonte * 0.8,
                bold=True,
                anchor="ne",
            )
        )
    return out


def _aviso(st: OverlayState, now_ms: int) -> list[Primitive]:
    """Chamada discreta para quem esta assistindo sem o cartao aberto."""
    prox = _proxima(st, now_ms)
    if prox is None:
        return []
    m, falta = prox
    if not (ANTES_MS < falta <= AVISO_MS):
        return []
    return [
        Label(
            st.width / 2,
            0.028 * st.height,
            f"próxima marcação em {falta // 1000}s  ·  {mmss(m.t_ms)}",
            color=cor_da_marca(m),
            size=0.014 * st.u,
            anchor="n",
        )
    ]


def _posicao_em(st: OverlayState, t_ms: int) -> tuple[float, float] | None:
    """Posicao do jogador em foco, interpolada entre frames.

    A Riot so entrega uma posicao por MINUTO. Interpolar linearmente entre dois
    pontos separados por 60 s e uma mentira util: acerta o lado do mapa, erra a
    rota exata. Por isso o desenho que usa isto e um circulo largo de atencao e
    nunca uma seta dizendo "voce estava exatamente aqui".
    """
    tr = st.focus_track
    if not tr:
        return None
    if t_ms <= tr[0][0]:
        return tr[0][1], tr[0][2]
    if t_ms >= tr[-1][0]:
        return tr[-1][1], tr[-1][2]
    for (ta, xa, ya), (tb, xb, yb) in pairwise(tr):
        if ta <= t_ms <= tb:
            f = (t_ms - ta) / (tb - ta) if tb > ta else 0.0
            return xa + f * (xb - xa), ya + f * (yb - ya)
    return None


def _minimapa(st: OverlayState, now_ms: int) -> list[Primitive]:
    if not st.show_minimap:
        return []
    pos = _posicao_em(st, now_ms)
    if pos is None:
        return []
    proj = MinimapProjector(
        minimap_rect(st.width, st.height, st.hud_scale), rotated=st.minimap_rotated
    )
    px, py = proj.to_px(*pos)
    u = st.u
    out: list[Primitive] = [
        # Anel largo em volta do icone. O icone do campeao ja cobre ~1000 u no
        # minimapa, e a projecao erra ate ~370 u: um anel folgado diz a verdade,
        # um ponto de 2 px fingiria uma precisao que nao existe.
        Circle(px, py, max(7.0, 0.013 * u), outline="#ffffff", width=max(1.5, 0.002 * u)),
    ]
    anterior = _posicao_em(st, now_ms - 60_000)
    if anterior is not None:
        ax, ay = proj.to_px(*anterior)
        if abs(ax - px) + abs(ay - py) > 3:
            out.append(Line(ax, ay, px, py, color="#ffffff", width=1.5, dash=True))
    return out


# --------------------------------------------------------------------------


def build(st: OverlayState, now_ms: int) -> Scene:
    """A cena inteira para este instante."""
    sc = Scene()
    sc.add(*_regua(st, now_ms))
    sc.add(*_minimapa(st, now_ms))
    m = ativa(st, now_ms) if st.show_card else None
    if m is not None:
        ia = sorted(
            (x for x in st.marks if x.author == "ai"),
            key=lambda x: (-(x.severity or 0), x.t_ms),
        )
        indice = ia.index(m) + 1 if m in ia else 0
        sc.add(*_cartao(st, now_ms, m, indice))
    else:
        sc.add(*_aviso(st, now_ms))
    return sc
