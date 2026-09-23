"""A caixa de texto do overlay: teclas entram, texto sai.

Existe pelo mesmo motivo que `desenho.Prancheta`: a logica que decide o que
cada tecla faz nao pode morar dentro do laco, senao so da para exerce-la com
o jogo aberto, um replay rodando e alguem digitando de verdade. Aqui ela e
uma funcao pura de (tecla) -> (novo estado, acao), e o teste roda em
milissegundos.

O laco fica com o que so ele pode fazer: pausar o replay, pintar a tela e
disparar a chamada de IA.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from riftcoach.analysis.pergunta import LIMITE_DA_PERGUNTA

# O que o laco deve fazer depois da tecla.
Acao = Literal["nada", "fechar", "enviar"]


@dataclass
class CaixaDePergunta:
    """O texto sendo digitado, e o que cada tecla significa."""

    texto: str = ""
    limite: int = LIMITE_DA_PERGUNTA

    def teclar(self, char: str, keysym: str) -> Acao:
        """Aplica uma tecla.

        `char` e `keysym` chegam juntos porque respondem perguntas diferentes:
        `char` e o caractere de verdade, ja com shift e acento aplicados
        (digitar "ç" da "ç"); `keysym` e o nome da tecla, e e o unico jeito de
        reconhecer Enter, BackSpace e Escape, que nao produzem caractere.
        """
        if keysym == "Escape":
            return "fechar"
        if keysym == "BackSpace":
            self.texto = self.texto[:-1]
            return "nada"
        if keysym in ("Return", "KP_Enter"):
            # Enter com a caixa vazia nao e "enviar nada": e um engano, e
            # mandar assim gastaria cota para receber uma resposta sobre
            # coisa nenhuma.
            return "enviar" if self.texto.strip() else "nada"
        # Shift, Ctrl, setas e F1 chegam com `char` vazio ou nao imprimivel.
        # Deixar isso entrar encheria a caixa de sujeira invisivel.
        if char and char.isprintable() and len(self.texto) < self.limite:
            self.texto += char
        return "nada"

    def limpar(self) -> str:
        """Esvazia e devolve o que havia — o texto que vai virar pergunta."""
        saiu = self.texto.strip()
        self.texto = ""
        return saiu
