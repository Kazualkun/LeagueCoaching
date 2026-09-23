"""Testes da leitura do game.cfg do League.

Este modulo existe por causa de uma descoberta que so apareceu contra o client
de verdade: a Replay API vem DESLIGADA de fabrica. Sem essa deteccao, o 404 em
/replay/playback era sempre explicado como "pode haver partida ao vivo" — o
que e alarmante, errado, e o que TODO usuario novo receberia.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from riftcoach.core.errors import LiveGameRefused
from riftcoach.replay import gamecfg
from riftcoach.replay.guard import BASE, ReplayGuard

# Trecho real do game.cfg deste projeto, encurtado.
CFG_REAL = """[General]
CfgVersion=16.15.801.3452
Height=900
Width=1600
WindowMode=2
[HUD]
MinimapScale=1.2900
GlobalScale=0.2800
[Replay]
ShowReplayTimeControls=1
EnableDirectedCamera=1
"""


@pytest.fixture
def cfg_desligada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "game.cfg"
    p.write_text(CFG_REAL, encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(p))
    return p


@pytest.fixture
def cfg_ligada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "game.cfg"
    p.write_text(CFG_REAL.replace("[General]", "[General]\nEnableReplayApi=1"), encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(p))
    return p


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_reads_sections_and_values(cfg_desligada: Path) -> None:
    cfg = gamecfg.parse(cfg_desligada)
    assert cfg.client_version == "16.15.801.3452"
    assert cfg.resolution == (1600, 900)
    assert cfg.get("Replay", "ShowReplayTimeControls") == "1"


def test_detects_the_api_as_disabled_by_default(cfg_desligada: Path) -> None:
    """O estado de fabrica. A documentacao da Riot: "By default the Replay API
    is disabled"."""
    assert gamecfg.parse(cfg_desligada).replay_api_enabled is False


def test_detects_the_api_as_enabled(cfg_ligada: Path) -> None:
    assert gamecfg.parse(cfg_ligada).replay_api_enabled is True


@pytest.mark.parametrize("valor", ["0", "", "false", "False"])
def test_explicit_off_values_count_as_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, valor: str
) -> None:
    p = tmp_path / "game.cfg"
    p.write_text(f"[General]\nEnableReplayApi={valor}\n", encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(p))
    assert gamecfg.parse(p).replay_api_enabled is False


def test_hud_scale_is_exposed_raw(cfg_desligada: Path) -> None:
    """Cru de proposito.

    `GlobalScale` e normalizado pelo slider e a faixa que ele representa nao e
    documentada. Inventar uma conversao para multiplicador produziria ROIs
    errados com aparencia de precisao — exatamente o modo de falha que a camada
    de visao existe para evitar.
    """
    cfg = gamecfg.parse(cfg_desligada)
    assert cfg.hud_scale_raw == pytest.approx(0.28)
    assert cfg.minimap_scale_raw == pytest.approx(1.29)


def test_malformed_values_do_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "game.cfg"
    p.write_text("[HUD]\nGlobalScale=abc\n[General]\nWidth=x\nHeight=y\n", encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(p))
    cfg = gamecfg.parse(p)
    assert cfg.hud_scale_raw is None
    assert cfg.resolution is None


def test_missing_file_is_distinguishable_from_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """None e False pedem acoes diferentes do usuario.

    "esta desligada, ligue assim" e acionavel; "nao achei seu game.cfg" pede
    outra coisa. Colapsar os dois daria a instrucao errada.
    """
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(tmp_path / "nao-existe.cfg"))
    habilitada, msg = gamecfg.replay_api_status()
    assert habilitada is None
    assert "RIFTCOACH_GAME_CFG" in msg


def test_status_message_is_actionable(cfg_desligada: Path) -> None:
    habilitada, msg = gamecfg.replay_api_status()
    assert habilitada is False
    assert "EnableReplayApi=1" in msg
    assert "[General]" in msg
    assert str(cfg_desligada) in msg


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------


def test_enabling_writes_a_backup_first(cfg_desligada: Path) -> None:
    """E o arquivo de configuracao do JOGO do usuario, nao nosso."""
    original = cfg_desligada.read_text(encoding="utf-8")
    backup = gamecfg.enable_replay_api(gamecfg.parse(cfg_desligada))
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    assert gamecfg.parse(cfg_desligada).replay_api_enabled is True


def test_enabling_preserves_the_other_settings(cfg_desligada: Path) -> None:
    gamecfg.enable_replay_api(gamecfg.parse(cfg_desligada))
    cfg = gamecfg.parse(cfg_desligada)
    assert cfg.client_version == "16.15.801.3452"
    assert cfg.minimap_scale_raw == pytest.approx(1.29)
    assert cfg.get("Replay", "EnableDirectedCamera") == "1"


def test_enabling_without_a_general_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / "game.cfg"
    p.write_text("[HUD]\nGlobalScale=0.5\n", encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(p))
    gamecfg.enable_replay_api(gamecfg.parse(p))
    assert gamecfg.parse(p).replay_api_enabled is True


# --------------------------------------------------------------------------
# O diagnostico do guard
# --------------------------------------------------------------------------


@respx.mock
async def test_a_404_blames_the_config_when_the_api_is_off(
    cfg_desligada: Path,
) -> None:
    """A regressao que motivou tudo isto.

    Antes, um 404 sempre virava "pode haver partida ao vivo em andamento".
    Como a API vir desligada e o estado PADRAO de qualquer instalacao, todo
    usuario novo recebia um aviso alarmante quando o que faltava era uma linha
    de configuracao. Diagnostico errado gasta mais tempo do que erro nenhum.
    """
    respx.get(f"{BASE}/replay/playback").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(LiveGameRefused) as e:
            await ReplayGuard(client).assert_replay_mode()

    assert "desligada" in e.value.message
    assert e.value.hint and "EnableReplayApi=1" in e.value.hint
    # E NAO pode acusar partida ao vivo, que seria o diagnostico errado.
    assert "partida" not in e.value.message


@respx.mock
async def test_a_404_still_refuses_when_the_api_is_on(cfg_ligada: Path) -> None:
    """Com a API ligada, um 404 volta a ser o sinal original: pode haver
    partida ao vivo. O comportamento nunca muda — so a explicacao."""
    respx.get(f"{BASE}/replay/playback").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(LiveGameRefused) as e:
            await ReplayGuard(client).assert_replay_mode()

    assert e.value.hint and "nunca roda durante uma partida" in e.value.hint


@respx.mock
async def test_the_guard_still_fails_closed_either_way(cfg_desligada: Path) -> None:
    """A propriedade que nao pode regredir: 404 e recusa, qualquer que seja o
    motivo. Melhorar a mensagem nunca pode virar permitir a operacao."""
    respx.get(f"{BASE}/replay/playback").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        with pytest.raises(LiveGameRefused):
            await ReplayGuard(client).assert_replay_mode()


# --------------------------------------------------------------------------
# Patch do replay contra o patch instalado
# --------------------------------------------------------------------------


@pytest.fixture
def instalacao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Uma pasta do League com a forma da real: Config/game.cfg e Game/."""
    raiz = tmp_path / "League of Legends"
    (raiz / "Config").mkdir(parents=True)
    (raiz / "Game").mkdir()
    cfg = raiz / "Config" / "game.cfg"
    cfg.write_text(CFG_REAL, encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(cfg))
    return raiz


def _versao(raiz: Path, versao: str) -> None:
    (raiz / "Game" / "compat-version-metadata.json").write_text(
        f'{{\n    "version": "{versao}"\n}}\n', encoding="utf-8"
    )


def test_o_patch_instalado_sai_do_arquivo_de_versao_do_jogo(instalacao: Path) -> None:
    """Valor copiado da instalacao real. O `CfgVersion` do mesmo game.cfg dizia
    16.15 com o jogo ja no 16.19 — por isso ele nao serve para isto."""
    _versao(instalacao, "16.19.8207193+branch.releases-16-19.code.public.content.release")
    assert gamecfg.installed_patch() == "16.19"


def test_partida_do_patch_anterior_explica_por_que_o_replay_nao_abre(instalacao: Path) -> None:
    """O caso que chegou do usuario: o client registrou "Replay is incompatible
    due to major-minor version mismatch: rofl=16.18 game=16.19", e o RiftCoach
    mandava baixar o replay e dar play."""
    _versao(instalacao, "16.19.8207193+branch.releases-16-19")
    problema = gamecfg.replay_patch_mismatch("16.18")
    assert problema is not None
    assert "16.18" in problema and "16.19" in problema


def test_partida_do_patch_atual_nao_gera_aviso(instalacao: Path) -> None:
    _versao(instalacao, "16.19.8207193+branch.releases-16-19")
    assert gamecfg.replay_patch_mismatch("16.19") is None


@pytest.mark.parametrize("conteudo", [None, "nao e json", '{"outra": 1}', '{"version": "x.y"}'])
def test_sem_versao_legivel_nao_ha_aviso(instalacao: Path, conteudo: str | None) -> None:
    """Aviso chutado seria pior que nenhum: sem saber a versao, silencio."""
    if conteudo is not None:
        (instalacao / "Game" / "compat-version-metadata.json").write_text(
            conteudo, encoding="utf-8"
        )
    assert gamecfg.installed_patch() is None
    assert gamecfg.replay_patch_mismatch("16.18") is None
