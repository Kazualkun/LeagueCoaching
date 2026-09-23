"""Perguntar dentro do overlay: a caixa desenha, e a espera nao congela.

Nada aqui abre jogo nem toca rede. O laco recebe a funcao de perguntar de
fora justamente para que este teste possa injetar uma falsa — e para que o
proprio laco continue sem saber o que e roteador, cota ou prompt.
"""

from __future__ import annotations

from riftcoach.overlay.atalhos import POR_CODIGO, TODOS, VK_PERGUNTAR
from riftcoach.overlay.scene import Label, OverlayState, build


def _estado(**kw: object) -> OverlayState:
    base: dict[str, object] = {"width": 1600, "height": 900, "duration_ms": 1_800_000}
    base.update(kw)
    return OverlayState(**base)  # type: ignore[arg-type]


def _textos(st: OverlayState) -> list[str]:
    return [p.text for p in build(st, 0).items if isinstance(p, Label)]


def test_a_caixa_so_aparece_no_modo_pergunta() -> None:
    assert not any("PERGUNTAR" in t for t in _textos(_estado()))
    assert any("PERGUNTAR" in t for t in _textos(_estado(modo_pergunta=True)))


def test_a_caixa_vazia_convida_a_digitar() -> None:
    """Uma caixa em branco nao diz o que fazer com ela."""
    textos = _textos(_estado(modo_pergunta=True))
    assert any("digite a sua pergunta" in t for t in textos)
    assert any("Enter" in t and "Esc" in t for t in textos)


def test_o_que_foi_digitado_aparece_na_tela() -> None:
    textos = _textos(_estado(modo_pergunta=True, texto_da_pergunta="por que eu morri aqui"))
    assert any("por que eu morri aqui" in t for t in textos)


def test_pergunta_comprida_nao_sai_pela_direita() -> None:
    """A linha digitada comeca depois do titulo e usa fonte cheia; com a
    largura do corpo ela vazava a partir de uns 160 caracteres."""
    from riftcoach.analysis.pergunta import LIMITE_DA_PERGUNTA

    st = _estado(modo_pergunta=True, texto_da_pergunta="x" * LIMITE_DA_PERGUNTA)
    linha = next(p for p in build(st, 0).items if isinstance(p, Label) and p.text.endswith("▌"))
    # 0,64 px por ponto de fonte: a mesma medida que o painel usa.
    assert linha.x + len(linha.text) * linha.size * 0.64 <= st.width


def test_a_espera_tem_sinal_proprio() -> None:
    """Sem sinal nenhum, alguns segundos de espera parecem travamento — e no
    modo pergunta o clique nao chega ao jogo, o que piora a impressao."""
    assert any("pensando" in t for t in _textos(_estado(modo_pergunta=True, pensando=True)))


def test_a_resposta_e_quebrada_e_tem_teto() -> None:
    """Resposta comprida nao pode cobrir o jogo que a pessoa esta assistindo."""
    from riftcoach.overlay.scene import MAX_LINHAS_DA_RESPOSTA

    longa = "palavra " * 800
    st = _estado(modo_pergunta=True, resposta=longa)
    corpo = [t for t in _textos(st) if t.startswith("palavra")]
    assert 0 < len(corpo) <= MAX_LINHAS_DA_RESPOSTA


def test_o_atalho_existe_e_esta_na_ajuda() -> None:
    """Atalho que nao aparece na ajuda e atalho que ninguem descobre."""
    assert VK_PERGUNTAR in POR_CODIGO
    assert POR_CODIGO[VK_PERGUNTAR].tecla == "Ctrl+Alt+I"
    assert any("IA" in a.descricao for a in TODOS)


# --------------------------------------------------------------------------
# A caixa de texto
# --------------------------------------------------------------------------


def test_digitar_monta_o_texto() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta()
    for ch in "por que":
        assert c.teclar(ch, ch) == "nada"
    assert c.texto == "por que"


def test_acento_entra_inteiro() -> None:
    """`char` ja vem com shift e acento aplicados; usar `keysym` aqui daria
    'ccedilla' no lugar do 'ç'."""
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta()
    c.teclar("ç", "ccedilla")
    c.teclar("ã", "atilde")
    assert c.texto == "çã"


def test_teclas_sem_caractere_nao_sujam_a_caixa() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta()
    for keysym in ("Shift_L", "Control_L", "Left", "F1", "Alt_L"):
        assert c.teclar("", keysym) == "nada"
    assert c.texto == ""


def test_backspace_apaga_de_tras_para_frente() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta(texto="abc")
    c.teclar("", "BackSpace")
    assert c.texto == "ab"
    # Apagar numa caixa vazia nao pode estourar.
    c.texto = ""
    assert c.teclar("", "BackSpace") == "nada"
    assert c.texto == ""


def test_enter_envia_e_escape_fecha() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta(texto="por que eu morri?")
    assert c.teclar("", "Return") == "enviar"
    assert c.teclar("", "Escape") == "fechar"


def test_enter_na_caixa_vazia_nao_gasta_cota() -> None:
    """Enter sem texto e engano, nao pergunta. Mandar assim gastaria ~2.000
    tokens de um teto de 8.000 por minuto para receber nada."""
    from riftcoach.overlay.caixa import CaixaDePergunta

    assert CaixaDePergunta(texto="   ").teclar("", "Return") == "nada"


def test_a_caixa_tem_teto() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta(limite=5)
    for _ in range(50):
        c.teclar("a", "a")
    assert len(c.texto) == 5


def test_limpar_devolve_e_esvazia() -> None:
    from riftcoach.overlay.caixa import CaixaDePergunta

    c = CaixaDePergunta(texto="  por que?  ")
    assert c.limpar() == "por que?"
    assert c.texto == ""
