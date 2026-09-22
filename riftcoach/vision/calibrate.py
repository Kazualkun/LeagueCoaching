"""Sincronizacao do video com a timeline, pelo relogio da HUD.

Tres relogios estao em jogo e nenhum concorda com o outro
(docs/03-vod-review.md, 3.4):

    timeline     ms desde o inicio da partida
    replay       segundos desde o inicio do ARQUIVO, incluindo carregamento
    video        segundos desde o inicio da GRAVACAO, arbitrario

No modo replay da para perguntar ao client. Num VOD nao ha a quem perguntar,
entao o relogio e recuperado OPTICAMENTE: OCR do timer em ~20 pontos, ajuste
de reta, e verificacao contra um evento conhecido da telemetria.

POR QUE MEDIANA E NAO MINIMOS QUADRADOS. O OCR erra alguns quadros — tela de
carregamento, transicao, replay de camera. Minimos quadrados com um outlier de
600 segundos produz um ajuste que nao passa perto de nenhum ponto, e o
resultado sai com cara de precisao. Como a inclinacao TEM que ser 1.0 (video e
partida correm na mesma velocidade), o deslocamento de cada amostra ja e uma
estimativa independente do mesmo numero — e a mediana de estimativas
independentes ignora outliers de graca.

A inclinacao ainda e estimada, mas para DETECTAR problema, nao para ajustar:
se der longe de 1.0, o VOD foi editado em velocidade diferente e nenhum
deslocamento unico serve.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

from riftcoach.core.errors import RiftCoachError

# Uma amostra so vale se o deslocamento dela ficar perto da mediana. 2s cobre
# a taxa de quadros e o arredondamento do relogio (que mostra segundos
# inteiros), sem aceitar leitura errada.
INLIER_TOLERANCE_S = 2.0
# Abaixo disto o ajuste nao tem sustentacao suficiente para ser confiavel.
MIN_INLIERS = 5
MIN_INLIER_RATIO = 0.4
# Video e partida correm na mesma velocidade. Fora desta faixa, foi editado.
SLOPE_TOLERANCE = 0.05


class CalibrationError(RiftCoachError):
    pass


@dataclass(frozen=True)
class Calibration:
    """O mapeamento entre o relogio do video e o da partida.

        video_s = timeline_s + offset_s
    """

    offset_s: float
    inliers: int
    samples: int
    slope: float
    residual_s: float
    verified: bool = False
    rejected: list[tuple[float, int]] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        return self.inliers / self.samples if self.samples else 0.0

    def to_video_s(self, timeline_ms: int) -> float:
        return timeline_ms / 1000.0 + self.offset_s

    def to_timeline_ms(self, video_s: float) -> int:
        return round((video_s - self.offset_s) * 1000)

    def describe(self) -> str:
        estado = "verificado" if self.verified else "NAO verificado"
        return (
            f"offset={self.offset_s:+.2f}s · {self.inliers}/{self.samples} amostras "
            f"· residuo={self.residual_s:.2f}s · inclinacao={self.slope:.3f} · {estado}"
        )


def fit(samples: list[tuple[float, int | None]]) -> Calibration:
    """Ajusta o deslocamento a partir de pares (instante no video, relogio lido).

    Leituras `None` — quadros em que o OCR nao achou relogio — sao descartadas
    antes de qualquer conta. Elas sao ESPERADAS: tela de carregamento, tela de
    vitoria e cortes de camera nao mostram o timer.
    """
    validos = [(v, g) for v, g in samples if g is not None]
    if len(validos) < MIN_INLIERS:
        raise CalibrationError(
            f"so {len(validos)} leituras de relogio em {len(samples)} amostras.",
            hint=(
                "O recorte do relogio pode estar errado para esta resolucao ou "
                "escala de HUD, ou o video pode nao mostrar a HUD. Rode "
                "`tools/calibrate_rois.py` para conferir o recorte."
            ),
        )

    # A INCLINACAO E CONFERIDA PRIMEIRO, e de proposito.
    #
    # Num VOD acelerado os deslocamentos divergem, entao a checagem de consenso
    # tambem reprovaria — mas com a mensagem errada ("as leituras nao
    # concordam, confira o recorte"), mandando o usuario mexer na HUD quando o
    # problema esta na edicao do video. Recusa que aponta para o lugar errado
    # custa mais tempo do que recusa nenhuma.
    inclinacao = _robust_slope(validos)
    if abs(inclinacao - 1.0) > SLOPE_TOLERANCE:
        raise CalibrationError(
            f"o video nao corre na velocidade da partida (inclinacao {inclinacao:.3f}).",
            hint=(
                "VOD acelerado, camera lenta ou edicao com cortes. Nenhum "
                "deslocamento unico alinha o video inteiro nesse caso."
            ),
        )

    # Cada amostra e uma estimativa independente do MESMO deslocamento, porque
    # a inclinacao e 1.0 — o que acabamos de confirmar.
    offsets = [v - g for v, g in validos]
    mediana = statistics.median(offsets)

    inliers = [
        (v, g) for (v, g), o in zip(validos, offsets, strict=True)
        if abs(o - mediana) <= INLIER_TOLERANCE_S
    ]
    rejeitados = [
        (v, g) for (v, g), o in zip(validos, offsets, strict=True)
        if abs(o - mediana) > INLIER_TOLERANCE_S
    ]

    if len(inliers) < MIN_INLIERS or len(inliers) / len(validos) < MIN_INLIER_RATIO:
        raise CalibrationError(
            f"as leituras nao concordam entre si: "
            f"{len(inliers)} de {len(validos)} dentro de {INLIER_TOLERANCE_S}s.",
            hint=(
                "Leituras dispersas normalmente significam recorte errado do "
                "relogio, ou um video que junta varias partidas. Melhor recusar "
                "do que deslocar todo finding no tempo."
            ),
        )

    offsets_bons = [v - g for v, g in inliers]
    offset = statistics.median(offsets_bons)
    residuo = statistics.median([abs(o - offset) for o in offsets_bons])

    return Calibration(
        offset_s=offset,
        inliers=len(inliers),
        samples=len(samples),
        slope=inclinacao,
        residual_s=residuo,
        rejected=rejeitados,
    )


def _robust_slope(pontos: list[tuple[float, int]]) -> float:
    """Inclinacao de video_s em funcao de game_s, por Theil-Sen.

    Mediana das inclinacoes de todos os pares de pontos.

    Minimos quadrados nao serve aqui: a inclinacao e conferida ANTES de
    filtrar outliers (para poder dar a mensagem certa), e um unico OCR errado
    de 600 segundos inclina a reta o bastante para reprovar um video perfeito.
    Estimar a inclinacao com o metodo sensivel a outlier, e so depois remover
    outliers usando essa inclinacao, seria circular.

    Theil-Sen aguenta ate ~29% de pontos corrompidos sem se mover, que e
    exatamente o regime aqui. Com 20 pontos sao 190 pares — custo irrelevante.
    """
    if len(pontos) < 2:
        return 1.0
    inclinacoes: list[float] = []
    for i in range(len(pontos)):
        vi, gi = pontos[i]
        for j in range(i + 1, len(pontos)):
            vj, gj = pontos[j]
            if gj != gi:
                inclinacoes.append((vj - vi) / (gj - gi))
    return statistics.median(inclinacoes) if inclinacoes else 1.0


def verify(
    cal: Calibration,
    known_event_ms: int,
    observed_clock_s: int | None,
    *,
    tolerance_s: float = 3.0,
) -> Calibration:
    """Confere o ajuste contra um evento conhecido da telemetria.

    O ajuste pode ser internamente consistente e ainda assim estar errado — por
    exemplo se o video contiver outra partida. Por isso pulamos para o instante
    previsto de um evento REAL (o primeiro abate serve bem) e conferimos se o
    relogio na tela bate.

    Falhar aqui NAO e um erro: e a diferenca entre entregar findings alinhados
    e entregar findings 40 segundos fora do lugar, com cara de certeza. Quem
    chama degrada para o modo sem correspondencia.
    """
    if observed_clock_s is None:
        return cal
    esperado = known_event_ms / 1000.0
    ok = abs(observed_clock_s - esperado) <= tolerance_s
    return Calibration(
        offset_s=cal.offset_s,
        inliers=cal.inliers,
        samples=cal.samples,
        slope=cal.slope,
        residual_s=cal.residual_s,
        verified=ok,
        rejected=cal.rejected,
    )


def calibrate_video(path: Path, *, n: int = 20, hud_scale: float = 1.0) -> Calibration:
    """Calibra um arquivo de video de ponta a ponta.

    Amarra OCR e ajuste. A matematica vive em `fit`, que e pura e testada sem
    depender de imagem nem de motor de OCR — este wrapper so coleta amostras.
    """
    from riftcoach.vision.ocr import read_clock
    from riftcoach.vision.rois import GAME_CLOCK
    from riftcoach.vision.sampler import extract_at, iter_calibration_points, probe

    info = probe(path)
    instantes = list(iter_calibration_points(info, n=n))
    quadros = extract_at(path, instantes)
    amostras: list[tuple[float, int | None]] = [
        (q.t_s, read_clock(q.image, GAME_CLOCK, scale=hud_scale)) for q in quadros
    ]
    return fit(amostras)
