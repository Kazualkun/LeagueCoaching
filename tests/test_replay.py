"""Testes do intertravamento de conformidade.

Estes sao os testes mais importantes do repositorio. Todo o resto do projeto,
se quebrar, entrega um relatorio ruim. Se ESTE quebrar, o RiftCoach fala com o
client do League durante uma partida ao vivo — e a promessa de capa do README
deixa de ser verdade.

A propriedade testada e uma assimetria: existe exatamente UM caminho que
prossegue (200 com corpo na forma certa) e todo o resto recusa. Cada teste
abaixo cobre uma forma de "resto".
"""

from __future__ import annotations

import ast
import json
import re
import struct
from pathlib import Path

import httpx
import pytest
import respx

from riftcoach.core.errors import LiveGameRefused
from riftcoach.replay.controller import (
    DRIFT_TOLERANCE_S,
    LEAD_IN_S,
    Calibration,
    ReplayController,
)
from riftcoach.replay.guard import (
    BASE,
    PlaybackState,
    ReplayGuard,
    cert_path,
    is_replay_running,
)
from riftcoach.replay.rofl import (
    RoflMeta,
    find_replay,
    match_id_from_filename,
    read_metadata,
)

PLAYBACK = f"{BASE}/replay/playback"
RAIZ = Path(__file__).resolve().parent.parent

BOM = {"time": 930.5, "length": 2116.0, "paused": False, "seeking": False, "speed": 1.0}


def _guard() -> ReplayGuard:
    return ReplayGuard(httpx.AsyncClient())


# --------------------------------------------------------------------------
# O unico caminho que prossegue
# --------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_a_running_replay_is_the_only_thing_that_passes() -> None:
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    estado = await _guard().assert_replay_mode()
    assert estado.time == 930.5
    assert estado.length == 2116.0


# --------------------------------------------------------------------------
# Tudo o mais recusa
# --------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_a_404_is_treated_as_a_possible_live_game() -> None:
    """O caso mais importante do modulo.

    /replay/* some durante uma partida ao vivo enquanto /liveclientdata/*
    continua respondendo. Um 404 aqui nao e "nao achei a rota" — e indicio
    POSITIVO de que pode haver partida em andamento.
    """
    respx.get(PLAYBACK).mock(return_value=httpx.Response(404))
    with pytest.raises(LiveGameRefused) as ex:
        await _guard().assert_replay_mode()
    assert "404" in ex.value.message
    assert "partida ao vivo" in (ex.value.hint or "")


@respx.mock
@pytest.mark.asyncio
async def test_a_connection_error_refuses() -> None:
    """Client fechado. Nao da para PROVAR que nao ha partida ao vivo, entao
    recusamos — falha fechado significa exatamente isto."""
    respx.get(PLAYBACK).mock(side_effect=httpx.ConnectError("recusada"))
    with pytest.raises(LiveGameRefused):
        await _guard().assert_replay_mode()


@respx.mock
@pytest.mark.asyncio
async def test_a_timeout_refuses() -> None:
    respx.get(PLAYBACK).mock(side_effect=httpx.ReadTimeout("demorou"))
    with pytest.raises(LiveGameRefused) as ex:
        await _guard().assert_replay_mode()
    assert "ambiguo" in (ex.value.hint or "").lower()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
async def test_any_non_success_status_refuses(status: int) -> None:
    respx.get(PLAYBACK).mock(return_value=httpx.Response(status))
    with pytest.raises(LiveGameRefused):
        await _guard().assert_replay_mode()


@respx.mock
@pytest.mark.asyncio
async def test_a_200_that_is_not_json_refuses() -> None:
    """Um proxy local devolvendo HTML com 200 nao prova que ha replay
    rodando."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, text="<html>oi</html>"))
    with pytest.raises(LiveGameRefused):
        await _guard().assert_replay_mode()


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corpo",
    [
        {},  # sem campo nenhum
        {"paused": False},  # sem `time`
        {"time": "nao e numero"},
        [],  # lista em vez de objeto
        "uma string",
        None,
    ],
)
async def test_a_200_with_the_wrong_shape_refuses(corpo: object) -> None:
    """Validar a FORMA, e nao so o status, e parte do intertravamento: 200 com
    corpo vazio nao prova nada."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=corpo))
    with pytest.raises(LiveGameRefused):
        await _guard().assert_replay_mode()


def test_playback_state_rejects_garbage_directly() -> None:
    with pytest.raises(LiveGameRefused):
        PlaybackState.parse({"nada": "aqui"})
    with pytest.raises(LiveGameRefused):
        PlaybackState.parse("nem isso")


# --------------------------------------------------------------------------
# Revalidacao: antes de CADA requisicao
# --------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_every_request_revalidates() -> None:
    """O usuario pode sair do replay por alt-tab e cair na selecao de campeoes
    no meio da analise. Cachear a verificacao seria exatamente o bug que este
    modulo existe para impedir."""
    sonda = respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.post(f"{BASE}/replay/render").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/liveclientdata/gamestats").mock(
        return_value=httpx.Response(200, json={"gameTime": 900.0})
    )

    g = _guard()
    await g.post("/replay/render", {"cameraMode": "fps"})
    await g.post("/replay/render", {"cameraMode": "fps"})
    await g.get("/liveclientdata/gamestats")

    assert sonda.call_count == 3, "uma verificacao por requisicao, sem cache"


@respx.mock
@pytest.mark.asyncio
async def test_a_replay_that_closes_mid_session_stops_everything() -> None:
    """Primeira chamada passa, segunda encontra o replay fechado."""
    respx.get(PLAYBACK).mock(
        side_effect=[
            httpx.Response(200, json=BOM),
            httpx.Response(404),
        ]
    )
    respx.post(f"{BASE}/replay/render").mock(return_value=httpx.Response(200, json={}))

    g = _guard()
    await g.post("/replay/render", {"cameraMode": "fps"})
    with pytest.raises(LiveGameRefused):
        await g.post("/replay/render", {"cameraMode": "fps"})


@respx.mock
@pytest.mark.asyncio
async def test_losing_the_connection_after_the_probe_still_refuses() -> None:
    """A janela entre a verificacao e a requisicao tambem e ambigua: o replay
    pode ter sido fechado nesse meio tempo."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.post(f"{BASE}/replay/render").mock(side_effect=httpx.ConnectError("caiu"))
    with pytest.raises(LiveGameRefused):
        await _guard().post("/replay/render", {"cameraMode": "fps"})


@respx.mock
@pytest.mark.asyncio
async def test_is_replay_running_never_raises() -> None:
    """A UI chama isto para decidir se mostra o botao. Recusa vira False, que
    e o valor conservador — e o unico lugar do projeto onde ela nao propaga."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(404))
    assert await is_replay_running() is False


# --------------------------------------------------------------------------
# TLS
# --------------------------------------------------------------------------


def test_the_riot_certificate_ships_with_the_package() -> None:
    caminho = cert_path()
    assert caminho.exists(), "certs/riotgames.pem precisa estar versionado"
    texto = caminho.read_text(encoding="utf-8")
    assert texto.startswith("-----BEGIN CERTIFICATE-----")
    assert "-----END CERTIFICATE-----" in texto


def test_the_certificate_actually_loads_as_a_trust_store() -> None:
    import ssl

    ctx = ssl.create_default_context(cafile=str(cert_path()))
    certs = ctx.get_ca_certs()
    assert len(certs) == 1
    assunto = dict(x[0] for x in certs[0]["subject"])  # type: ignore[index,misc]
    assert "Riot Games" in assunto.get("organizationName", "")


def _code_constants(arquivo: Path) -> list[object]:
    """Constantes que estao no CODIGO, sem docstrings.

    A primeira versao destes testes casava texto cru e acusava o proprio
    guard.py, porque o docstring dele diz "NUNCA verify=False". Um teste
    arquitetural que da falso positivo na prosa que o explica ensina as
    pessoas a adicionar excecao — que e o oposto do que ele existe para fazer.
    """
    arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    for no in ast.walk(arvore):
        if isinstance(
            no, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            corpo = getattr(no, "body", [])
            if (
                corpo
                and isinstance(corpo[0], ast.Expr)
                and isinstance(corpo[0].value, ast.Constant)
                and isinstance(corpo[0].value.value, str)
            ):
                docstrings.add(id(corpo[0].value))
    return [
        n.value
        for n in ast.walk(arvore)
        if isinstance(n, ast.Constant) and id(n) not in docstrings
    ]


def test_verification_is_never_disabled_anywhere() -> None:
    """`verify=False` derrubaria a protecao contra um MITM local alimentando o
    app com estado de jogo fabricado. Nao pode existir em lugar nenhum."""
    ofensores: list[str] = []
    for arquivo in (RAIZ / "riftcoach").rglob("*.py"):
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        for no in ast.walk(arvore):
            if not isinstance(no, ast.Call):
                continue
            for kw in no.keywords:
                if (
                    kw.arg == "verify"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    ofensores.append(f"{arquivo.relative_to(RAIZ)}:{no.lineno}")
    assert not ofensores, "TLS desabilitado em: " + ", ".join(ofensores)


# --------------------------------------------------------------------------
# A garantia arquitetural
# --------------------------------------------------------------------------


def test_no_module_outside_replay_talks_to_the_client() -> None:
    """O COMPLIANCE.md promete que uma regra de CI quebra o build se qualquer
    modulo fora de `riftcoach/replay/` referenciar a porta 2999.

    Implementado como teste, e nao como import-linter: uma dependencia a menos,
    roda na mesma suite, e a mensagem de erro aponta o arquivo em vez de um
    codigo de regra.

    Se este teste falhar, NAO adicione uma excecao. A resposta certa e mover o
    codigo para dentro de `riftcoach/replay/guard.py`, que e o unico lugar onde
    a verificacao de modo replay acontece.
    """
    permitido = RAIZ / "riftcoach" / "replay"
    porta = re.compile(r"\b2999\b")
    ofensores: list[str] = []

    for arquivo in (RAIZ / "riftcoach").rglob("*.py"):
        if permitido in arquivo.parents:
            continue
        # So o CODIGO conta. Um docstring que EXPLICA por que o modulo nao
        # fala com a porta 2999 nao e uma violacao — e documentacao da regra.
        for constante in _code_constants(arquivo):
            if isinstance(constante, int) and constante == 2999:
                ofensores.append(str(arquivo.relative_to(RAIZ)))
                break
            if isinstance(constante, str) and porta.search(constante):
                ofensores.append(str(arquivo.relative_to(RAIZ)))
                break

    assert not ofensores, (
        "modulos fora de riftcoach/replay/ referenciam a porta 2999: "
        + ", ".join(ofensores)
    )


def test_the_api_layer_goes_through_the_guard() -> None:
    """A rota /seek nao pode abrir conexao propria com o client."""
    texto = (RAIZ / "riftcoach" / "api" / "app.py").read_text(encoding="utf-8")
    assert "open_guard" in texto, "a API precisa usar o guard"
    assert "127.0.0.1:2999" not in texto


# --------------------------------------------------------------------------
# Controlador: relogios e navegacao
# --------------------------------------------------------------------------


def test_calibration_maps_both_ways() -> None:
    cal = Calibration(offset_s=42.0, measured_at_replay_s=100.0)
    assert cal.to_replay_s(60_000) == 102.0
    assert cal.to_timeline_ms(102.0) == 60_000


def test_calibration_never_seeks_before_zero() -> None:
    """Um offset negativo grande com um finding no minuto 1 daria tempo
    negativo, e o client rejeitaria a requisicao inteira."""
    cal = Calibration(offset_s=-500.0, measured_at_replay_s=0.0)
    assert cal.to_replay_s(1000) == 0.0


@respx.mock
@pytest.mark.asyncio
async def test_seek_lands_before_the_moment_not_on_it() -> None:
    """A regra de UX que justifica o modulo: pular para o instante exato da
    morte mostra a consequencia. O erro aconteceu 5 a 10 segundos antes."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.get(f"{BASE}/liveclientdata/gamestats").mock(
        return_value=httpx.Response(200, json={"gameTime": 900.0})
    )
    capturado: dict[str, object] = {}

    def registrar(request: httpx.Request) -> httpx.Response:
        capturado.update(json.loads(request.content))
        return httpx.Response(200, json={})

    respx.post(f"{BASE}/replay/playback").mock(side_effect=registrar)

    c = ReplayController(_guard())
    alvo = await c.seek_to_ms(1_095_657)

    # offset = 930.5 - 900.0 = 30.5; alvo = 1095.657 + 30.5 - 8.0
    assert alvo == pytest.approx(1118.157, abs=0.01)
    assert capturado["time"] == pytest.approx(alvo, abs=0.01)
    assert capturado["paused"] is False


@respx.mock
@pytest.mark.asyncio
async def test_the_lead_in_is_eight_seconds() -> None:
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.get(f"{BASE}/liveclientdata/gamestats").mock(
        return_value=httpx.Response(200, json={"gameTime": 930.5})
    )
    respx.post(f"{BASE}/replay/playback").mock(return_value=httpx.Response(200, json={}))

    c = ReplayController(_guard())
    sem_recuo = await c.seek_to_ms(600_000, lead_in_s=0.0)
    com_recuo = await c.seek_to_ms(600_000)
    assert sem_recuo - com_recuo == pytest.approx(LEAD_IN_S)


@respx.mock
@pytest.mark.asyncio
async def test_calibration_refuses_when_the_game_clock_is_unreadable() -> None:
    """Sem os dois relogios nao da para alinhar, e alinhar errado e pior que
    nao alinhar: um coach que pula 40 segundos fora perde a confianca na
    primeira vez."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.get(f"{BASE}/liveclientdata/gamestats").mock(
        return_value=httpx.Response(200, json={"algoOutro": 1})
    )
    with pytest.raises(LiveGameRefused):
        await ReplayController(_guard()).calibrate()


@respx.mock
@pytest.mark.asyncio
async def test_drift_is_detected_when_the_user_navigates_manually() -> None:
    # calibrate() sonda DUAS vezes: uma em assert_replay_mode e outra dentro
    # do guard.get. Um calibrate seguido de um check_drift consome quatro.
    respx.get(PLAYBACK).mock(
        side_effect=[
            httpx.Response(200, json=BOM),
            httpx.Response(200, json=BOM),
            httpx.Response(200, json={**BOM, "time": 1200.0}),
            httpx.Response(200, json={**BOM, "time": 1200.0}),
        ]
    )
    respx.get(f"{BASE}/liveclientdata/gamestats").mock(
        return_value=httpx.Response(200, json={"gameTime": 900.0})
    )
    c = ReplayController(_guard())
    await c.calibrate()
    deriva = await c.check_drift()
    assert deriva > DRIFT_TOLERANCE_S
    assert c.drift_events == 1


@respx.mock
@pytest.mark.asyncio
async def test_a_camera_failure_does_not_lose_the_seek() -> None:
    """Camera e polimento; perder a navegacao por causa dela seria trocar o
    essencial pelo acessorio."""
    respx.get(PLAYBACK).mock(return_value=httpx.Response(200, json=BOM))
    respx.post(f"{BASE}/replay/render").mock(return_value=httpx.Response(500))

    c = ReplayController(_guard(), calibration=Calibration(0.0, 0.0))
    await c.focus_camera("Garen")  # nao pode levantar


# --------------------------------------------------------------------------
# .rofl — somente metadados
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nome", "esperado"),
    [
        ("BR1-3239179616.rofl", "BR1_3239179616"),
        ("na1-123456.rofl", "NA1_123456"),
        ("EUW1-987.rofl", "EUW1_987"),
        ("BR1_3239179616.rofl", None),
        ("qualquercoisa.rofl", None),
        ("BR1-abc.rofl", None),
    ],
)
def test_the_filename_identifies_the_match(nome: str, esperado: str | None) -> None:
    """Os clients gravam <PLATAFORMA>-<GAMEID>.rofl, e isso ja e tudo o que
    precisamos para casar o arquivo com o cache."""
    assert match_id_from_filename(Path(nome)) == esperado


def test_a_missing_file_degrades_instead_of_raising(tmp_path: Path) -> None:
    meta = read_metadata(tmp_path / "BR1-123.rofl")
    assert meta.source == "filename"
    assert meta.match_id == "BR1_123"
    assert meta.stats == []


def test_a_file_that_is_not_a_rofl_degrades(tmp_path: Path) -> None:
    p = tmp_path / "BR1-123.rofl"
    p.write_bytes(b"NAOEROFL" + b"\x00" * 400)
    meta = read_metadata(p)
    assert meta.source == "filename"
    assert meta.match_id == "BR1_123"


def test_a_truncated_header_degrades(tmp_path: Path) -> None:
    """Os offsets do cabecalho mudaram entre versoes maiores do client. Um
    arquivo de uma versao futura precisa degradar, nao derrubar a analise."""
    p = tmp_path / "BR1-123.rofl"
    p.write_bytes(b"RIOT" + b"\x00" * 100)
    assert read_metadata(p).source == "filename"


def test_an_absurd_metadata_length_is_rejected(tmp_path: Path) -> None:
    """Sem o teto de sanidade, um struct mal interpretado mandaria o app ler
    gigabytes de um arquivo local."""
    p = tmp_path / "BR1-123.rofl"
    corpo = bytearray(b"RIOT" + b"\x00" * 300)
    corpo[262:270] = struct.pack("<II", 270, 0xFFFFFFFF)
    p.write_bytes(bytes(corpo))
    assert read_metadata(p).source == "filename"


def test_a_well_formed_header_is_read(tmp_path: Path) -> None:
    meta_json = json.dumps(
        {
            "gameLength": 2116000,
            # statsJson e uma STRING de JSON dentro do JSON. Sim, de verdade.
            "statsJson": json.dumps([{"NAME": "Garen", "CHAMPIONS_KILLED": "4"}]),
        }
    ).encode("utf-8")

    p = tmp_path / "BR1-3239179616.rofl"
    cabecalho = bytearray(b"RIOT" + b"\x00" * 300)
    offset = 300
    cabecalho[262:270] = struct.pack("<II", offset, len(meta_json))
    p.write_bytes(bytes(cabecalho[:offset]) + meta_json)

    meta = read_metadata(p)
    assert meta.source == "header"
    assert meta.game_length_ms == 2_116_000
    assert meta.duration_s == 2116
    assert meta.stats[0]["NAME"] == "Garen"
    assert meta.match_id == "BR1_3239179616"


def test_stats_json_that_is_broken_does_not_break_the_read(tmp_path: Path) -> None:
    meta_json = json.dumps({"gameLength": 1000, "statsJson": "{nao e json"}).encode()
    p = tmp_path / "BR1-1.rofl"
    cabecalho = bytearray(b"RIOT" + b"\x00" * 300)
    cabecalho[262:270] = struct.pack("<II", 300, len(meta_json))
    p.write_bytes(bytes(cabecalho[:300]) + meta_json)

    meta = read_metadata(p)
    assert meta.game_length_ms == 1000
    assert meta.stats == []


def test_find_replay_matches_by_name(tmp_path: Path) -> None:
    (tmp_path / "BR1-3239179616.rofl").write_bytes(b"RIOT")
    (tmp_path / "NA1-999.rofl").write_bytes(b"RIOT")
    achado = find_replay("BR1_3239179616", tmp_path)
    assert achado is not None and achado.name == "BR1-3239179616.rofl"
    assert find_replay("BR1_000", tmp_path) is None


def test_find_replay_handles_a_missing_directory(tmp_path: Path) -> None:
    assert find_replay("BR1_1", tmp_path / "nao-existe") is None


def test_rofl_meta_defaults_to_an_empty_stats_list() -> None:
    assert RoflMeta(path=Path("x.rofl")).stats == []
