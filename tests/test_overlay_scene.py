"""O que aparece na tela, e quando.

Nada aqui abre janela. A cena e uma lista de primitivas, entao da para afirmar
coisas sobre o desenho — "o cartao esta na tela", "a marcacao critica e mais
larga" — sem depender de ter o League instalado.
"""

from __future__ import annotations

from riftcoach.core.schema import Mark
from riftcoach.overlay.scene import (
    ANTES_MS,
    COR_CRITICO,
    COR_USUARIO,
    Box,
    Label,
    OverlayState,
    ativa,
    build,
    cor_da_marca,
)


def marca(t_s: int, sev: int = 3, autor: str = "ai", texto: str = "erro") -> Mark:
    return Mark(
        t_ms=t_s * 1000,
        author=autor,  # type: ignore[arg-type]
        kind="critical" if sev >= 4 else "error",
        text=texto,
        category="wave",
        severity=sev,
        wp_loss=4.2 if autor == "ai" else None,
    )


def estado(*marcas: Mark) -> OverlayState:
    return OverlayState(width=1600, height=900, duration_ms=35 * 60_000, marks=list(marcas))


# --------------------------------------------------------------------------
# A regra central: o cartao vem ANTES do erro
# --------------------------------------------------------------------------


def test_o_cartao_aparece_antes_do_momento_nao_depois() -> None:
    """Esta e a decisao de coaching inteira em um teste.

    O seek para 8 s antes da decisao. Se o cartao so surgisse no instante
    marcado, a pessoa leria o diagnostico depois de ja ter visto o desfecho —
    ou seja, leria a resposta antes de entender a pergunta.
    """
    st = estado(marca(900))  # 15:00
    assert ativa(st, 900_000 - ANTES_MS + 100) is not None, "devia estar na tela no seek"
    assert ativa(st, 900_000) is not None, "devia continuar no instante do erro"
    assert ativa(st, 900_000 - ANTES_MS - 2_000) is None, "cedo demais"
    assert ativa(st, 900_000 + 30_000) is None, "devia ter saido ha tempo"


def test_a_contagem_regressiva_vira_agora() -> None:
    st = estado(marca(900))
    antes = [i.text for i in build(st, 896_000).items if isinstance(i, Label)]
    assert any("o momento em" in t for t in antes)

    depois = [i.text for i in build(st, 901_000).items if isinstance(i, Label)]
    assert "AGORA" in depois


def test_entre_duas_marcacoes_vence_a_mais_grave() -> None:
    """Numa teamfight varias janelas se sobrepoem. Empilhar os cartoes cobriria
    o jogo justamente no momento em que a pessoa precisa ver o jogo."""
    leve = marca(900, sev=2, texto="leve")
    grave = marca(903, sev=5, texto="grave")
    st = estado(leve, grave)
    escolhida = ativa(st, 900_000)
    assert escolhida is not None and escolhida.text == "grave"


def test_o_cartao_mostra_o_custo_medido() -> None:
    st = estado(marca(900, sev=5))
    textos = [i.text for i in build(st, 900_000).items if isinstance(i, Label)]
    assert any("4.2 pontos de vitória" in t for t in textos)


def test_marcacao_do_usuario_nao_finge_custo() -> None:
    """Ele marcou o que sentiu, nao o que mediu. Inventar um numero ali
    apagaria a unica diferenca que importa entre as duas fontes."""
    st = estado(marca(900, autor="user", texto="senti que errei aqui"))
    textos = [i.text for i in build(st, 900_000).items if isinstance(i, Label)]
    assert any("SUA MARCAÇÃO" in t for t in textos)
    assert not any("pontos de vitória" in t for t in textos)


# --------------------------------------------------------------------------
# A regua
# --------------------------------------------------------------------------


def test_a_regua_poe_cada_marcacao_na_fracao_certa() -> None:
    st = estado(marca(0), marca(35 * 60), marca(1050))  # inicio, fim, 17:30
    # Num instante SEM cartao aberto: senao as caixas do cartao entram na
    # conta e o teste passa a medir outra coisa.
    caixas = [i for i in build(st, 300_000).items if isinstance(i, Box)]
    xs = sorted(c.rect.cx for c in caixas[1:])  # [0] e o trilho
    assert xs[0] < 10
    assert abs(xs[1] - 800) < 12  # metade de 1600
    assert xs[2] > 1590


def test_marcacao_critica_e_mais_larga_que_a_comum() -> None:
    """A unica diferenca de TAMANHO na regua. O resto e so cor — se cada
    gravidade tivesse um tamanho, a regua viraria enfeite e pararia de
    comunicar."""
    st = estado(marca(600, sev=2), marca(900, sev=5))
    caixas = [i for i in build(st, 0).items if isinstance(i, Box)][1:]
    larguras = sorted(c.rect.w for c in caixas)
    assert larguras[1] == larguras[0] * 2


def test_sem_duracao_nao_ha_regua() -> None:
    """Partida de duracao zero nao existe, mas dado corrompido existe — e
    dividir por ela mandaria a marcacao para o infinito."""
    st = OverlayState(width=1600, height=900, duration_ms=0, marks=[marca(900)])
    assert not [i for i in build(st, 0).items if isinstance(i, Box)]


# --------------------------------------------------------------------------
# Minimapa
# --------------------------------------------------------------------------


def test_sem_rastro_nao_desenha_no_minimapa() -> None:
    st = estado(marca(900))
    assert st.focus_track == []
    # Nenhum circulo: o unico circulo da cena vem do minimapa.
    from riftcoach.overlay.scene import Circle

    assert not [i for i in build(st, 900_000).items if isinstance(i, Circle)]


def test_o_anel_segue_o_jogador_pelo_mapa() -> None:
    from riftcoach.overlay.scene import Circle

    st = estado(marca(900))
    st.focus_track = [(0, 1000.0, 1000.0), (600_000, 13000.0, 13000.0)]

    cedo = next(i for i in build(st, 0).items if isinstance(i, Circle))
    tarde = next(i for i in build(st, 600_000).items if isinstance(i, Circle))
    # Da base azul (embaixo a esquerda) para a vermelha (em cima a direita).
    assert tarde.x > cedo.x and tarde.y < cedo.y


def test_a_posicao_e_interpolada_entre_frames() -> None:
    """A Riot so da uma posicao por MINUTO. Sem interpolar, o anel pularia de
    lugar uma vez por minuto e pareceria quebrado."""
    from riftcoach.overlay.scene import Circle

    st = estado()
    st.focus_track = [(0, 0.0, 0.0), (60_000, 14000.0, 0.0)]
    meio = next(i for i in build(st, 30_000).items if isinstance(i, Circle))
    inicio = next(i for i in build(st, 0).items if isinstance(i, Circle))
    fim = next(i for i in build(st, 60_000).items if isinstance(i, Circle))
    assert inicio.x < meio.x < fim.x
    assert abs(meio.x - (inicio.x + fim.x) / 2) < 1.0


# --------------------------------------------------------------------------
# Cores e texto
# --------------------------------------------------------------------------


def test_vermelho_so_para_o_que_custou_a_partida() -> None:
    """Se tudo e vermelho, nada e vermelho."""
    assert cor_da_marca(marca(900, sev=5)) == COR_CRITICO
    assert cor_da_marca(marca(900, sev=4)) == COR_CRITICO
    assert cor_da_marca(marca(900, sev=3)) != COR_CRITICO
    assert cor_da_marca(marca(900, sev=1)) != COR_CRITICO


def test_a_marcacao_do_jogador_tem_cor_propria() -> None:
    assert cor_da_marca(marca(900, autor="user")) == COR_USUARIO


def test_texto_longo_quebra_em_varias_linhas() -> None:
    longo = (
        "voce empurrou a wave ate a torre inimiga sem visao no rio enquanto o "
        "dragao infernal nascia em quarenta segundos e o seu jungler estava do "
        "outro lado do mapa"
    )
    st = estado(marca(900, texto=longo))
    labels = [i for i in build(st, 900_000).items if isinstance(i, Label)]
    corpo = [x for x in labels if x.text and x.text[0].islower()]
    assert len(corpo) >= 3, "texto longo tinha de ocupar varias linhas"
    assert all(len(x.text) <= 70 for x in corpo)


def test_palavra_nunca_e_cortada_ao_meio() -> None:
    from riftcoach.overlay.scene import _quebrar

    linhas = _quebrar("antidesestabelecimentarianismo e uma palavra enorme", 20)
    assert "antidesestabelecimentarianismo" in linhas


def test_as_camadas_podem_ser_desligadas() -> None:
    st = estado(marca(900))
    st.show_card = False
    st.show_ruler = False
    st.show_minimap = False
    assert build(st, 900_000).items == []


# --------------------------------------------------------------------------
# Navegacao pelas marcacoes
# --------------------------------------------------------------------------


def test_pular_para_a_proxima_nao_repesca_a_atual() -> None:
    """O cartao ja esta na tela quando a pessoa aperta. Sem folga, "proxima"
    devolveria a marcacao que ela esta vendo e o replay nao sairia do lugar."""
    from riftcoach.overlay.run import _proxima_marca_depois

    st = estado(marca(900), marca(1200))
    assert _proxima_marca_depois(st, 899_000) == 1_200_000
    assert _proxima_marca_depois(st, 600_000) == 900_000


def test_depois_da_ultima_marcacao_nao_ha_para_onde_pular() -> None:
    from riftcoach.overlay.run import _proxima_marca_depois

    assert _proxima_marca_depois(estado(marca(900)), 1_000_000) is None


def test_a_navegacao_ignora_as_marcacoes_do_proprio_usuario() -> None:
    """Pular de erro em erro e a revisao guiada. As marcacoes da pessoa ela ja
    sabe onde estao — ela mesma colocou."""
    from riftcoach.overlay.run import _proxima_marca_depois

    st = estado(marca(910, autor="user"), marca(1200))
    assert _proxima_marca_depois(st, 900_000) == 1_200_000
