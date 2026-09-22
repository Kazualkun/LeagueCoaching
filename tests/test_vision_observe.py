"""Testes da fronteira entre pixel e evidencia.

A propriedade central: o que sai da visao e SEMPRE T3_INFERRED, e sempre com a
premissa declarada. Se um numero lido de uma imagem comprimida entrasse no
relatorio como T1, o sistema de niveis inteiro perderia o sentido.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

pytest.importorskip("PIL", reason="a camada de visao e um extra opcional")

from riftcoach.core.schema import EvidenceTier
from riftcoach.vision.observe import (
    MAX_USABLE_DRIFT_S,
    FrameObservation,
    HudReading,
    ObservationSet,
    observe,
)


@dataclass
class FakeFrame:
    """So o que `observe` usa de um Frame."""

    t_s: float
    drift_s: float
    image: object = None
    requested_t_s: float = 0.0
    width: int = 1920
    height: int = 1080


class FakeCalibration:
    def __init__(self, offset: float) -> None:
        self.offset = offset

    def to_timeline_ms(self, video_s: float) -> int:
        return round((video_s - self.offset) * 1000)


def _obs(**kw: object) -> FrameObservation:
    base: dict[str, object] = {
        "timeline_ms": 862_000,
        "video_t_s": 900.0,
        "drift_s": 0.05,
        "hud": HudReading(game_clock_s=862, gold=1450, cs=187),
    }
    base.update(kw)
    return FrameObservation(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Niveis de evidencia
# --------------------------------------------------------------------------


def test_vision_evidence_is_always_t3() -> None:
    """A regra que nao se quebra.

    Telemetria da Riot e verdade medida; numero lido de imagem comprimida por
    OCR num quadro que pode estar 200 ms fora do lugar nao e a mesma coisa.
    """
    for e in _obs().to_evidence():
        assert e.tier is EvidenceTier.T3_INFERRED
        assert e.source == "vision"


def test_every_piece_of_evidence_declares_its_assumption() -> None:
    """O schema obriga T2/T3 a carregar premissa — aqui ela precisa dizer que
    foi LIDO de um quadro, e com quanto erro de tempo."""
    for e in _obs().to_evidence():
        assert e.assumption
        assert "OCR" in e.assumption
        assert "ms" in e.assumption


def test_unread_fields_produce_no_evidence() -> None:
    """None significa NAO LIDO, nunca zero.

    Colapsar os dois faria o coach afirmar "voce estava com 0 de ouro" quando
    o que houve foi um recorte ilegivel.
    """
    obs = _obs(hud=HudReading(game_clock_s=862, gold=None, cs=None))
    assert obs.to_evidence() == []


def test_zero_is_a_reading_and_none_is_not() -> None:
    obs = _obs(hud=HudReading(gold=0))
    textos = [e.statement for e in obs.to_evidence()]
    assert any("0 de ouro" in t for t in textos)


# --------------------------------------------------------------------------
# Observacao
# --------------------------------------------------------------------------


def test_a_drifted_frame_is_refused() -> None:
    """Quadro longe do instante pedido nao sustenta afirmacao sobre "o que
    estava na tela naquele momento"."""
    frame = FakeFrame(t_s=900.0, drift_s=MAX_USABLE_DRIFT_S + 0.1)
    assert observe(frame, FakeCalibration(38.0)) is None  # type: ignore[arg-type]


def test_an_empty_reading_produces_no_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observacao vazia nao e inofensiva: ocuparia contexto e pareceria
    confirmacao de alguma coisa."""
    monkeypatch.setattr("riftcoach.vision.observe.read_hud", lambda *a, **k: HudReading())
    frame = FakeFrame(t_s=900.0, drift_s=0.0)
    assert observe(frame, FakeCalibration(38.0)) is None  # type: ignore[arg-type]


def test_the_frames_own_clock_beats_the_calibration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O relogio lido do proprio quadro e medicao direta daquele quadro; a
    calibracao e uma estimativa global. Quando os dois discordam, o quadro
    manda — e a confianca sobe."""
    monkeypatch.setattr(
        "riftcoach.vision.observe.read_hud",
        lambda *a, **k: HudReading(game_clock_s=600, gold=1000),
    )
    # A calibracao diria 862s; o quadro diz 600s.
    frame = FakeFrame(t_s=900.0, drift_s=0.0)
    obs = observe(frame, FakeCalibration(38.0))  # type: ignore[arg-type]
    assert obs is not None
    assert obs.timeline_ms == 600_000
    assert obs.confidence > 0.5


def test_without_a_clock_the_calibration_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("riftcoach.vision.observe.read_hud", lambda *a, **k: HudReading(gold=1000))
    frame = FakeFrame(t_s=900.0, drift_s=0.0)
    obs = observe(frame, FakeCalibration(38.0))  # type: ignore[arg-type]
    assert obs is not None
    assert obs.timeline_ms == 862_000
    assert obs.confidence == 0.5


def test_timeline_never_goes_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Quadro antes do inicio da partida (tela de carregamento) com uma
    calibracao grande daria instante negativo — que o schema recusa."""
    monkeypatch.setattr("riftcoach.vision.observe.read_hud", lambda *a, **k: HudReading(gold=500))
    frame = FakeFrame(t_s=5.0, drift_s=0.0)
    obs = observe(frame, FakeCalibration(60.0))  # type: ignore[arg-type]
    assert obs is not None
    assert obs.timeline_ms == 0
    # E precisa continuar construindo Evidence sem estourar validacao.
    obs.to_evidence()


# --------------------------------------------------------------------------
# Conjunto de observacoes
# --------------------------------------------------------------------------


def test_observations_join_the_timeline_by_instant() -> None:
    """E assim que a visao se junta a telemetria: mesma linha do tempo."""
    s = ObservationSet()
    s.add(_obs(timeline_ms=100_000))
    s.add(_obs(timeline_ms=862_000))
    s.add(_obs(timeline_ms=863_000))
    s.add(None)

    assert len(s) == 3
    perto = s.near(862_000, window_ms=5_000)
    assert len(perto) == 2


def test_evidence_for_a_moment_is_all_t3() -> None:
    s = ObservationSet()
    s.add(_obs(timeline_ms=862_000))
    ev = s.evidence_for(862_000)
    assert ev
    assert all(e.tier is EvidenceTier.T3_INFERRED for e in ev)


def test_a_moment_with_no_frames_yields_nothing() -> None:
    s = ObservationSet()
    s.add(_obs(timeline_ms=100_000))
    assert s.evidence_for(900_000) == []
