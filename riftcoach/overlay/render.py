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
from tkinter import font as tkfont

from riftcoach.overlay import window as win
from riftcoach.overlay.geometry import Rect
from riftcoach.overlay.scene import (
    COR_TRANSPARENTE,
    Box,
    Circle,
    Label,
    Line,
    Scene,
)

FAMILIA = "Segoe UI"


class OverlayWindow:
    """Uma janela sem borda, sempre no topo, que o mouse atravessa."""

    def __init__(self) -> None:
        win.set_dpi_aware()
        self.root = tk.Tk()
        self.root.title("RiftCoach Overlay")
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

        self.root.update_idletasks()
        self._hwnd = self._descobrir_hwnd()
        if self._hwnd:
            win.make_click_through(self._hwnd)

        self._rect: Rect | None = None
        self._fontes: dict[tuple[int, bool], tkfont.Font] = {}
        self._visivel = True

    def _descobrir_hwnd(self) -> int:
        """O HWND real da janela de topo.

        Com `overrideredirect`, o Tk as vezes devolve o filho e as vezes o
        proprio topo. Pegar o pai quando ele existe cobre os dois casos; sem
        isso os estilos estendidos iriam para a janela errada e o clique
        continuaria sendo capturado.
        """
        try:
            ident = int(self.root.winfo_id())
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
        """Encaixa a janela sobre o retangulo dado, se ele mudou."""
        if self._rect == r:
            return
        self._rect = r
        self.root.geometry(f"{int(r.w)}x{int(r.h)}+{int(r.x)}+{int(r.y)}")
        # Reafirmar o topmost: o League, ao ganhar o foco, empurra o overlay
        # para tras uma vez. Uma reafirmacao a cada mudanca de geometria cobre
        # isso sem ficar piscando a cada quadro.
        self.root.attributes("-topmost", True)

    def mostrar(self, visivel: bool) -> None:
        if visivel == self._visivel:
            return
        self._visivel = visivel
        if visivel:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()

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
            else:
                self._texto(it)

    def _texto(self, it: Label) -> None:
        """Texto com contorno preto.

        Sem isso, texto claro sobre o rio ou sobre a base azul some. O contorno
        por deslocamento e feio de perto e perfeitamente legivel em movimento,
        que e a unica condicao em que alguem vai ler isto.
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

    def bombear(self) -> None:
        """Deixa o tkinter respirar sem entregar o controle do laco."""
        self.root.update()

    def fechar(self) -> None:
        with contextlib.suppress(tk.TclError):
            self.root.destroy()
