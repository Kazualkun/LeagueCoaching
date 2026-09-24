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

DOIS RITMOS, e confundi-los era o motivo de o pincel parecer lento:

  eventos   ~60 Hz   mouse, teclado e atalhos. Barato; e o que a mao sente.
  quadro     10 Hz   relogio do client e redesenho completo da cena.

Antes os dois andavam juntos a 10 Hz: um arrasto so era processado a cada
100 ms, o traco so aparecia no quadro seguinte, e cada quadro ainda relia o
JSON da revisao do disco. Desfazer e limpar esperavam a mesma fila.
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
from riftcoach.core.review import add_user_mark, load_session, save_session, session_path
from riftcoach.core.schema import ReviewScreenshot, ReviewSession, Stroke
from riftcoach.overlay import window as win
from riftcoach.overlay.atalhos import (
    POR_CODIGO,
    TODOS,
    VK_AJUDA,
    VK_CONTROL,
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
from riftcoach.overlay.geometry import Rect, minimap_rect
from riftcoach.overlay.render import OverlayWindow
from riftcoach.overlay.scene import OverlayState, area_da_barra, build, paleta_do_pincel
from riftcoach.replay.controller import ReplayController
from riftcoach.replay.guard import open_guard

# 10 quadros por segundo. O relogio do replay so muda de decimo em decimo, e a
# maquina de referencia deste projeto e uma Intel HD 4000 — gastar 60 fps para
# redesenhar a mesma coisa seria tirar quadros do jogo para nao mostrar nada
# de novo.
FPS = 10.0

# O ritmo dos EVENTOS: mouse, teclado, atalhos. Separado do FPS de proposito
# (ver o cabecalho). 60 Hz e o que o olho percebe como imediato; no Windows o
# timer do asyncio arredonda para ~16 ms, entao pedir mais nao entrega mais.
EVENTOS_HZ = 60.0

# Quantos quadros seguidos sem achar a janela do jogo antes de desistir.
SUMICOS_ATE_SAIR = 30

# Quantas recusas seguidas do guard antes de fechar. Uma recusa isolada
# acontece enquanto o client processa um salto; trinta (3 s) significam que o
# replay fechou. Na PRIMEIRA recusa o overlay ja some da tela: se o que abriu
# no lugar for uma partida ao vivo, nada nosso pode ficar desenhado por cima.
RECUSAS_ATE_SAIR = 30

# De quanto em quanto tempo a posicao atual vai para o disco.
GRAVA_POSICAO_S = 10.0

# De quanto em quanto tempo o overlay confere se o relatorio web mexeu na
# revisao (marcacao apagada, anotacao nova). So a DATA do arquivo e lida; o
# arquivo so e relido quando ela muda.
CONFERE_ARQUIVO_S = 1.5

# Quanto tempo o cartao de apresentacao fica na tela. Contado em relogio de
# PAREDE, nao no do replay: ele precisa sumir sozinho mesmo com o replay
# pausado, que e exatamente como a maioria das pessoas abre o overlay.
BOAS_VINDAS_S = 12.0

# Depois de um atalho com o replay pausado, por quanto tempo uma despausa que
# NAO pedimos e desfeita. Existe porque o League recebe a mesma tecla que o
# overlay (a leitura do atalho e pelo estado do teclado, sem engolir nada), e
# algumas combinacoes mexem no replay. O overlay nao consegue impedir a tecla
# de chegar ao jogo — mas consegue devolver o replay ao estado em que estava.
PROTEGE_PAUSA_S = 1.2

# Com que frequencia tentar medir o minimapa na tela, ate conseguir.
MEDE_MINIMAPA_S = 20.0


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
# Gravacao fora do laco
# --------------------------------------------------------------------------

Operacao = Callable[[ReviewSession], None]


class _Gravador:
    """Grava a revisao numa tarefa de fundo, APLICANDO a mudanca a versao mais
    nova do arquivo — e nao despejando a copia que esta na memoria.

    A diferenca importa porque ha dois escritores: o overlay e o relatorio
    web. Se o overlay gravasse a copia inteira dele, uma anotacao feita na
    pagina entre duas conferencias seria apagada pela proxima gravacao da
    posicao. Aplicando so a operacao (acrescentar este traco, mudar esta
    posicao) em cima do que esta em disco, os dois lados convivem.

    Uma gravacao de cada vez; operacoes que chegam no meio vao juntas na
    seguinte. O laco nunca espera o disco.
    """

    def __init__(self, base: ReviewSession, log: Callable[[str], None]) -> None:
        self._base = base
        self._log = log
        self._fila: list[Operacao] = []
        self._tarefa: asyncio.Task[None] | None = None
        # A versao que acabou de ir para o disco, e a data do arquivo depois
        # dela — para o laco nao "descobrir" a propria gravacao como mudanca.
        self.gravada: ReviewSession | None = None
        self.mtime_conhecido: int = _mtime(base)
        self.erro: str = ""

    @property
    def ocupado(self) -> bool:
        return bool(self._fila) or (self._tarefa is not None and not self._tarefa.done())

    def pedir(self, op: Operacao) -> None:
        self._fila.append(op)
        if self._tarefa is None or self._tarefa.done():
            self._tarefa = asyncio.create_task(self._drenar())

    async def _drenar(self) -> None:
        while self._fila:
            ops, self._fila = self._fila, []
            base = self._base

            def trabalho(ops: list[Operacao] = ops, base: ReviewSession = base) -> ReviewSession:
                atual = load_session(base.match_id, base.puuid) or base.model_copy(deep=True)
                for op in ops:
                    op(atual)
                save_session(atual)
                return atual

            try:
                self.gravada = await asyncio.to_thread(trabalho)
                self._base = self.gravada
                self.mtime_conhecido = _mtime(self.gravada)
                self.erro = ""
            except (OSError, ValueError) as exc:
                self.erro = str(exc) or exc.__class__.__name__
                self._log(f"erro ao gravar a revisao: {self.erro}")

    async def esperar(self) -> None:
        while self._tarefa is not None and not self._tarefa.done():
            with contextlib.suppress(Exception):
                await self._tarefa


def _mtime(rs: ReviewSession) -> int:
    try:
        return session_path(rs.match_id, rs.puuid).stat().st_mtime_ns
    except OSError:
        return 0


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

    instancia = win.InstanciaUnica()
    if not instancia.adquirir():
        res.motivo = "o overlay que ja estava aberto nao fechou a tempo"
        return res
    if instancia.substituiu:
        log("fechei o overlay que ja estava aberto para abrir este")

    overlay: OverlayWindow | None = None
    # Tarefas de fundo. Nenhuma conversa com o client acontece DENTRO do
    # laco de eventos esperando resposta: pausar, pular e recalibrar levam de
    # dezenas de ms a segundos, e o overlay congelava durante cada uma.
    tarefas: set[asyncio.Task[Any]] = set()
    tarefa_da_pergunta: asyncio.Task[None] | None = None
    gravador: _Gravador | None = None
    cliente = None
    try:
        cliente, guard = await open_guard()
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

        gravador = _Gravador(rs, log) if rs is not None else None

        teclas = _Teclas()
        oculto = False
        sumicos = 0
        recusas = 0
        passo = 1.0 / EVENTOS_HZ
        periodo_quadro = 1.0 / max(1.0, fps)
        ultimo_quadro = 0.0
        ultima_conferencia = time.monotonic()
        ultima_posicao_gravada = time.monotonic()
        ultima_medicao = -MEDE_MINIMAPA_S
        minimapa_medido_em: tuple[int, int] | None = None

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

        # O estado que o laco carrega entre um tique e outro.
        agora_ms = 0
        pausado = True
        hwnd: int | None = None
        rect: Rect | None = None
        frente = False
        sujo = True  # redesenhar no proximo tique, sem esperar o quadro
        notificacao_ate = 0.0
        protege_pausa_ate = 0.0
        sair_do_pincel = False
        painel_desenhado = False

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

        def em_fundo(coro: Awaitable[Any]) -> None:
            t: asyncio.Task[Any] = asyncio.ensure_future(coro)
            tarefas.add(t)
            t.add_done_callback(tarefas.discard)

        def notificar(texto: str, segundos: float = 2.5) -> None:
            """Aviso curto, medido em relogio de PAREDE.

            Antes o prazo era no relogio do replay — e com o replay pausado
            (que e como se desenha e se anota) ele nunca vencia: "rabisco salvo"
            ficava na tela a sessao inteira do pincel, cobrindo o jogo.
            """
            nonlocal notificacao_ate, sujo
            st.notificacao = texto
            st.notificacao_ate_ms = 1 << 62
            notificacao_ate = time.monotonic() + segundos
            sujo = True

        async def pausar() -> None:
            with contextlib.suppress(LiveGameRefused):
                await ctrl.pause()

        def proteger_pausa() -> None:
            """Chamado a cada atalho: se o replay estava parado, ele continua
            parado — ver PROTEGE_PAUSA_S."""
            nonlocal protege_pausa_ate
            if pausado:
                protege_pausa_ate = time.monotonic() + PROTEGE_PAUSA_S

        def atualizar_tracos() -> None:
            if rs is None:
                return
            alvo = instante_do_pincel if st.modo_desenho else agora_ms
            st.strokes = rs.strokes_em(alvo)

        # ------------------------------------------------------------
        # Pincel
        # ------------------------------------------------------------

        def px(p: tuple[float, float]) -> tuple[float, float]:
            return p[0] * st.width, p[1] * st.height

        def ao_comecar(x: float, y: float) -> None:
            nonlocal sujo
            # A faixa de cores fica no rodape e recebe clique como botao, nao
            # como lousa: escolher uma cor nao pode virar um traco curto.
            xp, yp = x * st.width, y * st.height
            if yp >= area_da_barra(st):
                for cx, cy, raio, indice in paleta_do_pincel(st):
                    if (xp - cx) ** 2 + (yp - cy) ** 2 <= raio**2 and prancheta.usar_cor(indice):
                        sujo = True
                        return
                return
            prancheta.comecar(x, y)

        def ao_mover(x: float, y: float) -> None:
            antes = prancheta.ultimo
            if antes is not None and prancheta.mover(x, y) and overlay is not None:
                depois = prancheta.ultimo
                assert depois is not None
                overlay.segmento(px(antes), px(depois), prancheta.cor, prancheta.espessura)

        def ao_soltar() -> None:
            nonlocal sujo
            traco = prancheta.terminar(instante_do_pincel)
            sujo = True
            if traco is None or rs is None or gravador is None:
                prancheta.descartar()
                return
            # Primeiro a memoria e a tela; o disco vem numa tarefa de fundo.
            rs.strokes.append(traco)
            atualizar_tracos()
            gravador.pedir(lambda r, t=traco: r.strokes.append(t))

        def desfazer() -> None:
            nonlocal sujo
            if rs is None or gravador is None:
                return
            saiu = apagar_ultimo(rs.strokes, instante_do_pincel)
            if saiu is None:
                notificar("nada para desfazer neste instante")
                return
            atualizar_tracos()
            sujo = True

            def op(r: ReviewSession, alvo: Stroke = saiu) -> None:
                if alvo in r.strokes:
                    r.strokes.remove(alvo)

            gravador.pedir(op)

        def limpar() -> None:
            if rs is None or gravador is None:
                return
            if not limpar_instante(rs.strokes, instante_do_pincel):
                notificar("não há rabiscos neste instante")
                return
            atualizar_tracos()
            notificar("rabiscos deste instante apagados")
            t = instante_do_pincel
            gravador.pedir(lambda r: limpar_instante(r.strokes, t) and None)

        def ao_teclar(tecla: str) -> None:
            nonlocal sujo, sair_do_pincel
            if tecla.isdigit() and prancheta.usar_cor(int(tecla) - 1):
                sujo = True
            elif tecla.lower() == "z":
                desfazer()
            elif tecla.lower() == "c":
                limpar()
            elif tecla.lower() == "x":
                prancheta.proxima_espessura()
                sujo = True
            elif tecla == "Escape":
                sair_do_pincel = True

        def ligar_pincel() -> None:
            nonlocal instante_do_pincel, sujo
            assert overlay is not None
            st.modo_desenho = True
            instante_do_pincel = agora_ms
            atualizar_tracos()
            # A superficie liga ANTES de falar com o client: a pausa pode
            # demorar, e esperar por ela fazia o pincel parecer travado.
            overlay.modo_desenho(
                True,
                ao_comecar=ao_comecar,
                ao_mover=ao_mover,
                ao_soltar=ao_soltar,
                ao_teclar=ao_teclar,
                ao_desfazer=desfazer,
            )
            em_fundo(pausar())
            sujo = True
            log(f"pincel ligado em {_mmss(instante_do_pincel)}")

        def desligar_pincel() -> None:
            nonlocal sujo
            st.modo_desenho = False
            prancheta.descartar()
            if overlay is not None:
                overlay.modo_desenho(False)
            # Devolve o foco ao League: a lousa saiu da frente.
            if hwnd is not None:
                win.activate(hwnd)
            atualizar_tracos()
            sujo = True
            log("pincel desligado")

        # ------------------------------------------------------------
        # Caixas de texto
        # ------------------------------------------------------------

        caixa = CaixaDePergunta()
        caixa_de_anotacao = CaixaDePergunta()

        async def _buscar_resposta(texto: str, instante_ms: int) -> None:
            nonlocal sujo
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
                sujo = True

        def fechar_pergunta() -> None:
            """Fecha os DOIS lados do modo: o estado que desenha e a janela
            que captura o teclado. Fechar so um deixaria o overlay comendo as
            teclas com a caixa ja invisivel — e sem caixa na tela, ninguem
            adivinha que precisa apertar Escape de novo."""
            nonlocal sujo
            st.modo_pergunta = False
            # A resposta que chegasse depois de fechar apareceria na PROXIMA
            # caixa aberta, embaixo de outra pergunta, sobre outro instante.
            if tarefa_da_pergunta is not None and not tarefa_da_pergunta.done():
                tarefa_da_pergunta.cancel()
            if overlay is not None:
                overlay.modo_digitacao(False)
            if hwnd is not None:
                win.activate(hwnd)
            sujo = True

        def fechar_anotacao() -> None:
            nonlocal sujo
            st.modo_anotacao = False
            caixa_de_anotacao.limpar()
            st.texto_da_anotacao = ""
            if overlay is not None:
                overlay.modo_digitacao(False)
            if hwnd is not None:
                win.activate(hwnd)
            sujo = True

        def ao_digitar(char: str, keysym: str) -> None:
            nonlocal tarefa_da_pergunta, sujo
            sujo = True
            if st.modo_anotacao:
                acao = caixa_de_anotacao.teclar(char, keysym)
                if acao == "fechar":
                    fechar_anotacao()
                elif acao == "enviar":
                    texto = caixa_de_anotacao.limpar()
                    if texto and rs is not None and gravador is not None:
                        m = add_user_mark(
                            rs, instante_da_anotacao, st.tipo_da_anotacao, texto, gravar=False
                        )
                        gravador.pedir(lambda r, m=m: r.add(m))
                        st.marks = sorted([*st.marks, m], key=lambda x: x.t_ms)
                        res.marcas_do_usuario += 1
                        log(f"anotacao salva em {_mmss(instante_da_anotacao)}")
                        notificar("anotação salva no relatório")
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

        # ------------------------------------------------------------
        # Tarefas que falam com o client
        # ------------------------------------------------------------

        async def navegar(alvo_ms: int) -> None:
            nonlocal cal
            try:
                await ctrl.seek_to_ms(alvo_ms)
            except LiveGameRefused as e:
                log(f"nao consegui pular: {e.message}")
                return
            # Depois de um salto o client reconstroi os dois relogios, com uma
            # diferenca transitoria de alguns segundos. Recalibrar cedo demais
            # grava um offset absurdo; entao espera, tenta, e mantem o antigo
            # se nao assentar — ele continua quase certo.
            for espera in (2.5, 2.0, 3.0):
                await asyncio.sleep(espera)
                try:
                    cal = await ctrl.calibrate()
                    return
                except LiveGameRefused:
                    continue

        async def salvar_print(r: Rect, t_ms: int) -> None:
            destino, erro = await asyncio.to_thread(_salvar_print, r, st.match_id, t_ms)
            if destino and rs is not None and gravador is not None:
                shot = ReviewScreenshot(t_ms=t_ms, path=destino)
                rs.screenshots.append(shot)
                gravador.pedir(lambda s, shot=shot: s.screenshots.append(shot))
                notificar("print salvo no relatório")
                log(f"print salvo em {destino}")
            else:
                if destino:
                    erro = "sessão do relatório não está disponível"
                notificar(f"erro ao salvar print: {erro}", 4.0)
                log(f"erro ao salvar print: {erro}")

        async def medir_minimapa(r: Rect) -> None:
            """Remede a caixa do minimapa na tela, pelas torres.

            Fora do laco porque custa meio segundo de Python puro. Falhar e
            normal (minimapa coberto, poucas torres de pe): fica a caixa
            padrao e tenta de novo mais tarde.
            """
            nonlocal minimapa_medido_em, sujo
            try:
                from PIL import ImageGrab

                from riftcoach.overlay.calibrar import ajustar
            except ImportError:
                minimapa_medido_em = (int(r.w), int(r.h))  # sem PIL, nao insiste
                return
            chute = minimap_rect(int(r.w), int(r.h), st.hud_scale)

            def medir() -> Any:
                img = ImageGrab.grab(
                    bbox=(int(r.x), int(r.y), int(r.right), int(r.bottom)), all_screens=False
                )
                return ajustar(img, chute)

            try:
                medida = await asyncio.to_thread(medir)
            except (OSError, ValueError):
                return
            if medida is None:
                return
            st.minimap_px = medida.rect
            minimapa_medido_em = (int(r.w), int(r.h))
            sujo = True
            log(
                f"minimapa medido na tela: {medida.rect.w:.0f}px em "
                f"({medida.rect.x:.0f},{medida.rect.y:.0f}), {medida.torres} torres, "
                f"erro {medida.residuo_px}px"
            )

        # ------------------------------------------------------------
        # O laco
        # ------------------------------------------------------------

        while True:
            if instancia.pediram_para_sair():
                res.motivo = "outro overlay foi aberto no lugar deste"
                break

            agora_s = time.monotonic()
            quadro = agora_s - ultimo_quadro >= periodo_quadro

            if quadro:
                ultimo_quadro = agora_s
                hwnd = win.find_league_window()
                if hwnd is None:
                    sumicos += 1
                    if sumicos >= SUMICOS_ATE_SAIR:
                        res.motivo = "a janela do League foi fechada"
                        break
                    overlay.mostrar(False)
                    await asyncio.sleep(periodo_quadro)
                    continue
                sumicos = 0
                rect = win.client_rect_on_screen(hwnd)
                # O PROPRIO OVERLAY (e a lousa) contam como "a pessoa esta no
                # jogo". Sem isto ele se esconde por estar aparecendo.
                frente = win.is_foreground(hwnd, *overlay.hwnds)

            if rect is None or not frente or oculto or hwnd is None:
                # Some junto com o jogo. Um overlay que continua flutuando por
                # cima do navegador enquanto a pessoa le o relatorio e pior que
                # nenhum overlay.
                overlay.mostrar(False)
                overlay.bombear()
                if atalhos and frente and VK_OCULTAR in teclas.novas([VK_OCULTAR]):
                    oculto = not oculto
                    sujo = True
                await asyncio.sleep(passo)
                continue

            if quadro:
                if (int(rect.w), int(rect.h)) != (st.width, st.height):
                    # A resolucao pode mudar no meio (alt-tab, troca de modo de
                    # tela). Os ROIs sao fracao da altura para sobreviver a isso.
                    st.width, st.height = int(rect.w), int(rect.h)
                    st.minimap_px = None
                    sujo = True
                overlay.cobrir(rect)
                overlay.mostrar(True)
                if not ja_apareceu:
                    ja_apareceu = True
                    # O relogio das boas-vindas so comeca a contar agora: antes
                    # disso ninguem estava olhando para a tela do jogo.
                    aberto_em = agora_s
                    log(f"overlay na tela · {len(st.marks)} marcacoes")

                if (
                    minimapa_medido_em != (int(rect.w), int(rect.h))
                    and agora_s - ultima_medicao >= MEDE_MINIMAPA_S
                    and not st.modo_desenho
                ):
                    ultima_medicao = agora_s
                    em_fundo(medir_minimapa(rect))

                if not st.modo_desenho:
                    # No pincel o replay ja esta pausado e a cena fica presa no
                    # instante escolhido; perguntar o relogio ali so gastava
                    # tempo do laco de eventos.
                    try:
                        pb = await guard.assert_replay_mode()
                    except LiveGameRefused as e:
                        recusas += 1
                        overlay.mostrar(False)
                        if recusas >= RECUSAS_ATE_SAIR:
                            res.motivo = f"o client parou de expor o replay: {e.message}"
                            break
                        await asyncio.sleep(periodo_quadro)
                        continue
                    recusas = 0
                    novo_ms = cal.to_timeline_ms(pb.time)
                    if novo_ms != agora_ms:
                        agora_ms = novo_ms
                        sujo = True
                    if not pb.paused and agora_s < protege_pausa_ate:
                        protege_pausa_ate = 0.0
                        em_fundo(pausar())
                        log("o replay despausou junto com o atalho; pausei de novo")
                    pausado = pb.paused
                    atualizar_tracos()

                if st.notificacao and agora_s >= notificacao_ate:
                    st.notificacao = ""
                    sujo = True

                if rs is not None and gravador is not None and not gravador.ocupado:
                    if gravador.gravada is not None and gravador.gravada is not rs:
                        # A gravacao juntou o que estava no disco com o que
                        # fizemos aqui; ela passa a ser a versao de trabalho.
                        rs = gravador.gravada
                        gravador.gravada = None
                    if agora_s - ultima_conferencia >= CONFERE_ARQUIVO_S:
                        ultima_conferencia = agora_s
                        if _mtime(rs) != gravador.mtime_conhecido:
                            # O relatorio web mexeu na revisao (anotacao nova,
                            # marcacao apagada). Relida SO quando o arquivo
                            # muda — antes era relida dez vezes por segundo.
                            atualizada = load_session(rs.match_id, rs.puuid)
                            if atualizada is not None:
                                rs = atualizada
                                gravador.mtime_conhecido = _mtime(rs)
                                st.marks = _com_lugares(rs.timeline(), st.marks)
                                atualizar_tracos()
                                sujo = True
                    if (
                        agora_s - ultima_posicao_gravada >= GRAVA_POSICAO_S
                        and agora_ms != rs.last_position_ms
                    ):
                        ultima_posicao_gravada = agora_s
                        rs.last_position_ms = agora_ms
                        t_pos = agora_ms
                        gravador.pedir(lambda r, t=t_pos: setattr(r, "last_position_ms", t))

            if sair_do_pincel and st.modo_desenho:
                sair_do_pincel = False
                desligar_pincel()

            # Atalhos, no ritmo dos eventos.
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
                    sujo = True
                    if vk not in (VK_RETOMAR, VK_SEGUINTE):
                        proteger_pausa()
                    if vk == VK_OCULTAR:
                        oculto = True
                    elif vk == VK_AJUDA:
                        # A ajuda precisa estar disponivel SEMPRE, e nao so nos
                        # primeiros segundos: quem abriu o overlay no minuto
                        # vinte nunca viu o cartao de apresentacao.
                        ajuda = not ajuda
                    elif vk == VK_PINCEL:
                        if st.modo_desenho:
                            desligar_pincel()
                        else:
                            if st.modo_pergunta:
                                fechar_pergunta()
                            if st.modo_anotacao:
                                fechar_anotacao()
                            ligar_pincel()
                    elif vk == VK_PERGUNTAR:
                        if perguntar is None:
                            notificar("perguntar exige um provedor de IA configurado", 4.0)
                            continue
                        if st.modo_pergunta:
                            fechar_pergunta()
                            log("pergunta fechada")
                            continue
                        if st.modo_desenho:
                            # Os dois modos disputam a mesma lousa: cada um
                            # prende o teclado do seu jeito.
                            desligar_pincel()
                        st.modo_pergunta = True
                        instante_da_pergunta = agora_ms
                        caixa.limpar()
                        st.texto_da_pergunta = ""
                        st.resposta = ""
                        # A caixa abre ANTES da pausa responder: esperar o
                        # client aqui era o atraso que se sentia ao apertar.
                        overlay.modo_digitacao(True, ao_digitar=ao_digitar)
                        em_fundo(pausar())
                        log(f"pergunta aberta em {_mmss(instante_da_pergunta)}")
                    elif atalho.marca is not None:
                        if st.modo_pergunta:
                            continue
                        if st.modo_anotacao:
                            fechar_anotacao()
                            log("anotacao fechada")
                            continue
                        if st.modo_desenho:
                            desligar_pincel()
                        st.modo_anotacao = True
                        instante_da_anotacao = agora_ms
                        st.tipo_da_anotacao = atalho.marca
                        caixa_de_anotacao.limpar()
                        st.texto_da_anotacao = ""
                        overlay.modo_digitacao(True, ao_digitar=ao_digitar)
                        em_fundo(pausar())
                        log(f"anotacao aberta em {_mmss(instante_da_anotacao)}")
                    elif vk == VK_PRINT:
                        # Redesenha antes de fotografar: o print tem de sair com
                        # o que esta na tela AGORA, inclusive o traco recem-feito.
                        st.traco_em_andamento = prancheta.em_andamento
                        overlay.desenhar(_cena(st, agora_ms, instante_do_pincel, False))
                        overlay.bombear()
                        em_fundo(
                            salvar_print(rect, instante_do_pincel if st.modo_desenho else agora_ms)
                        )
                    elif vk in (VK_RETOMAR, VK_SEGUINTE):
                        alvo = (
                            retomar_de if vk == VK_RETOMAR else _proxima_marca_depois(st, agora_ms)
                        )
                        if alvo is None:
                            notificar("não há para onde pular a partir daqui")
                            continue
                        if st.modo_desenho:
                            desligar_pincel()
                        em_fundo(navegar(alvo))
                        log(f"pulando para {_mmss(alvo)}")

            # O painel aparece sozinho no comeco e sob demanda depois. No modo
            # desenho ele sai da frente: a tela toda e a lousa. Ele vence em
            # relogio de PAREDE — o replay pausado nao marcaria a cena como
            # suja, e o cartao ficaria na tela para sempre.
            painel = not (st.modo_desenho or st.modo_pergunta or st.modo_anotacao) and (
                ajuda or (agora_s - aberto_em) < BOAS_VINDAS_S
            )
            if painel != painel_desenhado:
                sujo = True
            if sujo:
                sujo = False
                painel_desenhado = painel
                st.traco_em_andamento = prancheta.em_andamento
                st.cor_do_pincel = prancheta.cor
                st.espessura_do_pincel = prancheta.espessura
                overlay.desenhar(_cena(st, agora_ms, instante_do_pincel, painel))
                res.quadros += 1

            overlay.bombear()
            await asyncio.sleep(passo)

    except KeyboardInterrupt:
        res.motivo = "encerrado por voce"
    finally:
        # Quem chamou fecha o roteador logo depois de `executar` voltar; uma
        # pergunta ainda no ar ficaria falando com um cliente ja fechado.
        if tarefa_da_pergunta is not None and not tarefa_da_pergunta.done():
            tarefa_da_pergunta.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tarefa_da_pergunta
        for t in list(tarefas):
            if not t.done():
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(t, timeout=3.0)
        if gravador is not None:
            await gravador.esperar()
        if overlay is not None:
            overlay.fechar()
        if cliente is not None:
            with contextlib.suppress(Exception):
                await cliente.aclose()
        instancia.liberar()
    return res


def _cena(st: OverlayState, agora_ms: int, instante_do_pincel: int, painel: bool) -> Any:
    """No pincel a cena fica presa no instante em que ele foi ligado."""
    return build(st, instante_do_pincel if st.modo_desenho else agora_ms, boas_vindas=painel)


def _com_lugares(novas: list[Any], antigas: list[Any]) -> list[Any]:
    """Marcacoes relidas do disco, sem perder o lugar no mapa das antigas.

    `where`/`you` sao calculados ao abrir o overlay (precisam da timeline
    crua). Uma marcacao que veio do disco sem eles, mas que o overlay ja tinha
    posicionado, herda a posicao.
    """
    por_chave = {(m.t_ms, m.text): m for m in antigas}
    for m in novas:
        velha = por_chave.get((m.t_ms, m.text))
        if velha is not None:
            if m.where is None:
                m.where = velha.where
            if m.you is None:
                m.you = velha.you
    return novas


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
