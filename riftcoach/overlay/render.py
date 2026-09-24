"""Primitivas -> pixels. tkinter, que ja vem com o Python.

A escolha de tkinter e deliberada e vale explicar, porque a primeira reacao de
quem le e "por que nao Qt". Tres motivos, nesta ordem:

1. Ele ja esta instalado. Qt sao 150 MB de download para desenhar circulo e
   texto, e este projeto precisa caber na vida de quem so quer melhorar de
   elo.
2. No Windows, `-transparentcolor` faz exatamente o que o overlay precisa —
   a cor-chave vira buraco de verdade, nao vidro fosco.
3. A maquina alvo e fraca. Este projeto foi desenvolvido e medido numa Intel
   HD 4000, e um Canvas com algumas dezenas de itens a 10 fps nao custa nada
   nela.

O preco e nao ter antialias. Para circulo, retangulo e texto de overlay isso
nao aparece; se um dia entrar desenho em espaco 3D, ai sim vale trocar.
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from collections.abc import Callable
from tkinter import font as tkfont

from riftcoach.overlay import window as win
from riftcoach.overlay.geometry import Rect
from riftcoach.overlay.scene import (
    COR_TRANSPARENTE,
    Box,
    Circle,
    Label,
    Line,
    Path,
    Scene,
)

FAMILIA = "Segoe UI"


class OverlayWindow:
    """Uma janela sem borda, sempre no topo, que o mouse atravessa.

    DUAS JANELAS, e a segunda e o conserto do pincel. A janela do overlay usa
    uma cor-chave: pixel daquela cor vira buraco, e o Windows manda o clique
    do buraco para o jogo embaixo. Para desenhar, a versao anterior trocava o
    fundo por uma cor opaca — o que resolvia o mouse, mas COBRIA O JOGO com
    uma tela escura justamente enquanto a pessoa tentava rabiscar sobre ele.

    Agora o overlay nunca deixa de atravessar o clique. Quem recebe o mouse e
    o teclado no pincel e na digitacao e a LOUSA: uma segunda janela, do
    mesmo tamanho, com opacidade de 1%. Ela e invisivel na pratica, mas nao e
    buraco — entao o clique para nela. O overlay fica por cima e mostra o
    traco; a lousa fica por baixo e ouve.
    """

    def __init__(self) -> None:
        win.set_dpi_aware()
        self.root = tk.Tk()
        self.root.title("RiftCoach Overlay")
        # ESCONDIDA ATE OS ESTILOS ESTAREM POSTOS, e a ordem aqui e o conserto
        # de um bug que fazia o overlay piscar uma vez e sumir para sempre.
        #
        # Uma janela tkinter que aparece antes de receber WS_EX_NOACTIVATE
        # ativa-se sozinha e vira a janela em primeiro plano. O laco entao
        # pergunta "o jogo esta na frente?", recebe nao — porque quem esta na
        # frente e o proprio overlay — e se esconde. Ele se escondia por estar
        # aparecendo, e nada no log dizia isso.
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=COR_TRANSPARENTE)

        self.vazado = True
        try:
            self.root.attributes("-transparentcolor", COR_TRANSPARENTE)
        except tk.TclError:
            # Sem cor-chave o overlay ainda serve, so fica com um veu por cima
            # do jogo. Melhor degradar do que recusar: quem esta numa maquina
            # exotica prefere ver as marcacoes com veu a nao ver nada.
            self.vazado = False
            self.root.attributes("-alpha", 0.82)
            self.root.configure(bg="#000000")

        self.canvas = tk.Canvas(
            self.root,
            bg=COR_TRANSPARENTE if self.vazado else "#000000",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        # Cria o HWND sem mapear a janela: `update_idletasks` numa janela
        # retirada processa a criacao mas nao a exibicao.
        self.root.update_idletasks()
        self._hwnd = self._descobrir_hwnd(self.root)
        if self._hwnd:
            win.make_click_through(self._hwnd)

        self.lousa = tk.Toplevel(self.root)
        self.lousa.withdraw()
        self.lousa.overrideredirect(True)
        self.lousa.attributes("-topmost", True)
        self.lousa.configure(bg="#000000", cursor="crosshair")
        with contextlib.suppress(tk.TclError):
            # 1% e invisivel a olho nu e ainda conta como janela para o
            # clique; zero voltaria a ser buraco.
            self.lousa.attributes("-alpha", 0.01)
        self.lousa.update_idletasks()
        self._hwnd_lousa = self._descobrir_hwnd(self.lousa)
        self._lousa_visivel = False

        self._rect: Rect | None = None
        self._fontes: dict[tuple[int, bool], tkfont.Font] = {}
        # Comeca escondida de verdade. Antes comecava como `True` sem estar
        # visivel, e a primeira chamada a `mostrar(True)` virava um no-op.
        self._visivel = False

    @property
    def hwnd(self) -> int:
        """O identificador da janela, para quem precisa perguntar ao Windows
        sobre ela — o laco usa para nao se confundir com o proprio overlay."""
        return self._hwnd

    @property
    def hwnds(self) -> tuple[int, ...]:
        """As janelas que pertencem a revisao: o overlay e a lousa.

        Com a lousa em primeiro plano (pincel ou digitacao), a pergunta "a
        pessoa esta no jogo?" tem de responder sim — senao o laco esconderia o
        overlay justamente enquanto ela desenha.
        """
        return tuple(h for h in (self._hwnd, self._hwnd_lousa) if h)

    def _descobrir_hwnd(self, janela: tk.Misc) -> int:
        """O HWND real da janela de topo.

        Com `overrideredirect`, o Tk as vezes devolve o filho e as vezes o
        proprio topo. Pegar o pai quando ele existe cobre os dois casos; sem
        isso os estilos estendidos iriam para a janela errada e o clique
        continuaria sendo capturado.
        """
        try:
            ident = int(janela.winfo_id())
        except tk.TclError:
            return 0
        if not win.disponivel():
            return ident
        import ctypes

        pai = int(ctypes.windll.user32.GetParent(ident))
        return pai or ident

    # ----------------------------------------------------------------
    # Posicao
    # ----------------------------------------------------------------

    def cobrir(self, r: Rect) -> None:
        """Encaixa a janela (e a lousa) sobre o retangulo dado, se ele mudou."""
        if self._rect == r:
            return
        self._rect = r
        geo = f"{int(r.w)}x{int(r.h)}+{int(r.x)}+{int(r.y)}"
        self.root.geometry(geo)
        self.lousa.geometry(geo)
        # Reafirmar o topmost: o League, ao ganhar o foco, empurra o overlay
        # para tras uma vez. Uma reafirmacao a cada mudanca de geometria cobre
        # isso sem ficar piscando a cada quadro.
        self.root.attributes("-topmost", True)

    def mostrar(self, visivel: bool) -> None:
        """Aparece e some SEM tirar o foco de quem esta usando o computador.

        `deiconify` do tkinter ativa a janela, e ativar a nossa tira o jogo da
        frente — que e exatamente o estado que faz o laco decidir se esconder.
        `ShowWindow(SW_SHOWNOACTIVATE)` mostra sem ativar; e o unico jeito.
        """
        if visivel == self._visivel:
            return
        self._visivel = visivel
        if not self._hwnd:  # fora do Windows nao ha o que fazer
            (self.root.deiconify if visivel else self.root.withdraw)()
            return
        if visivel:
            win.show_no_activate(self._hwnd)
            self.root.attributes("-topmost", True)
            if self._lousa_visivel:
                win.raise_topmost(self._hwnd)
        else:
            win.hide(self._hwnd)

    # ----------------------------------------------------------------
    # Desenho
    # ----------------------------------------------------------------

    def _fonte(self, tamanho: float, negrito: bool) -> tkfont.Font:
        chave = (max(7, round(tamanho)), negrito)
        f = self._fontes.get(chave)
        if f is None:
            f = tkfont.Font(family=FAMILIA, size=chave[0], weight="bold" if negrito else "normal")
            self._fontes[chave] = f
        return f

    def desenhar(self, cena: Scene) -> None:
        c = self.canvas
        # Apagar e redesenhar tudo. Com algumas dezenas de itens isso custa
        # menos que manter identidade de item entre quadros, e nao tem o modo
        # de falha de sobrar lixo na tela quando a cena muda de forma.
        c.delete("all")
        for it in cena.items:
            if isinstance(it, Box):
                r = it.rect
                if it.translucido and it.fill:
                    # Pontilhado: um quarto dos pixels vira a cor-chave, ou
                    # seja, buraco. O jogo aparece por tras como por uma tela
                    # fina — e o jeito do Tk de ter meia transparencia.
                    c.create_rectangle(
                        r.x,
                        r.y,
                        r.right,
                        r.bottom,
                        fill=it.fill,
                        outline=it.outline or "",
                        width=it.width,
                        stipple="gray75",
                    )
                else:
                    c.create_rectangle(
                        r.x,
                        r.y,
                        r.right,
                        r.bottom,
                        fill=it.fill or "",
                        outline=it.outline or "",
                        width=it.width,
                    )
            elif isinstance(it, Circle):
                c.create_oval(
                    it.x - it.r,
                    it.y - it.r,
                    it.x + it.r,
                    it.y + it.r,
                    fill=it.fill or "",
                    outline=it.outline or "",
                    width=it.width,
                )
            elif isinstance(it, Line):
                # `dash=None` nao e o mesmo que omitir: o Tk rejeita o None.
                if it.dash:
                    c.create_line(
                        it.x1,
                        it.y1,
                        it.x2,
                        it.y2,
                        fill=it.color,
                        width=it.width,
                        dash=(4, 3),
                    )
                else:
                    c.create_line(it.x1, it.y1, it.x2, it.y2, fill=it.color, width=it.width)
            elif isinstance(it, Path):
                self._caminho(it.points, it.color, it.width)
            else:
                self._texto(it)

    def _caminho(
        self, pontos: list[tuple[float, float]], cor: str, esp: float, tag: str = ""
    ) -> None:
        """Um traco inteiro como UMA linha, com pontas e juntas redondas.

        Desenhar segmento por segmento deixava "dentes" nas curvas (cada
        segmento termina reto) e custava um item de canvas por ponto.
        """
        if len(pontos) < 2:
            return
        coords = [v for p in pontos for v in p]
        self.canvas.create_line(
            *coords,
            fill=cor,
            width=esp,
            capstyle=tk.ROUND,
            joinstyle=tk.ROUND,
            tags=(tag,) if tag else (),
        )

    def segmento(
        self, a: tuple[float, float], b: tuple[float, float], cor: str, esp: float
    ) -> None:
        """Desenha NA HORA um pedaco do traco em andamento, em pixel.

        E o que faz o pincel responder ao mouse. Antes o traco so aparecia no
        proximo quadro do laco, e desenhar parecia arrastar. O quadro seguinte
        apaga tudo e redesenha o traco inteiro a partir da prancheta, entao
        nao sobra lixo.
        """
        self._caminho([a, b], cor, esp, tag="andamento")

    def _texto(self, it: Label) -> None:
        """Texto com contorno escuro.

        Sem isso, texto claro sobre o rio ou sobre a base azul some. O contorno
        por deslocamento e feio de perto e perfeitamente legivel em movimento,
        que e a unica condicao em que alguem vai ler isto.

        A cor-chave e quase preta (scene.COR_TRANSPARENTE): o antisserrilhado
        do Windows mistura a borda da letra com a cor de fundo, e com a chave
        magenta antiga toda letra ganhava uma franja rosa.
        """
        f = self._fonte(it.size, it.bold)
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            self.canvas.create_text(
                it.x + dx,
                it.y + dy,
                text=it.text,
                fill="#000000",
                font=f,
                anchor=it.anchor,
            )
        self.canvas.create_text(it.x, it.y, text=it.text, fill=it.color, font=f, anchor=it.anchor)

    # ----------------------------------------------------------------
    # A lousa: pincel e digitacao
    # ----------------------------------------------------------------

    _EVENTOS_DA_LOUSA = (
        "<ButtonPress-1>",
        "<B1-Motion>",
        "<ButtonRelease-1>",
        "<ButtonPress-3>",
        "<KeyPress>",
    )

    def _soltar_lousa(self) -> None:
        for evento in self._EVENTOS_DA_LOUSA:
            self.lousa.unbind(evento)
        if self._lousa_visivel:
            self.lousa.withdraw()
            self._lousa_visivel = False

    def _prender_lousa(self) -> None:
        """Mostra a lousa por baixo do overlay e da a ela o teclado."""
        if self._rect is not None:
            r = self._rect
            self.lousa.geometry(f"{int(r.w)}x{int(r.h)}+{int(r.x)}+{int(r.y)}")
        if not self._lousa_visivel:
            self.lousa.deiconify()
            self._lousa_visivel = True
        self.lousa.attributes("-topmost", True)
        self.lousa.update_idletasks()
        # O overlay precisa ficar POR CIMA da lousa, senao o traco fica atras
        # dela. Os dois sao topmost; vale quem foi reafirmado por ultimo.
        if self._hwnd:
            win.raise_topmost(self._hwnd)
        with contextlib.suppress(tk.TclError):
            self.lousa.focus_force()
        if self._hwnd_lousa:
            win.activate(self._hwnd_lousa)

    def modo_desenho(
        self,
        ligado: bool,
        *,
        ao_comecar: Callable[[float, float], None] | None = None,
        ao_mover: Callable[[float, float], None] | None = None,
        ao_soltar: Callable[[], None] | None = None,
        ao_teclar: Callable[[str], None] | None = None,
        ao_desfazer: Callable[[], None] | None = None,
    ) -> None:
        """Liga o pincel: a lousa passa a ouvir o mouse e o teclado.

        As coordenadas chegam ao chamador em FRACAO da janela, e nao em pixel.
        Converter aqui, no unico lugar que conhece o tamanho real, evita que a
        logica do pincel precise saber de resolucao — e e o que faz um desenho
        sobreviver a abrir o replay noutra tela.
        """
        self._soltar_lousa()
        if not ligado:
            return
        self.lousa.configure(cursor="crosshair")

        def fracao(e: tk.Event[tk.Misc]) -> tuple[float, float]:
            larg = max(1, self.lousa.winfo_width())
            alt = max(1, self.lousa.winfo_height())
            return e.x / larg, e.y / alt

        if ao_comecar is not None:
            self.lousa.bind("<ButtonPress-1>", lambda e: ao_comecar(*fracao(e)))
        if ao_mover is not None:
            self.lousa.bind("<B1-Motion>", lambda e: ao_mover(*fracao(e)))
        if ao_soltar is not None:
            self.lousa.bind("<ButtonRelease-1>", lambda e: ao_soltar())
        if ao_desfazer is not None:
            # Botao direito desfaz: e o gesto que a mao ja faz no mouse, sem
            # tirar o olho do replay para achar a tecla Z.
            self.lousa.bind("<ButtonPress-3>", lambda e: ao_desfazer())
        if ao_teclar is not None:
            self.lousa.bind("<KeyPress>", lambda e: ao_teclar(e.keysym))
        self._prender_lousa()

    def modo_digitacao(
        self,
        ligado: bool,
        *,
        ao_digitar: Callable[[str, str], None] | None = None,
    ) -> None:
        """Liga a digitacao: a lousa recebe o teclado e monta um texto.

        Entrega `char` E `keysym` ao chamador, porque os dois respondem
        perguntas diferentes: `char` e o caractere de verdade, ja com shift e
        acento aplicados — digitar "ç" da o "ç"; `keysym` e o nome da tecla, e
        e o unico jeito de distinguir Enter de BackSpace de Escape, que nao
        produzem caractere nenhum.
        """
        self._soltar_lousa()
        if not ligado:
            return
        self.lousa.configure(cursor="xterm")
        if ao_digitar is not None:
            self.lousa.bind("<KeyPress>", lambda e: ao_digitar(e.char or "", e.keysym))
        self._prender_lousa()

    def bombear(self) -> None:
        """Deixa o tkinter respirar sem entregar o controle do laco."""
        self.root.update()

    def fechar(self) -> None:
        with contextlib.suppress(tk.TclError):
            self.root.destroy()
