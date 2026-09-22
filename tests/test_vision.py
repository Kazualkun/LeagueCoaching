"""Testes da camada de visao (etapa 7).

Geometria de ROI e matematica pura e testada exaustivamente. A extracao de
frames e testada contra um video SINTETICO gerado aqui — cada quadro carrega o
proprio numero desenhado, entao da para afirmar exatamente qual quadro voltou.
E o unico jeito honesto de provar que a caminhada apos o seek funciona sem ter
gravacao real de partida.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("av", reason="a camada de visao e um extra opcional")
pytest.importorskip("PIL", reason="a camada de visao e um extra opcional")

from riftcoach.vision import rois
from riftcoach.vision.rois import ALL, GAME_CLOCK, MINIMAP, Anchor, Roi
from riftcoach.vision.sampler import (
    OFFSETS_S,
    VideoError,
    extract_at,
    iter_calibration_points,
    moment_timestamps,
    probe,
)

# --------------------------------------------------------------------------
# Geometria de ROI
# --------------------------------------------------------------------------


def test_roi_boxes_are_inside_the_screen() -> None:
    for w, h in ((1920, 1080), (2560, 1440), (3840, 2160), (1280, 720)):
        for r in ALL:
            x0, y0, x1, y1 = r.pixels(w, h)
            assert 0 <= x0 < x1 <= w, f"{r.name} em {w}x{h}"
            assert 0 <= y0 < y1 <= h, f"{r.name} em {w}x{h}"


def test_rois_scale_with_resolution_not_with_pixels() -> None:
    """Dobrar a resolucao dobra o recorte — a HUD e proporcional a altura."""
    a = GAME_CLOCK.pixels(1920, 1080)
    b = GAME_CLOCK.pixels(3840, 2160)
    larg_a, larg_b = a[2] - a[0], b[2] - b[0]
    assert larg_b == pytest.approx(larg_a * 2, abs=2)


def test_ultrawide_keeps_the_hud_in_the_corner() -> None:
    """A armadilha que motiva o modulo inteiro.

    A HUD do LoL escala com a ALTURA e se ancora nas bordas. Se normalizassemos
    por largura, num 21:9 o minimapa iria para o meio da tela e todo recorte
    sairia errado — em silencio, que e o pior jeito.
    """
    largo = MINIMAP.pixels(2560, 1080)  # 21:9
    normal = MINIMAP.pixels(1920, 1080)  # 16:9

    # Mesmo tamanho: depende so da altura, que e igual nos dois.
    assert (largo[2] - largo[0]) == (normal[2] - normal[0])
    # Mesma distancia ate a borda direita.
    assert (2560 - largo[2]) == (1920 - normal[2])
    # E continua colado no canto, nao no meio.
    assert largo[0] > 2560 * 0.75


def test_hud_scale_shrinks_every_roi() -> None:
    """Quem joga com HUD a 80% tem todo elemento menor e mais perto do canto."""
    cheio = GAME_CLOCK.pixels(1920, 1080, scale=1.0)
    menor = GAME_CLOCK.pixels(1920, 1080, scale=0.8)
    assert (menor[2] - menor[0]) < (cheio[2] - cheio[0])
    assert menor[2] > cheio[2], "com HUD menor, o relogio encosta mais na borda"


@pytest.mark.parametrize(
    ("anchor", "esperado"),
    [
        (Anchor.TOP_LEFT, "esquerda-cima"),
        (Anchor.TOP_RIGHT, "direita-cima"),
        (Anchor.BOTTOM_LEFT, "esquerda-baixo"),
        (Anchor.BOTTOM_RIGHT, "direita-baixo"),
    ],
)
def test_every_anchor_pulls_toward_its_own_corner(anchor: Anchor, esperado: str) -> None:
    """Deslocamento sempre cresce para DENTRO da tela, qualquer que seja o
    canto — senao cada ROI precisaria inverter sinal na mao."""
    r = Roi("t", anchor, dx=0.05, dy=0.05, w=0.1, h=0.1)
    x0, y0, x1, y1 = r.pixels(1000, 1000)
    if "esquerda" in esperado:
        assert x0 < 500
    else:
        assert x1 > 500
    if "cima" in esperado:
        assert y0 < 500
    else:
        assert y1 > 500


def test_center_anchors_stay_centered() -> None:
    r = Roi("t", Anchor.BOTTOM_CENTER, dx=0.0, dy=0.05, w=0.1, h=0.05)
    x0, _, x1, _ = r.pixels(1920, 1080)
    assert (x0 + x1) / 2 == pytest.approx(960, abs=2)


def test_invalid_resolution_is_rejected() -> None:
    for w, h in ((0, 1080), (1920, 0), (-1, -1)):
        with pytest.raises(ValueError, match="resolucao invalida"):
            GAME_CLOCK.pixels(w, h)


def test_roi_names_are_unique() -> None:
    assert len({r.name for r in ALL}) == len(ALL)
    assert set(rois.BY_NAME) == {r.name for r in ALL}


def test_only_cheap_rois_go_to_ocr() -> None:
    """Nunca se paga um VLM para ler um contador de quatro digitos.

    O minimapa e o unico que exige modelo de visao; o resto e OCR barato e
    deterministico.
    """
    assert MINIMAP not in rois.OCR_ROIS
    assert GAME_CLOCK in rois.OCR_ROIS


def test_minimap_is_square() -> None:
    x0, y0, x1, y1 = MINIMAP.pixels(1920, 1080)
    assert (x1 - x0) == pytest.approx(y1 - y0, abs=2)


# --------------------------------------------------------------------------
# Calculo de instantes
# --------------------------------------------------------------------------


def test_moment_timestamps_apply_the_clock_offset() -> None:
    """`video_s = timeline_s + offset`. Sem essa calibracao os frames saem do
    lugar e toda observacao vira ruido."""
    ts = moment_timestamps(600_000, offset_s=12.5)
    assert ts == [600.0 + 12.5 + d for d in OFFSETS_S]


def test_offsets_look_before_and_after() -> None:
    """t-5s mostra a decisao se formando, t+2s confirma o desfecho."""
    assert min(OFFSETS_S) < 0 < max(OFFSETS_S)


def test_calibration_points_avoid_the_extremes(tmp_path: Path) -> None:
    """Comeco tem tela de carregamento e fim tem tela de vitoria — nos dois o
    relogio da HUD nao esta visivel."""
    from riftcoach.vision.sampler import VideoInfo

    info = VideoInfo(tmp_path / "x.mp4", duration_s=2000.0, width=1920, height=1080, fps=30)
    pts = list(iter_calibration_points(info, n=20))
    assert len(pts) == 20
    assert pts[0] >= 2000 * 0.09
    assert pts[-1] <= 2000 * 0.91
    assert pts == sorted(pts)


def test_calibration_points_handle_a_zero_length_video(tmp_path: Path) -> None:
    from riftcoach.vision.sampler import VideoInfo

    info = VideoInfo(tmp_path / "x.mp4", duration_s=0.0, width=0, height=0, fps=0)
    assert list(iter_calibration_points(info)) == []


# --------------------------------------------------------------------------
# Extracao de frames (video sintetico)
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Video de 10s a 10fps onde CADA QUADRO carrega o proprio indice.

    Desenhar o numero em cada quadro e o que permite afirmar qual quadro
    voltou. Com quadros identicos, um seek errado passaria despercebido.

    O intervalo entre keyframes e longo de proposito (gop_size alto): e
    justamente com GOP longo que a caminhada apos o seek importa.
    """
    import av
    from PIL import Image, ImageDraw

    caminho = tmp_path_factory.mktemp("video") / "sintetico.mp4"
    fps = 10
    total = 100  # 10 segundos

    with av.open(str(caminho), mode="w") as container:
        stream = container.add_stream("libx264", rate=fps)
        stream.width = 320
        stream.height = 180
        stream.pix_fmt = "yuv420p"
        stream.gop_size = 50  # keyframe a cada 5 segundos

        for i in range(total):
            img = Image.new("RGB", (320, 180), (0, 0, 0))
            d = ImageDraw.Draw(img)
            # Numero grande e um degrade: dois sinais independentes do indice.
            d.text((20, 70), f"{i:03d}", fill=(255, 255, 255))
            d.rectangle([0, 0, max(1, i * 3), 10], fill=(255, 0, 0))
            frame = av.VideoFrame.from_image(img)
            for pacote in stream.encode(frame):
                container.mux(pacote)
        for pacote in stream.encode():
            container.mux(pacote)

    return caminho


def test_probe_reads_metadata(video: Path) -> None:
    info = probe(video)
    assert info.width == 320
    assert info.height == 180
    assert info.duration_s == pytest.approx(10.0, abs=0.5)
    assert info.fps == pytest.approx(10.0, abs=0.5)


def test_probe_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VideoError, match="nao encontrado"):
        probe(tmp_path / "nao-existe.mp4")


def test_probe_rejects_a_non_video(tmp_path: Path) -> None:
    ruim = tmp_path / "ruim.mp4"
    ruim.write_bytes(b"isto nao e um video")
    with pytest.raises(VideoError):
        probe(ruim)


def test_extraction_lands_on_the_requested_instant(video: Path) -> None:
    """A armadilha de codec numero 1.

    `seek` pousa no KEYFRAME ANTERIOR. Sem decodificar para a frente ate o PTS
    alvo, pedir 7,0s devolveria o keyframe de 5,0s — dois segundos errado, sem
    erro nenhum. Num VOD de partida isso desalinha todo finding.
    """
    quadros = extract_at(video, [7.0])
    assert len(quadros) == 1
    q = quadros[0]
    assert q.t_s == pytest.approx(7.0, abs=0.2), (
        f"caiu em {q.t_s:.2f}s — provavelmente parou no keyframe anterior"
    )
    assert q.drift_s < 0.2


def test_extraction_is_accurate_across_the_whole_video(video: Path) -> None:
    pedidos = [1.0, 3.5, 5.0, 6.2, 8.8]
    quadros = extract_at(video, pedidos)
    assert len(quadros) == len(pedidos)
    for q in quadros:
        assert q.drift_s < 0.2, f"pediu {q.requested_t_s}s, veio {q.t_s}s"


def test_frames_come_back_in_order_and_distinct(video: Path) -> None:
    quadros = extract_at(video, [8.0, 2.0, 5.0])
    tempos = [q.t_s for q in quadros]
    assert tempos == sorted(tempos), "instantes sao ordenados antes de buscar"
    # Quadros distintos: se o seek falhasse, viriam imagens iguais.
    assert len({q.image.tobytes() for q in quadros}) == 3


def test_drift_travels_with_the_frame(video: Path) -> None:
    """Uma observacao com erro de tempo ainda e util — mas so se souber que tem."""
    q = extract_at(video, [4.44])[0]
    assert q.requested_t_s == 4.44
    assert q.drift_s == abs(q.t_s - 4.44)


def test_timestamps_past_the_end_are_skipped_not_fatal(video: Path) -> None:
    """Um momento perto do fim tem t+2s alem do ultimo quadro. Normal, nao erro."""
    quadros = extract_at(video, [5.0, 9999.0, -50.0])
    assert len(quadros) == 1
    assert quadros[0].t_s == pytest.approx(5.0, abs=0.2)


def test_empty_request_returns_empty(video: Path) -> None:
    assert extract_at(video, []) == []


def test_frame_carries_the_video_resolution(video: Path) -> None:
    """Os ROIs precisam da resolucao para virar pixels."""
    q = extract_at(video, [3.0])[0]
    assert (q.width, q.height) == (320, 180)
    x0, y0, x1, y1 = GAME_CLOCK.pixels(q.width, q.height)
    assert q.image.crop((x0, y0, x1, y1)).size == (x1 - x0, y1 - y0)
