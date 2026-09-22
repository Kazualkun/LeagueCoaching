"""Leitura deterministica dos numeros da HUD.

Nunca se paga um VLM para ler um contador de quatro digitos. OCR em ONNX roda
em CPU, custa ~15 ms por recorte e nao alucina: quando nao consegue ler,
devolve nada, em vez de inventar um numero plausivel. Essa diferenca e o
motivo de o relogio passar por aqui e nao pelo modelo de visao.

O relogio e o ROI mais importante do projeto inteiro. E por ele que o video
sincroniza com a timeline (docs/03-vod-review.md, 3.4). Se a leitura estiver
errada, TODO finding do modo video sai deslocado no tempo — e deslocado com
cara de certeza.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from riftcoach.vision.rois import Roi

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

# O OCR confunde formas parecidas o tempo todo. Estas trocas sao seguras porque
# o unico alfabeto valido num relogio e digito e dois-pontos — qualquer letra
# aqui ja e, por definicao, um erro de leitura.
_CONFUSIONS = str.maketrans(
    {
        "O": "0",
        "o": "0",
        "D": "0",
        "Q": "0",
        "l": "1",
        "I": "1",
        "i": "1",
        "|": "1",
        "!": "1",
        "]": "1",
        "[": "1",
        "S": "5",
        "s": "5",
        "B": "8",
        "G": "6",
        "Z": "2",
        "z": "2",
        "T": "7",
        "A": "4",
        ".": ":",
        ",": ":",
        ";": ":",
        "-": ":",
        " ": "",
    }
)

_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{2})$")

# Uma partida de LoL nao passa de ~99 minutos na pratica. Acima disso e leitura
# errada, nao partida longa.
MAX_MINUTES = 99

_engine: Any = None


def _ocr_engine() -> Any:
    """Motor carregado sob demanda e reaproveitado.

    Carregar os modelos ONNX leva ~1 s. Fazer isso por recorte transformaria
    uma amostragem de 20 pontos em 20 segundos de espera.
    """
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR

        _engine = RapidOCR()
    return _engine


@dataclass(frozen=True)
class Reading:
    """Uma leitura de OCR, com o texto cru preservado.

    O texto cru viaja junto de proposito: quando a calibracao falha, a primeira
    pergunta e "o que ele LEU?", e sem isso a resposta seria um encolher de
    ombros.
    """

    text: str
    confidence: float

    @property
    def clean(self) -> str:
        return self.text.strip()


def parse_clock(text: str) -> int | None:
    """'14:22' -> 862 segundos. Devolve None quando nao da para confiar.

    Funcao pura de proposito: e a parte que mais quebra e a que mais precisa
    de teste, entao ela nao depende de imagem nem de motor de OCR.
    """
    if not text:
        return None
    limpo = text.strip().translate(_CONFUSIONS)

    # O SEPARADOR E OBRIGATORIO.
    #
    # A versao anterior tentava recuperar "dois-pontos perdido" tratando
    # qualquer 3-4 digitos como MMSS. Medido contra um print real de replay,
    # isso aceitava como horario:
    #
    #     "FPS:31" -> 5:31     "124"   -> 1:24
    #     "0/1/1"  -> 0:11     "29.2k0"-> 29:20
    #
    # Ou seja: contagem de CS, ouro do time e ate o contador de FPS viravam
    # relogio. Numa varredura de calibracao isso injeta dezenas de leituras
    # falsas, e a mediana so aguenta outlier enquanto ele for minoria.
    #
    # O custo de exigir o separador e nao recuperar um dois-pontos realmente
    # perdido. Isso e barato: a leitura vira None, e descartar uma amostra de
    # vinte nao machuca. Aceitar uma leitura falsa, sim.
    m = _CLOCK_RE.match(limpo)
    if m is None:
        return None

    minutos, segundos = int(m.group(1)), int(m.group(2))
    # Segundo >= 60 e o sinal mais forte de leitura errada que existe aqui:
    # um relogio real nunca produz isso.
    if segundos >= 60 or minutos > MAX_MINUTES:
        return None
    return minutos * 60 + segundos


def read_text(image: Image, roi: Roi | None = None, *, scale: float = 1.0) -> list[Reading]:
    """Le o texto de uma imagem, opcionalmente recortando um ROI antes."""
    import io

    alvo = image
    if roi is not None:
        alvo = image.crop(roi.pixels(image.width, image.height, scale))

    # Recortes da HUD sao pequenos; ampliar melhora bastante o OCR e custa
    # quase nada nesse tamanho.
    if alvo.width < 160:
        fator = max(2, 160 // max(1, alvo.width))
        alvo = alvo.resize((alvo.width * fator, alvo.height * fator))

    # PNG em memoria, e nao um array do numpy.
    #
    # O RapidOCR aceita os dois, mas numpy traz junto stubs que usam sintaxe de
    # 3.12+ e derrubam a checagem de tipo contra o alvo 3.11 do projeto (o CI
    # testa 3.11 de verdade). Suprimir o erro seria remendo; nao precisar do
    # import resolve de vez. Para um recorte de HUD o custo de codificar e
    # irrelevante perto do proprio OCR.
    buf = io.BytesIO()
    alvo.convert("RGB").save(buf, format="PNG")
    resultado, _ = _ocr_engine()(buf.getvalue())
    if not resultado:
        return []
    return [
        Reading(text=str(linha[1]), confidence=float(linha[2]))
        for linha in resultado
        if len(linha) >= 3
    ]


def read_clock(image: Image, roi: Roi, *, scale: float = 1.0) -> int | None:
    """Le o relogio da partida. Segundos desde o inicio, ou None.

    Tenta todas as linhas devolvidas pelo OCR: o recorte pode pegar um vizinho
    da HUD, e a validacao de formato ja descarta o que nao for relogio.
    """
    for leitura in read_text(image, roi, scale=scale):
        if (s := parse_clock(leitura.clean)) is not None:
            return s
    return None


def read_int(image: Image, roi: Roi, *, scale: float = 1.0, maximo: int = 100_000) -> int | None:
    """Le um contador inteiro da HUD (ouro, CS).

    Separado de `read_clock` porque as validacoes sao diferentes: aqui nao
    existe dois-pontos nem limite de 60.
    """
    for leitura in read_text(image, roi, scale=scale):
        digitos = re.sub(r"\D", "", leitura.clean.translate(_CONFUSIONS))
        if digitos and len(digitos) <= 6:
            valor = int(digitos)
            if 0 <= valor <= maximo:
                return valor
    return None
