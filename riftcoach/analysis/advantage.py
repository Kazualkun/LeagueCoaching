"""Motor de vantagem: avaliacao da partida no estilo de uma engine de xadrez.

Uma engine de xadrez nao diz "voce jogou mal". Ela diz: a posicao valia +1.5,
voce jogou Cf3, a posicao passou a valer -0.8, logo esse lance custou 2.3. O
erro fica MEDIDO, nao opinado.

Trazemos a mesma disciplina para o LoL:

    avaliar(estado) -> vantagem em ouro-equivalente -> probabilidade de vitoria
    blunder         = queda de probabilidade de vitoria atribuivel ao jogador

Por que isso muda a arquitetura: sem o motor, `Finding.severity` seria um
palpite do modelo — e modelos sao pessimos em calibrar gravidade. Com o motor,
gravidade vira PERDA DE PROBABILIDADE DE VITORIA MEDIDA (T1/T2), e o modelo
fica so com o trabalho que ele faz bem: explicar por que aconteceu e o que
fazer diferente.

Duas regras de projeto:

1. A avaliacao NUNCA e um escalar sozinho. Uma engine de xadrez pode dizer so
   "+1.5"; um coach precisa dizer "+1.5 PORQUE voce esta 2k de ouro a frente e
   com dois dragoes". Toda avaliacao devolve a decomposicao em termos.
2. Os pesos ficam em constantes nomeadas e editaveis, nao espalhados pelo
   codigo. Sao heuristicos (ver CALIBRACAO abaixo) e precisam ser discutiveis.

CALIBRACAO: os pesos abaixo sao estimativas de ouro-equivalente baseadas no
valor de ouro direto de cada objetivo mais o controle de mapa que ele concede.
Eles NAO foram ajustados estatisticamente ainda — para isso e preciso uma
amostra grande de partidas com resultado conhecido, que e exatamente o job de
CI ja planejado para os benchmarks (docs/04-knowledge-base.md, L3). Enquanto
nao existe, preferimos um modelo transparente e explicavel a um ajustado em 16
partidas, que daria overfit com cara de precisao.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from riftcoach.parse.facts import MatchFacts

# --- pesos em ouro-equivalente -------------------------------------------
# Ouro e a unidade natural do LoL, como o peao e a do xadrez: tudo se converte
# nela. Cada valor inclui o ouro direto MAIS o controle de mapa concedido.
TOWER_VALUE = 1000.0
INHIBITOR_VALUE = 1600.0
DRAGON_VALUE = 650.0
SOUL_BONUS = 3000.0  # o 4o dragao vale muito mais que os tres primeiros
BARON_VALUE = 1800.0
BARON_DURATION_MS = 180_000  # o buff decai ate zero nesta janela
HERALD_VALUE = 450.0
GRUB_VALUE = 120.0
# Um nivel de vantagem de time vale aproximadamente isto em ouro-equivalente.
LEVEL_VALUE = 180.0

# --- probabilidade de vitoria --------------------------------------------
# A mesma vantagem de ouro pesa MENOS no late game: o ouro total no mapa cresce,
# entao 5k aos 15 min e uma fracao muito maior da partida do que 5k aos 40.
# A escala cresce com o tempo para refletir isso.
WP_SCALE_BASE = 3000.0
WP_SCALE_PER_MINUTE = 150.0

# Limiares de gravidade, em pontos percentuais de probabilidade de vitoria.
#
# CALIBRADOS contra a distribuicao real (tools/calibrate_severity.py, 1.013
# erros medidos em 160 jogadores). Mediana de 7 erros por jogador; percentis:
#
#     p50=5.8  p70=9.6  p80=11.8  p90=16.4  p95=19.1  p99=25.7  max=35.9
#
# Alvo de projeto: ~1,5 criticos (sev4+) por jogador por partida. Limiar alto
# demais e a marcacao nunca aparece e o jogador nao aprende nada; baixo demais
# e tudo fica vermelho e a marcacao perde o sentido. Os valores abaixo saem
# dos percentis p21/p53/p76/p97.
#
# Os limiares ANTERIORES (2/5/10/18/30) foram chutados e davam 0,5 criticos por
# jogador — metade das pessoas nunca veria um.
SEVERITY_THRESHOLDS = [(1.5, 1), (3.0, 2), (6.0, 3), (11.0, 4), (21.0, 5)]

# Ate esta distancia do objetivo, consideramos que o jogador estava NA luta.
# 2.500 unidades cobre o pit e a area em volta de onde da para contestar —
# mais que isso e outra parte do mapa.
NEAR_OBJECTIVE_UNITS = 2_500

Involvement = Literal["direct", "positional", "team"]


@dataclass(frozen=True)
class EvalTerm:
    """Uma parcela da avaliacao, com o numero cru que a originou.

    O numero cru e obrigatorio: sem ele a avaliacao vira um escalar magico e
    deixa de poder virar evidencia citavel.
    """

    name: str
    raw: float
    contribution: float

    def render(self) -> str:
        return f"{self.name}={self.raw:+.0f} ({self.contribution:+.0f}g)"


@dataclass(frozen=True)
class Evaluation:
    t_ms: int
    advantage: float  # ouro-equivalente, do ponto de vista do time em foco
    win_probability: float  # 0..1
    terms: list[EvalTerm] = field(default_factory=list)

    def explain(self) -> str:
        partes = " ".join(t.render() for t in self.terms if abs(t.contribution) >= 50)
        return f"vantagem={self.advantage:+.0f}g wp={100 * self.win_probability:.0f}% {partes}"


@dataclass(frozen=True)
class Blunder:
    """Um erro medido: quanto de probabilidade de vitoria custou.

    `involvement` separa o que o jogador FEZ do que aconteceu com ele:

      direct     — ele morreu, ou entregou o abate/objetivo
      positional — aconteceu longe dele, e ele estava no lado errado do mapa
      team       — ele estava presente e mesmo assim deu errado

    A distincao importa para o coaching: "voce morreu" e "seu time perdeu o
    barao enquanto voce empurrava a top" pedem correcoes completamente
    diferentes.
    """

    t_ms: int
    kind: str
    wp_before: float
    wp_after: float
    involvement: Involvement
    detail: str

    @property
    def wp_loss(self) -> float:
        """Em pontos percentuais. O equivalente ao centipawn loss."""
        return 100 * (self.wp_before - self.wp_after)

    @property
    def severity(self) -> int:
        """1..5, derivado da perda MEDIDA — nao de opiniao do modelo."""
        sev = 1
        for limiar, valor in SEVERITY_THRESHOLDS:
            if self.wp_loss >= limiar:
                sev = valor
        return sev

    @property
    def is_critical(self) -> bool:
        return self.severity >= 4


def win_probability(advantage: float, t_ms: int) -> float:
    minutos = t_ms / 60_000
    escala = WP_SCALE_BASE + WP_SCALE_PER_MINUTE * minutos
    return 1.0 / (1.0 + math.exp(-advantage / escala))


def _objective_state(facts: MatchFacts, t_ms: int) -> list[EvalTerm]:
    """Acumula os objetivos ja tomados ate t_ms, dos dois lados."""
    torres = inibidores = dragoes = arautos = grubs = 0
    barao_ms: int | None = None

    for o in facts.objectives:
        if o.t_ms > t_ms:
            break
        sinal = 1 if o.taken_by_focus_team else -1
        if o.kind == "DRAGON":
            dragoes += sinal
        elif o.kind == "BARON_NASHOR":
            barao_ms = o.t_ms if o.taken_by_focus_team else -o.t_ms
        elif o.kind == "RIFTHERALD":
            arautos += sinal
        elif o.kind == "HORDE":
            grubs += sinal
        elif (o.subtype or "") == "INHIBITOR_BUILDING":
            inibidores += sinal
        elif o.kind == "TOWER_BUILDING":
            torres += sinal

    termos = [
        EvalTerm("torres", torres, torres * TOWER_VALUE),
        EvalTerm("inibidores", inibidores, inibidores * INHIBITOR_VALUE),
        EvalTerm("dragoes", dragoes, dragoes * DRAGON_VALUE),
        EvalTerm("arauto", arautos, arautos * HERALD_VALUE),
        EvalTerm("grubs", grubs, grubs * GRUB_VALUE),
    ]

    # Alma do dragao: o 4o vale muito mais que os anteriores.
    if abs(dragoes) >= 4:
        lado = 1 if dragoes > 0 else -1
        termos.append(EvalTerm("alma", lado, lado * SOUL_BONUS))

    # Barao decai ao longo da duracao do buff, em vez de sumir de uma vez.
    if barao_ms is not None:
        idade = t_ms - abs(barao_ms)
        if 0 <= idade < BARON_DURATION_MS:
            restante = 1.0 - idade / BARON_DURATION_MS
            lado = 1 if barao_ms > 0 else -1
            termos.append(EvalTerm("barao", lado, lado * BARON_VALUE * restante))

    return termos


def evaluate(facts: MatchFacts, t_ms: int) -> Evaluation:
    """Avalia a partida no instante t_ms, do ponto de vista do time em foco."""
    idx = min(
        len(facts.team_gold_diff_series) - 1,
        max(0, round(t_ms / 60_000)),
    )
    ouro = float(facts.team_gold_diff_series[idx]) if facts.team_gold_diff_series else 0.0

    termos = [EvalTerm("ouro", ouro, ouro)]
    if facts.team_xp_diff_series:
        j = min(len(facts.team_xp_diff_series) - 1, idx)
        xp = float(facts.team_xp_diff_series[j])
        # XP vira ouro-equivalente por niveis: ~1000 de xp e cerca de um nivel
        # de time no meio da partida. Aproximacao grosseira e assumida.
        termos.append(EvalTerm("xp", xp, (xp / 1000.0) * LEVEL_VALUE))

    termos.extend(_objective_state(facts, t_ms))
    total = sum(t.contribution for t in termos)
    return Evaluation(
        t_ms=t_ms,
        advantage=total,
        win_probability=win_probability(total, t_ms),
        terms=termos,
    )


def advantage_series(facts: MatchFacts) -> list[Evaluation]:
    """Avaliacao a cada minuto. E a 'curva de avaliacao' do xadrez."""
    n = len(facts.team_gold_diff_series)
    return [evaluate(facts, i * 60_000) for i in range(n)]


def find_blunders(facts: MatchFacts, window_ms: int = 45_000) -> list[Blunder]:
    """Encontra os momentos que mais custaram probabilidade de vitoria.

    Avaliamos ANTES e DEPOIS de cada evento significativo. A janela e de 45s
    porque os frames sao de minuto em minuto: menos que isso e os dois lados da
    janela caem no mesmo frame e a diferenca da zero.

    Consequencia honesta: o instante exato do prejuizo tem resolucao de ~1
    minuto. Por isso todo blunder e T2, nunca T1 — sabemos QUE custou, com
    precisao de minuto, nao o segundo exato.
    """
    out: list[Blunder] = []

    def par(t_ms: int) -> tuple[Evaluation, Evaluation]:
        return (
            evaluate(facts, max(0, t_ms - window_ms)),
            evaluate(facts, t_ms + window_ms),
        )

    for d in facts.deaths:
        antes, depois = par(d.t_ms)
        detalhe = f"morreu em {d.zone} para {'+'.join(d.killers) or '?'} (swing {d.gold_swing}g)"
        if d.objective_window:
            detalhe += f", com {d.objective_window}"
        if d.gold_at_death >= 1000:
            detalhe += f", segurando {d.gold_at_death}g nao gastos"
        out.append(
            Blunder(
                t_ms=d.t_ms,
                kind="morte",
                wp_before=antes.win_probability,
                wp_after=depois.win_probability,
                involvement="direct",
                detail=detalhe,
            )
        )

    for o in facts.objectives:
        if o.taken_by_focus_team or o.kind not in (
            "DRAGON",
            "BARON_NASHOR",
            "RIFTHERALD",
        ):
            continue
        antes, depois = par(o.t_ms)
        # O jogador estava NA luta, ou do outro lado do mapa?
        #
        # A versao anterior olhava so o prefixo da zona: "OWN_" ou "BASE"
        # contava como longe, e qualquer outra coisa como presente. Isso
        # produziu, num relatorio real, "o time perdeu isto com voce presente"
        # para um Arauto tomado no pit do Barao enquanto o jogador estava em
        # NEUTRAL_MID_LANE — que nao e perto de nada.
        #
        # Zona nao responde a pergunta: NEUTRAL_MID_LANE nao diz se voce estava
        # a mil ou a nove mil unidades do pit. Distancia responde, e a
        # destilacao ja mede. Sem ela (posicao nao lida), a zona volta a ser o
        # criterio, porque e o unico que sobra.
        if o.focus_player_distance_u is not None:
            longe = o.focus_player_distance_u > NEAR_OBJECTIVE_UNITS
        else:
            longe = o.focus_player_zone.startswith("OWN_") or "BASE" in o.focus_player_zone
        out.append(
            Blunder(
                t_ms=o.t_ms,
                kind=f"perdeu {o.kind}",
                wp_before=antes.win_probability,
                wp_after=depois.win_probability,
                involvement="positional" if longe else "team",
                detail=(
                    f"{o.kind}{'/' + o.subtype if o.subtype else ''} para o inimigo"
                    f"; voce estava em {o.focus_player_zone}"
                    + (
                        f" ({o.focus_player_distance_u}u do objetivo)"
                        if o.focus_player_distance_u is not None
                        else ""
                    )
                    + ("" if o.wards_placed_60s_before else "; nenhuma ward sua nos 60s")
                ),
            )
        )

    # So interessa o que REALMENTE custou. Ganhos entram na curva, nao na lista
    # de erros — um coach que lista 40 "erros" nao e usado duas vezes.
    perdas = [b for b in out if b.wp_loss > 0]
    perdas.sort(key=lambda b: -b.wp_loss)
    return perdas


def summarize(facts: MatchFacts) -> str:
    """Resumo determinístico da curva — entra no relatorio L5, sem IA."""
    serie = advantage_series(facts)
    if not serie:
        return ""
    blunders = find_blunders(facts)
    criticos = [b for b in blunders if b.is_critical]
    pico = max(serie, key=lambda e: e.advantage)
    vale = min(serie, key=lambda e: e.advantage)

    linhas = [
        "",
        "VANTAGEM (probabilidade de vitoria por minuto, estilo engine de xadrez)",
        " ".join(f"{100 * e.win_probability:.0f}" for e in serie[::2]),
        f"melhor={100 * pico.win_probability:.0f}% aos {pico.t_ms // 60000}min"
        f" · pior={100 * vale.win_probability:.0f}% aos {vale.t_ms // 60000}min",
    ]
    if blunders:
        linhas.append("")
        linhas.append(
            f"ERROS POR CUSTO MEDIDO ({len(blunders)}, "
            f"{len(criticos)} criticos) — perda de prob. de vitoria"
        )
        for b in blunders[:8]:
            marca = "CRITICO" if b.is_critical else f"sev{b.severity}"
            linhas.append(
                f"{b.t_ms // 60000}:{(b.t_ms // 1000) % 60:02d} [{marca}] "
                f"-{b.wp_loss:.0f}pp {b.kind} ({b.involvement}) — {b.detail}"
            )
    return "\n".join(linhas)
