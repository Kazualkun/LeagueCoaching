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

from riftcoach.core.schema import Mark, Stroke
from riftcoach.overlay.atalhos import TODOS as ATALHOS
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
    match_id: str = ""
    # Ligar/desligar cada camada. A regua incomoda menos que o cartao, entao
    # quem quiser so o essencial desliga o cartao e fica com ela.
    show_ruler: bool = True
    show_card: bool = True
    show_minimap: bool = True
    # Os desenhos a mao deste instante, ja filtrados por quem chama, mais o
    # traco em andamento (que ainda nao virou `Stroke` porque o botao nao
    # soltou).
    strokes: list[Stroke] = field(default_factory=list)
    traco_em_andamento: list[tuple[float, float]] = field(default_factory=list)
    cor_do_pincel: str = ""
    espessura_do_pincel: float = 0.0
    modo_desenho: bool = False
    # Perguntar a IA. `pensando` existe separado de `resposta` porque a espera
    # e longa o bastante (alguns segundos) para que uma tela sem sinal nenhum
    # pareca travada — e no modo pergunta o clique nao chega ao jogo, entao
    # parecer travado e especialmente ruim.
    modo_pergunta: bool = False
    texto_da_pergunta: str = ""
    resposta: str = ""
    pensando: bool = False

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
    espectador deixa livre de ponta a ponta — e uma linha do tempo PRECISA da
    largura toda, senao deixa de ser uma linha do tempo.

    A primeira versao era fina demais e muda: oito pixels de listra colorida
    no canto de cima, sem rotulo nenhum. Quem via nao entendia que aquilo era
    a partida inteira, nem o que as cores queriam dizer — e com razao. O que
    conserta isso nao e cor mais forte, e CONTEXTO:

      - o nome do programa na ponta esquerda, para as listras terem dono;
      - o placar de marcacoes e o proximo erro na ponta direita, que e o que
        a pessoa quer saber ("falta muito?");
      - divisoes a cada 5 minutos, que transformam uma faixa em uma escala;
      - um cursor com cabeca redonda, que se acha de relance.

    As duas pontas ficam nos cantos que o placar do espectador nao alcanca
    (ele comeca por volta de 21% da largura e acaba por volta de 80%).
    """
    if not st.show_ruler or st.duration_ms <= 0:
        return []
    u = st.u
    alt = max(8.0, 0.016 * u)
    larg_tela = float(st.width)
    fonte = max(8.0, 0.0115 * u)

    def x_de(t_ms: int) -> float:
        return larg_tela * min(1.0, max(0.0, t_ms / st.duration_ms))

    out: list[Primitive] = [Box(Rect(0.0, 0.0, larg_tela, alt), fill="#0b0e14")]

    # A escala: uma divisao a cada 5 minutos. Sem elas a faixa nao diz que
    # representa tempo; com elas, diz sozinha.
    for minuto in range(5, st.duration_ms // 60_000 + 1, 5):
        x = x_de(minuto * 60_000)
        out.append(Line(x, alt * 0.45, x, alt, color="#30363d", width=1.0))

    for m in st.marks:
        x = x_de(m.t_ms)
        grave = m.kind == "critical" or (m.severity or 0) >= 4
        # Marcacao critica ganha o dobro de largura. E a unica diferenca de
        # tamanho na regua; o resto e so cor, para nao virar enfeite.
        meia = max(2.0, 0.0028 * u) * (2 if grave else 1)
        out.append(Box(Rect(x - meia, 0.0, meia * 2, alt), fill=cor_da_marca(m)))

    # O cursor: haste mais a cabeca redonda logo abaixo da faixa. A cabeca e o
    # que permite achar o cursor sem procurar, porque nada mais na tela e um
    # circulo branco solido naquela altura.
    xc = x_de(now_ms)
    out.append(Line(xc, 0.0, xc, alt, color="#ffffff", width=max(2.0, 0.0025 * u)))
    out.append(Circle(xc, alt + max(2.0, 0.003 * u), max(3.0, 0.0042 * u), fill="#ffffff"))

    y_rotulo = alt + max(4.0, 0.006 * u)
    out.append(
        Label(
            max(4.0, 0.004 * u),
            y_rotulo,
            "RIFTCOACH",
            color=COR_FRACO,
            size=fonte,
            bold=True,
        )
    )

    ia = [m for m in st.marks if m.author == "ai"]
    passados = sum(1 for m in ia if m.t_ms <= now_ms)
    direita = f"{passados}/{len(ia)} erros" if ia else "sem erros marcados"
    prox = _proxima(st, now_ms)
    if prox is not None:
        direita += f"   ·   próximo em {mmss(prox[0].t_ms)}"
    out.append(
        Label(
            larg_tela - max(4.0, 0.004 * u),
            y_rotulo,
            direita,
            color=COR_TEXTO,
            size=fonte,
            anchor="ne",
        )
    )
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
    # O motor de regras as vezes ja escreve o custo dentro da propria frase
    # ("Perdeu dragon em 8:41 custou 6pp."). Repetir logo abaixo, arredondado
    # de outro jeito, fica pior do que nao dizer: parecem dois numeros
    # diferentes para a mesma coisa.
    mostrar_custo = bool(m.wp_loss) and "pp." not in m.text and "pontos" not in m.text
    # 3.1 linhas de folga: titulo, categoria, e o par contagem + barra no pe.
    # Medido contra a tela: com 2.6 a barra encostava na borda de baixo e
    # parecia corte de renderizacao, nao elemento.
    altura = pad * 2 + linhas_h * (len(corpo) + 3.1)
    if mostrar_custo:
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

    if mostrar_custo and m.wp_loss:
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


def _boas_vindas(st: OverlayState, now_ms: int) -> list[Primitive]:
    """O cartao que aparece nos primeiros segundos, e so neles.

    Existe por causa de um jeito especifico de o overlay parecer quebrado sem
    estar: a pessoa abre o replay do inicio, as marcacoes estao aos 12, aos 18
    e aos 24 minutos, e a tela fica — corretamente — vazia. Sem nada que diga
    "estou aqui e conectado", a conclusao razoavel e que nao funcionou.

    Entao ele responde as tres perguntas daquele momento, nesta ordem:
    conectou? quantos erros tem? o que essas cores significam? E termina
    dizendo como pular para o primeiro, que e o que a pessoa vai querer fazer
    em seguida.
    """
    u = st.u
    fonte = 0.0155 * u
    pad = 0.014 * u
    linha = fonte * 1.5
    larg = 0.37 * st.width
    x0 = 0.105 * st.width
    y0 = 0.13 * st.height

    ia = [m for m in st.marks if m.author == "ai"]
    criticos = sum(1 for m in ia if m.kind == "critical" or (m.severity or 0) >= 4)
    primeira = min((m.t_ms for m in ia), default=None)

    legenda = [
        (COR_CRITICO, "custou a partida"),
        (COR_ERRO, "erro claro"),
        (COR_LEVE, "detalhe a corrigir"),
        (COR_USUARIO, "suas marcações"),
    ]
    # A explicacao do minimapa fica aqui E nos rotulos do proprio mapa. Quem
    # chega no minuto vinte nunca viu este cartao.
    mapa = "no minimapa: VOCÊ = onde você estava  ·  AQUI = onde a jogada aconteceu"
    # TODOS os atalhos, e nao uma amostra. Este painel e a unica documentacao
    # que a pessoa tem enquanto assiste, e uma lista parcial faz procurar no
    # manual — que e exatamente o que ela nao vai fazer no meio de um replay.
    atalhos = [(a.tecla, a.descricao) for a in ATALHOS]
    altura = pad * 2 + linha * (4.5 + len(legenda) + len(atalhos))
    caixa = Rect(x0, y0, larg, altura)

    out: list[Primitive] = [
        Box(caixa, fill=COR_FUNDO, outline=COR_BOM, width=max(1.5, 0.0022 * u)),
        Box(Rect(x0, y0, max(3.0, 0.005 * u), altura), fill=COR_BOM),
    ]
    tx, ty = x0 + pad, y0 + pad
    out.append(Label(tx, ty, "RIFTCOACH · AJUDA", color=COR_BOM, size=fonte, bold=True))
    out.append(
        Label(
            x0 + larg - pad,
            ty,
            "Ctrl+Alt+A abre e fecha",
            color=COR_FRACO,
            size=fonte * 0.82,
            anchor="ne",
        )
    )
    ty += linha

    if ia:
        resumo = f"{len(ia)} erros marcados, {criticos} deles graves"
    else:
        resumo = "nenhum erro marcado nesta partida"
    out.append(Label(tx, ty, resumo, color=COR_TEXTO, size=fonte))
    ty += linha * 1.25

    # A legenda e a razao de o cartao existir. Listra colorida sem legenda e
    # decoracao; com legenda, vira informacao.
    for cor, texto in legenda:
        out.append(Box(Rect(tx, ty + fonte * 0.18, fonte * 0.75, fonte * 0.75), fill=cor))
        out.append(Label(tx + fonte * 1.25, ty, texto, color=COR_FRACO, size=fonte * 0.92))
        ty += linha
    ty += linha * 0.25

    out.append(Label(tx, ty, mapa, color=COR_FRACO, size=fonte * 0.86))
    ty += linha * 1.3

    for tecla, texto in atalhos:
        out.append(Label(tx, ty, tecla, color=COR_TEXTO, size=fonte * 0.92, bold=True))
        # 8 unidades de fonte para a coluna da tecla. Medido na tela, nao
        # estimado: "Ctrl+Alt+S" em negrito ocupa cerca de 7, e com 6,2 a
        # descricao encostava nela.
        out.append(Label(tx + fonte * 8.0, ty, texto, color=COR_FRACO, size=fonte * 0.92))
        ty += linha

    if primeira is not None:
        rodape = (
            f"o primeiro erro é em {mmss(primeira)}"
            if primeira > now_ms
            else f"o próximo erro depois daqui é em {mmss(primeira)}"
        )
        out.append(
            Label(
                x0 + larg - pad,
                y0 + altura - pad * 0.7,
                rodape,
                color=COR_BOM,
                size=fonte * 0.88,
                anchor="se",
            )
        )
    return out


def _minimapa(st: OverlayState, m: Mark | None) -> list[Primitive]:
    """O mapa da jogada marcada. So aparece junto com o cartao.

    A VERSAO ANTERIOR ESTAVA ERRADA e vale dizer por que, porque o erro e
    tentador: ela desenhava um anel branco seguindo o jogador o tempo todo,
    interpolado entre frames de 60 em 60 segundos. Duas coisas ruins de uma
    vez — o anel discordava visivelmente do icone que o proprio jogo desenha,
    e nao explicava nada, porque o jogo JA mostra onde voce esta. Quem viu
    perguntou o que era aquela bola branca, e a pergunta estava certa.

    O que o jogo NAO mostra e a relacao entre dois lugares num instante
    passado: onde a jogada aconteceu e onde voce estava. Isso e exatamente a
    pergunta de macro — "o barao caiu enquanto eu empurrava a top" — e e o que
    este desenho responde agora.

    Cada elemento vem rotulado NA TELA. Legenda em cartao que some depois de
    dez segundos nao serve para quem chegou no minuto vinte.
    """
    if not st.show_minimap or m is None:
        return []
    if m.where is None and m.you is None:
        return []

    u = st.u
    proj = MinimapProjector(
        minimap_rect(st.width, st.height, st.hud_scale), rotated=st.minimap_rotated
    )
    cor = cor_da_marca(m)
    fonte = max(8.0, 0.0125 * u)
    out: list[Primitive] = []

    onde = proj.to_px(*m.where) if m.where else None
    voce = proj.to_px(*m.you) if m.you else None

    # A linha entre os dois pontos E a informacao: o comprimento dela e "o
    # quanto voce estava longe". Desenhada primeiro para ficar por baixo.
    if onde and voce:
        out.append(Line(voce[0], voce[1], onde[0], onde[1], color=cor, width=1.5, dash=True))

    def centrado(x: float, texto: str) -> float:
        """Prende o rotulo na tela, na horizontal.

        O minimapa fica colado no canto inferior direito, entao um ponto
        proximo da borda joga metade do texto para fora — e no canto de baixo
        nao ha para onde rolar. Meio caractere por 0,35 do tamanho da fonte e
        uma estimativa grosseira de largura, e grosseira basta: o erro e de
        poucos pixels e sempre para dentro.
        """
        meia = len(texto) * fonte * 0.35
        return min(max(x, meia + 2), st.width - meia - 2)

    def acima_ou_abaixo(y: float, r: float, preferir_abaixo: bool) -> tuple[float, Anchor]:
        """Escolhe o lado que cabe.

        Metade do minimapa encosta na borda de baixo da tela, entao um rotulo
        sempre posto abaixo do ponto sai fora em metade dos casos. Prefere-se o
        lado pedido e troca-se quando nao ha espaco.
        """
        folga = fonte * 1.6
        if preferir_abaixo and y + r + folga < st.height - 2:
            return y + r + fonte * 0.2, "n"
        if not preferir_abaixo and y - r - folga > 2:
            return y - r - fonte * 0.2, "s"
        # O lado preferido nao cabe: vai para o outro.
        return (y - r - fonte * 0.2, "s") if preferir_abaixo else (y + r + fonte * 0.2, "n")

    if voce:
        # Area, nao ponto. A posicao do jogador vem de um frame por minuto:
        # circulo pequeno fingiria uma precisao que o dado nao tem.
        r = max(7.0, 0.014 * u)
        out.append(Circle(voce[0], voce[1], r, outline="#ffffff", width=max(1.5, 0.002 * u)))
        ly, anc = acima_ou_abaixo(voce[1], r, preferir_abaixo=False)
        out.append(
            Label(
                centrado(voce[0], "VOCÊ"),
                ly,
                "VOCÊ",
                color="#ffffff",
                size=fonte,
                bold=True,
                anchor=anc,
            )
        )

    if onde:
        r = max(4.0, 0.007 * u)
        out.append(Circle(onde[0], onde[1], r, fill=cor, outline="#000000", width=1.0))
        ly, anc = acima_ou_abaixo(onde[1], r, preferir_abaixo=True)
        out.append(
            Label(
                centrado(onde[0], "AQUI"),
                ly,
                "AQUI",
                color=cor,
                size=fonte,
                bold=True,
                anchor=anc,
            )
        )
    return out


# --------------------------------------------------------------------------


def _desenhos(st: OverlayState) -> list[Primitive]:
    """Os tracos a mao, convertidos de fracao da tela para pixel.

    A conversao acontece aqui, e so aqui, porque este e o unico ponto que
    conhece a resolucao atual. Guardar pixel na revisao faria o desenho
    encolher num canto ao reabrir o replay noutra tela.
    """
    out: list[Primitive] = []
    largura, altura = float(st.width), float(st.height)

    def em_pixel(pontos: list[tuple[float, float]], cor: str, esp: float) -> None:
        for (x1, y1), (x2, y2) in pairwise(pontos):
            out.append(
                Line(x1 * largura, y1 * altura, x2 * largura, y2 * altura, color=cor, width=esp)
            )

    for s in st.strokes:
        em_pixel(list(s.points), s.color, s.width)
    if len(st.traco_em_andamento) >= 2:
        em_pixel(
            st.traco_em_andamento,
            st.cor_do_pincel or COR_CRITICO,
            st.espessura_do_pincel or 4.0,
        )
    return out


def _barra_do_pincel(st: OverlayState) -> list[Primitive]:
    """A faixa que aparece enquanto o pincel esta ligado.

    Ela tem de ser inconfundivel: no modo desenho o clique NAO chega mais ao
    jogo, e alguem que nao perceba que entrou nele vai achar que o League
    travou.
    """
    u = st.u
    fonte = max(9.0, 0.016 * u)
    alt = fonte * 2.6
    y = st.height - alt
    out: list[Primitive] = [
        Box(Rect(0.0, y, float(st.width), alt), fill="#161b22", outline=COR_CRITICO, width=2.0)
    ]
    x = 0.02 * st.width
    meio = y + alt / 2
    out.append(
        Label(x, meio, "PINCEL LIGADO", color=COR_CRITICO, size=fonte, bold=True, anchor="w")
    )
    # 12 unidades de fonte: "PINCEL LIGADO" em negrito ocupa perto de 10, e
    # com 9,5 as bolinhas de cor encostavam no texto.
    x += fonte * 12.0

    from riftcoach.overlay.desenho import CORES

    for i, (cor, _) in enumerate(CORES):
        r = fonte * (0.62 if cor != st.cor_do_pincel else 0.85)
        out.append(
            Circle(
                x,
                meio,
                r,
                fill=cor,
                outline="#ffffff" if cor == st.cor_do_pincel else "#000000",
                width=2.0,
            )
        )
        out.append(
            Label(x, meio + fonte * 1.15, str(i + 1), color=COR_FRACO, size=fonte * 0.7, anchor="n")
        )
        x += fonte * 2.1

    out.append(
        Label(
            float(st.width) - 0.02 * st.width,
            meio,
            "arraste para desenhar  ·  Z desfaz  ·  C limpa  ·  Ctrl+Alt+D sai",
            color=COR_TEXTO,
            size=fonte * 0.8,
            anchor="e",
        )
    )
    return out


# Teto de linhas da resposta na tela. A IA e instruida a responder em dois a
# quatro paragrafos, o que costuma caber; o teto existe para o caso em que ela
# nao obedece — um painel que cresce sem limite cobriria o jogo inteiro, que e
# exatamente o que a pessoa esta tentando assistir.
MAX_LINHAS_DA_RESPOSTA = 9


def _painel_da_pergunta(st: OverlayState) -> list[Primitive]:
    """A caixa de perguntar a IA, no rodape.

    Mesma regra do pincel: no modo pergunta o clique e o teclado NAO chegam
    mais ao jogo, entao a faixa precisa ser inconfundivel — quem nao perceber
    que entrou nele vai achar que o League travou.
    """
    u = st.u
    fonte = max(9.0, 0.015 * u)
    margem = 0.02 * st.width
    # 0,636 saiu de medicao, nao de chute: e a largura media do caractere em
    # pixels por ponto de fonte, no corpo (que desenha a 0,92 da fonte base).
    # Estava 0,52 aqui, e a primeira figura gerada mostrou a resposta saindo
    # pela direita da tela, cortada no meio da palavra.
    larg_ch = max(30, int((st.width - 2 * margem) / (fonte * 0.92 * 0.64)))

    if st.pensando:
        corpo = ["pensando..."]
    elif st.resposta:
        corpo = _quebrar(st.resposta, larg_ch)[:MAX_LINHAS_DA_RESPOSTA]
    else:
        corpo = []

    alt_corpo = len(corpo) * fonte * 1.4 + (fonte * 0.8 if corpo else 0.0)
    # 4,6 e nao 4,0: com 4,0 a ultima linha da resposta encostava na borda de
    # baixo da tela na figura gerada, a poucos pixels de ser cortada.
    alt = fonte * 4.6 + alt_corpo
    y = st.height - alt
    out: list[Primitive] = [
        Box(Rect(0.0, y, float(st.width), alt), fill=COR_FUNDO, outline=COR_PERGUNTA, width=2.0)
    ]

    x = margem
    linha = y + fonte * 1.3
    out.append(
        Label(x, linha, "PERGUNTAR À IA", color=COR_PERGUNTA, size=fonte, bold=True, anchor="w")
    )
    # O cursor piscando sairia caro (exige relogio no desenho, que e puro);
    # uma barra fixa diz a mesma coisa: "o texto entra aqui".
    digitado = st.texto_da_pergunta or "digite a sua pergunta"
    cor_digitado = COR_TEXTO if st.texto_da_pergunta else COR_FRACO
    # A linha digitada tem conta propria: comeca depois do titulo e usa a
    # fonte cheia, nao a do corpo. Com a largura do corpo ela saia pela
    # direita a partir de uns 160 caracteres — e o limite e 600.
    recuo = fonte * 12.6
    larg_digitado = max(10, int((st.width - 2 * margem - recuo) / (fonte * 0.64)) - 1)
    out.append(
        Label(
            x + recuo,
            linha,
            f"{digitado[-larg_digitado:]}▌" if st.texto_da_pergunta else digitado,
            color=cor_digitado,
            size=fonte,
            anchor="w",
        )
    )

    linha += fonte * 1.5
    out.append(
        Label(
            x,
            linha,
            "Enter pergunta · Esc fecha · Backspace apaga",
            color=COR_FRACO,
            size=fonte * 0.78,
            anchor="w",
        )
    )

    linha += fonte * 1.2
    for texto in corpo:
        linha += fonte * 1.4
        out.append(
            Label(
                x,
                linha,
                texto,
                color=COR_FRACO if st.pensando else COR_TEXTO,
                size=fonte * 0.92,
                anchor="w",
            )
        )
    return out


def build(st: OverlayState, now_ms: int, *, boas_vindas: bool = False) -> Scene:
    """A cena inteira para este instante.

    `boas_vindas` e decidido por quem chama, com relogio de PAREDE — e nao
    aqui, com o relogio do replay. Os dois nao tem nada a ver um com o outro:
    a pessoa pode abrir o overlay com o replay parado, e o cartao de
    apresentacao ainda assim precisa sumir sozinho depois de alguns segundos.
    Manter a decisao fora daqui e o que deixa esta funcao pura e testavel.
    """
    sc = Scene()
    sc.add(*_regua(st, now_ms))
    # Os desenhos ficam por BAIXO dos cartoes: eles apontam para o jogo, e um
    # rabisco cruzando o texto do diagnostico atrapalha os dois.
    sc.add(*_desenhos(st))
    if st.modo_desenho:
        sc.add(*_barra_do_pincel(st))
    if st.modo_pergunta:
        sc.add(*_painel_da_pergunta(st))
    if boas_vindas:
        # Ele ocupa o lugar do cartao de erro, e por isso suprime os dois. Nos
        # primeiros segundos "o que e isto" importa mais que qualquer erro —
        # e empilhar os dois cobriria o jogo inteiro.
        sc.add(*_boas_vindas(st, now_ms))
        return sc
    m = ativa(st, now_ms) if st.show_card else None
    # O minimapa acompanha o cartao: os dois falam da MESMA jogada, e um sem o
    # outro vira enfeite — pontos no mapa sem explicacao, ou explicacao sem
    # lugar.
    sc.add(*_minimapa(st, m))
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
