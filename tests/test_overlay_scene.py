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


def marca_com_lugar(
    t_s: int = 900,
    sev: int = 5,
    onde: tuple[float, float] | None = (5000.0, 10000.0),
    voce: tuple[float, float] | None = (10000.0, 4000.0),
) -> Mark:
    m = marca(t_s, sev=sev)
    m.where = onde
    m.you = voce
    return m


def test_o_minimapa_so_aparece_junto_com_o_cartao() -> None:
    """Ponto no mapa sem explicacao ao lado e enfeite. Os dois falam da mesma
    jogada, entao vivem e somem juntos."""
    from riftcoach.overlay.scene import Circle

    st = estado(marca_com_lugar(900))

    def circulos(now_ms: int) -> list[Circle]:
        return [
            i for i in build(st, now_ms).items if isinstance(i, Circle) and i.y > st.height * 0.5
        ]

    assert circulos(900_000), "com o cartao na tela, o mapa tem de estar marcado"
    assert not circulos(300_000), "longe de qualquer marcacao, o mapa fica limpo"


def test_o_minimapa_rotula_os_dois_pontos_na_propria_tela() -> None:
    """Legenda que some depois de dez segundos nao serve para quem chegou no
    minuto vinte. Os rotulos ficam no mapa, sempre."""
    st = estado(marca_com_lugar(900))
    textos = [i.text for i in build(st, 900_000).items if isinstance(i, Label)]
    assert "VOCÊ" in textos
    assert "AQUI" in textos


def test_os_dois_pontos_caem_em_lados_opostos_do_mapa() -> None:
    """O comprimento da linha entre eles E a informacao: o quanto voce estava
    longe da jogada."""
    from riftcoach.overlay.scene import Circle

    st = estado(marca_com_lugar(900, onde=(1500.0, 1800.0), voce=(13000.0, 13000.0)))
    pontos = [
        i for i in build(st, 900_000).items if isinstance(i, Circle) and i.y > st.height * 0.5
    ]
    assert len(pontos) == 2
    a, b = sorted(pontos, key=lambda c: c.x)
    assert a.x < b.x and a.y > b.y, "base azul embaixo a esquerda, vermelha em cima"


def test_marcacao_sem_lugar_nao_inventa_um() -> None:
    """ "Voce recuou com ouro sobrando" acontece no tempo, nao no mapa. Cravar
    uma coordenada nela seria pior que deixar vazio."""
    from riftcoach.overlay.scene import Circle

    st = estado(marca_com_lugar(900, onde=None, voce=None))
    assert not [
        i for i in build(st, 900_000).items if isinstance(i, Circle) and i.y > st.height * 0.5
    ]


def test_so_o_lugar_da_jogada_ja_basta() -> None:
    """Nem toda marcacao sabe onde o jogador estava, e isso nao pode impedir de
    mostrar onde a jogada foi."""
    st = estado(marca_com_lugar(900, voce=None))
    textos = [i.text for i in build(st, 900_000).items if isinstance(i, Label)]
    assert "AQUI" in textos
    assert "VOCÊ" not in textos


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


# --------------------------------------------------------------------------
# A regua precisa se explicar sozinha
# --------------------------------------------------------------------------


def test_a_regua_diz_de_quem_ela_e_e_quanto_falta() -> None:
    """Listra colorida sem rotulo nao comunica nada — foi exatamente o que
    aconteceu na primeira versao, e a pessoa que usou nao entendeu que aquilo
    era a partida inteira nem o que as cores queriam dizer."""
    st = estado(marca(600), marca(900), marca(1500))
    textos = [i.text for i in build(st, 300_000).items if isinstance(i, Label)]
    assert any("RIFTCOACH" in t for t in textos), "as listras precisam ter dono"
    assert any("erros" in t for t in textos), "precisa dizer quantos sao"
    assert any("próximo em" in t for t in textos), "precisa dizer quanto falta"


def test_a_regua_conta_quantos_erros_ja_passaram() -> None:
    st = estado(marca(600), marca(900), marca(1500))

    def placar(now_ms: int) -> str:
        return next(
            i.text for i in build(st, now_ms).items if isinstance(i, Label) and "erros" in i.text
        )

    assert placar(0).startswith("0/3")
    assert placar(700_000).startswith("1/3")
    assert placar(1_600_000).startswith("3/3")


def test_a_regua_tem_uma_divisao_a_cada_cinco_minutos() -> None:
    """Sem escala a faixa nao diz que representa tempo. Com ela, diz sozinha."""
    from riftcoach.overlay.scene import COR_DIVISAO
    from riftcoach.overlay.scene import Line as SLine

    st = estado()  # 35 min -> divisoes em 5,10,15,20,25,30,35
    divisoes = [i for i in build(st, 0).items if isinstance(i, SLine) and i.color == COR_DIVISAO]
    assert len(divisoes) == 7


# --------------------------------------------------------------------------
# Boas-vindas
# --------------------------------------------------------------------------


def test_o_cartao_de_boas_vindas_explica_as_cores() -> None:
    """A razao de ele existir: sem legenda, listra colorida e decoracao."""
    st = estado(marca(900, sev=5), marca(1200, sev=2))
    textos = [i.text for i in build(st, 0, boas_vindas=True).items if isinstance(i, Label)]
    assert any("AJUDA" in t for t in textos)
    assert any("custou a partida" in t for t in textos)
    assert any("suas marcações" in t for t in textos)
    assert any("Ctrl+Alt+S" in t for t in textos)


def test_as_boas_vindas_dizem_onde_esta_o_primeiro_erro() -> None:
    """A pessoa abre o replay no comeco e as marcacoes estao aos 15 minutos.
    Sem isto, a tela fica corretamente vazia e parece quebrada."""
    st = estado(marca(900), marca(1200))
    textos = [i.text for i in build(st, 0, boas_vindas=True).items if isinstance(i, Label)]
    assert any("15:00" in t for t in textos)


def test_as_boas_vindas_tomam_o_lugar_do_cartao_de_erro() -> None:
    """Nos primeiros segundos, "o que e isto" importa mais que qualquer erro.
    Empilhar os dois cobriria o jogo inteiro."""
    # Texto distintivo: "primeiro erro" contem "o erro", e uma busca ingenua
    # daria falso positivo contra o rodape do proprio cartao de boas-vindas.
    st = estado(marca(900, sev=5, texto="wave empurrada sem visao"))
    textos = [i.text for i in build(st, 900_000, boas_vindas=True).items if isinstance(i, Label)]
    assert any("AJUDA" in t for t in textos)
    assert not any("wave empurrada" in t for t in textos)


def test_partida_sem_erro_marcado_nao_mente() -> None:
    st = estado()
    textos = [i.text for i in build(st, 0, boas_vindas=True).items if isinstance(i, Label)]
    assert any("nenhum erro marcado" in t for t in textos)


# --------------------------------------------------------------------------
# O overlay nao pode se esconder por estar aparecendo
# --------------------------------------------------------------------------


def test_a_janela_do_proprio_overlay_conta_como_estar_no_jogo() -> None:
    """A regressao mais cara que este projeto teve ate agora.

    O laco escondia o overlay quando a janela ativa nao era a do jogo. So que
    a janela ativa podia ser a DO PROPRIO OVERLAY — e entao ele se escondia
    justamente por estar aparecendo. Piscava uma vez e sumia, sem nenhuma
    linha de log que denunciasse o motivo.
    """
    from riftcoach.overlay.window import pertence_a_revisao

    JOGO, NOSSO, OUTRO = 100, 200, 300
    assert pertence_a_revisao(JOGO, JOGO, NOSSO) is True
    assert pertence_a_revisao(NOSSO, JOGO, NOSSO) is True, (
        "a nossa propria janela nao pode significar 'a pessoa saiu do jogo'"
    )
    assert pertence_a_revisao(OUTRO, JOGO, NOSSO) is False


def test_sem_janela_nossa_declarada_so_o_jogo_vale() -> None:
    """O comportamento antigo continua valendo quando nada mais e declarado —
    quem chama sem informar a propria janela nao ganha permissao extra."""
    from riftcoach.overlay.window import pertence_a_revisao

    assert pertence_a_revisao(100, 100) is True
    assert pertence_a_revisao(200, 100) is False


def test_o_custo_nao_e_dito_duas_vezes() -> None:
    """O motor de regras as vezes ja escreve o custo na frase. Repetir logo
    abaixo, arredondado de outro jeito, parece dois numeros diferentes para a
    mesma coisa — foi o que apareceu na tela: "custou 6pp" e "custou 6.5"."""
    m = marca(900, sev=4, texto="Perdeu dragon em 8:41 custou 6pp.")
    st = estado(m)
    textos = [i.text for i in build(st, 900_000).items if isinstance(i, Label)]
    assert not any("pontos de vitória" in t for t in textos)

    # E quando a frase NAO traz o custo, ele continua aparecendo.
    st2 = estado(marca(900, sev=4, texto="voce empurrou a wave sem visao"))
    textos2 = [i.text for i in build(st2, 900_000).items if isinstance(i, Label)]
    assert any("pontos de vitória" in t for t in textos2)


def test_rotulo_perto_da_borda_nao_vaza_da_tela() -> None:
    """O minimapa fica colado no canto inferior direito. Um ponto na beirada
    joga metade do texto para fora, e no canto de baixo nao ha para onde
    rolar."""
    # Canto do mapa que cai no extremo direito do minimapa.
    st = estado(marca_com_lugar(900, onde=(14800.0, 100.0), voce=(14800.0, 200.0)))
    rotulos = [i for i in build(st, 900_000).items if isinstance(i, Label)]
    nos_cantos = [i for i in rotulos if i.text in ("VOCÊ", "AQUI")]
    assert len(nos_cantos) == 2
    for lab in nos_cantos:
        meia = len(lab.text) * lab.size * 0.35
        assert lab.x - meia >= 0, f"{lab.text} vazou pela esquerda"
        assert lab.x + meia <= st.width, f"{lab.text} vazou pela direita"
        assert 0 <= lab.y <= st.height, f"{lab.text} vazou na vertical"


def test_rotulo_no_pe_do_minimapa_sobe_em_vez_de_sair_da_tela() -> None:
    """Metade do minimapa encosta na borda de baixo. Um rotulo sempre posto
    abaixo do ponto sairia da tela em metade dos casos."""
    # Canto inferior do mapa: base azul, que cai no pe do minimapa.
    st = estado(marca_com_lugar(900, onde=(500.0, 500.0), voce=None))
    aqui = next(i for i in build(st, 900_000).items if isinstance(i, Label) and i.text == "AQUI")
    assert aqui.anchor == "s", "sem espaco embaixo, o rotulo tem de ir para cima"
    assert aqui.y < st.height


# --------------------------------------------------------------------------
# Uma fonte so para os atalhos
# --------------------------------------------------------------------------


def test_o_painel_mostra_todos_os_atalhos() -> None:
    """Lista parcial faz procurar no manual — que e exatamente o que ninguem
    vai fazer no meio de um replay."""
    from riftcoach.overlay.atalhos import TODOS

    st = estado(marca(900))
    textos = [i.text for i in build(st, 0, boas_vindas=True).items if isinstance(i, Label)]
    for a in TODOS:
        assert a.tecla in textos, f"{a.tecla} ({a.descricao}) nao aparece no painel"


def test_a_ajuda_da_cli_nao_diverge_dos_atalhos() -> None:
    """Os atalhos ja moraram em tres lugares. Acrescentar um e esquecer de um
    dos tres produz ou uma tecla que ninguem descobre ou um texto que promete
    o que nao existe — as duas falham em silencio."""
    from riftcoach.cli import overlay as comando_overlay
    from riftcoach.overlay.atalhos import TODOS

    ajuda = comando_overlay.__doc__ or ""
    for a in TODOS:
        assert a.tecla in ajuda, f"{a.tecla} falta na ajuda de `riftcoach overlay`"
