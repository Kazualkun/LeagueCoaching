"""Testes do OCR do relogio e da calibracao optica.

A calibracao e a peca de que TUDO no modo video depende: se o deslocamento
estiver errado, todo finding sai deslocado no tempo — e deslocado com cara de
certeza, que e o pior modo de falha do projeto.

A matematica e testada sem imagem e sem motor de OCR, de proposito: `fit`
recebe pares (instante, relogio) e nada mais. O OCR e uma fronteira separada.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PIL", reason="a camada de visao e um extra opcional")

from riftcoach.vision.calibrate import (
    INLIER_TOLERANCE_S,
    CalibrationError,
    fit,
    verify,
)
from riftcoach.vision.ocr import parse_clock

# --------------------------------------------------------------------------
# Leitura do relogio (funcao pura)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("14:22", 862),
        ("0:00", 0),
        ("00:00", 0),
        ("9:59", 599),
        ("45:07", 2707),
        ("  14:22  ", 862),
    ],
)
def test_parses_a_clean_clock(texto: str, esperado: int) -> None:
    assert parse_clock(texto) == esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("I4:22", 862),   # I lido no lugar de 1
        ("14:2O", 860),   # O lido no lugar de 0
        ("l4:22", 862),   # L minusculo
        ("14.22", 862),   # ponto no lugar de dois-pontos
        ("14 : 22", 862), # espacos sobrando
        ("1422", 862),    # dois-pontos perdido
        ("S:30", 330),    # S lido no lugar de 5
    ],
)
def test_recovers_from_common_ocr_confusions(texto: str, esperado: int) -> None:
    """Digito e dois-pontos sao o unico alfabeto valido num relogio.

    Qualquer letra aqui ja e, por definicao, erro de leitura — o que torna as
    trocas seguras em vez de chute.
    """
    assert parse_clock(texto) == esperado


@pytest.mark.parametrize("texto", ["14:75", "14:60", "99:99"])
def test_rejects_impossible_seconds(texto: str) -> None:
    """Segundo >= 60 e o sinal mais forte de leitura errada que existe aqui:
    um relogio real nunca produz isso."""
    assert parse_clock(texto) is None


@pytest.mark.parametrize(
    "texto", ["", "   ", "abc", "Nexo", "1234567", "120:00", ":::", "12:"]
)
def test_rejects_garbage(texto: str) -> None:
    assert parse_clock(texto) is None


def test_never_invents_a_number() -> None:
    """A propriedade que faz OCR ser preferivel a um VLM aqui: quando nao
    consegue ler, devolve nada, em vez de um numero plausivel."""
    for lixo in ("tela de carregamento", "VITORIA", "???"):
        assert parse_clock(lixo) is None


# --------------------------------------------------------------------------
# Ajuste da calibracao
# --------------------------------------------------------------------------


def _amostras(offset: float, n: int = 20, inicio: int = 60) -> list[tuple[float, int | None]]:
    """Amostras perfeitas: video_s = game_s + offset."""
    return [(inicio + i * 60 + offset, inicio + i * 60) for i in range(n)]


def test_recovers_a_clean_offset() -> None:
    cal = fit(_amostras(offset=137.5))
    assert cal.offset_s == pytest.approx(137.5, abs=0.01)
    assert cal.inliers == 20
    assert cal.confidence == 1.0
    assert cal.slope == pytest.approx(1.0, abs=0.01)


def test_offset_converts_both_ways() -> None:
    cal = fit(_amostras(offset=100.0))
    assert cal.to_video_s(600_000) == pytest.approx(700.0)
    assert cal.to_timeline_ms(700.0) == 600_000


def test_unreadable_frames_are_expected_not_fatal() -> None:
    """Tela de carregamento, tela de vitoria e corte de camera nao mostram o
    timer. Isso e normal — nao pode derrubar a calibracao."""
    amostras = _amostras(offset=50.0)
    amostras[0] = (amostras[0][0], None)
    amostras[5] = (amostras[5][0], None)
    amostras[11] = (amostras[11][0], None)
    cal = fit(amostras)
    assert cal.offset_s == pytest.approx(50.0, abs=0.01)
    assert cal.samples == 20
    assert cal.inliers == 17


def test_a_wild_outlier_does_not_move_the_offset() -> None:
    """A razao de ser da mediana.

    Minimos quadrados com um outlier de 600s produz um ajuste que nao passa
    perto de NENHUM ponto — e sai com cara de precisao.
    """
    amostras = _amostras(offset=30.0)
    v, g = amostras[7]
    amostras[7] = (v, g + 600)  # OCR leu um numero completamente errado

    cal = fit(amostras)
    assert cal.offset_s == pytest.approx(30.0, abs=0.01)
    assert cal.inliers == 19
    assert len(cal.rejected) == 1


def test_several_outliers_still_recover() -> None:
    amostras = _amostras(offset=12.0, n=20)
    for i in (2, 9, 14):
        v, g = amostras[i]
        amostras[i] = (v, g + 300)
    cal = fit(amostras)
    assert cal.offset_s == pytest.approx(12.0, abs=0.01)
    assert cal.inliers == 17


def test_small_jitter_is_tolerated() -> None:
    """O relogio mostra segundos inteiros e o video tem taxa de quadros
    propria — pequena variacao e esperada, nao erro."""
    amostras = [
        (60 + i * 60 + 20.0 + (0.4 if i % 2 else -0.4), 60 + i * 60) for i in range(20)
    ]
    cal = fit(amostras)  # type: ignore[arg-type]
    assert cal.offset_s == pytest.approx(20.0, abs=0.5)
    assert cal.inliers == 20
    assert cal.residual_s < INLIER_TOLERANCE_S


def test_too_few_readings_is_refused_with_a_hint() -> None:
    amostras: list[tuple[float, int | None]] = [(10.0, 5), (20.0, 15), (30.0, None)]
    with pytest.raises(CalibrationError, match="leituras de relogio") as e:
        fit(amostras)
    assert e.value.hint and "recorte" in e.value.hint


def test_disagreeing_readings_are_refused() -> None:
    """Melhor recusar do que deslocar todo finding no tempo.

    Leituras dispersas normalmente significam recorte errado, ou um video que
    junta varias partidas.

    O deslocamento cicla entre tres valores distantes entre si mas proximos o
    bastante para a INCLINACAO continuar ~1.0 — senao o teste tropecaria na
    checagem de velocidade e nunca exercitaria o consenso, que e o que ele
    existe para cobrir.
    """
    offsets = (10.0, 25.0, 40.0)
    amostras: list[tuple[float, int | None]] = [
        (60 + i * 600 + offsets[i % 3], 60 + i * 600) for i in range(15)
    ]
    with pytest.raises(CalibrationError, match="nao concordam"):
        fit(amostras)


def test_a_speed_edited_vod_is_refused() -> None:
    """Inclinacao != 1.0: nenhum deslocamento unico alinha o video inteiro."""
    amostras: list[tuple[float, int | None]] = [
        (60 + i * 60 * 1.25, 60 + i * 60) for i in range(20)
    ]
    with pytest.raises(CalibrationError, match="velocidade"):
        fit(amostras)


def test_slope_is_diagnostic_not_a_fitted_parameter() -> None:
    """A inclinacao e estimada para DETECTAR problema, nao para ajustar — o
    deslocamento sai da mediana, sempre."""
    cal = fit(_amostras(offset=77.0))
    assert cal.slope == pytest.approx(1.0, abs=0.01)
    assert cal.offset_s == pytest.approx(77.0, abs=0.01)


# --------------------------------------------------------------------------
# Verificacao contra a telemetria
# --------------------------------------------------------------------------


def test_verification_confirms_a_good_fit() -> None:
    cal = fit(_amostras(offset=40.0))
    assert not cal.verified
    # Primeiro abate aos 8:20; o relogio na tela mostra o mesmo.
    conferido = verify(cal, known_event_ms=500_000, observed_clock_s=500)
    assert conferido.verified
    assert conferido.offset_s == cal.offset_s


def test_verification_catches_an_internally_consistent_but_wrong_fit() -> None:
    """O caso que so a verificacao pega.

    Um ajuste pode concordar consigo mesmo e ainda assim estar errado — por
    exemplo se o video contiver outra partida. So um evento REAL da telemetria
    desmente isso.
    """
    cal = fit(_amostras(offset=40.0))
    conferido = verify(cal, known_event_ms=500_000, observed_clock_s=460)
    assert not conferido.verified


def test_verification_without_a_reading_leaves_the_fit_alone() -> None:
    """Nao conseguir ler o quadro de conferencia nao e desmentir o ajuste."""
    cal = fit(_amostras(offset=40.0))
    conferido = verify(cal, known_event_ms=500_000, observed_clock_s=None)
    assert conferido.verified is False
    assert conferido.offset_s == cal.offset_s


def test_describe_says_whether_it_was_verified() -> None:
    """A UI precisa distinguir 'alinhado e conferido' de 'alinhado, eu acho'."""
    cal = fit(_amostras(offset=40.0))
    assert "NAO verificado" in cal.describe()
    assert "NAO verificado" not in verify(cal, 500_000, 500).describe()


def test_slope_check_survives_a_wild_outlier() -> None:
    """A inclinacao e conferida ANTES de filtrar outliers, entao ela precisa
    ser robusta — senao um unico OCR errado reprovaria um video perfeito como
    se fosse acelerado.

    Theil-Sen aguenta ate ~29% de pontos corrompidos. Minimos quadrados nao
    aguenta um.
    """
    amostras = _amostras(offset=30.0, n=20)
    v, g = amostras[7]
    amostras[7] = (v, g + 600)
    cal = fit(amostras)  # nao pode levantar erro de velocidade
    assert cal.slope == pytest.approx(1.0, abs=0.01)
    assert cal.offset_s == pytest.approx(30.0, abs=0.01)
