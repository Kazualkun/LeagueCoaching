"""L3 — benchmarks: o que transforma uma observacao em uma meta treinavel.

"Seu CS estava baixo" nao e coaching. "61 CS@10 te coloca no percentil 28 para
meio Esmeralda, onde a mediana e 68" e — porque diz o quanto, comparado a quem,
e quanto falta.

TRES FONTES, EM ORDEM DE PREFERENCIA (ver docs/04-knowledge-base.md, L3):

  1. PARQUET      knowledge/benchmarks/{patch}.parquet, gerado pelo job do
                  mantenedor amostrando partidas reais pela API da Riot. So
                  agregados: sem PUUID, sem linha por jogador.
  2. HISTORICO    percentis calculados sobre as ultimas partidas do PROPRIO
                  usuario. Cobre rotas fora do meta e funciona no dia 1.
  3. MODELO       a tabela parametrica embarcada aqui embaixo.

A fonte usada SEMPRE viaja junto do resultado, e o relatorio a imprime. Um
percentil vindo do modelo embarcado e um chute educado; um vindo do parquet e
uma medicao. Apagar essa diferenca seria exatamente o tipo de precisao falsa que
o resto do projeto existe para evitar.

NAO RASPE op.gg/u.gg. Viola os termos, quebra sozinho e e desnecessario.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Literal

from riftcoach.config import data_dir
from riftcoach.parse.facts import MatchFacts

Metric = Literal[
    "cs_at_10",
    "gd_at_10",
    "dpm",
    "vision_per_min",
    "deaths",
    "time_dead_pct",
]

Source = Literal["parquet", "history", "model"]

# Tiers da league-v4. MASTER+ colapsa os tres ultimos: a amostra la e pequena
# demais para sustentar percentis separados, e a diferenca entre eles nao muda
# nenhuma recomendacao de coaching.
TIERS = (
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
    "MASTER+",
)
DEFAULT_TIER = "EMERALD"

ROLES = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")

# Amostra minima para os percentis do proprio historico valerem alguma coisa.
# Abaixo disso, um unico jogo ruim move o percentil em dezenas de pontos e a
# comparacao engana mais do que informa.
MIN_HISTORY = 10

# Partidas mais curtas que isto nao produzem NENHUMA metrica comparavel. No SR
# uma partida que acaba antes dos 8 minutos e, na pratica, sempre um remake:
# ninguem jogou o suficiente para ter um numero que descreva o jogo.
MIN_MATCH_MINUTES = 8.0

# Metricas ancoradas no minuto 10 (cs@10, gd@10) so existem se a partida
# chegou la. Comparar "0 CS aos 10" numa partida de 1:10 nao e um percentil
# baixo — e uma medicao que nunca aconteceu.
LANE_PHASE_MINUTES = 10.0

METRIC_LABEL: dict[Metric, str] = {
    "cs_at_10": "CS@10",
    "gd_at_10": "diferenca de ouro @10",
    "dpm": "dano por minuto",
    "vision_per_min": "visao por minuto",
    "deaths": "mortes",
    "time_dead_pct": "% do tempo morto",
}

# Percentis abaixo/acima destes viram finding. O intervalo do meio e "normal
# para o seu elo" e nao merece uma linha no relatorio — um coach que aponta
# trinta coisas nao e lido ate o fim.
WEAK_PERCENTILE = 30.0
STRONG_PERCENTILE = 75.0


@dataclass(frozen=True)
class Distribution:
    """Como uma metrica se distribui num (rota, tier).

    `log_scale` separa duas familias com formatos diferentes:

      multiplicativa  CS, dano, visao — sempre positivas, com cauda longa a
                      direita. Normal no log descreve isso bem.
      aditiva         diferenca de ouro @10 — simetrica em torno de zero por
                      construcao (a sua vantagem e a desvantagem do outro).
                      Log seria matematicamente invalido aqui.
    """

    median: float
    spread: float  # desvio padrao, na escala indicada por log_scale
    log_scale: bool = True
    higher_is_better: bool = True

    def percentile_of(self, value: float) -> float:
        """Onde `value` cai nesta distribuicao, de 0 a 100.

        Sempre orientado a qualidade: 90 significa "melhor que 90% dos
        jogadores", inclusive em metricas onde menos e melhor (mortes). Sem
        essa normalizacao, todo consumidor precisaria lembrar o sentido de cada
        metrica — e um deles iria esquecer.
        """
        if self.log_scale:
            if value <= 0 or self.median <= 0:
                # Zero CS ou zero dano e o fundo da distribuicao, nao um erro.
                return 0.0 if self.higher_is_better else 100.0
            z = (math.log(value) - math.log(self.median)) / self.spread
        else:
            z = (value - self.median) / self.spread

        p = 100.0 * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        return p if self.higher_is_better else 100.0 - p

    def at_percentile(self, p: float) -> float:
        """O valor que fica no percentil `p`. Usado para dizer quanto falta."""
        q = p if self.higher_is_better else 100.0 - p
        q = min(max(q, 0.1), 99.9)
        z = _probit(q / 100.0)
        if self.log_scale:
            return math.exp(math.log(self.median) + z * self.spread)
        return self.median + z * self.spread


@dataclass(frozen=True)
class Benchmark:
    """Um valor do jogador, situado contra uma populacao."""

    metric: Metric
    value: float
    percentile: float
    median: float
    role: str
    tier: str
    source: Source
    sample: int | None = None  # tamanho da amostra, quando conhecido

    @property
    def is_weak(self) -> bool:
        return self.percentile < WEAK_PERCENTILE

    @property
    def is_strong(self) -> bool:
        return self.percentile >= STRONG_PERCENTILE

    @property
    def source_label(self) -> str:
        """O relatorio imprime isto. A fonte nao e detalhe: um percentil do
        modelo embarcado e um chute educado, um do parquet e uma medicao."""
        if self.source == "parquet":
            n = f", {self.sample} partidas" if self.sample else ""
            return f"amostra da Riot{n}"
        if self.source == "history":
            return f"seu proprio historico, {self.sample or 0} partidas"
        return "modelo embarcado (nao calibrado)"

    def render(self) -> str:
        v = f"{self.value:.1f}".rstrip("0").rstrip(".")
        m = f"{self.median:.1f}".rstrip("0").rstrip(".")
        return (
            f"{METRIC_LABEL[self.metric]}={v} "
            f"(p{self.percentile:.0f} para {self.role} {self.tier}, mediana {m}) "
            f"[{self.source_label}]"
        )


# --------------------------------------------------------------------------
# O modelo embarcado
# --------------------------------------------------------------------------
#
# AVISO, E ELE E O PONTO: os numeros abaixo NAO sao medidos. Sao estimativas
# ajustadas a mao, na mesma disciplina dos pesos de analysis/advantage.py —
# preferimos um modelo transparente e explicavel a um ajustado em dezesseis
# partidas, que daria overfit com cara de precisao.
#
# Eles existem para que o relatorio funcione no primeiro dia, sem download e
# sem historico. Assim que houver parquet para o patch, ele vence; assim que o
# usuario tiver MIN_HISTORY partidas em cache, o historico dele vence o modelo.
#
# A mediana e declarada no tier de referencia e deslocada por TIER_SHIFT.

REFERENCE_TIER = "EMERALD"

_BASE: dict[str, dict[Metric, Distribution]] = {
    "TOP": {
        "cs_at_10": Distribution(62, 0.20),
        "dpm": Distribution(560, 0.35),
        "vision_per_min": Distribution(0.70, 0.45),
        "deaths": Distribution(5.5, 0.45, higher_is_better=False),
        "time_dead_pct": Distribution(11.0, 0.50, higher_is_better=False),
    },
    "JUNGLE": {
        # CS da selva conta monstros; a escala e outra e a variancia e maior,
        # porque estilo de jungle (farm vs gank) move muito esse numero.
        "cs_at_10": Distribution(48, 0.26),
        "dpm": Distribution(500, 0.38),
        "vision_per_min": Distribution(0.95, 0.40),
        "deaths": Distribution(5.5, 0.45, higher_is_better=False),
        "time_dead_pct": Distribution(10.5, 0.50, higher_is_better=False),
    },
    "MIDDLE": {
        "cs_at_10": Distribution(68, 0.18),
        "dpm": Distribution(650, 0.33),
        "vision_per_min": Distribution(0.75, 0.42),
        "deaths": Distribution(5.5, 0.45, higher_is_better=False),
        "time_dead_pct": Distribution(11.0, 0.50, higher_is_better=False),
    },
    "BOTTOM": {
        "cs_at_10": Distribution(70, 0.18),
        "dpm": Distribution(700, 0.32),
        "vision_per_min": Distribution(0.65, 0.42),
        "deaths": Distribution(5.0, 0.45, higher_is_better=False),
        "time_dead_pct": Distribution(10.0, 0.50, higher_is_better=False),
    },
    "UTILITY": {
        # O suporte nao farma de proposito: CS baixo aqui e execucao correta,
        # nao erro. A metrica entra na tabela so para nao faltar, e as regras
        # (rules.py) nao geram finding de CS para esta rota.
        "cs_at_10": Distribution(14, 0.55),
        "dpm": Distribution(300, 0.45),
        "vision_per_min": Distribution(1.80, 0.32),
        "deaths": Distribution(6.5, 0.42, higher_is_better=False),
        "time_dead_pct": Distribution(12.0, 0.48, higher_is_better=False),
    },
}

# Diferenca de ouro @10 e a mesma para todas as rotas: por construcao ela e
# simetrica em zero, porque a sua vantagem e exatamente a desvantagem do
# oponente direto. O que muda por elo e a DISPERSAO, nao o centro — rotas de
# elo baixo abrem mais cedo e mais forte.
_GD10_SPREAD: dict[str, float] = {
    "IRON": 620,
    "BRONZE": 580,
    "SILVER": 540,
    "GOLD": 500,
    "PLATINUM": 470,
    "EMERALD": 450,
    "DIAMOND": 420,
    "MASTER+": 400,
}

# Deslocamento multiplicativo da mediana por tier, relativo a REFERENCE_TIER.
# Direcao: mais CS, mais dano e mais visao em elo alto; menos mortes e menos
# tempo morto. O sentido de "melhor" ja esta em higher_is_better, entao aqui o
# fator sempre empurra na direcao da metrica.
_TIER_SHIFT: dict[str, dict[str, float]] = {
    #            cs/dpm/visao   mortes/tempo_morto
    "IRON": {"up": 0.70, "down": 1.30},
    "BRONZE": {"up": 0.79, "down": 1.22},
    "SILVER": {"up": 0.86, "down": 1.15},
    "GOLD": {"up": 0.92, "down": 1.08},
    "PLATINUM": {"up": 0.97, "down": 1.03},
    "EMERALD": {"up": 1.00, "down": 1.00},
    "DIAMOND": {"up": 1.04, "down": 0.96},
    "MASTER+": {"up": 1.09, "down": 0.91},
}

_UP_METRICS: tuple[Metric, ...] = ("cs_at_10", "dpm", "vision_per_min")


def normalize_tier(tier: str | None) -> str:
    """league-v4 -> os tiers desta tabela. Desconhecido vira o padrao.

    Sem ranked na fila (normal, quickplay, ARAM) a API nao devolve tier
    nenhum, e isso e o caso comum, nao a excecao.
    """
    if not tier:
        return DEFAULT_TIER
    t = tier.strip().upper()
    if t in ("MASTER", "GRANDMASTER", "CHALLENGER"):
        return "MASTER+"
    return t if t in TIERS else DEFAULT_TIER


def normalize_role(role: str | None) -> str:
    r = (role or "").strip().upper()
    return r if r in ROLES else "MIDDLE"


def model_distribution(role: str, tier: str, metric: Metric) -> Distribution:
    """A distribuicao parametrica de (rota, tier, metrica)."""
    role = normalize_role(role)
    tier = normalize_tier(tier)

    if metric == "gd_at_10":
        return Distribution(
            median=0.0,
            spread=_GD10_SPREAD.get(tier, _GD10_SPREAD[DEFAULT_TIER]),
            log_scale=False,
            higher_is_better=True,
        )

    base = _BASE[role][metric]
    shift = _TIER_SHIFT[tier]["up" if metric in _UP_METRICS else "down"]
    return Distribution(
        median=base.median * shift,
        spread=base.spread,
        log_scale=base.log_scale,
        higher_is_better=base.higher_is_better,
    )


def _probit(p: float) -> float:
    """Inversa da normal padrao (Acklam). Usada so por `at_percentile`.

    Precisao de ~1e-9, muito alem do necessario aqui — mas escrever a inversa
    a mao evita arrastar scipy para dentro do caminho sem IA, que precisa
    continuar leve.
    """
    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    plow, phigh = 0.02425, 1 - 0.02425

    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
        * q
        / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    )


# --------------------------------------------------------------------------
# Extracao das metricas de um MatchFacts
# --------------------------------------------------------------------------


def metrics_of(facts: MatchFacts) -> dict[Metric, float]:
    """As metricas comparaveis de uma partida — so as que existem de verdade.

    Tres razoes para uma metrica NAO sair daqui, e todas produzem a mesma
    coisa quando ignoradas: um percentil confiante calculado sobre nada.

    1. A partida nao chegou no minuto 10. `cs_at_10` de um remake de 1:10 vale
       zero, e zero comparado com a mediana de 68 vira "percentil 0, gravidade
       4" — um relatorio que abre acusando o jogador de nao farmar numa
       partida que nunca comecou.
    2. A partida e curta demais para taxas. Dano por minuto e visao por minuto
       medidos em oito minutos sao dominados por ruido de abertura.
    3. O oponente direto nao foi resolvido (troca de rota): comparar a sua
       diferenca de ouro com a de ninguem nao significa nada.

    Ausente e honesto. Zero afirmaria um empate que nunca foi medido.
    """
    minutos = facts.duration_s / 60
    if minutos < MIN_MATCH_MINUTES:
        return {}

    out: dict[Metric, float] = {
        "dpm": facts.dpm,
        "vision_per_min": facts.vision.vision_per_min,
        "deaths": float(facts.focus.deaths),
        "time_dead_pct": facts.time_dead_pct,
    }
    if minutos >= LANE_PHASE_MINUTES:
        out["cs_at_10"] = float(facts.cs_at_10)
        if facts.gd_at_10 is not None:
            out["gd_at_10"] = float(facts.gd_at_10)
    return out


# --------------------------------------------------------------------------
# A tabela
# --------------------------------------------------------------------------


def benchmarks_dirs() -> list[Path]:
    """Onde procurar parquets, em ordem de precedencia.

    O diretorio do repositorio vence o do usuario: um parquet versionado veio
    de release revisada e e o mesmo para todo mundo; o baixado depois pode ser
    de qualquer origem. Quem instala pelo PyPI so tem o segundo.
    """
    return [
        Path(__file__).resolve().parents[2] / "knowledge" / "benchmarks",
        data_dir() / "benchmarks",
    ]


@dataclass
class _Empirical:
    """Percentis tabelados que vieram de uma amostra real."""

    points: list[tuple[float, float]]  # (percentil, valor), ordenado por valor
    median: float
    higher_is_better: bool
    sample: int

    def percentile_of(self, value: float) -> float:
        pts = self.points
        if value <= pts[0][1]:
            p = pts[0][0]
        elif value >= pts[-1][1]:
            p = pts[-1][0]
        else:
            p = pts[-1][0]
            for (p0, v0), (p1, v1) in pairwise(pts):
                if v0 <= value <= v1:
                    # Interpolacao linear entre dois pontos tabelados. Entre
                    # p50 e p75 a curva real nao e reta, mas o erro fica em
                    # poucos pontos percentuais e nao muda recomendacao.
                    frac = 0.0 if v1 == v0 else (value - v0) / (v1 - v0)
                    p = p0 + frac * (p1 - p0)
                    break
        return p if self.higher_is_better else 100.0 - p


class BenchmarkTable:
    """Situa uma metrica contra a melhor populacao disponivel.

    Ordem de preferencia por consulta, nao por tabela: uma metrica pode vir do
    parquet enquanto outra, ausente dele, cai no modelo. O `source` de cada
    `Benchmark` diz qual caminho foi usado naquela linha.
    """

    def __init__(
        self,
        patch: str | None = None,
        history: list[MatchFacts] | None = None,
        directory: Path | None = None,
    ) -> None:
        self.patch = patch
        self._dirs = [directory] if directory else benchmarks_dirs()
        self._parquet: dict[tuple[str, str, Metric], _Empirical] = {}
        self._history: dict[tuple[str, Metric], _Empirical] = {}
        if patch:
            self._load_parquet(patch)
        if history:
            self._load_history(history)

    # -- fonte 1: parquet --------------------------------------------------

    def _load_parquet(self, patch: str) -> None:
        """Carrega knowledge/benchmarks/{patch}.parquet, se existir.

        Falhar aqui nunca e fatal: a ausencia do arquivo e o caso normal (ele
        so existe depois que o job do mantenedor rodou para aquele patch), e o
        modelo embarcado cobre.
        """
        path = next(
            (d / f"{patch}.parquet" for d in self._dirs if (d / f"{patch}.parquet").exists()),
            None,
        )
        if path is None:
            return
        try:
            import pyarrow.parquet as pq  # instalado pelo extra 'knowledge'
        except ImportError:
            return

        try:
            table = pq.read_table(path).to_pydict()
        except Exception:
            return

        needed = {"role", "tier", "metric", "p10", "p25", "p50", "p75", "p90"}
        if not needed.issubset(table.keys()):
            return

        samples = table.get("sample", [0] * len(table["role"]))
        for i, role in enumerate(table["role"]):
            m = str(table["metric"][i])
            # A checagem e em runtime porque um parquet de origem desconhecida
            # pode trazer qualquer string nesta coluna. O mypy estreita `m` de
            # str para Metric por conta deste `in`, entao nao ha cast nem
            # ignore aqui: a verificacao real e a verificacao de tipo sao a
            # mesma linha.
            if m not in METRIC_LABEL:
                continue
            hib = model_distribution(str(role), str(table["tier"][i]), m).higher_is_better
            pts = [(float(p), float(table[f"p{p}"][i])) for p in (10, 25, 50, 75, 90)]
            self._parquet[(normalize_role(str(role)), normalize_tier(str(table["tier"][i])), m)] = (
                _Empirical(
                    points=pts,
                    median=float(table["p50"][i]),
                    higher_is_better=hib,
                    sample=int(samples[i] or 0),
                )
            )

    # -- fonte 2: o historico do proprio usuario ---------------------------

    def _load_history(self, history: list[MatchFacts]) -> None:
        """Percentis sobre as proprias partidas do usuario.

        Existe por dois motivos, e o segundo e o que importa: alem de funcionar
        no dia 1, ele cobre rotas e campeoes fora do meta que nenhuma tabela
        agregada descreve bem. Se voce e o unico Singed AP support do servidor,
        a sua distribuicao e a unica honesta.
        """
        por_rota: dict[str, list[dict[Metric, float]]] = {}
        for f in history:
            por_rota.setdefault(normalize_role(f.focus.position), []).append(metrics_of(f))

        for role, amostras in por_rota.items():
            if len(amostras) < MIN_HISTORY:
                continue
            for m in METRIC_LABEL:
                vals = sorted(a[m] for a in amostras if m in a)
                if len(vals) < MIN_HISTORY:
                    continue
                hib = model_distribution(role, DEFAULT_TIER, m).higher_is_better
                pts = [(float(p), _quantile(vals, p / 100.0)) for p in (10, 25, 50, 75, 90)]
                self._history[(role, m)] = _Empirical(
                    points=pts,
                    median=_quantile(vals, 0.5),
                    higher_is_better=hib,
                    sample=len(vals),
                )

    # -- consulta ----------------------------------------------------------

    def lookup(self, metric: Metric, value: float, role: str, tier: str) -> Benchmark:
        role = normalize_role(role)
        tier = normalize_tier(tier)

        emp = self._parquet.get((role, tier, metric))
        source: Source = "parquet"
        if emp is None:
            emp = self._history.get((role, metric))
            source = "history"

        if emp is not None:
            return Benchmark(
                metric=metric,
                value=value,
                percentile=emp.percentile_of(value),
                median=emp.median,
                role=role,
                tier=tier,
                source=source,
                sample=emp.sample,
            )

        dist = model_distribution(role, tier, metric)
        return Benchmark(
            metric=metric,
            value=value,
            percentile=dist.percentile_of(value),
            median=dist.median,
            role=role,
            tier=tier,
            source="model",
        )

    def evaluate(self, facts: MatchFacts, tier: str | None = None) -> list[Benchmark]:
        """Todas as metricas comparaveis de uma partida, ja situadas."""
        role = normalize_role(facts.focus.position)
        t = normalize_tier(tier)
        return [self.lookup(m, v, role, t) for m, v in sorted(metrics_of(facts).items())]


def _quantile(sorted_values: list[float], q: float) -> float:
    """Quantil com interpolacao linear. Lista JA ordenada."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (pos - lo) * (sorted_values[hi] - sorted_values[lo])


def summarize(marks: list[Benchmark]) -> str:
    """Bloco de benchmarks do relatorio L5, sem IA."""
    if not marks:
        return ""
    cabecalho = f"BENCHMARKS — {marks[0].role} {marks[0].tier}"
    linhas = [
        "",
        cabecalho,
        *(f"  {b.render()}" for b in sorted(marks, key=lambda b: b.percentile)),
    ]
    if all(b.source == "model" for b in marks):
        linhas.append(
            "  NOTA: sem parquet para este patch e sem historico suficiente — "
            "estes percentis vem do modelo embarcado, que NAO e calibrado."
        )
    return "\n".join(linhas)
