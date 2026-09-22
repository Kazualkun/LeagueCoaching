"""A mesma cena, desenhada num PNG em vez de na tela.

Existe por tres motivos, e nenhum deles e enfeite:

1. DA PARA VER O OVERLAY SEM ABRIR O JOGO. Ajustar posicao de cartao abrindo
   o League, carregando um replay e esperando a calibracao a cada tentativa
   seria inviavel — e esse tipo de atrito e exatamente o que faz alguem
   desistir de arrumar o alinhamento.

2. O MANUAL PRECISA DE IMAGENS. Descrever "aparece um cartao a esquerda" nao
   substitui mostrar. As figuras da documentacao saem daqui, entao elas nunca
   ficam desatualizadas em relacao ao codigo: sao geradas por ele.

3. Quem for contribuir consegue conferir uma mudanca de layout olhando um
   arquivo, sem ter o jogo instalado.

Usa PIL, que e do extra `vision`. O overlay de verdade NAO depende disto.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from riftcoach.overlay.scene import Box, Circle, Label, Line, Scene

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

# Fontes do Windows, na ordem em que valem a tentativa. O fallback e a fonte
# embutida do PIL, que ignora tamanho — feia, mas nunca falta.
FONTES = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")
FONTES_NEGRITO = ("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")


# O tkinter mede fonte em PONTOS; o PIL, em PIXELS. A 96 dpi um ponto vale
# 4/3 de pixel, entao sem esta conversao a pre-visualizacao desenha o texto
# 25% menor do que ele sai na tela de verdade — e passa a mentir exatamente
# sobre o que ela existe para mostrar. Foi assim que uma sobreposicao de
# rotulos no cartao passou batida por quatro figuras do manual.
PONTO_EM_PIXEL = 4 / 3


def _fonte(tamanho: float, negrito: bool) -> Any:
    from PIL import ImageFont

    px = max(7, round(tamanho * PONTO_EM_PIXEL))
    for nome in FONTES_NEGRITO if negrito else FONTES:
        try:
            return ImageFont.truetype(nome, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _ancora_pil(a: str) -> str:
    """Do vocabulario do Canvas para o do PIL.

    O tkinter ancora pelo canto ("nw"); o PIL usa duas letras, horizontal e
    vertical ("la" = left/ascender). Sem a traducao o texto sai deslocado de
    meia linha e ninguem descobre por que.
    """
    h = {"w": "l", "e": "r", "c": "m"}
    vert = "a" if a.startswith("n") else "d" if a.startswith("s") else "m"
    if a in ("n", "s", "center"):
        horiz = "m"
    elif a.endswith("w"):
        horiz = "l"
    elif a.endswith("e"):
        horiz = "r"
    else:
        horiz = h.get(a, "l")
    return horiz + vert


def desenhar_em(img: Image, cena: Scene) -> Image:
    """Pinta a cena sobre a imagem, devolvendo uma copia."""
    from PIL import ImageDraw

    out = img.convert("RGB").copy()
    d = ImageDraw.Draw(out)
    for it in cena.items:
        if isinstance(it, Box):
            r = it.rect
            d.rectangle(
                [r.x, r.y, r.right, r.bottom],
                fill=it.fill,
                outline=it.outline,
                width=max(1, round(it.width)),
            )
        elif isinstance(it, Circle):
            d.ellipse(
                [it.x - it.r, it.y - it.r, it.x + it.r, it.y + it.r],
                fill=it.fill,
                outline=it.outline,
                width=max(1, round(it.width)),
            )
        elif isinstance(it, Line):
            d.line(
                [it.x1, it.y1, it.x2, it.y2],
                fill=it.color,
                width=max(1, round(it.width)),
            )
        else:
            _texto(d, it)
    return out


def _texto(d: Any, it: Label) -> None:
    f = _fonte(it.size, it.bold)
    anc = _ancora_pil(it.anchor)
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((it.x + dx, it.y + dy), it.text, font=f, fill="#000000", anchor=anc)
    d.text((it.x, it.y), it.text, font=f, fill=it.color, anchor=anc)


def gerar(fundo: Path, cena: Scene, destino: Path, *, escala: float = 1.0) -> Path:
    """Renderiza `cena` sobre `fundo` e grava em `destino`.

    `escala` permite gerar a figura numa resolucao e reduzir para o manual sem
    que o texto vire borrao: desenha-se grande e reduz-se depois.

    O FORMATO SEGUE A EXTENSAO, e a escolha importa mais do que parece. Um
    print de partida e uma imagem FOTOGRAFICA: em PNG ele sai com 1,3 MB, e
    quatro figuras dessas sao 5 MB entrando no repositorio para sempre. Em JPEG
    a 88 sao 150 KB sem diferenca visivel no texto do cartao. Telas de interface
    lisa, ao contrario, comprimem muito melhor em PNG — e JPEG borraria a borda
    da letra.
    """
    from PIL import Image as PILImage

    img = PILImage.open(fundo)
    out = desenhar_em(img, cena)
    if escala != 1.0:
        # `Image.LANCZOS` existe em tempo de execucao mas nao nos tipos do PIL
        # desde que os filtros viraram enum; o caminho tipado e por `Resampling`.
        out = out.resize(
            (round(out.width * escala), round(out.height * escala)),
            PILImage.Resampling.LANCZOS,
        )
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.suffix.lower() in (".jpg", ".jpeg"):
        out.save(destino, quality=88, optimize=True, subsampling=0)
    else:
        out.save(destino, optimize=True)
    return destino
