"""O laco: relogio do replay -> cena -> pixels.

TUDO PASSA PELO GUARD. Este modulo nao conhece a porta 2999, nao monta URL e
nao abre socket; ele pede ao `ReplayController`, que pede ao `ReplayGuard`,
que confere ANTES de cada requisicao que o que esta rodando e um replay e nao
uma partida ao vivo. Se um dia alguem precisar de um dado novo do client, o
caminho continua sendo por la.

E por isso que o overlay e compativel com as regras da Riot: ele so existe
depois que o guard provou que a partida ja acabou. Sobrepor informacao a uma
gravacao e a mesma coisa que um treinador desenhar por cima do video do jogo
de domingo. Sobrepor a uma partida ao vivo seria outra coisa inteiramente — e
o intertravamento torna essa outra coisa inalcancavel a partir daqui.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from riftcoach.config import data_dir
from riftcoach.core.errors import LiveGameRefused
from riftcoach.core.review import add_user_mark, load_session, save_session
from riftcoach.core.schema import ReviewScreenshot, ReviewSession
from riftcoach.overlay import window as win
from riftcoach.overlay.atalhos import (
    POR_CODIGO,
    TODOS,
    VK_AJUDA,
    VK_ANOTAR,
    VK_CONTROL,
    VK_DUVIDA,
    VK_MENU,
    VK_OCULTAR,
    VK_PERGUNTAR,
    VK_PINCEL,
    VK_PRINT,
    VK_RETOMAR,
    VK_SEGUINTE,
)
from riftcoach.overlay.caixa import CaixaDePergunta
from riftcoach.overlay.desenho import Prancheta, apagar_ultimo, limpar_instante
from riftcoach.overlay.geometry import Rect
from riftcoach.overlay.render import OverlayWindow
from riftcoach.overlay.scene import OverlayState, build
from riftcoach.replay.controller import ReplayController
from riftcoach.replay.guard import open_guard

# 10 quadros por segundo. O relogio do replay so muda de decimo em decimo, e a
# maquina de referencia deste projeto e uma Intel HD 4000 — gastar 60 fps para
# redesenhar a mesma coisa seria tirar quadros do jogo para nao mostrar nada
# de novo.
FPS = 10.0

# Quantas leituras seguidas sem achar a janela do jogo antes de desistir.
# Uma falha isolada acontece durante troca de resolucao; tres seguidas
# significam que a pessoa fechou o League.
SUMICOS_ATE_SAIR = 30

# De quanto em quanto tempo a posicao atual vai para o disco.
GRAVA_POSICAO_S = 10.0

# Quanto tempo o cartao de apresentacao fica na tela. Contado em relogio de
# PAREDE, nao no do replay: ele precisa sumir sozinho mesmo com o replay
# pausado, que e exatamente como a maioria das pessoas abre o overlay.
BOAS_VINDAS_S = 12.0


# --------------------------------------------------------------------------
# Atalhos
# --------------------------------------------------------------------------

# Os codigos e os rotulos vivem em `atalhos.py`, definidos UMA vez. Eles ja
# moraram em tres lugares — aqui, no painel e na ajuda da CLI — e tres copias
# da mesma verdade envelhecem em ritmos diferentes.
#
# ATENCAO, e isto e questao de postura e nao de codigo: a leitura e do ESTADO
# do teclado pelo sistema operacional, so enquanto a janela do League esta em
# primeiro plano, e nunca ha injecao de tecla ou de clique. Nada e enviado
# para o jogo em momento nenhum. Combinado com o guard, que so deixa o overlay
# existir sobre um replay, nao ha caminho daqui para dentro de uma partida ao
# vivo.


class _Teclas:
    """Deteccao de borda de subida. Sem isto, segurar a tecla por meio segundo
    criaria cinco marcacoes no mesmo instante."""

    def __init__(self) -> None:
        self._antes: set[int] = set()

    @staticmethod
    def _pressionada(vk: int) -> bool:
        if not win.disponivel():
            return False
        import ctypes

        return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)

    def novas(self, vks: list[int]) -> list[int]:
        if not (self._pressionada(VK_CONTROL) and self._pressionada(VK_MENU)):
            self._antes.clear()
            return []
        agora = {vk for vk in vks if self._pressionada(vk)}
        saiu = sorted(agora - self._antes)
        self._antes = agora
        return saiu


# --------------------------------------------------------------------------


@dataclass
class OverlayRun:
    """Resultado da sessao de overlay, para a CLI ter o que contar."""

    quadros: int = 0
    marcas_do_usuario: int = 0
    motivo: str = ""
    avisos: list[str] = field(default_factory=list)


async def executar(
    st: OverlayState,
    rs: ReviewSession | None = None,
    *,
    fps: float = FPS,
    atalhos: bool = True,
    log: Callable[[str], None] = print,
    perguntar: Callable[[str, int], Awaitable[str]] | None = None,
) -> OverlayRun:
    """Abre o overlay e fica nele ate o replay ou o usuario terminarem.

    `perguntar(texto, instante_ms) -> resposta` chega de fora de proposito:
    assim este modulo continua sem saber o que e roteador, cota ou prompt, e
    o teste do laco continua rodando sem rede. `None` desliga o recurso — e o
    que acontece quando nao ha provedor de IA configurado.
    """
    res = OverlayRun()
    if not win.disponivel():
        res.motivo = "o overlay so funciona no Windows"
        return res

    cliente, guard = await open_guard()
    overlay: OverlayWindow | None = None
    # A pergunta em curso. Fica fora do laco porque a resposta chega por uma
    # tarefa de fundo: bloquear o laco esperando a IA congelaria o overlay por
    # segundos — sem repintar, sem ouvir tecla — bem no meio de um replay
    # rodando. Declarada antes do `try` porque o `finally` a cancela, e a
    # calibracao pode falhar antes de o laco existir.
    tarefa_da_pergunta: asyncio.Task[None] | None = None
    tarefa_do_stroke: asyncio.Task[None] | None = None
    tarefa_da_pausa: asyncio.Task[None] | None = None
    try:
        ctrl = ReplayController(guard)
        cal = await ctrl.calibrate()
        log(f"replay sincronizado (defasagem de {cal.offset_s:+.2f}s)")
        # Quem rodou o comando esta olhando para o TERMINAL, e o overlay so
        # aparece sobre a janela do jogo. Sem esta frase a pessoa fica
        # esperando na tela errada — que foi exatamente o que aconteceu.
        log("CLIQUE NA JANELA DO LEAGUE para ver as marcacoes aparecerem")

        overlay = OverlayWindow()
        if not overlay.vazado:
            res.avisos.append(
                "este Windows nao aceitou cor transparente; o overlay vai "
                "aparecer com um veu escuro por cima do jogo"
            )
            log(res.avisos[-1])

        teclas = _Teclas()
        oculto = False
        sumicos = 0
        desde_gravou = 0.0
        intervalo = 1.0 / max(1.0, fps)

        # Onde a revisao anterior parou. Guardado ANTES do laco comecar, porque
        # o proprio laco vai sobrescrever `last_position_ms` no primeiro quadro
        # — ler depois devolveria "onde voce esta", que nao serve para nada.
        aberto_em = time.monotonic()
        ja_apareceu = False
        ajuda = False
        prancheta = Prancheta()
        retomar_de = rs.last_position_ms if rs and rs.last_position_ms > 0 else None
        if retomar_de:
            log(f"voce parou em {_mmss(retomar_de)} — Ctrl+Alt+R volta para la")

        # O instante em que o pincel foi ligado. Os tracos ficam amarrados a
        # ELE, e nao ao relogio corrente: com o replay pausado os dois sao o
        # mesmo, mas se alguem despausar no meio do desenho a anotacao tem de
        # continuar pertencendo ao momento que estava sendo analisado.
        instante_do_pincel = 0
        # Mesmo raciocinio para a pergunta: ela e SOBRE o momento em que foi
        # aberta. Se o replay andar enquanto a pessoa digita, a resposta ainda
        # tem de falar do instante que ela estava olhando.
        instante_da_pergunta = 0
        instante_da_anotacao = 0
        ultima_sync_s = 0.0

        def ao_comecar(x: float, y: float) -> None:
            # A faixa de cores fica no rodape. Como o canvas recebe os cliques
            # agora, escolher uma cor por clique nao pode virar um traco curto
            # no jogo. Os numeros continuam disponiveis como atalho.
            if y >= 0.94:
                indice = int((x - 0.12) / 0.019)
                if 0 <= indice < 5 and prancheta.usar_cor(indice):
                    return
            prancheta.comecar(x, y)

        def ao_mover(x: float, y: float) -> None:
            prancheta.mover(x, y)

        def ao_soltar() -> None:
            nonlocal tarefa_do_stroke
            traco = prancheta.terminar(instante_do_pincel)
            if traco is None or rs is None:
                prancheta.descartar()
                return
            # Primeiro atualiza a memoria e a tela. Gravar JSON no callback do
            # mouse fazia o desenho sumir ate a escrita terminar.
            rs.strokes.append(traco)
            st.strokes = rs.strokes_em(instante_do_pincel)
            st.notificacao = "rabisco salvo no relatório"
            st.notificacao_ate_ms = instante_do_pincel + 4_000
            sessao = rs.model_copy(deep=True)
            anterior = tarefa_do_stroke

            async def persistir(
                anterior_task: asyncio.Task[None] | None,
            ) -> None:
                if anterior_task is not None:
                    with contextlib.suppress(Exception):
                        await anterior_task
                try:
                    await asyncio.to_thread(save_session, sessao)
                except (OSError, ValueError) as exc:
                    log(f"erro ao salvar rabisco: {exc}")
                    st.notificacao = "erro ao salvar rabisco"
                    st.notificacao_ate_ms = instante_do_pincel + 4_000

            tarefa_do_stroke = asyncio.create_task(persistir(anterior))

        def persistir_sessao_sem_bloquear() -> None:
            """Grava uma cópia sem bloquear os atalhos do pincel."""
            nonlocal tarefa_do_stroke
            sessao = rs.model_copy(deep=True) if rs is not None else None
            if sessao is None:
                return
            anterior = tarefa_do_stroke

            async def persistir(anterior_task: asyncio.Task[None] | None) -> None:
                if anterior_task is not None:
                    with contextlib.suppress(Exception):
                        await anterior_task
                try:
                    await asyncio.to_thread(save_session, sessao)
                except (OSError, ValueError) as exc:
                    log(f"erro ao salvar alteracao do pincel: {exc}")
                    st.notificacao = "erro ao salvar alteração do pincel"
                    st.notificacao_ate_ms = instante_do_pincel + 4_000

            tarefa_do_stroke = asyncio.create_task(persistir(anterior))

        caixa = CaixaDePergunta()
        caixa_de_anotacao = CaixaDePergunta()

        async def _buscar_resposta(texto: str, instante_ms: int) -> None:
            assert perguntar is not None
            try:
                st.resposta = await perguntar(texto, instante_ms)
            except Exception as e:
                # Erro de cota ou de rede nao pode derrubar o overlay: quem
                # esta revisando perde a sessao inteira por causa de uma
                # pergunta. Vira texto na propria caixa.
                st.resposta = f"nao consegui responder: {e}"
            finally:
                st.pensando = False

        def fechar_pergunta() -> None:
            """Fecha os DOIS lados do modo: o estado que desenha e a janela
            que captura o teclado. Fechar so um deixaria o overlay comendo as
            teclas com a caixa ja invisivel — e sem caixa na tela, ninguem
            adivinha que precisa apertar Escape de novo."""
            st.modo_pergunta = False
            # A resposta que chegasse depois de fechar apareceria na PROXIMA
            # caixa aberta, embaixo de outra pergunta, sobre outro instante.
            if tarefa_da_pergunta is not None and not tarefa_da_pergunta.done():
                tarefa_da_pergunta.cancel()
            if overlay is not None:
                overlay.modo_digitacao(False)

        def fechar_anotacao() -> None:
            st.modo_anotacao = False
            caixa_de_anotacao.limpar()
            st.texto_da_anotacao = ""
            if overlay is not None:
                overlay.modo_digitacao(False)

        def ao_digitar(char: str, keysym: str) -> None:
            nonlocal tarefa_da_pergunta
            if st.modo_anotacao:
                acao = caixa_de_anotacao.teclar(char, keysym)
                if acao == "fechar":
                    fechar_anotacao()
                elif acao == "enviar":
                    texto = caixa_de_anotacao.limpar()
                    if texto and rs is not None:
                        m = add_user_mark(rs, instante_da_anotacao, st.tipo_da_anotacao, texto)
                        st.marks = sorted([*st.marks, m], key=lambda x: x.t_ms)
                        res.marcas_do_usuario += 1
                        log(f"anotacao salva em {_mmss(instante_da_anotacao)}")
                    fechar_anotacao()
                st.texto_da_anotacao = caixa_de_anotacao.texto
                return
            acao = caixa.teclar(char, keysym)
            if acao == "fechar":
                fechar_pergunta()
            elif acao == "enviar" and perguntar is not None and not st.pensando:
                st.pensando = True
                st.resposta = ""
                tarefa_da_pergunta = asyncio.create_task(
                    _buscar_resposta(caixa.limpar(), instante_da_pergunta)
                )
            st.texto_da_pergunta = caixa.texto

        def ao_teclar(tecla: str) -> None:
            if rs is None:
                return
            if tecla.isdigit() and prancheta.usar_cor(int(tecla) - 1):
                return
            if tecla.lower() == "z":
                if apagar_ultimo(rs.strokes, instante_do_pincel) is not None:
                    st.strokes = rs.strokes_em(instante_do_pincel)
                    st.notificacao = "último rabisco apagado"
                    st.notificacao_ate_ms = instante_do_pincel + 4_000
                    persistir_sessao_sem_bloquear()
            elif tecla.lower() == "c":
                if limpar_instante(rs.strokes, instante_do_pincel):
                    st.strokes = rs.strokes_em(instante_do_pincel)
                    st.notificacao = "rabiscos apagados deste instante"
                    st.notificacao_ate_ms = instante_do_pincel + 4_000
                    persistir_sessao_sem_bloquear()
                else:
                    st.notificacao = "não há rabiscos neste instante"
                    st.notificacao_ate_ms = instante_do_pincel + 4_000
            elif tecla.lower() == "x":
                prancheta.proxima_espessura()
                st.notificacao = f"espessura: {prancheta.espessura:g}px"
                st.notificacao_ate_ms = instante_do_pincel + 2_000

        while True:
            hwnd = win.find_league_window()
            if hwnd is None:
                sumicos += 1
                if sumicos >= SUMICOS_ATE_SAIR:
                    res.motivo = "a janela do League foi fechada"
                    break
                overlay.mostrar(False)
                await asyncio.sleep(intervalo)
                continue
            sumicos = 0

            rect = win.client_rect_on_screen(hwnd)
            # O PROPRIO OVERLAY conta como "a pessoa esta no jogo". Sem
            # isto ele se esconde por estar aparecendo: a janela dele vira a
            # de primeiro plano, a pergunta "o jogo esta na frente?" responde
            # nao, e o laco o retira da tela para sempre.
            frente = win.is_foreground(hwnd, overlay.hwnd)
            if rect is None or not frente or oculto:
                # Some junto com o jogo. Um overlay que continua flutuando por
                # cima do navegador enquanto a pessoa le o relatorio e pior que
                # nenhum overlay.
                overlay.mostrar(False)
                await asyncio.sleep(intervalo)
                if atalhos and frente and VK_OCULTAR in teclas.novas([VK_OCULTAR]):
                    oculto = not oculto
                continue

            # A resolucao pode mudar no meio (alt-tab, troca de modo de tela).
            # Os ROIs sao fracao da altura justamente para sobreviver a isso.
            st.width, st.height = int(rect.w), int(rect.h)
            overlay.cobrir(rect)
            overlay.mostrar(True)
            if not ja_apareceu:
                ja_apareceu = True
                # O relogio das boas-vindas so comeca a contar agora: antes
                # disso ninguem estava olhando para a tela do jogo, e um
                # cartao de apresentacao exibido para ninguem e o mesmo que
                # nao ter cartao nenhum.
                aberto_em = time.monotonic()
                log(f"overlay na tela · {len(st.marks)} marcacoes")

            try:
                pb = await guard.assert_replay_mode()
            except LiveGameRefused as e:
                res.motivo = f"o client parou de expor o replay: {e.message}"
                break

            agora_ms = cal.to_timeline_ms(pb.time)
            # A pausa do client pode chegar alguns frames depois da requisicao.
            # Enquanto o pincel esta ativo, a cena precisa continuar presa ao
            # instante escolhido; caso contrario os marcadores do mapa parecem
            # andar enquanto o jogador desenha.
            if st.modo_desenho:
                agora_ms = instante_do_pincel
            # Os desenhos deste instante. Com o pincel ligado o instante e o em
            # que ele foi ligado — senao o traco sairia de cena enquanto ainda
            # esta sendo feito.
            alvo_dos_tracos = instante_do_pincel if st.modo_desenho else agora_ms
            if (
                rs is not None
                and not st.modo_desenho
                and time.monotonic() - ultima_sync_s >= 2.0
            ):
                st.strokes = rs.strokes_em(alvo_dos_tracos)
            st.traco_em_andamento = prancheta.em_andamento
            st.cor_do_pincel = prancheta.cor
            st.espessura_do_pincel = prancheta.espessura

            # O painel aparece sozinho no comeco e sob demanda depois. No modo
            # desenho ele sai da frente: a tela toda e a lousa.
            painel = not (st.modo_desenho or st.modo_pergunta) and (
                ajuda or (time.monotonic() - aberto_em) < BOAS_VINDAS_S
            )
            overlay.desenhar(build(st, agora_ms, boas_vindas=painel))
            overlay.bombear()
            res.quadros += 1

            if atalhos:
                for vk in teclas.novas([a.vk for a in TODOS]):
                    atalho = POR_CODIGO[vk]
                    if (st.modo_pergunta and vk != VK_PERGUNTAR) or (
                        st.modo_anotacao and atalho.marca is None
                    ):
                        # Digitando, so o atalho que fecha a caixa vale. No
                        # Windows, AltGr E Ctrl+Alt: no teclado ABNT2, AltGr+Q
                        # da "/" e AltGr+E da "°" — sem este filtro, escrever a
                        # pergunta criaria marcacoes ou pularia o replay.
                        continue
                    if vk == VK_OCULTAR:
                        oculto = True
                    elif vk == VK_AJUDA:
                        # A ajuda precisa estar disponivel SEMPRE, e nao so nos
                        # primeiros segundos: quem abriu o overlay no minuto
                        # vinte nunca viu o cartao de apresentacao.
                        ajuda = not ajuda
                    elif vk == VK_PINCEL:
                        st.modo_desenho = not st.modo_desenho
                        if st.modo_desenho:
                            instante_do_pincel = agora_ms
                            # Ativa a superficie antes da chamada ao client.
                            # A API de pausa pode demorar; esperar aqui fazia
                            # o pincel parecer travado ao ser aberto.
                            overlay.modo_desenho(
                                True,
                                ao_comecar=ao_comecar,
                                ao_mover=ao_mover,
                                ao_soltar=ao_soltar,
                                ao_teclar=ao_teclar,
                            )
                            async def pausar_replay() -> None:
                                with contextlib.suppress(LiveGameRefused):
                                    await ctrl.pause()

                            tarefa_da_pausa = asyncio.create_task(pausar_replay())
                            log(f"pincel ligado em {_mmss(instante_do_pincel)}")
                        else:
                            prancheta.descartar()
                            overlay.modo_desenho(False)
                            # Devolve o foco ao League depois que o pincel
                            # voltou a ser click-through.
                            win.activate(hwnd)
                            log("pincel desligado")
                    elif vk == VK_PERGUNTAR:
                        if perguntar is None:
                            log("perguntar exige um provedor de IA configurado")
                            continue
                        st.modo_pergunta = not st.modo_pergunta
                        if st.modo_pergunta:
                            if st.modo_desenho:
                                # Os dois modos disputam a mesma janela: cada
                                # um liga o clique e prende o teclado do seu
                                # jeito, e desligar um soltaria o do outro.
                                st.modo_desenho = False
                                prancheta.descartar()
                                overlay.modo_desenho(False)
                            instante_da_pergunta = agora_ms
                            caixa.limpar()
                            st.texto_da_pergunta = ""
                            st.resposta = ""
                            # PAUSA, pelo mesmo motivo do pincel: ninguem
                            # digita uma pergunta enquanto o replay corre e o
                            # momento que motivou a pergunta passa.
                            with contextlib.suppress(LiveGameRefused):
                                await ctrl.pause()
                            overlay.modo_digitacao(True, ao_digitar=ao_digitar)
                            log(f"pergunta aberta em {_mmss(instante_da_pergunta)}")
                        else:
                            fechar_pergunta()
                            log("pergunta fechada")
                    elif atalho.marca is not None:
                        if st.modo_pergunta:
                            continue
                        st.modo_anotacao = not st.modo_anotacao
                        if st.modo_anotacao:
                            instante_da_anotacao = agora_ms
                            st.tipo_da_anotacao = atalho.marca
                            caixa_de_anotacao.limpar()
                            st.texto_da_anotacao = ""
                            with contextlib.suppress(LiveGameRefused):
                                await ctrl.pause()
                            overlay.modo_digitacao(True, ao_digitar=ao_digitar)
                            log(f"anotacao aberta em {_mmss(instante_da_anotacao)}")
                        else:
                            fechar_anotacao()
                            log("anotacao fechada")
                    elif vk == VK_PRINT:
                        destino, erro = _salvar_print(rect, st.match_id, agora_ms)
                        if destino and rs is not None:
                            rs.screenshots.append(
                                ReviewScreenshot(t_ms=agora_ms, path=destino)
                            )
                            try:
                                save_session(rs)
                            except (OSError, ValueError) as exc:
                                rs.screenshots.pop()
                                erro = str(exc) or "não foi possível atualizar o relatório"
                                destino = None
                        elif destino:
                            erro = "sessão do relatório não está disponível"
                        mensagem = (
                            "print salvo no relatório"
                            if destino and rs is not None
                            else f"erro ao salvar print: {erro}"
                        )
                        st.notificacao = mensagem
                        st.notificacao_ate_ms = agora_ms + 4_000
                        log(f"print salvo em {destino}" if destino else mensagem)
                    elif vk in (VK_RETOMAR, VK_SEGUINTE):
                        alvo = (
                            retomar_de if vk == VK_RETOMAR else _proxima_marca_depois(st, agora_ms)
                        )
                        if alvo is None:
                            log("nao ha para onde pular a partir daqui")
                            continue
                        await ctrl.seek_to_ms(alvo)
                        # Depois de um seek o client pode reconstruir os dois
                        # relógios com uma pequena diferença transitória.
                        # Recalibrar aqui evita que todas as marcações
                        # seguintes fiquem deslocadas pelo offset antigo.
                        cal = await ctrl.calibrate()
                        log(f"pulando para {_mmss(alvo)}")

            if rs is not None and not st.modo_desenho:
                # O relatório web pode apagar marcações enquanto o replay
                # continua aberto. Recarregar a sessão mantém os dois lados
                # visualmente sincronizados.
                atualizada = load_session(rs.match_id, rs.puuid)
                if atualizada is not None:
                    rs = atualizada
                ultima_sync_s = time.monotonic()
                # Gravar a posicao a cada quadro seria uma escrita em disco dez
                # vezes por segundo. A cada ~10 s basta: o pior caso e a pessoa
                # retomar 10 segundos antes de onde parou, que e onde ela
                # provavelmente queria estar mesmo.
                rs.last_position_ms = agora_ms
                desde_gravou += intervalo
                if desde_gravou >= GRAVA_POSICAO_S:
                    desde_gravou = 0.0
                    save_session(rs)

            await asyncio.sleep(intervalo)

    except KeyboardInterrupt:
        res.motivo = "encerrado por voce"
    finally:
        # Quem chamou fecha o roteador logo depois de `executar` voltar; uma
        # pergunta ainda no ar ficaria falando com um cliente ja fechado.
        if tarefa_da_pergunta is not None and not tarefa_da_pergunta.done():
            tarefa_da_pergunta.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarefa_da_pergunta
        if tarefa_do_stroke is not None and not tarefa_do_stroke.done():
            with contextlib.suppress(Exception):
                await tarefa_do_stroke
        if tarefa_da_pausa is not None and not tarefa_da_pausa.done():
            with contextlib.suppress(Exception):
                await tarefa_da_pausa
        if overlay is not None:
            overlay.fechar()
        with contextlib.suppress(Exception):
            await cliente.aclose()
    return res


# --------------------------------------------------------------------------
# Montagem do estado
# --------------------------------------------------------------------------


def _mmss(ms: int) -> str:
    return f"{ms // 60_000}:{(ms // 1000) % 60:02d}"


def _proxima_marca_depois(st: OverlayState, agora_ms: int) -> int | None:
    """A proxima marcacao da IA, com folga para nao repescar a atual.

    A folga de 2 s existe porque o cartao ja esta na tela quando a pessoa
    aperta: sem ela, "proxima" devolveria a marcacao que ela esta vendo e o
    replay nao sairia do lugar.
    """
    futuras = [m.t_ms for m in st.marks if m.author == "ai" and m.t_ms > agora_ms + 2_000]
    return min(futuras) if futuras else None


def focus_track(timeline: dict[str, Any], participant_id: int) -> list[tuple[int, float, float]]:
    """Posicao do jogador em foco, um ponto por frame da timeline.

    A Riot entrega um frame por MINUTO, e nao ha nada a fazer sobre isso: nao
    existe endpoint com resolucao maior. O consumidor disto (scene._minimapa)
    interpola e desenha um anel largo, nunca um ponto exato.
    """
    pontos: list[tuple[int, float, float]] = []
    for frame in timeline.get("info", {}).get("frames", []):
        pf = frame.get("participantFrames", {}).get(str(participant_id))
        if not pf or "position" not in pf:
            continue
        p = pf["position"]
        pontos.append((int(frame.get("timestamp", 0)), float(p["x"]), float(p["y"])))
    return pontos


async def demonstrar(segundos: float = 15.0, log: Callable[[str], None] = print) -> OverlayRun:
    """Mostra o overlay sobre a area de trabalho, sem replay nenhum.

    Serve para responder em quinze segundos a pergunta que, de outra forma, so
    apareceria no meio da revisao: "isso funciona na MINHA maquina?". Se a cor
    transparente nao pegar, se a janela nao ficar no topo, se a fonte nao
    existir — aparece aqui, longe do jogo, e nao depois de baixar um replay.

    Nao toca no client do League em momento nenhum. Nao ha guard a consultar
    porque nao ha nada para consultar: e uma janela desenhando por cima da
    area de trabalho.
    """
    from riftcoach.core.schema import Mark

    res = OverlayRun()
    if not win.disponivel():
        res.motivo = "o overlay so funciona no Windows"
        return res

    marcas = [
        Mark(
            t_ms=4 * 60_000,
            author="ai",
            kind="error",
            text="exemplo de erro medio",
            category="wave",
            severity=3,
            wp_loss=3.4,
        ),
        Mark(t_ms=8 * 60_000, author="user", kind="question", text="exemplo de marcacao sua"),
        Mark(
            t_ms=12 * 60_000,
            author="ai",
            kind="critical",
            text="exemplo de erro critico: a wave empurrada sem visao no rio "
            "enquanto o dragao nascia",
            category="wave",
            severity=5,
            wp_loss=7.3,
        ),
    ]

    overlay = OverlayWindow()
    try:
        import ctypes

        u = ctypes.windll.user32
        largura, altura = int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1))
        st = OverlayState(
            width=largura,
            height=altura,
            duration_ms=20 * 60_000,
            marks=marcas,
            focus_track=[(0, 1_500.0, 1_800.0), (20 * 60_000, 11_000.0, 4_000.0)],
        )
        overlay.cobrir(Rect(0.0, 0.0, float(largura), float(altura)))
        log(f"overlay em {largura}x{altura} — vidro: {'sim' if overlay.vazado else 'NAO'}")

        # O tempo corre rapido de proposito: em quinze segundos a pessoa ve o
        # cartao entrar, a contagem zerar e a regua percorrer a partida toda.
        passo = 20 * 60_000 / max(1.0, segundos * FPS)
        agora = 0.0
        while agora < 20 * 60_000:
            overlay.desenhar(build(st, int(agora)))
            overlay.bombear()
            res.quadros += 1
            agora += passo
            await asyncio.sleep(1.0 / FPS)
        res.motivo = "demonstracao concluida"
    finally:
        overlay.fechar()
    return res


def _salvar_print(rect: Rect, match_id: str, t_ms: int) -> tuple[str | None, str]:
    """Fotografa a area do jogo, com o overlay por cima, e grava em disco.

    E o que o pedido chamou de "como um print": a jogada, o cartao do erro e o
    rabisco na mesma imagem, pronta para mandar para alguem. A captura e da
    TELA, entao ela ja pega o overlay — nao ha composicao a fazer, e por isso
    o que sai e exatamente o que se viu.

    Depende do PIL, que e do extra `vision`. Sem ele devolve None em vez de
    estourar: perder o print e chato, perder a revisao seria pior.
    """
    try:
        from PIL import ImageGrab
    except ImportError:
        return None, "Pillow não está instalado"

    pasta = data_dir() / "prints"
    pasta.mkdir(parents=True, exist_ok=True)
    base = f"{match_id or 'partida'}_{t_ms // 60_000:02d}m{(t_ms // 1000) % 60:02d}s"
    destino = pasta / f"{base}.png"
    contador = 2
    while destino.exists():
        destino = pasta / f"{base}_{contador}.png"
        contador += 1
    caixa = (int(rect.x), int(rect.y), int(rect.right), int(rect.bottom))
    try:
        # `rect` e o retangulo da area cliente do League, ja convertido para
        # coordenadas da tela. Nao usar all_screens: o print deve conter
        # somente o jogo e o overlay, nunca a area virtual de outros monitores.
        imagem = ImageGrab.grab(bbox=caixa, all_screens=False)
        if imagem.width <= 0 or imagem.height <= 0:
            return None, "a captura retornou uma imagem vazia"
        imagem.save(destino, format="PNG")
    except (OSError, ValueError, TypeError) as exc:
        return None, str(exc) or "o Windows recusou a captura"
    return str(destino), ""
