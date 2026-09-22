"""Cores, fontes e os poucos widgets que valem a pena ter prontos.

O tkinter sem estilo parece um programa de 1998, e isso importa: quem esta
decidindo se confia num .exe baixado do GitHub decide em parte pela cara. Nao
e vaidade, e a primeira prova de que alguem cuidou.

Widgets de tk puro em vez de ttk, de proposito. O ttk usa o tema nativo do
Windows e ignora metade das cores que a gente pede; o tk puro aceita todas e
fica igual em qualquer maquina — que e o que permite ter UMA aparencia, e nao
uma por versao de Windows.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

FUNDO = "#0d1117"
FUNDO_CARTAO = "#161b22"
BORDA = "#30363d"
TEXTO = "#e6edf3"
TEXTO_FRACO = "#8b949e"
ACENTO = "#58a6ff"
ACENTO_ESCURO = "#1f6feb"
SUCESSO = "#3fb950"
ALERTA = "#d29922"
ERRO = "#f85149"

FAMILIA = "Segoe UI"


def fonte(tamanho: int = 10, negrito: bool = False) -> tuple[str, int, str]:
    return (FAMILIA, tamanho, "bold" if negrito else "normal")


class Botao(tk.Label):
    """Botao desenhado a mao.

    O `tk.Button` no Windows ignora `bg` quando o tema esta ativo e aparece
    cinza-sistema, que nao combina com nada. Um Label com bind de clique aceita
    qualquer cor e se comporta igual — inclusive o cursor de mao, que e o que
    diz a pessoa que aquilo e clicavel.
    """

    def __init__(
        self,
        master: tk.Misc,
        texto: str,
        comando: Callable[[], None],
        *,
        principal: bool = True,
        **kw: object,
    ) -> None:
        self._cor = ACENTO_ESCURO if principal else FUNDO_CARTAO
        self._cor_hover = ACENTO if principal else BORDA
        self._comando = comando
        self._ligado = True
        super().__init__(
            master,
            text=texto,
            bg=self._cor,
            fg="#ffffff" if principal else TEXTO,
            font=fonte(10, negrito=principal),
            padx=18,
            pady=9,
            cursor="hand2",
            **kw,  # type: ignore[arg-type]
        )
        self.bind("<Button-1>", self._clique)
        self.bind("<Enter>", lambda _e: self._pintar(self._cor_hover))
        self.bind("<Leave>", lambda _e: self._pintar(self._cor))

    def _pintar(self, cor: str) -> None:
        if self._ligado:
            self.configure(bg=cor)

    def _clique(self, _e: object) -> None:
        if self._ligado:
            self._comando()

    def habilitar(self, ligado: bool) -> None:
        """Desligar sem esconder. Sumir com o botao faz a tela pular e a pessoa
        perde a referencia de onde estava."""
        self._ligado = ligado
        self.configure(
            bg=self._cor if ligado else BORDA,
            fg="#ffffff" if ligado else TEXTO_FRACO,
            cursor="hand2" if ligado else "arrow",
        )


class Campo(tk.Entry):
    """Entrada de texto com a mesma cara do resto."""

    def __init__(self, master: tk.Misc, *, senha: bool = False, **kw: object) -> None:
        super().__init__(
            master,
            bg="#0d1117",
            fg=TEXTO,
            insertbackground=TEXTO,
            font=fonte(11),
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDA,
            highlightcolor=ACENTO,
            show="•" if senha else "",
            **kw,  # type: ignore[arg-type]
        )


def titulo(master: tk.Misc, texto: str, tamanho: int = 16) -> tk.Label:
    return tk.Label(
        master,
        text=texto,
        bg=FUNDO,
        fg=TEXTO,
        font=fonte(tamanho, negrito=True),
        anchor="w",
        justify="left",
    )


def paragrafo(master: tk.Misc, texto: str, cor: str = TEXTO_FRACO) -> tk.Label:
    return tk.Label(
        master,
        text=texto,
        bg=FUNDO,
        fg=cor,
        font=fonte(10),
        anchor="w",
        justify="left",
        wraplength=560,
    )


def centralizar(janela: tk.Tk, largura: int, altura: int) -> None:
    """No meio do monitor onde a janela nasceu.

    Sem isso o tkinter abre no canto superior esquerdo, que num monitor grande
    parece que a janela apareceu por acidente.
    """
    janela.update_idletasks()
    x = (janela.winfo_screenwidth() - largura) // 2
    y = max(0, (janela.winfo_screenheight() - altura) // 2 - 40)
    janela.geometry(f"{largura}x{altura}+{x}+{y}")
