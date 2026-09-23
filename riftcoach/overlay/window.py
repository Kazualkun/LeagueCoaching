"""Onde esta a janela do League, em pixels de tela. So Windows.

Tudo aqui e ctypes puro. Nenhuma dependencia nova: quem vai usar o RiftCoach
nao deveria ter de instalar pywin32 para ver um circulo no minimapa.

TRES ARMADILHAS, e as tres fazem o overlay aparecer no lugar errado em vez de
falhar — que e o modo de falha mais caro, porque parece funcionar:

1. DPI. Sem declarar consciencia de DPI, o Windows mente sobre as coordenadas:
   num monitor a 150% ele devolve tudo dividido por 1,5 e o overlay fica
   deslocado para cima e para a esquerda. Tem de ser declarado ANTES de criar
   qualquer janela.

2. Area de cliente != janela. `GetWindowRect` inclui borda e barra de titulo
   quando existem. O que interessa e a area onde o jogo desenha, entao e
   `GetClientRect` + `ClientToScreen`.

3. TELA CHEIA EXCLUSIVA NAO ACEITA OVERLAY. Nao e limitacao nossa: nesse modo
   a aplicacao e dona da cadeia de apresentacao e nada do sistema aparece por
   cima. E preciso jogar em "Sem bordas". Nao da para detectar isso com
   confianca pela API, entao o jeito honesto e avisar antes, no `doctor`, e
   nao deixar a pessoa achando que o programa quebrou.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
from ctypes import wintypes

from riftcoach.overlay.geometry import Rect

# A classe da janela do JOGO. Nao confundir com a do client (loja, lobby), que
# e uma aplicacao completamente diferente e nunca roda replay.
CLASSE_JOGO = "RiotWindowClass"
TITULO_JOGO = "League of Legends (TM) Client"

# Estilos estendidos, montados na janela do overlay depois de criada.
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020  # o clique atravessa
WS_EX_TOOLWINDOW = 0x00000080  # fora do Alt+Tab e da barra de tarefas
WS_EX_NOACTIVATE = 0x08000000  # nunca rouba o foco do jogo
GWL_EXSTYLE = -20


def disponivel() -> bool:
    return sys.platform == "win32"


def set_dpi_aware() -> None:
    """Chame antes de criar qualquer janela. Silencioso se nao der."""
    if not disponivel():
        return
    try:
        # -4 = PER_MONITOR_AWARE_V2, o unico que acerta em monitor misto.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        with contextlib.suppress(AttributeError, OSError):  # Windows 7
            ctypes.windll.user32.SetProcessDPIAware()


def find_league_window() -> int | None:
    """HWND da janela do jogo, ou None.

    Procura pela classe primeiro porque o titulo muda de idioma; se falhar,
    tenta o titulo, que e o mesmo em todos os idiomas por ser marca
    registrada.
    """
    if not disponivel():
        return None
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(CLASSE_JOGO, None)
    if not hwnd:
        hwnd = u.FindWindowW(None, TITULO_JOGO)
    if not hwnd or not u.IsWindowVisible(hwnd):
        return None
    return int(hwnd)


def client_rect_on_screen(hwnd: int) -> Rect | None:
    """A area desenhavel do jogo, em coordenadas de tela."""
    if not disponivel():
        return None
    u = ctypes.windll.user32
    r = wintypes.RECT()
    if not u.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(r)):
        return None
    p = wintypes.POINT(r.left, r.top)
    if not u.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(p)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None
    return Rect(float(p.x), float(p.y), float(w), float(h))


def pertence_a_revisao(frente: int, jogo: int, *nossas: int) -> bool:
    """A janela ativa faz parte da revisao?

    Esta funcao e pequena demais para merecer existir — e existe mesmo assim,
    porque o bug que ela impede e invisivel e caro.

    A versao anterior perguntava "o jogo e a janela ativa?". Acontece que a
    JANELA DO OVERLAY tambem pode estar em primeiro plano; quando estava, a
    resposta era "nao", e o laco tirava o overlay da tela. Ele se escondia por
    estar aparecendo. O sintoma era um lampejo e nada depois, sem uma linha de
    log que explicasse.

    A pergunta certa nao e "o jogo esta na frente" e sim "a janela ativa
    pertence a esta revisao" — e uma delas e a nossa.
    """
    return frente == jogo or frente in nossas


def is_foreground(hwnd: int, *tambem_vale: int) -> bool:
    """A pessoa esta olhando para o jogo (ou para o nosso overlay)?

    O overlay some quando ela nao esta — senao fica flutuando por cima do
    navegador enquanto ela le o relatorio, e um overlay que aparece onde nao
    deveria e pior que nenhum overlay.
    """
    if not disponivel():
        return False
    frente = int(ctypes.windll.user32.GetForegroundWindow())
    return pertence_a_revisao(frente, hwnd, *tambem_vale)


# Argumentos de ShowWindow. `SHOWNOACTIVATE` e o ponto: mostrar SEM roubar o
# foco de quem estiver usando. O `deiconify` do tkinter nao tem esse pudor —
# ele ativa a janela, e ativar a nossa tira o jogo da frente.
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4


def show_no_activate(hwnd: int) -> None:
    if disponivel():
        ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)


def hide(hwnd: int) -> None:
    if disponivel():
        ctypes.windll.user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)


def make_click_through(hwnd: int) -> None:
    """Deixa a janela invisivel ao mouse e ao Alt+Tab.

    Sem `WS_EX_TRANSPARENT`, a parte OPACA do overlay engoliria cliques — e um
    cartao no meio da tela bloquearia justamente a area onde a pessoa clica
    para controlar o replay.
    """
    set_click_through(hwnd, True)


def set_click_through(hwnd: int, atravessa: bool) -> None:
    """Liga e desliga a transparencia ao mouse.

    DESLIGAR e o que permite desenhar: para receber o arrasto do ponteiro a
    janela precisa deixar de ser atravessavel e passar a poder ganhar foco.
    Fora do modo desenho ela volta a atravessar, porque um overlay que come
    cliques e pior que nenhum overlay — ele bloquearia justamente a barra onde
    se controla o replay.

    `WS_EX_NOACTIVATE` sai junto com `WS_EX_TRANSPARENT`: sem poder ativar, a
    janela nao recebe teclado, e o modo desenho precisa das teclas de cor.
    """
    if not disponivel():
        return
    u = ctypes.windll.user32
    u.SetWindowLongW.restype = ctypes.c_long
    atual = int(u.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE))
    passa_clique = WS_EX_TRANSPARENT | WS_EX_NOACTIVATE
    novo = (
        atual | WS_EX_LAYERED | WS_EX_TOOLWINDOW | passa_clique
        if atravessa
        else (atual | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~passa_clique
    )
    u.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE, novo)
