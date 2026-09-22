"""Frame -> observacao estruturada. A fronteira entre pixel e evidencia.

REGRA QUE NAO SE QUEBRA: o que sai daqui e SEMPRE T3_INFERRED.

A telemetria da Riot e verdade medida. Um numero lido de uma imagem comprimida,
com OCR, num quadro que pode estar 200 ms fora do lugar, nao e a mesma coisa —
e misturar os dois apagaria justamente a distincao que o projeto inteiro existe
para manter (docs/ARCHITECTURE.md, secao 4).

O que a visao acrescenta e o que a telemetria NAO tem:

    vida ao longo de uma troca     — a timeline so da vida de minuto em minuto
    cooldown de habilidade         — a timeline nao emite uso de habilidade
    para onde a camera apontava    — nao existe na telemetria
    posicao das wards              — WARD_PLACED nao carga posicao

Os dois primeiros sao `trading` e `combo`, as categorias que a etapa 1 teve de
deixar em aberto por falta de dado (docs/06-advantage-engine.md, secao 8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from riftcoach.core.schema import Evidence, EvidenceTier
from riftcoach.vision import rois
from riftcoach.vision.ocr import read_clock, read_int

if TYPE_CHECKING:  # pragma: no cover
    from riftcoach.vision.sampler import Frame

# Acima disto o quadro esta longe demais do instante pedido para sustentar
# afirmacao sobre "o que estava na tela naquele momento".
MAX_USABLE_DRIFT_S = 0.5


@dataclass(frozen=True)
class HudReading:
    """Numeros lidos da HUD. Cada campo e None quando nao deu para ler.

    None aqui significa NAO LIDO, nunca zero. Colapsar os dois faria o coach
    afirmar "voce estava com 0 de ouro" quando o que houve foi um recorte
    ilegivel — exatamente o tipo de numero inventado que o projeto recusa.
    """

    game_clock_s: int | None = None
    gold: int | None = None
    cs: int | None = None

    @property
    def read_any(self) -> bool:
        return any(v is not None for v in (self.game_clock_s, self.gold, self.cs))


@dataclass(frozen=True)
class FrameObservation:
    """O que um quadro mostra. Sempre T3.

    `timeline_ms` e o instante da PARTIDA, ja convertido pela calibracao — e o
    que permite a observacao se juntar as evidencias da telemetria na mesma
    linha do tempo.
    """

    timeline_ms: int
    video_t_s: float
    drift_s: float
    hud: HudReading
    confidence: float = 0.5

    def to_evidence(self) -> list[Evidence]:
        """Converte para o tipo que o resto do sistema entende.

        Toda linha sai com a premissa declarada, porque o schema obriga
        evidencia T2/T3 a carregar uma — e aqui a premissa e sempre a mesma:
        foi lido de um quadro, nao medido pela Riot.
        """
        premissa = (
            f"lido por OCR do quadro em {self.video_t_s:.1f}s do video "
            f"(erro de {self.drift_s * 1000:.0f} ms ate o instante pedido)"
        )
        out: list[Evidence] = []
        if self.hud.gold is not None:
            out.append(
                Evidence(
                    tier=EvidenceTier.T3_INFERRED,
                    timestamp_ms=self.timeline_ms,
                    statement=f"HUD mostrava {self.hud.gold} de ouro disponivel",
                    source="vision",
                    assumption=premissa,
                )
            )
        if self.hud.cs is not None:
            out.append(
                Evidence(
                    tier=EvidenceTier.T3_INFERRED,
                    timestamp_ms=self.timeline_ms,
                    statement=f"HUD mostrava {self.hud.cs} de CS",
                    source="vision",
                    assumption=premissa,
                )
            )
        return out


class ClockCalibration(Protocol):
    """So o que `observe` precisa de uma calibracao.

    Protocol em vez do tipo concreto para que o modo replay — onde o offset vem
    do client, nao do OCR — encaixe sem herdar nada.
    """

    def to_timeline_ms(self, video_s: float) -> int: ...


def read_hud(frame: Frame, *, hud_scale: float = 1.0) -> HudReading:
    """Le os contadores da HUD de um quadro.

    Apenas OCR: nunca se paga um modelo de visao para ler numero. Alem do
    custo, o OCR tem a propriedade que importa aqui — quando nao consegue ler,
    devolve nada, em vez de um numero plausivel.
    """
    return HudReading(
        game_clock_s=read_clock(frame.image, rois.GAME_CLOCK, scale=hud_scale),
        gold=read_int(frame.image, rois.GOLD, scale=hud_scale, maximo=99_999),
        cs=read_int(frame.image, rois.CS, scale=hud_scale, maximo=999),
    )


def observe(
    frame: Frame, calibration: ClockCalibration, *, hud_scale: float = 1.0
) -> FrameObservation | None:
    """Transforma um quadro em observacao, ou devolve None.

    Devolve None quando o quadro esta longe demais do instante pedido, ou
    quando nada foi lido. Observacao vazia nao e inofensiva: ela ocuparia
    espaco no contexto e pareceria confirmacao de alguma coisa.
    """
    if frame.drift_s > MAX_USABLE_DRIFT_S:
        return None
    hud = read_hud(frame, hud_scale=hud_scale)
    if not hud.read_any:
        return None

    # O relogio lido do proprio quadro vence a calibracao quando existe: e
    # medicao direta daquele quadro, enquanto a calibracao e uma estimativa
    # global. Discordancia grande entre os dois e sinal de calibracao ruim, e
    # nesse caso o quadro manda.
    if hud.game_clock_s is not None:
        timeline_ms = hud.game_clock_s * 1000
        confianca = 0.8
    else:
        timeline_ms = calibration.to_timeline_ms(frame.t_s)
        confianca = 0.5

    return FrameObservation(
        timeline_ms=max(0, timeline_ms),
        video_t_s=frame.t_s,
        drift_s=frame.drift_s,
        hud=hud,
        confidence=confianca,
    )


@dataclass
class ObservationSet:
    """As observacoes de uma partida, indexadas por instante da timeline."""

    observations: list[FrameObservation] = field(default_factory=list)

    def add(self, obs: FrameObservation | None) -> None:
        if obs is not None:
            self.observations.append(obs)

    def near(self, timeline_ms: int, window_ms: int = 5_000) -> list[FrameObservation]:
        return [o for o in self.observations if abs(o.timeline_ms - timeline_ms) <= window_ms]

    def evidence_for(self, timeline_ms: int, window_ms: int = 5_000) -> list[Evidence]:
        out: list[Evidence] = []
        for o in self.near(timeline_ms, window_ms):
            out.extend(o.to_evidence())
        return out

    def __len__(self) -> int:
        return len(self.observations)
