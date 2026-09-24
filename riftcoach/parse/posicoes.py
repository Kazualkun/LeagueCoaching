"""Onde cada jogador estava — e se estava vivo — em qualquer instante.

A Riot entrega a posicao de cada jogador UMA VEZ POR MINUTO. O codigo antigo
pegava o frame mais proximo e tratava como "onde voce estava": um objetivo aos
8:17 usava a posicao das 8:00, um aos 8:36 a das 9:00. Em trinta segundos um
campeao atravessa meio mapa, e o relatorio dizia "voce estava longe para
contestar" do Barao em que o jogador tinha ASSISTENCIA.

Entre um frame e outro, porem, a timeline e cheia de ancoras exatas:

  - cada abate traz a posicao, o assassino, as assistencias e a lista de
    TODO MUNDO que causou ou levou dano na luta (`victimDamageReceived` e
    `victimDamageDealt`) — todos eles estavam ali;
  - objetivo, torre e placa trazem posicao e quem matou/assistiu;
  - compra, venda e desfazer so acontecem na LOJA, que fica na base;
  - morte tira o jogador do mapa ate o renascimento, e o tempo de
    renascimento sai do nivel (eventos LEVEL_UP) e do minuto da partida.

Cada ancora carrega um ERRO em unidades: zero para a vitima de um abate,
algumas centenas para quem matou, mais para quem so deu assistencia. Entre
duas ancoras a posicao e interpolada, e o erro cresce com o tempo que passou
desde a ancora mais proxima — ninguem anda mais rapido que ~380 u/s. Quem usa
o numero recebe o erro junto e decide se ele basta para afirmar alguma coisa.

Continua sendo T2, derivado. So que agora com a premissa declarada em
unidades, e nao escondida num "frame mais proximo".
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Any

# Velocidade tipica de deslocamento, com botas, em unidades por segundo. E o
# que transforma "tempo desde a ultima ancora" em "raio de duvida". Recall e
# Teleporte quebram este modelo (saltam o mapa); as compras na base e as
# ancoras de luta corrigem isso logo depois.
VELOCIDADE_U_S = 380.0

# Onde se renasce e se compra. Medido no frame 0 de 20 partidas (todo mundo
# nasce ali); o raio cobre a plataforma e a area da loja.
FONTE = {100: (400.0, 420.0), 200: (14340.0, 14390.0)}
ERRO_FONTE_U = 900.0

# Erro de cada tipo de ancora, em unidades.
ERRO_VITIMA = 0.0
ERRO_ASSASSINO = 700.0
ERRO_ASSISTENCIA = 1500.0
ERRO_NA_LUTA = 1600.0  # causou ou levou dano do abatido
ERRO_OBJETIVO = 700.0  # quem deu o golpe final em monstro/estrutura
ERRO_RENASCEU = 300.0

# O tempo de morte estimado erra ~15% para cada lado numa morte isolada
# (conferido contra `totalTimeSpentDead` de 20 partidas: razao real/estimado
# mediana 1,02, p10 0,85, p90 1,13). Depois do renascimento estimado existe
# uma faixa em que o jogador PODE ainda estar morto — e enquanto esta morto o
# frame da Riot guarda a posicao do CADAVER, nao a da fonte.
FOLGA_DA_MORTE = 0.2
# Frame a menos disto do lugar da morte, dentro da folga, e o cadaver.
RAIO_DO_CADAVER_U = 600.0

# Acima disto a estimativa nao sustenta afirmacao nenhuma de lugar.
ERRO_MAXIMO_UTIL = 3500.0

# Tempo base de renascimento por nivel (BRW), em segundos, niveis 1..18.
BRW_S = (6, 6, 8, 8, 10, 12, 16, 21, 26, 32.5, 35, 37.5, 40, 42.5, 45, 47.5, 50, 52.5)


def fator_de_tempo(t_ms: int) -> float:
    """O multiplicador que alonga o tempo de morte com a partida (TIF).

    0 ate os 15 min; depois cresce por meio-minuto: 0,425% ate os 30, 0,30%
    ate os 45 e 1,45% ate os 55, com teto de 50%.
    """
    minutos = t_ms / 60_000
    if minutos < 15:
        return 0.0
    if minutos < 30:
        return math.ceil(2 * (minutos - 15)) * 0.00425
    if minutos < 45:
        return 0.1275 + math.ceil(2 * (minutos - 30)) * 0.0030
    return min(0.50, 0.2175 + math.ceil(2 * (minutos - 45)) * 0.0145)


def tempo_de_morte_s(nivel: int, t_ms: int) -> float:
    brw = BRW_S[min(len(BRW_S), max(1, nivel)) - 1]
    return brw * (1.0 + fator_de_tempo(t_ms))


@dataclass(frozen=True)
class Ancora:
    t_ms: int
    x: float
    y: float
    erro_u: float
    fonte: str


@dataclass(frozen=True)
class Estimativa:
    """Onde o jogador estava, com o raio de duvida em unidades de mundo."""

    x: float
    y: float
    erro_u: float
    fonte: str
    morto: bool = False
    renasce_em_s: float | None = None

    @property
    def confiavel(self) -> bool:
        return not self.morto and self.erro_u <= ERRO_MAXIMO_UTIL

    def distancia(self, x: float, y: float) -> float:
        return math.hypot(self.x - x, self.y - y)


def _pos(e: dict[str, Any]) -> tuple[float, float] | None:
    p = e.get("position")
    if not isinstance(p, dict) or "x" not in p or "y" not in p:
        return None
    return float(p["x"]), float(p["y"])


def _pid(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 10 else None


class Posicoes:
    """Consulta de posicao e de vida para os dez jogadores de uma partida."""

    def __init__(self, timeline: dict[str, Any], times: dict[int, int]) -> None:
        self.times = times
        frames: list[dict[str, Any]] = timeline.get("info", {}).get("frames", [])
        eventos = sorted(
            (e for f in frames for e in f.get("events", [])),
            key=lambda e: int(e.get("timestamp", 0)),
        )
        self._ancoras: dict[int, list[Ancora]] = {pid: [] for pid in times}
        self._subidas: dict[int, list[int]] = {pid: [] for pid in times}
        self._mortes: dict[int, list[tuple[int, int]]] = {pid: [] for pid in times}

        for f in frames:
            ts = int(f.get("timestamp", 0))
            for chave, pf in f.get("participantFrames", {}).items():
                pid = _pid(chave)
                pos = pf.get("position")
                if pid in self._ancoras and isinstance(pos, dict):
                    self._ancoras[pid].append(
                        Ancora(ts, float(pos["x"]), float(pos["y"]), 0.0, "frame")
                    )

        for e in eventos:
            if e.get("type") == "LEVEL_UP" and (pid := _pid(e.get("participantId"))):
                self._subidas.setdefault(pid, []).append(int(e["timestamp"]))
        for subidas in self._subidas.values():
            subidas.sort()

        self._folgas: dict[int, list[tuple[int, int, float, float]]] = {pid: [] for pid in times}
        for e in eventos:
            self._ler(e)

        for dono, ancoras in self._ancoras.items():
            ancoras.sort(key=lambda a: a.t_ms)
            self._ancoras[dono] = [a for a in ancoras if not self._duvidosa(dono, a)]
        self._t_ancoras = {dono: [a.t_ms for a in lst] for dono, lst in self._ancoras.items()}

    def _duvidosa(self, pid: int, a: Ancora) -> bool:
        """Ancora que pode ter sido registrada com o jogador ainda morto.

        Na folga logo depois do renascimento estimado: um frame colado no
        lugar da morte e o cadaver, e uma compra pode ter sido feita morto
        (nao diz onde ele esta). As duas mentiriam sobre a posicao.
        """
        if a.fonte not in ("frame", "loja"):
            return False
        for ini, fim_folga, mx, my in self._folgas.get(pid, []):
            if ini < a.t_ms < fim_folga:
                if a.fonte == "loja":
                    return True
                if math.hypot(a.x - mx, a.y - my) <= RAIO_DO_CADAVER_U:
                    return True
        return False

    # ------------------------------------------------------------------

    def _anc(
        self, pid: int | None, t: int, p: tuple[float, float], erro: float, fonte: str
    ) -> None:
        if pid in self._ancoras:
            self._ancoras[pid].append(Ancora(t, p[0], p[1], erro, fonte))

    def _ler(self, e: dict[str, Any]) -> None:
        tipo = e.get("type")
        t = int(e.get("timestamp", 0))
        p = _pos(e)
        if tipo == "CHAMPION_KILL" and p is not None:
            vitima = _pid(e.get("victimId"))
            self._anc(vitima, t, p, ERRO_VITIMA, "morte")
            self._anc(_pid(e.get("killerId")), t, p, ERRO_ASSASSINO, "abate")
            for a in e.get("assistingParticipantIds", []) or []:
                self._anc(_pid(a), t, p, ERRO_ASSISTENCIA, "assistencia")
            vistos = {vitima, _pid(e.get("killerId"))}
            for lista in ("victimDamageReceived", "victimDamageDealt"):
                for d in e.get(lista, []) or []:
                    pid = _pid(d.get("participantId"))
                    if pid is not None and pid not in vistos:
                        vistos.add(pid)
                        self._anc(pid, t, p, ERRO_NA_LUTA, "luta")
            if vitima is not None:
                nivel = self.nivel(vitima, t)
                duracao_s = tempo_de_morte_s(nivel, t)
                volta = t + round(1000 * duracao_s)
                self._mortes.setdefault(vitima, []).append((t, volta))
                folga = round(1000 * duracao_s * FOLGA_DA_MORTE)
                self._folgas.setdefault(vitima, []).append((t, volta + folga, p[0], p[1]))
                time = self.times.get(vitima)
                if time in FONTE:
                    # A duvida sobre QUANDO renasceu vira duvida sobre ONDE esta.
                    erro = ERRO_RENASCEU + VELOCIDADE_U_S * duracao_s * FOLGA_DA_MORTE
                    self._anc(vitima, volta, FONTE[time], erro, "renasceu")
        elif tipo in ("ELITE_MONSTER_KILL", "BUILDING_KILL", "TURRET_PLATE_DESTROYED") and p:
            self._anc(_pid(e.get("killerId")), t, p, ERRO_OBJETIVO, "objetivo")
            for a in e.get("assistingParticipantIds", []) or []:
                self._anc(_pid(a), t, p, ERRO_ASSISTENCIA, "assistencia")
        elif tipo in ("ITEM_PURCHASED", "ITEM_SOLD", "ITEM_UNDO") and t > 0:
            pid = _pid(e.get("participantId"))
            time = self.times.get(pid or 0)
            if pid is not None and time in FONTE:
                self._anc(pid, t, FONTE[time], ERRO_FONTE_U, "loja")

    # ------------------------------------------------------------------
    # Consultas
    # ------------------------------------------------------------------

    def nivel(self, pid: int, t_ms: int) -> int:
        return 1 + bisect.bisect_right(self._subidas.get(pid, []), t_ms)

    def mortes(self, pid: int) -> list[tuple[int, int]]:
        """(instante da morte, instante estimado do renascimento)."""
        return list(self._mortes.get(pid, []))

    def morte_em(self, pid: int, t_ms: int) -> tuple[int, int] | None:
        for ini, fim in self._mortes.get(pid, []):
            if ini <= t_ms < fim:
                return ini, fim
        return None

    def vivo(self, pid: int, t_ms: int) -> bool:
        return self.morte_em(pid, t_ms) is None

    def vivos(self, time: int, t_ms: int) -> list[int]:
        return [pid for pid, tm in self.times.items() if tm == time and self.vivo(pid, t_ms)]

    def onde(self, pid: int, t_ms: int) -> Estimativa | None:
        """A melhor estimativa de posicao, ou None sem ancora nenhuma."""
        janela = self.morte_em(pid, t_ms)
        time = self.times.get(pid)
        if janela is not None:
            fx, fy = FONTE.get(time or 0, (0.0, 0.0))
            return Estimativa(
                fx,
                fy,
                0.0,
                "morto",
                morto=True,
                renasce_em_s=round((janela[1] - t_ms) / 1000, 1),
            )
        ancoras = self._ancoras.get(pid, [])
        if not ancoras:
            return None
        tempos = self._t_ancoras[pid]
        i = bisect.bisect_right(tempos, t_ms)
        antes = self._valida(pid, ancoras, i - 1, -1, t_ms)
        depois = self._valida(pid, ancoras, i, +1, t_ms)
        if antes is None and depois is None:
            return None
        if antes is None or depois is None:
            a = antes or depois
            assert a is not None
            erro = a.erro_u + VELOCIDADE_U_S * abs(t_ms - a.t_ms) / 1000
            return Estimativa(a.x, a.y, erro, a.fonte)
        if depois.t_ms == antes.t_ms:
            melhor = min(antes, depois, key=lambda a: a.erro_u)
            return Estimativa(melhor.x, melhor.y, melhor.erro_u, melhor.fonte)
        f = (t_ms - antes.t_ms) / (depois.t_ms - antes.t_ms)
        x = antes.x + f * (depois.x - antes.x)
        y = antes.y + f * (depois.y - antes.y)
        erro_a = antes.erro_u + VELOCIDADE_U_S * (t_ms - antes.t_ms) / 1000
        erro_d = depois.erro_u + VELOCIDADE_U_S * (depois.t_ms - t_ms) / 1000
        perto = antes if erro_a <= erro_d else depois
        return Estimativa(x, y, min(erro_a, erro_d), perto.fonte)

    def _valida(
        self, pid: int, ancoras: list[Ancora], i: int, passo: int, t_ms: int
    ) -> Ancora | None:
        """A ancora mais proxima na direcao dada que NAO atravessa uma morte.

        Posicao de antes de morrer nao diz nada sobre depois do renascimento —
        o jogador foi parar na fonte. E ancora que cai DENTRO de uma janela de
        morte (compra feita morto) nao e posicao de mapa: pula-se para a
        seguinte. A propria ancora da morte vale para o lado de antes: e onde
        o jogador foi parar andando.
        """
        mortes = self._mortes.get(pid, [])
        while 0 <= i < len(ancoras):
            a = ancoras[i]
            if any(ini < a.t_ms < fim for ini, fim in mortes):
                i += passo
                continue
            if passo < 0:
                cruza = any(a.t_ms <= ini < t_ms for ini, _ in mortes)
            else:
                cruza = any(t_ms < ini < a.t_ms for ini, _ in mortes)
            return None if cruza else a
        return None

    def visto_por_ultimo(self, pid: int, t_ms: int, erro_max: float = 1000.0) -> Ancora | None:
        """A ancora precisa (frame, abate, luta...) mais recente antes de t.

        Quando a estimativa no instante e larga demais para afirmar distancia,
        "visto por ultimo na rota de baixo, 20 s antes" ainda e informacao
        verdadeira — e ajuda a pessoa a achar o momento no replay.
        """
        ancoras = self._ancoras.get(pid, [])
        i = bisect.bisect_right(self._t_ancoras.get(pid, []), t_ms) - 1
        while i >= 0:
            a = ancoras[i]
            if a.erro_u <= erro_max and a.fonte not in ("loja", "renasceu"):
                return a
            i -= 1
        return None

    def participou(self, pid: int, evento: dict[str, Any]) -> bool:
        """Deu o golpe final ou tem assistencia no evento."""
        if _pid(evento.get("killerId")) == pid:
            return True
        return pid in {_pid(a) for a in evento.get("assistingParticipantIds", []) or []}
