"""Gera as figuras do manual a partir do codigo que desenha o overlay.

Rode assim, da raiz do repositorio:

    uv run --extra vision python scripts/gerar_figuras.py

As figuras vao para `docs/img/`. Elas sao geradas, e nao capturadas a mao, de
proposito: assim nunca descrevem uma versao do overlay que nao existe mais.
O fundo e um print de replay de verdade, o mesmo que calibra a projecao do
minimapa nos testes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from riftcoach.cli import _console_em_utf8
from riftcoach.core.schema import Mark, Stroke
from riftcoach.overlay.preview import gerar
from riftcoach.overlay.scene import OverlayState, build

RAIZ = Path(__file__).resolve().parent.parent
FUNDO = RAIZ / "tests" / "fixtures" / "hud-spectator-1600x900.png"
SAIDA = RAIZ / "docs" / "img"

# Uma partida de exemplo com a cara do que o motor de vantagem produz: poucos
# erros criticos, varios medios, e as marcacoes da pessoa no meio.
MARCAS = [
    Mark(
        t_ms=4 * 60_000 + 20_000,
        author="ai",
        kind="error",
        text="recall com 1.180 de ouro sem item completo à vista",
        category="recall",
        severity=2,
        wp_loss=1.8,
    ),
    Mark(
        t_ms=8 * 60_000 + 45_000,
        author="ai",
        kind="error",
        text="wave empurrada até a torre sem visão no rio",
        category="wave",
        severity=3,
        wp_loss=3.4,
    ),
    Mark(
        t_ms=11 * 60_000,
        author="user",
        kind="question",
        text="por que eu recuei aqui?",
        where=None,
        you=(7000.0, 7000.0),
    ),
    Mark(
        t_ms=14 * 60_000 + 54_000,
        author="ai",
        kind="critical",
        text=(
            "você empurrou a wave sem visão no rio enquanto o dragão infernal nascia em 40 segundos"
        ),
        category="wave",
        severity=5,
        wp_loss=7.3,
        # O dragão caiu no pit de baixo enquanto o jogador estava na top.
        where=(9866.0, 4414.0),
        you=(3000.0, 11500.0),
    ),
    Mark(
        t_ms=21 * 60_000 + 10_000,
        author="ai",
        kind="critical",
        text="morte no meio do mapa segurando 2.400 de ouro",
        category="positioning",
        severity=4,
        wp_loss=5.9,
    ),
    Mark(
        t_ms=26 * 60_000,
        author="user",
        kind="good",
        text="bom freeze aqui",
    ),
]

# O jogador em foco, indo da base azul para o rio de baixo ao longo da partida.
RASTRO = [
    (0, 1_500.0, 1_800.0),
    (5 * 60_000, 4_000.0, 4_200.0),
    (10 * 60_000, 7_200.0, 6_800.0),
    (15 * 60_000, 8_600.0, 5_400.0),
    (20 * 60_000, 10_100.0, 4_300.0),
]


def estado() -> OverlayState:
    return OverlayState(
        width=1600,
        height=900,
        duration_ms=35 * 60_000,
        marks=MARCAS,
        focus_track=RASTRO,
        focus_champion="Garen",
    )


FIGURAS: list[tuple[str, int, str]] = [
    (
        "overlay-cartao-critico.jpg",
        14 * 60_000 + 50_000,
        "o cartão 4 segundos ANTES do erro crítico",
    ),
    (
        "overlay-agora.jpg",
        14 * 60_000 + 55_000,
        "o mesmo cartão no instante do erro",
    ),
    (
        "overlay-sua-marcacao.jpg",
        11 * 60_000,
        "uma marcação colocada por você, com Ctrl+Alt+Q",
    ),
    (
        "overlay-regua.jpg",
        18 * 60_000,
        "só a régua, entre um erro e outro",
    ),
]


# --------------------------------------------------------------------------
# As telas da janela
# --------------------------------------------------------------------------


def figuras_da_janela() -> None:
    """Fotografa as telas da janela, uma a uma.

    Nao da para capturar uma janela que esta atras de outra, entao ela e
    trazida para a frente antes de cada foto. Por isso este trecho e separado
    e opcional: numa maquina sem tela ele nao tem o que fazer, e no meio de
    outra tarefa ele rouba o foco de quem estiver usando o computador.
    """
    import time

    from PIL import ImageGrab

    from riftcoach.gui.app import App

    # Sem rede: as telas so precisam MONTAR para serem fotografadas.
    App._tarefa = lambda self, fabrica, quando_terminar: None  # type: ignore[method-assign]

    telas = {
        "janela-chave.png": (lambda a: a.tela_chave(), "onde você cola a chave da Riot"),
        "janela-conta.png": (lambda a: a.tela_conta(), "onde você informa o seu Riot ID"),
        "janela-pronto.png": (_tela_final, "o que fazer com a análise pronta"),
    }

    app = App()
    for agendado in app.root.tk.call("after", "info"):
        app.root.after_cancel(agendado)

    for nome, (montar, descricao) in telas.items():
        app._limpar()
        montar(app)
        app.root.lift()
        app.root.attributes("-topmost", True)
        app.root.update()
        time.sleep(0.4)
        app.root.update()
        x, y = app.root.winfo_rootx(), app.root.winfo_rooty()
        w, h = app.root.winfo_width(), app.root.winfo_height()
        destino = SAIDA / nome
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(destino)
        print(f"{destino.relative_to(RAIZ)}  —  {descricao}")
    app.root.destroy()


def _tela_final(a: object) -> None:
    app = a  # type: ignore[assignment]
    app.estado.resumo = "Garen 3/5/7  ·  derrota em 35 minutos"  # type: ignore[attr-defined]
    app.estado.marcacoes = 7  # type: ignore[attr-defined]
    app.tela_pronto()  # type: ignore[attr-defined]


MOMENTO_CRITICO = 14 * 60_000 + 54_000


def _com_pincel(st: OverlayState) -> OverlayState:
    """O mesmo estado, com o pincel ligado e um desenho de exemplo.

    O desenho imita o que um treinador faz na lousa: circula o que estava
    errado, e aponta para onde era para estar.
    """
    import math


    st.modo_desenho = True
    st.cor_do_pincel = "#ff453a"
    st.espessura_do_pincel = 4.0
    circulo = [
        (0.24 + 0.055 * math.cos(a / 9 * 2 * math.pi), 0.50 + 0.085 * math.sin(a / 9 * 2 * math.pi))
        for a in range(10)
    ]
    st.strokes = [
        Stroke(t_ms=MOMENTO_CRITICO, points=circulo, color="#ff453a", width=4.0),
        Stroke(
            t_ms=MOMENTO_CRITICO,
            points=[(0.30, 0.47), (0.40, 0.40), (0.47, 0.36)],
            color="#ffd60a",
            width=4.0,
        ),
        Stroke(
            t_ms=MOMENTO_CRITICO, points=[(0.47, 0.36), (0.43, 0.38)], color="#ffd60a", width=4.0
        ),
        Stroke(
            t_ms=MOMENTO_CRITICO, points=[(0.47, 0.36), (0.45, 0.41)], color="#ffd60a", width=4.0
        ),
    ]
    return st


def main() -> None:
    _console_em_utf8()
    if not FUNDO.exists():
        raise SystemExit(f"fundo nao encontrado: {FUNDO}")
    st = estado()
    for nome, t_ms, descricao in FIGURAS:
        # 0,8 da 1280x720: o GitHub renderiza a ~800 px de largura mesmo, e
        # cada figura cai de 360 KB para cerca de 230 KB.
        caminho = gerar(FUNDO, build(st, t_ms), SAIDA / nome, escala=0.8)
        print(f"{caminho.relative_to(RAIZ)}  —  {descricao}")

    # O painel de ajuda e a documentacao do overlay que a pessoa le enquanto
    # assiste; a figura dele sai do mesmo codigo, entao nunca descreve uma
    # versao que nao existe mais.
    caminho = gerar(FUNDO, build(st, 0, boas_vindas=True), SAIDA / "overlay-ajuda.jpg", escala=0.8)
    print(f"{caminho.relative_to(RAIZ)}  —  o painel de ajuda, aberto com Ctrl+Alt+A")

    caminho = gerar(FUNDO, build(_com_pincel(st), MOMENTO_CRITICO), SAIDA / "overlay-pincel.jpg",
                    escala=0.8)
    print(f"{caminho.relative_to(RAIZ)}  —  o pincel ligado, com um circulo e uma seta")

    if "--sem-janela" not in sys.argv:
        figuras_da_janela()


if __name__ == "__main__":
    main()
