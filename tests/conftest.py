"""Isolamento da suite em relacao a maquina que a roda.

Duas regras, e as duas ja foram quebradas na pratica — por isso estao aqui, em
um lugar so, em vez de repetidas em cada teste:

  1. NENHUM teste pode tocar no client do League de verdade.
  2. NENHUM teste pode ler o game.cfg de verdade.

O historico de cada uma:

  - `test_a_refused_seek_is_409_with_an_explanation` abria conexao real com
    127.0.0.1:2999 e so passava onde a porta recusava na hora. Foi corrigido
    com respx — mas quando o guard ganhou a conferencia de impressao digital,
    ela usa SOCKET CRU, que o respx nao intercepta, e os testes voltaram a
    abrir conexao real.

  - `test_a_404_is_treated_as_a_possible_live_game` passou a ler o game.cfg da
    maquina quando o guard aprendeu a diagnosticar a Replay API desligada. Numa
    maquina com o League instalado e a API desligada (o padrao), ele falhava.

Teste que depende do que esta instalado na maquina do desenvolvedor nao prova
nada, e o pior e que ele passa no CI e falha na mao de quem joga.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# game.cfg neutro: existe, tem forma valida, e NAO liga a Replay API. Assim o
# padrao dos testes e o mesmo estado de fabrica que o usuario encontra.
_CFG_NEUTRO = """[General]
CfgVersion=0.0.0.0
Width=1920
Height=1080
[HUD]
GlobalScale=1.0000
"""


@pytest.fixture(autouse=True)
def _sem_client_de_verdade(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Neutraliza as duas portas de saida para a maquina real.

    Testes que precisam exercitar esses caminhos sobrescrevem: basta definir
    `RIFTCOACH_GAME_CFG` na propria fixture (como `tests/test_gamecfg.py`) ou
    monkeypatchar de novo a conferencia de certificado.
    """
    # 1. A conferencia de impressao digital abre socket cru, fora do alcance do
    #    respx. Sem isto, cada teste que chama `open_guard` espera o timeout.
    #
    #    CUIDADO: neutralizar isto faz `open_guard` nunca levantar, e ja
    #    escondeu um bug real — a rota /api/seek chamava `open_guard` FORA do
    #    try, entao uma recusa virava 500 em vez de 409, e nenhum teste
    #    percebeu. Quem for testar o caminho de recusa precisa sobrescrever
    #    este monkeypatch, como faz `test_open_guard_failure_is_409`.
    monkeypatch.setattr(
        "riftcoach.replay.guard.assert_pinned_certificate",
        lambda: "0" * 64,
    )

    # 2. O diagnostico de 404 le o game.cfg. Apontamos para um arquivo nosso
    #    para que o resultado nao dependa de haver League instalado.
    cfg: Path = tmp_path_factory.mktemp("gamecfg") / "game.cfg"
    cfg.write_text(_CFG_NEUTRO, encoding="utf-8")
    monkeypatch.setenv("RIFTCOACH_GAME_CFG", str(cfg))
