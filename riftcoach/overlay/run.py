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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from riftcoach.core.errors import LiveGameRefused
from riftcoach.core.review import add_user_mark, save_session
from riftcoach.core.schema import MarkKind, ReviewSession
from riftcoach.overlay import window as win
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

# Ctrl+Alt+<tecla>. Escolhido porque o League nao usa essa combinacao em nada,
# entao nao ha como colidir com um atalho do jogo — e porque um atalho de uma
# tecla so seria disparado sem querer o tempo todo durante a revisao.
#
# ATENCAO, e isto e questao de postura e nao de codigo: a leitura e do ESTADO
# do teclado pelo sistema operacional, so enquanto a janela do League esta em
# primeiro plano, e nunca ha injecao de tecla ou de clique. Nada e enviado
# para o jogo em momento nenhum. Combinado com o guard, que so deixa o overlay
# existir sobre um replay, nao ha caminho daqui para dentro de uma partida ao
# vivo.
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

ATALHOS: dict[int, tuple[str, MarkKind]] = {
    0x45: ("erro", "error"),  # E
    0x4E: ("nota", "note"),  # N
    0x47: ("acerto", "good"),  # G
    0x51: ("duvida", "question"),  # Q
}
VK_OCULTAR = 0x48  # H
VK_RETOMAR = 0x52  # R — volta para onde a revisao parou
VK_SEGUINTE = 0x53  # S — pula para a proxima marcacao

# As duas teclas de navegacao fazem o replay ANDAR, entao elas passam pelo
# controller e nao pelo guard cru: e ele que sabe que o alvo de um erro e 8
# segundos ANTES da decisao, e nao o instante marcado.
NAVEGACAO = (VK_RETOMAR, VK_SEGUINTE)


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
) -> OverlayRun:
    """Abre o overlay e fica nele ate o replay ou o usuario terminarem."""
    res = OverlayRun()
    if not win.disponivel():
        res.motivo = "o overlay so funciona no Windows"
        return res

    cliente, guard = await open_guard()
    overlay: OverlayWindow | None = None
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
        retomar_de = rs.last_position_ms if rs and rs.last_position_ms > 0 else None
        if retomar_de:
            log(f"voce parou em {_mmss(retomar_de)} — Ctrl+Alt+R volta para la")

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
            saudando = (time.monotonic() - aberto_em) < BOAS_VINDAS_S
            overlay.desenhar(build(st, agora_ms, boas_vindas=saudando))
            overlay.bombear()
            res.quadros += 1

            if atalhos:
                for vk in teclas.novas([*ATALHOS, VK_OCULTAR, *NAVEGACAO]):
                    if vk == VK_OCULTAR:
                        oculto = True
                    elif vk in NAVEGACAO:
                        alvo = (
                            retomar_de if vk == VK_RETOMAR else _proxima_marca_depois(st, agora_ms)
                        )
                        if alvo is None:
                            log("nao ha para onde pular a partir daqui")
                            continue
                        await ctrl.seek_to_ms(alvo)
                        log(f"pulando para {_mmss(alvo)}")
                    elif rs is not None:
                        rotulo, kind = ATALHOS[vk]
                        m = add_user_mark(rs, agora_ms, kind, f"({rotulo}) marcado por voce")
                        st.marks = sorted([*st.marks, m], key=lambda x: x.t_ms)
                        res.marcas_do_usuario += 1
                        log(f"marcacao '{rotulo}' salva em {_mmss(agora_ms)}")

            if rs is not None:
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
