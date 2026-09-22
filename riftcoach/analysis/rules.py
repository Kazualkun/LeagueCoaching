"""O motor de regras deterministico — o relatorio L5, sem IA nenhuma.

Este modulo NAO e um detalhe secundario nem um premio de consolacao. Ele e:

  1. O relatorio que sai quando todos os provedores estao fora, toda cota
     queimada e nao ha GPU. Aproximadamente 40% do valor do produto ja esta
     aqui (docs/01-model-routing.md, escada de degradacao).
  2. A rede de seguranca que mantem o resto honesto. Se o LLM discorda do que
     esta medido aqui, o LLM esta errado.
  3. A fonte dos gatilhos deterministicos que vao guiar a recuperacao do RAG
     na etapa 3 (docs/04-knowledge-base.md, 4.4) — ver `TRIGGERS`.

REGRA DE PROJETO: nenhuma regra aqui opina. Cada `Finding` precisa citar o
numero que o originou, e a gravidade vem ou do motor de vantagem (perda MEDIDA
de probabilidade de vitoria) ou da distancia ate a mediana do elo. Se uma regra
nao consegue apontar o dado que a disparou, ela nao entra.

O outro lado dessa regra: e MELHOR apontar cinco coisas certas do que vinte
plausiveis. Um coach que lista quarenta erros nao e usado duas vezes. Os
limiares existem para calar a regra no caso normal.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise

from riftcoach.analysis.advantage import Blunder, find_blunders
from riftcoach.core.schema import (
    Category,
    Evidence,
    EvidenceTier,
    Finding,
    Phase,
)
from riftcoach.knowledge.benchmarks import (
    Benchmark,
    BenchmarkTable,
    Metric,
)
from riftcoach.parse.distill import mmss
from riftcoach.parse.facts import MatchFacts

# Fronteiras de fase, em minutos. Grosseiras de proposito: a transicao real
# depende da composicao e do estado do mapa, e fingir precisao aqui nao
# melhoraria nenhuma recomendacao.
EARLY_UNTIL_MIN = 14
MID_UNTIL_MIN = 25

# Sentinela de Controle. Id estavel ha muitos patches; se mudar, o
# FactValidator da etapa 2 pega, porque o nome nao resolveria.
CONTROL_WARD_ID = 2055

# --- limiares ------------------------------------------------------------
# Cada um destes existe para a regra FICAR CALADA no caso normal. Onde o
# numero e arbitrario, ele esta nomeado aqui em vez de enterrado no codigo,
# para poder ser discutido num PR.

GOLD_HOARD_AT_DEATH = 1000  # ouro nao gasto carregado na hora da morte
GOLD_HOARD_MIN_DEATHS = 2  # uma vez e azar; duas e habito
DEATH_CLUSTER_WINDOW_MS = 150_000  # 2min30 — duas mortes aqui dentro e padrao
DEATH_CLUSTER_MIN = 3  # mortes na mesma zona para virar finding
CONTROL_WARD_RATE = 0.4  # sentinelas por recall abaixo disso vira finding
MIN_RECALLS_FOR_WARD_RULE = 4  # com 2 recalls a taxa nao significa nada
BLUNDER_MIN_SEVERITY = 3  # abaixo disso o erro nao merece uma linha

# Gatilhos estruturados emitidos por este modulo. Sao o filtro RIGIDO que roda
# antes de qualquer busca semantica na etapa 3: dado o MatchFacts, sabemos
# exatamente o que aconteceu, entao nada precisa ser "buscado" (4.4).
#
# Contribuidores de knowledge/principles/ marcam os arquivos deles com estes
# nomes — ver CONTRIBUTING.md. Mudar um nome aqui quebra a recuperacao
# daqueles arquivos, entao trate esta lista como contrato publico.
TRIGGERS = frozenset(
    {
        "wave_proxy=PUSHING_TO_ENEMY",
        "wave_proxy=HOLDING_MID",
        "wave_proxy=HELD_IN_OWN_HALF",
        "recall_error",
        "objective_window",
        "death_cluster",
        "vision_gap",
        "gold_hoarding",
        "itemization_gap",
        "tempo_loss",
        "lane_deficit",
    }
)


def phase_of(t_ms: int) -> Phase:
    m = t_ms / 60_000
    if m <= EARLY_UNTIL_MIN:
        return "early"
    return "mid" if m <= MID_UNTIL_MIN else "late"


def _severity_from_percentile(p: float) -> int:
    """Gravidade de um finding de benchmark.

    Teto 4, nao 5: gravidade 5 significa "custou a partida", e isso e uma
    afirmacao sobre um MOMENTO. Um CS@10 ruim e uma tendencia — importa muito,
    mas nao ha um instante em que ela tenha custado o jogo. Gravidade 5 fica
    reservada ao motor de vantagem, que mede exatamente isso.
    """
    if p < 5:
        return 4
    if p < 15:
        return 3
    return 2


def _t1(statement: str, t_ms: int, source: str = "timeline") -> Evidence:
    return Evidence(
        tier=EvidenceTier.T1_MEASURED,
        timestamp_ms=t_ms,
        statement=statement,
        source=source,  # type: ignore[arg-type]
    )


def _t2(statement: str, t_ms: int, assumption: str, source: str = "timeline") -> Evidence:
    """T2 SEMPRE com premissa. O schema recusa o contrario, e e esse o ponto:
    sem a premissa o usuario nao consegue discordar de forma util."""
    return Evidence(
        tier=EvidenceTier.T2_DERIVED,
        timestamp_ms=t_ms,
        statement=statement,
        source=source,  # type: ignore[arg-type]
        assumption=assumption,
    )


def _benchmark_evidence(b: Benchmark) -> Evidence:
    """Um percentil e SEMPRE T2, inclusive vindo do parquet.

    O valor e medido, mas a comparacao carrega uma premissa — a de que aquela
    populacao e a populacao certa para voce. Ela nao e, exatamente: filas
    diferentes, campeoes diferentes e composicoes diferentes movem todas essas
    medianas. A premissa declarada diz de qual populacao estamos falando.
    """
    return Evidence(
        tier=EvidenceTier.T2_DERIVED,
        timestamp_ms=0,
        statement=b.render(),
        source="benchmark",
        assumption=(
            f"comparado com {b.role} {b.tier} a partir de {b.source_label}; "
            "campeao, fila e composicao nao entram nesse recorte"
        ),
    )


@dataclass
class RuleContext:
    """Tudo o que as regras podem olhar. Nada de rede, nada de modelo."""

    facts: MatchFacts
    benchmarks: list[Benchmark] = field(default_factory=list)
    blunders: list[Blunder] = field(default_factory=list)

    def bench(self, metric: Metric) -> Benchmark | None:
        return next((b for b in self.benchmarks if b.metric == metric), None)

    @property
    def role(self) -> str:
        return self.facts.focus.position or "MIDDLE"

    @property
    def minutes(self) -> float:
        return max(1.0, self.facts.duration_s / 60)


# --------------------------------------------------------------------------
# As regras
# --------------------------------------------------------------------------
#
# Cada uma recebe o contexto e devolve zero ou mais findings. Devolver zero e
# o resultado esperado na maior parte das partidas.


def rule_measured_blunders(ctx: RuleContext) -> list[Finding]:
    """Os erros que o motor de vantagem MEDIU, convertidos em findings.

    Esta e a regra mais forte do modulo, porque a gravidade nao e opinada: e
    perda de probabilidade de vitoria, do mesmo jeito que uma engine de xadrez
    mede um lance ruim em centipawns.
    """
    out: list[Finding] = []
    for b in ctx.blunders:
        if b.severity < BLUNDER_MIN_SEVERITY:
            continue

        categoria: Category = "positioning" if b.involvement == "positional" else "decision"
        if b.kind.startswith("perdeu"):
            categoria = "objective"

        evidencias = [
            _t1(f"{b.kind} em {mmss(b.t_ms)}: {b.detail}", b.t_ms),
            _t2(
                f"custou {b.wp_loss:.0f} pontos percentuais de probabilidade de vitoria "
                f"({100 * b.wp_before:.0f}% -> {100 * b.wp_after:.0f}%)",
                b.t_ms,
                assumption=(
                    "a avaliacao usa frames de minuto em minuto, entao o custo e "
                    "atribuido com resolucao de ~1 minuto, nao ao segundo exato"
                ),
            ),
        ]

        if b.involvement == "positional":
            fix = (
                "Voce estava do outro lado do mapa quando isso foi decidido. A correcao "
                "nao e lutar melhor — e estar la. Resolva a rota antes, nao depois."
            )
            drill = (
                "Nas proximas 3 partidas: quando o timer de um objetivo chegar em 60s, "
                "olhe onde voce esta. Se estiver no lado errado, o erro ja aconteceu."
            )
        elif b.kind == "morte":
            fix = (
                "Essa morte custou mais que o ouro dela. Antes de entrar nessa posicao "
                "de novo, pergunte o que voce ganharia se desse certo — se a resposta "
                "for 'um pouco de CS', a troca nunca valeu."
            )
            drill = (
                "Nas proximas 3 partidas, antes de cada morte que voce sentir vindo: "
                "diga em voz alta o que voce esta tentando ganhar."
            )
        else:
            fix = (
                "O time perdeu isto com voce presente. Reveja o inicio da luta: quem "
                "comecou, com que visao, e se havia como recusar."
            )
            drill = "Reveja este momento no replay e decida se dava para recusar."

        out.append(
            Finding(
                category=categoria,
                phase=phase_of(b.t_ms),
                severity=b.severity,
                timestamp_ms=b.t_ms,
                claim=f"{b.kind.capitalize()} em {mmss(b.t_ms)} custou {b.wp_loss:.0f}pp.",
                evidence=evidencias,
                fix=fix,
                drill=drill,
                # Alta, mas nunca 1.0: a resolucao de minuto do frame e um
                # limite real da telemetria, nao um detalhe de implementacao.
                confidence=0.85,
            )
        )
    return out


def rule_lane_economy(ctx: RuleContext) -> list[Finding]:
    """CS@10 e diferenca de ouro @10 contra a mediana do elo.

    Nao roda para UTILITY: o suporte nao farma de proposito, e apontar CS baixo
    para um suporte e o exemplo classico de metrica aplicada na rota errada.
    """
    if ctx.role == "UTILITY":
        return []

    out: list[Finding] = []
    cs = ctx.bench("cs_at_10")
    if cs is not None and cs.is_weak:
        falta = max(0.0, cs.median - cs.value)
        out.append(
            Finding(
                category="wave",
                phase="early",
                timestamp_ms=10 * 60_000,
                severity=_severity_from_percentile(cs.percentile),
                claim=(
                    f"{cs.value:.0f} CS aos 10 minutos — percentil {cs.percentile:.0f} "
                    f"para {cs.role} {cs.tier}."
                ),
                evidence=[
                    _t1(f"{ctx.facts.cs_at_10} CS aos 10:00", 10 * 60_000, source="match"),
                    _benchmark_evidence(cs),
                ],
                fix=(
                    f"Faltam cerca de {falta:.0f} minions ate a mediana — e isso e "
                    "aproximadamente uma wave e meia, nao uma reforma do seu jogo. "
                    "Quase sempre sao os minions perdidos logo depois de um recall e "
                    "os que morrem sozinhos na torre enquanto voce roda."
                ),
                drill=(
                    "Nas proximas 3 partidas, olhe o seu CS exatamente aos 10:00 e "
                    "anote o numero. So medir ja move a media."
                ),
                confidence=0.75,
            )
        )

    gd = ctx.bench("gd_at_10")
    if gd is not None and gd.is_weak and ctx.facts.opponent is not None:
        out.append(
            Finding(
                category="trading",
                phase="early",
                timestamp_ms=10 * 60_000,
                severity=_severity_from_percentile(gd.percentile),
                claim=(
                    f"Voce saiu da fase de rota {abs(gd.value):.0f} de ouro atras de "
                    f"{ctx.facts.opponent.champion} aos 10 minutos."
                ),
                evidence=[
                    _t1(
                        f"diferenca de ouro aos 10:00 = {gd.value:+.0f} contra "
                        f"{ctx.facts.opponent.champion}",
                        10 * 60_000,
                    ),
                    _benchmark_evidence(gd),
                ],
                fix=(
                    "Diferenca de ouro no minuto 10 quase nunca vem de trocas perdidas: "
                    "vem de CS perdido e de tempo fora da rota. Olhe primeiro os seus "
                    "recalls e as suas mortes, nao o seu combo."
                ),
                drill=(
                    "Reveja os 10 primeiros minutos no replay e conte quantas waves "
                    "voce perdeu inteiras. Esse numero explica quase toda a diferenca."
                ),
                confidence=0.7,
            )
        )
    return out


def rule_vision(ctx: RuleContext) -> list[Finding]:
    """Visao por minuto, e a Sentinela de Controle como item de checklist."""
    out: list[Finding] = []

    vpm = ctx.bench("vision_per_min")
    if vpm is not None and vpm.is_weak:
        out.append(
            Finding(
                category="vision",
                phase="mid",
                timestamp_ms=ctx.facts.duration_s * 500,  # meio da partida
                severity=_severity_from_percentile(vpm.percentile),
                claim=(
                    f"{vpm.value:.2f} de visao por minuto — percentil "
                    f"{vpm.percentile:.0f} para {vpm.role} {vpm.tier}."
                ),
                evidence=[
                    _t1(
                        f"{ctx.facts.vision.wards_placed} wards colocadas e "
                        f"{ctx.facts.vision.wards_killed} destruidas em "
                        f"{ctx.minutes:.0f} minutos",
                        0,
                        source="match",
                    ),
                    _benchmark_evidence(vpm),
                ],
                fix=(
                    "Visao nao e trabalho do suporte, e trabalho de quem esta perto do "
                    "objetivo seguinte. O teto barato aqui e simplesmente gastar a "
                    "ward amarela sempre que ela estiver carregada — ela recarrega "
                    "sozinha e nao guardar custa zero."
                ),
                drill=(
                    "Nas proximas 3 partidas: toda vez que voltar para a base, saia "
                    "com a ward amarela em cooldown, nao carregada."
                ),
                confidence=0.7,
            )
        )

    # Sentinela de Controle por recall. Isto e checklist, nao skill — por isso
    # a correcao e tao barata e por isso vale apontar.
    n_recalls = len(ctx.facts.recalls)
    compradas = ctx.facts.vision.control_wards_bought
    if n_recalls >= MIN_RECALLS_FOR_WARD_RULE and compradas < CONTROL_WARD_RATE * n_recalls:
        com_sentinela = sum(1 for r in ctx.facts.recalls if CONTROL_WARD_ID in r.bought)
        out.append(
            Finding(
                category="vision",
                phase="mid",
                timestamp_ms=ctx.facts.recalls[min(1, n_recalls - 1)].t_ms,
                severity=2,
                claim=(
                    f"Voce comprou Sentinela de Controle em {com_sentinela} dos "
                    f"{n_recalls} recalls."
                ),
                evidence=[
                    _t1(
                        f"{compradas} sentinelas de controle compradas na partida inteira",
                        0,
                        source="match",
                    ),
                    _t2(
                        f"{n_recalls} recalls detectados; sentinela presente na compra "
                        f"de {com_sentinela}",
                        0,
                        assumption=(
                            "a Riot nao emite evento de recall; eles sao inferidos de "
                            "transicoes de posicao para a base somadas as compras"
                        ),
                    ),
                ],
                fix=(
                    "A Sentinela de Controle e a compra com melhor retorno por ouro do "
                    "jogo, e nao e so do suporte. Isto nao e um erro de visao, e um "
                    "erro de checklist: ela entra em todo recall em que sobrar ouro."
                ),
                drill=(
                    "Nas proximas 3 partidas, compre uma sentinela de controle em TODO "
                    "recall. Nao otimize, so compre."
                ),
                confidence=0.65,
            )
        )
    return out


def rule_gold_hoarding(ctx: RuleContext) -> list[Finding]:
    """Morrer carregando ouro nao gasto.

    Ouro parado nao da dano, nem vida, nem velocidade — e ainda vira bounty
    quando voce morre com ele. Uma vez e azar; duas e um habito de recall.
    """
    caros = [d for d in ctx.facts.deaths if d.gold_at_death >= GOLD_HOARD_AT_DEATH]
    if len(caros) < GOLD_HOARD_MIN_DEATHS:
        return []

    perdido = sum(d.gold_at_death for d in caros)
    pior = max(caros, key=lambda d: d.gold_at_death)
    return [
        Finding(
            category="recall",
            phase=phase_of(pior.t_ms),
            severity=3 if perdido >= 3000 else 2,
            timestamp_ms=pior.t_ms,
            claim=(
                f"Voce morreu {len(caros)} vezes carregando ouro nao gasto — "
                f"{perdido} de ouro no total, sendo {pior.gold_at_death} em {pior.t}."
            ),
            evidence=[
                _t1(
                    " · ".join(
                        f"{d.t} morreu com {d.gold_at_death}g em {d.zone}" for d in caros[:4]
                    ),
                    pior.t_ms,
                ),
            ],
            fix=(
                "O limiar de recall nao e o preco do item completo, e o preco do "
                "proximo componente que muda a sua troca. Ouro no bolso vale zero, e "
                "morrer com ele vale menos que zero."
            ),
            drill=(
                "Nas proximas 3 partidas: sempre que passar de 1200 de ouro, o proximo "
                "crash de wave e recall obrigatorio. Sem excecao, sem negociar."
            ),
            confidence=0.8,
        )
    ]


def rule_death_clusters(ctx: RuleContext) -> list[Finding]:
    """Mortes que se repetem no mesmo lugar, ou muito juntas no tempo.

    Duas formas do mesmo problema: quando a mesma coisa te mata tres vezes, o
    erro nao e de execucao, e de padrao — e padrao e o que da para treinar.
    """
    mortes = ctx.facts.deaths
    if len(mortes) < DEATH_CLUSTER_MIN:
        return []

    out: list[Finding] = []

    por_zona = Counter(d.zone for d in mortes)
    zona, n = por_zona.most_common(1)[0]
    if n >= DEATH_CLUSTER_MIN:
        nessa = [d for d in mortes if d.zone == zona]
        out.append(
            Finding(
                category="positioning",
                phase=phase_of(nessa[0].t_ms),
                severity=3 if n >= 4 else 2,
                timestamp_ms=nessa[0].t_ms,
                claim=f"{n} das suas {len(mortes)} mortes aconteceram em {zona}.",
                evidence=[
                    _t1(
                        " · ".join(f"{d.t} {'+'.join(d.killers) or '?'}" for d in nessa[:5]),
                        nessa[0].t_ms,
                    ),
                ],
                fix=(
                    f"Quando o mesmo lugar te mata {n} vezes, o problema nao e a luta — "
                    f"e ir ate la. Antes de entrar em {zona} de novo, exija uma das "
                    "duas: visao, ou um aliado."
                ),
                drill=(
                    f"Reveja as {n} mortes em {zona} no replay, uma atras da outra. "
                    "O padrao aparece sozinho quando elas estao lado a lado."
                ),
                confidence=0.8,
            )
        )

    # Mortes em sequencia rapida: quase sempre e voltar para a mesma luta
    # perdida, ou insistir numa rota que ja estava resolvida.
    for a, b in pairwise(mortes):
        if b.t_ms - a.t_ms <= DEATH_CLUSTER_WINDOW_MS:
            out.append(
                Finding(
                    category="decision",
                    phase=phase_of(b.t_ms),
                    severity=2,
                    timestamp_ms=b.t_ms,
                    claim=(f"Duas mortes em {(b.t_ms - a.t_ms) // 1000}s — {a.t} e {b.t}."),
                    evidence=[
                        _t1(f"{a.t} morreu em {a.zone}", a.t_ms),
                        _t1(f"{b.t} morreu em {b.zone}", b.t_ms),
                    ],
                    fix=(
                        "Morrer duas vezes seguidas quase sempre e voltar para uma luta "
                        "que ja estava perdida. Depois de morrer, a primeira wave e "
                        "para farmar, nao para revanche."
                    ),
                    drill=(
                        "Nas proximas 3 partidas, depois de cada morte: uma wave inteira "
                        "de farm antes de tocar em qualquer briga."
                    ),
                    confidence=0.7,
                )
            )
            break  # um exemplo basta; listar todos vira ruido

    return out


def rule_objective_deaths(ctx: RuleContext) -> list[Finding]:
    """Morrer com um objetivo prestes a nascer.

    O exemplo de abertura do README e exatamente isto, e nao por acaso: e o
    erro de macro mais caro que a telemetria consegue enxergar sozinha.
    """
    perto = [d for d in ctx.facts.deaths if d.objective_window]
    if not perto:
        return []

    pior = max(perto, key=lambda d: d.gold_swing)
    ev = [
        _t1(
            f"morte D{pior.n} em {pior.t}, em {pior.zone}, para "
            f"{'+'.join(pior.killers) or '?'} (swing {pior.gold_swing}g)",
            pior.t_ms,
        ),
        _t1(f"janela de objetivo no momento da morte: {pior.objective_window}", pior.t_ms),
    ]
    if pior.wave_proxy != "UNKNOWN":
        ev.append(
            _t2(
                f"estado de wave no momento: {pior.wave_proxy}",
                pior.t_ms,
                assumption=(
                    "a API da Riot nao tem campo de estado de wave; isto e derivado do "
                    "ritmo de CS e da sua posicao, e pode estar errado"
                ),
            )
        )

    return [
        Finding(
            category="macro",
            phase=phase_of(pior.t_ms),
            severity=4 if len(perto) >= 2 else 3,
            timestamp_ms=pior.t_ms,
            claim=(
                f"Voce morreu em {pior.t} com {pior.objective_window}"
                + (f" — e isso aconteceu {len(perto)} vezes." if len(perto) >= 2 else ".")
            ),
            evidence=ev,
            fix=(
                "Com um objetivo a menos de um minuto de nascer, o seu trabalho nao e "
                "conseguir mais uma coisa: e estar do lado certo do mapa, com visao. "
                "Atravesse no recall ANTES do objetivo, nao depois."
            ),
            drill=(
                "Nas proximas 3 partidas: quando o timer de um objetivo chegar em 60s, "
                "olhe onde voce esta. Se estiver no lado errado do mapa, o erro ja "
                "aconteceu — e ele aconteceu 90 segundos antes."
            ),
            confidence=0.8,
        )
    ]


def rule_tempo(ctx: RuleContext) -> list[Finding]:
    """Tempo morto como fracao da partida.

    Mortes contam quantas vezes; tempo morto conta quanto custou. Sao numeros
    diferentes: cinco mortes no early doem muito menos que tres no late.
    """
    td = ctx.bench("time_dead_pct")
    if td is None or not td.is_weak:
        return []

    segundos = ctx.facts.time_dead_s
    return [
        Finding(
            category="tempo",
            phase="mid",
            timestamp_ms=0,
            severity=_severity_from_percentile(td.percentile),
            claim=(
                f"Voce passou {td.value:.0f}% da partida morto "
                f"({segundos // 60}min{segundos % 60:02d}s) — percentil {td.percentile:.0f}."
            ),
            evidence=[
                _t1(
                    f"{ctx.facts.focus.deaths} mortes, {segundos}s de tempo morto em "
                    f"{ctx.minutes:.0f} minutos de partida",
                    0,
                    source="match",
                ),
                _benchmark_evidence(td),
            ],
            fix=(
                "Tempo morto e o unico recurso do jogo que nao da para recuperar. "
                "Repare que este numero pesa as mortes tardias muito mais que as "
                "iniciais — se ele esta alto e a sua contagem de mortes nao esta, o "
                "problema e QUANDO voce morre, nao quantas vezes."
            ),
            drill=(
                "Reveja as suas duas ultimas mortes da partida no replay. Depois dos "
                "25 minutos, uma morte sozinha costuma valer mais que todas as do early."
            ),
            confidence=0.75,
        )
    ]


RULES = (
    rule_measured_blunders,
    rule_objective_deaths,
    rule_gold_hoarding,
    rule_death_clusters,
    rule_lane_economy,
    rule_vision,
    rule_tempo,
)


# --------------------------------------------------------------------------
# Execucao
# --------------------------------------------------------------------------


def evaluate(
    facts: MatchFacts,
    table: BenchmarkTable | None = None,
    tier: str | None = None,
) -> list[Finding]:
    """Roda todas as regras e devolve os findings ordenados.

    Ordem: gravidade, depois qualidade da evidencia, depois confianca. Um
    finding lastreado em medicao vale mais que um lastreado em derivacao,
    mesmo com a mesma gravidade — e o `CoachingReport.ranked()` usa
    exatamente este criterio.
    """
    tabela = table or BenchmarkTable(patch=facts.patch)
    ctx = RuleContext(
        facts=facts,
        benchmarks=tabela.evaluate(facts, tier),
        blunders=find_blunders(facts),
    )

    achados: list[Finding] = []
    for regra in RULES:
        achados.extend(regra(ctx))

    achados.sort(key=lambda f: (-f.severity, f.best_tier.value, -f.confidence))
    return achados


def triggers_of(facts: MatchFacts, findings: list[Finding]) -> set[str]:
    """Os gatilhos estruturados desta partida.

    Na etapa 3 isto e o filtro rigido que roda ANTES da busca semantica:
    normalmente corta o corpus de principios de ~120 chunks para ~15, sem
    embedding nenhum. Ver docs/04-knowledge-base.md, 4.4.
    """
    out = {f"role={facts.focus.position or 'MIDDLE'}"}

    categoria_para_gatilho: dict[Category, str] = {
        "recall": "recall_error",
        "vision": "vision_gap",
        "tempo": "tempo_loss",
        "itemization": "itemization_gap",
        "trading": "lane_deficit",
        "wave": "lane_deficit",
    }
    for f in findings:
        out.add(f"phase={f.phase}")
        if (gatilho := categoria_para_gatilho.get(f.category)) is not None:
            out.add(gatilho)
        if f.category == "positioning" and "mortes" in f.claim:
            out.add("death_cluster")
        if f.category == "macro":
            out.add("objective_window")

    if any(d.gold_at_death >= GOLD_HOARD_AT_DEATH for d in facts.deaths):
        out.add("gold_hoarding")

    # O estado de wave mais frequente nas mortes e o gatilho de wave mais util:
    # ele diz em que situacao o jogador estava quando as coisas deram errado.
    waves = Counter(d.wave_proxy for d in facts.deaths if d.wave_proxy != "UNKNOWN")
    if waves:
        gatilho = f"wave_proxy={waves.most_common(1)[0][0]}"
        if gatilho in TRIGGERS:
            out.add(gatilho)

    return out
