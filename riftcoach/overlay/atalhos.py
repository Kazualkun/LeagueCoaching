"""Os atalhos do overlay, definidos UMA vez.

Antes desta lista eles viviam em tres lugares — o codigo das teclas em
`run.py`, a descricao no painel em `scene.py`, e o texto de ajuda em `cli.py`.
Tres copias da mesma verdade envelhecem em ritmos diferentes: acrescentar um
atalho e esquecer de um dos tres produz ou uma tecla que ninguem descobre ou
um texto que promete o que nao existe. As duas falham em silencio.

Aqui ficam o codigo virtual, o rotulo que a pessoa le e o que ele faz. Quem
precisa de um dos tres pega daqui.
"""

from __future__ import annotations

from dataclasses import dataclass

from riftcoach.core.schema import MarkKind

# Modificadores. Ctrl+Alt porque o League nao usa essa combinacao em nada, e
# porque um atalho de uma tecla so dispararia sem querer o tempo todo durante
# a revisao.
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt


@dataclass(frozen=True)
class Atalho:
    vk: int
    tecla: str  # como a pessoa le
    descricao: str
    # Preenchido so nos atalhos que criam marcacao.
    marca: MarkKind | None = None
    rotulo: str = ""


MARCAR = (
    Atalho(0x45, "Ctrl+Alt+E", "marcar um erro seu", "error", "erro"),
    Atalho(0x4E, "Ctrl+Alt+N", "marcar uma anotação", "note", "nota"),
    Atalho(0x47, "Ctrl+Alt+G", "marcar algo que você fez bem", "good", "acerto"),
    Atalho(0x51, "Ctrl+Alt+Q", "marcar uma dúvida", "question", "duvida"),
)

NAVEGAR = (
    Atalho(0x53, "Ctrl+Alt+S", "pular para a próxima marcação"),
    Atalho(0x52, "Ctrl+Alt+R", "voltar para onde você parou"),
)

FERRAMENTAS = (
    Atalho(0x44, "Ctrl+Alt+D", "pincel: desenhar por cima do replay"),
    Atalho(0x50, "Ctrl+Alt+P", "salvar um print do momento, com os desenhos"),
    Atalho(0x49, "Ctrl+Alt+I", "perguntar à IA sobre este momento"),
)

JANELA = (
    Atalho(0x41, "Ctrl+Alt+A", "abrir e fechar esta ajuda"),
    Atalho(0x48, "Ctrl+Alt+H", "esconder o overlay"),
)

TODOS = MARCAR + NAVEGAR + FERRAMENTAS + JANELA

# Atalhos individuais, para quem precisa comparar contra um codigo especifico.
VK_SEGUINTE = NAVEGAR[0].vk
VK_RETOMAR = NAVEGAR[1].vk
VK_PINCEL = FERRAMENTAS[0].vk
VK_PRINT = FERRAMENTAS[1].vk
VK_PERGUNTAR = FERRAMENTAS[2].vk
VK_ANOTAR = MARCAR[1].vk
VK_DUVIDA = MARCAR[3].vk
VK_AJUDA = JANELA[0].vk
VK_OCULTAR = JANELA[1].vk

POR_CODIGO: dict[int, Atalho] = {a.vk: a for a in TODOS}


def como_texto(largura_tecla: int = 12) -> list[str]:
    """Uma linha por atalho, para a ajuda da CLI."""
    return [f"    {a.tecla:<{largura_tecla}} {a.descricao}" for a in TODOS]
