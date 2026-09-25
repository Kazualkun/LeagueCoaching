"""A janela monta? Cada tela desenha sem estourar?

Nao e teste de aparencia — nao da para afirmar que algo esta bonito. E teste
de que cada tela CONSTROI: um nome de cor trocado, um atributo que nao existe
mais, um `pack` numa variavel errada. Esse tipo de erro nao aparece no mypy
(tkinter aceita quase tudo como `str`) e, num programa aberto por dois
cliques, ele nao aparece em lugar NENHUM: o processo roda sem console, a
excecao vai para o vazio e a pessoa ve uma janela que nao abre.

O `_tarefa` e trocado por um gravador. As telas dependem de rede, e o que
importa aqui e a montagem — se a tela pediu a tarefa certa, o resto ja tem
teste proprio.
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

import pytest

from riftcoach.core.errors import LiveGameRefused
from riftcoach.gui.app import App, Resultado


# UMA raiz Tk para o modulo inteiro, e nao uma por teste.
#
# Criar e destruir varias raizes no mesmo processo funciona ate a oitava e
# entao o Tcl recusa com "tk wasn't installed properly" — que nao tem nada a
# ver com instalacao e tudo a ver com estado que o destroy nao limpa. Uma so,
# com a tela remontada a cada teste, e mais rapida e nao tem esse teto.
@pytest.fixture(scope="module")
def _janela() -> Any:
    pedidos: list[str] = []

    def falso_tarefa(self: App, fabrica: Any, quando_terminar: Any) -> None:
        pedidos.append(getattr(fabrica, "__name__", "?"))
        # Guardar o retorno deixa o teste entregar a resposta que quiser, e e
        # assim que da para exercitar os caminhos de falha sem rede.
        self._ultimo_retorno = quando_terminar

    original = App._tarefa
    App._tarefa = falso_tarefa  # type: ignore[method-assign]
    try:
        a = App()
    except tk.TclError as e:  # pragma: no cover
        # Sem servidor grafico (CI em Linux) nao ha o que testar.
        #
        # A verificacao acontece AQUI, e nao numa sonda no topo do modulo,
        # porque a sonda criava e destruia uma raiz Tk so para decidir se
        # pulava — e criar/destruir raizes repetidamente e exatamente o que o
        # Tcl nao aguenta. Numa suite inteira isso virava
        # "Can't find a usable tk.tcl" em um teste que passava sozinho.
        App._tarefa = original  # type: ignore[method-assign]
        pytest.skip(f"sem ambiente grafico: {e}")
    for depois in a.root.tk.call("after", "info"):
        a.root.after_cancel(depois)
    a.pedidos = pedidos  # type: ignore[attr-defined]
    yield a
    a.root.destroy()
    App._tarefa = original  # type: ignore[method-assign]


@pytest.fixture
def app(_janela: App) -> Any:
    """A mesma janela, zerada — como se tivesse acabado de abrir."""
    from riftcoach.gui.app import Estado

    _janela.estado = Estado()
    _janela.pedidos.clear()  # type: ignore[attr-defined]
    _janela._limpar()
    return _janela


def _desenhou(a: App) -> int:
    a.root.update_idletasks()
    return len(a.corpo.winfo_children())


def test_a_janela_abre_com_a_trilha_de_passos(app: App) -> None:
    assert app.root.title() == "RiftCoach AI"
    assert len(app._chips) == 4


def test_a_tela_da_chave_monta(app: App) -> None:
    app.tela_chave()
    assert _desenhou(app) > 0
    assert app._passo == 0


def test_a_tela_da_chave_explica_quando_a_anterior_expirou(app: App) -> None:
    """Erro nunca e beco sem saida: a tela tem de dizer o que fazer agora."""
    app.tela_chave(motivo="Unknown apikey")
    textos = _todos_os_textos(app)
    assert any("expiram" in t for t in textos)


def test_a_tela_da_conta_monta_e_avanca_o_passo(app: App) -> None:
    app.tela_conta()
    assert _desenhou(app) > 0
    assert app._passo == 1


def test_conta_ja_conhecida_pula_a_pergunta(app: App) -> None:
    """Rodar de novo nao pode repetir pergunta ja respondida."""
    app.estado.riot_id = "Fulano#BR1"
    app.tela_conta(pular_se_souber=True)
    assert app._passo == 2  # foi direto para a analise


def test_a_tela_de_analise_pede_a_tarefa(app: App) -> None:
    app.estado.riot_id = "Fulano#BR1"
    app.tela_partida()
    assert app._passo == 2
    # A tela agora LISTA as partidas recentes primeiro (a pessoa escolhe qual
    # analisar, inclusive Flex); a analise so comeca depois da escolha.
    assert app.pedidos == ["buscar"]  # type: ignore[attr-defined]


def test_falha_na_analise_vira_tela_de_erro_com_saidas(app: App) -> None:
    app.tela_erro("nenhuma partida encontrada", "jogue uma partida e tente de novo")
    textos = _todos_os_textos(app)
    assert any("nenhuma partida encontrada" in t for t in textos)
    # Tres saidas, e nenhuma delas apaga nada.
    assert any("Tentar de novo" in t for t in textos)
    assert any("Trocar de conta" in t for t in textos)
    assert any("Trocar a chave" in t for t in textos)


def test_a_tela_final_oferece_os_dois_caminhos(app: App) -> None:
    app.estado.resumo = "Garen 3/5/7 · derrota em 35 minutos"
    app.estado.marcacoes = 7
    app.tela_pronto()
    textos = _todos_os_textos(app)
    assert any("Abrir relatório" in t for t in textos)
    assert any("Abrir overlay" in t for t in textos)
    assert any("7 momentos marcados" in t for t in textos)


def test_sem_console_as_saidas_ganham_destino(monkeypatch: pytest.MonkeyPatch) -> None:
    """A causa raiz do botao que "nao fazia nada".

    Aberto por dois cliques, o RiftCoach roda sob pythonw, e ali sys.stdout e
    sys.stderr sao None. O uvicorn monta o log colorido com
    `sys.stdout.isatty()` e estoura ANTES de ligar na porta: o servidor nunca
    subia, e sem stderr a mensagem nao tinha para onde ir. Rodando pelo
    console nada disso aparece — por isso passou tanto tempo escondido.
    """
    import sys as _sys

    from riftcoach.gui.app import _garante_saidas

    monkeypatch.setattr(_sys, "stdout", None)
    monkeypatch.setattr(_sys, "stderr", None)
    _garante_saidas()

    # O que importa nao e o valor de isatty() — no Windows o `nul` e um
    # dispositivo de caractere e responde True, o que so faz o uvicorn
    # escrever cor para o vazio. O que importa e que a chamada EXISTA: era
    # ela, em None, que derrubava o servidor antes de ele ligar na porta.
    for saida in (_sys.stdout, _sys.stderr):
        assert saida is not None
        assert isinstance(saida.isatty(), bool)
        saida.write("descartado")


def test_relatorio_avisa_quando_a_porta_ja_esta_em_uso(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falha ao subir o servidor nao pode morrer calada.

    Sob pythonw nao ha stderr: o uvicorn desiste com sys.exit(), a thread
    morre, e sem tratamento o botao so parecia nao fazer nada. Agora vira
    aviso na barra de status, e o botao aceita nova tentativa.
    """
    from types import SimpleNamespace

    app._preparado = SimpleNamespace(facts=None, report=None, benchmarks=None)
    app._servidor_no_ar = False
    monkeypatch.setattr("riftcoach.api.app.adopt", lambda *a, **k: None)
    monkeypatch.setattr("riftcoach.api.app.esta_no_ar", lambda *a, **k: False)

    def servidor_falso(*, port: int, open_browser: bool) -> None:
        raise SystemExit(3)

    monkeypatch.setattr("riftcoach.api.app.serve", servidor_falso)

    app._abrir_web()

    import time

    limite = time.time() + 2
    while time.time() < limite:
        app._bombear()
        if "ocupada" in app.status.cget("text"):
            break
        time.sleep(0.02)

    assert "ocupada" in app.status.cget("text")
    assert app._servidor_no_ar is False


def test_sem_replay_aberto_a_janela_ensina_em_vez_de_so_falhar(app: App) -> None:
    """O erro mais comum do overlay e nao ter replay rodando. Dizer so 'nao
    encontrei' deixaria a pessoa sem o proximo passo — e 'Sem bordas' e uma
    exigencia que ninguem adivinha."""
    app.estado.resumo = "x"
    app.tela_pronto()
    app._abrir_overlay()
    # A conferencia foi pedida; simulamos a recusa sem motivo acionavel.
    pronto = app._ultimo_retorno  # type: ignore[attr-defined]
    pronto(Resultado(ok=True, dados=LiveGameRefused("Não encontrei nenhum replay rodando.")))
    textos = _todos_os_textos(app)
    assert any("Sem bordas" in t for t in textos)
    assert any("Partidas" in t for t in textos)


def test_motivo_da_recusa_chega_ate_a_janela(app: App) -> None:
    """A Replay API desligada e 'sem replay rodando' pedem coisas diferentes:
    a primeira so sai editando o game.cfg. A janela descartava o diagnostico
    do guard e mandava dar play de novo — conselho que nunca ia funcionar."""
    app.estado.resumo = "x"
    app.tela_pronto()
    app._abrir_overlay()
    pronto = app._ultimo_retorno  # type: ignore[attr-defined]
    pronto(
        Resultado(
            ok=True,
            dados=LiveGameRefused(
                "A Replay API do League está desligada.",
                hint="Adicione EnableReplayApi=1 na secao [General] do game.cfg.",
            ),
        )
    )
    textos = _todos_os_textos(app)
    assert any("Replay API" in t for t in textos)
    assert any("EnableReplayApi=1" in t for t in textos)


def _todos_os_textos(a: App) -> list[str]:
    a.root.update_idletasks()
    out: list[str] = []

    def andar(w: tk.Misc) -> None:
        try:
            texto = w.cget("text")  # type: ignore[call-overload]
        except tk.TclError:
            texto = ""
        if isinstance(texto, str) and texto:
            out.append(texto)
        for f in w.winfo_children():
            andar(f)

    andar(a.corpo)
    return out
