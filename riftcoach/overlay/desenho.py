"""O pincel: desenhar por cima do replay, como um treinador na lousa.

A LOGICA MORA AQUI, LONGE DO MOUSE. Este modulo nao conhece tkinter nem
Windows — ele recebe "o ponteiro foi para (x, y)" e "o botao soltou", e
devolve tracos. Assim da para testar o comportamento inteiro do pincel sem
abrir janela nenhuma, que e a parte que costuma nao ter teste.

TRES DECISOES QUE PARECEM DETALHE E NAO SAO:

1. OS PONTOS SAO FRACAO DA TELA. Quem desenha em 1600x900 e reabre o replay
   em 2560x1440 veria o traco encolhido num canto. Em fracao ele acompanha
   qualquer resolucao, e continua apontando para o que apontava.

2. O REPLAY PAUSA AO ENTRAR NO MODO DESENHO. Desenhar sobre imagem em
   movimento e desenhar no lugar errado: quando voce termina o circulo, o
   campeao ja saiu de dentro dele.

3. PONTOS SAO DILUIDOS NA HORA. O mouse gera dezenas de eventos por segundo e
   quase todos caem em cima do anterior. Guardar todos engorda a revisao em
   disco sem mudar um pixel do que se ve.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riftcoach.core.schema import Stroke

# Paleta do pincel. Poucas cores, e cada uma com um uso obvio, porque um
# seletor com vinte tons vira escolha de cor em vez de analise de partida.
CORES: tuple[tuple[str, str], ...] = (
    ("#ff453a", "vermelho — o erro"),
    ("#ffd60a", "amarelo — atencao"),
    ("#30d158", "verde — o que era para fazer"),
    ("#0a84ff", "azul — rota e movimentacao"),
    ("#ffffff", "branco — anotacao livre"),
)

ESPESSURAS: tuple[float, ...] = (2.0, 4.0, 7.0)

# Distancia minima, em fracao da tela, entre dois pontos guardados. Abaixo
# disso o ponto nao muda nada do que se ve e so ocupa disco. 0,004 da cerca de
# 6 px numa tela de 1600 de largura.
PASSO_MINIMO = 0.004


@dataclass
class Prancheta:
    """O estado do pincel enquanto a pessoa desenha.

    Nao guarda os tracos terminados — eles vao para a `ReviewSession`, que e
    quem sabe gravar. Esta classe cuida so do traco em andamento e das
    escolhas de cor e espessura.
    """

    cor: str = CORES[0][0]
    espessura: float = ESPESSURAS[1]
    ativo: bool = False
    _atual: list[tuple[float, float]] = field(default_factory=list)

    # ----------------------------------------------------------------
    # O traco em andamento
    # ----------------------------------------------------------------

    def comecar(self, x: float, y: float) -> None:
        self._atual = [_preso(x, y)]

    def mover(self, x: float, y: float) -> bool:
        """Acrescenta um ponto. Devolve True se ele mudou alguma coisa.

        Diluir aqui, e nao ao gravar, mantem a lista curta o tempo todo: um
        traco de tres segundos com 60 eventos por segundo viraria 180 pontos,
        e vira uns 25.
        """
        if not self._atual:
            return False
        p = _preso(x, y)
        ultimo = self._atual[-1]
        if abs(p[0] - ultimo[0]) + abs(p[1] - ultimo[1]) < PASSO_MINIMO:
            return False
        self._atual.append(p)
        return True

    def terminar(self, t_ms: int) -> Stroke | None:
        """Fecha o traco e devolve. None quando nao houve traco de verdade.

        Um clique sem arrastar produz um ponto so, e um ponto nao e um traco —
        devolver isso encheria a revisao de rabiscos invisiveis a cada clique
        errado.
        """
        pontos, self._atual = self._atual, []
        if len(pontos) < 2:
            return None
        return Stroke(t_ms=max(0, t_ms), points=pontos, color=self.cor, width=self.espessura)

    @property
    def em_andamento(self) -> list[tuple[float, float]]:
        return list(self._atual)

    def descartar(self) -> None:
        self._atual = []

    # ----------------------------------------------------------------
    # Escolhas
    # ----------------------------------------------------------------

    def usar_cor(self, indice: int) -> bool:
        if 0 <= indice < len(CORES):
            self.cor = CORES[indice][0]
            return True
        return False

    def proxima_espessura(self) -> float:
        i = ESPESSURAS.index(self.espessura) if self.espessura in ESPESSURAS else 0
        self.espessura = ESPESSURAS[(i + 1) % len(ESPESSURAS)]
        return self.espessura


def _preso(x: float, y: float) -> tuple[float, float]:
    """Prende em [0,1].

    O ponteiro sai da janela — arrastando para fora da tela, por exemplo — e
    o evento ainda chega com coordenada negativa. Guardar isso quebraria a
    validacao do `Stroke` na hora de gravar, ou seja, a pessoa perderia o
    desenho por ter passado do canto.
    """
    return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))


def apagar_ultimo(strokes: list[Stroke], t_ms: int, janela_ms: int = 6_000) -> Stroke | None:
    """Tira o traco mais recente DESTE instante. Devolve o que saiu.

    Restrito a janela de propósito: desfazer nao pode alcancar um desenho
    feito noutro momento da partida, que a pessoa nem esta vendo.
    """
    candidatos = [s for s in strokes if abs(s.t_ms - t_ms) <= janela_ms]
    if not candidatos:
        return None
    # O mais recente e o ultimo da lista original, nao o de maior `t_ms`:
    # tracos do mesmo instante compartilham o timestamp.
    for s in reversed(strokes):
        if s in candidatos:
            strokes.remove(s)
            return s
    return None


def limpar_instante(strokes: list[Stroke], t_ms: int, janela_ms: int = 6_000) -> int:
    """Apaga tudo o que pertence a este instante. Devolve quantos sairam."""
    restantes = [s for s in strokes if abs(s.t_ms - t_ms) > janela_ms]
    saidos = len(strokes) - len(restantes)
    strokes[:] = restantes
    return saidos
