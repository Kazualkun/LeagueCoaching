"""Extracao de frames de um VOD, nos instantes que a telemetria ja apontou.

O ponto da etapa 7 nao e "analisar o video". E amostrar 3 frames nos momentos
que a destilacao JA provou serem decisivos, e so isso. Comparacao, de
docs/01-model-routing.md 1.4:

    captura ingenua a 1 fps, partida de 30 min -> 1.800 frames, ~1,4M tokens
    amostragem dirigida por momento              ->    ~48 frames,   ~13k tokens

~100x mais barato E melhor, porque cada frame sobrevivente e um que a timeline
ja indicou ser importante.

DUAS ARMADILHAS DE CODEC, as duas com teste:

1. `container.seek()` pousa no KEYFRAME ANTERIOR, nao no instante pedido.
   Decodificar o primeiro frame depois do seek devolve um quadro que pode
   estar varios segundos atras. E preciso decodificar PARA A FRENTE ate o PTS
   alvo. Pular isso desalinha todo finding do modo video sem erro nenhum.

2. Seek e em unidades de TIME_BASE do stream, nao em segundos. Passar segundos
   direto "funciona" em alguns arquivos e erra feio em outros.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from riftcoach.core.errors import RiftCoachError

if TYPE_CHECKING:  # pragma: no cover
    from PIL.Image import Image

# Tres frames por momento. O de t-1s e o unico que vai para o VLM: t-5s mostra
# a decisao se formando e t+2s confirma o desfecho, mas quem carrega o estado
# que interessa e o quadro imediatamente anterior.
OFFSETS_S: tuple[float, ...] = (-5.0, -1.0, 2.0)
VLM_OFFSET_S = -1.0


class VideoError(RiftCoachError):
    pass


@dataclass(frozen=True)
class Frame:
    """Um quadro extraido, com o instante REAL em que caiu.

    `t_s` e o tempo do quadro entregue, nao o pedido. Os dois diferem pela
    taxa de quadros do video, e a diferenca precisa viajar junto: uma
    observacao com 400 ms de erro ainda e util, mas so se souber que tem.
    """

    requested_t_s: float
    t_s: float
    image: Image
    width: int
    height: int

    @property
    def drift_s(self) -> float:
        return abs(self.t_s - self.requested_t_s)


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration_s: float
    width: int
    height: int
    fps: float

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def probe(path: Path) -> VideoInfo:
    """Le os metadados do video sem decodificar nada."""
    import av

    if not path.exists():
        raise VideoError(
            f"video nao encontrado: {path}",
            hint="Confira o caminho. O modo video aceita mp4, mkv e webm.",
        )
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise VideoError(
                    f"{path.name} nao tem trilha de video.",
                    hint="Arquivos so de audio nao servem para o modo video.",
                )
            stream = container.streams.video[0]
            fps = float(stream.average_rate or 0.0)
            duracao = (
                float(stream.duration * stream.time_base)
                if stream.duration and stream.time_base
                else float((container.duration or 0) / 1_000_000)
            )
            return VideoInfo(
                path=path,
                duration_s=duracao,
                width=int(stream.width or 0),
                height=int(stream.height or 0),
                fps=fps,
            )
    except VideoError:
        raise
    except Exception as e:  # pragma: no cover - depende do codec instalado
        raise VideoError(
            f"nao foi possivel abrir {path.name}: {e}",
            hint="O arquivo pode estar corrompido ou usar um codec sem suporte.",
        ) from e


def _to_stream_ts(t_s: float, time_base: Fraction | Any) -> int:
    """Segundos -> unidades de TIME_BASE do stream.

    `seek` NAO aceita segundos. Passar segundos direto acerta por acidente
    quando time_base e 1/1000 e erra por ordens de grandeza quando nao e.
    """
    return int(t_s / float(time_base))


def extract_at(
    path: Path, timestamps_s: Sequence[float], *, max_drift_s: float = 1.0
) -> list[Frame]:
    """Extrai um quadro em cada instante pedido.

    Um unico container aberto para todos os instantes: abrir por quadro custa
    ~600 ms de inicializacao contra ~40 ms de seek-e-decodifica.

    Instantes fora da duracao do video sao PULADOS em silencio — um momento
    perto do fim pode ter t+2s alem do ultimo quadro, e isso e normal, nao erro.
    """
    import av

    info = probe(path)
    pedidos = sorted(t for t in timestamps_s if 0 <= t <= info.duration_s)
    if not pedidos:
        return []

    out: list[Frame] = []
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        tb = stream.time_base

        for alvo in pedidos:
            # backward=True: pousa no keyframe ANTERIOR, de onde da para
            # decodificar para a frente ate o alvo. Sem isso, um seek para um
            # quadro que nao e keyframe devolve lixo.
            container.seek(_to_stream_ts(alvo, tb), stream=stream, backward=True, any_frame=False)
            quadro = _decode_forward_to(container, stream, alvo, tb)
            if quadro is None:
                continue
            t_real, img = quadro
            if abs(t_real - alvo) > max_drift_s:
                # Melhor nenhuma observacao do que uma ancorada no instante
                # errado: evidencia T3 fora de hora e pior que ausencia dela.
                continue
            out.append(
                Frame(
                    requested_t_s=alvo,
                    t_s=t_real,
                    image=img,
                    width=info.width,
                    height=info.height,
                )
            )
    return out


def _decode_forward_to(
    container: Any, stream: Any, alvo_s: float, time_base: Fraction | Any
) -> tuple[float, Image] | None:
    """Decodifica a partir do keyframe ate alcancar o instante alvo.

    Devolve o PRIMEIRO quadro cujo tempo alcanca o alvo. Sem esta caminhada,
    o quadro devolvido e o do keyframe — que em video de jogo, com GOP longo,
    pode estar varios segundos antes.
    """
    ultimo: tuple[float, Any] | None = None
    for frame in container.decode(stream):
        if frame.pts is None:
            continue
        t = float(frame.pts * time_base)
        if t >= alvo_s:
            return t, frame.to_image()
        ultimo = (t, frame)
        # Nao percorrer o arquivo inteiro se o alvo ficou para tras.
        if t > alvo_s + 5.0:
            break
    if ultimo is not None:
        return ultimo[0], ultimo[1].to_image()
    return None


def moment_timestamps(
    moment_t_ms: int, offset_s: float, *, offsets: Sequence[float] = OFFSETS_S
) -> list[float]:
    """Instantes do video para um momento da timeline.

    `offset_s` e a calibracao de relogio: `video_s = timeline_s + offset`.
    Ver docs/03-vod-review.md, 3.4 — sem essa calibracao os frames saem do
    lugar e toda observacao vira ruido.
    """
    base = moment_t_ms / 1000.0 + offset_s
    return [base + d for d in offsets]


def iter_calibration_points(info: VideoInfo, n: int = 20) -> Iterator[float]:
    """Instantes espalhados pelo video, para calibrar o relogio por OCR.

    Evita os extremos: comeco costuma ter tela de carregamento e fim costuma
    ter tela de vitoria — nos dois o relogio da HUD nao esta visivel.
    """
    if info.duration_s <= 0 or n <= 0:
        return
    inicio = info.duration_s * 0.10
    fim = info.duration_s * 0.90
    passo = (fim - inicio) / max(1, n - 1)
    for i in range(n):
        yield inicio + i * passo
