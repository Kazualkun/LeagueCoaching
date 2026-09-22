"""O relatorio L5 renderizado — metricas + benchmarks + regras, sem IA.

Este e o artefato que o usuario le quando nenhum modelo esta disponivel, e ele
precisa ser bom o bastante para valer sozinho. Nao e um log de diagnostico: e o
produto.

Tres decisoes de apresentacao, todas com consequencia:

1. TODA afirmacao mostra o nivel da evidencia, e T2/T3 mostram a premissa. O
   usuario tem que conseguir discordar de forma util — "essa premissa nao vale
   no meu caso" e um relato de bug acionavel; "isso esta errado" nao e.

2. O topo sao TRES findings, de categorias diferentes. Um relatorio que abre
   com tres mortes ensina uma coisa so.

3. O rodape diz explicitamente que nenhum modelo tocou nisto. Quando a etapa 3
   entrar, o mesmo campo vai listar os modelos usados — entao a ausencia deles
   precisa ser visivel, nao silenciosa.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riftcoach.analysis import advantage
from riftcoach.analysis.advantage import find_blunders
from riftcoach.analysis.analysts import run_analysts, run_head_coach
from riftcoach.analysis.merge import build_report
from riftcoach.analysis.packet import build_packet
from riftcoach.analysis.rules import evaluate as run_rules
from riftcoach.core.schema import CoachingReport, EvidenceTier, Finding
from riftcoach.knowledge import benchmarks as bench
from riftcoach.knowledge.benchmarks import Benchmark, BenchmarkTable
from riftcoach.knowledge.sync import PatchDB
from riftcoach.knowledge.validator import validate_findings
from riftcoach.llm.router import ModelRouter
from riftcoach.parse import render as facts_render
from riftcoach.parse.distill import mmss
from riftcoach.parse.facts import MatchFacts


@dataclass
class AiTrace:
    """A procedencia do relatorio: o que a IA fez, e o que ela deixou de fazer.

    Isto vai impresso no rodape. Um relatorio que usou IA e um que degradou
    para as regras precisam ser distinguiveis pelo leitor — apresentar os dois
    do mesmo jeito seria esconder exatamente a informacao que decide o quanto
    confiar no texto.
    """

    providers: dict[str, str] = field(default_factory=dict)
    by_analyst: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    validation: str = ""
    head_coach_error: str = ""
    note: str = ""

    @property
    def used_ai(self) -> bool:
        return bool(self.by_analyst) and any(self.by_analyst.values())

CATEGORY_PT: dict[str, str] = {
    "wave": "wave",
    "trading": "trocas",
    "recall": "recall",
    "itemization": "itens",
    "vision": "visao",
    "objective": "objetivos",
    "positioning": "posicionamento",
    "tempo": "tempo",
    "macro": "macro",
    "decision": "decisao",
    "movement": "movimentacao",
    "combo": "combo",
}

TIER_PT: dict[EvidenceTier, str] = {
    EvidenceTier.T1_MEASURED: "T1, medido",
    EvidenceTier.T2_DERIVED: "T2, derivado",
    EvidenceTier.T3_INFERRED: "T3, inferido",
}


def render_finding(f: Finding, n: int, indent: str = "") -> list[str]:
    """Um finding, no formato que a UI e a CLI compartilham."""
    out = [
        f"{indent}#{n} · gravidade {f.severity} · {mmss(f.timestamp_ms)} · "
        f"{CATEGORY_PT.get(f.category, f.category)}",
        f"{indent}  {f.claim}",
        f"{indent}  Evidencias:",
    ]
    for e in f.evidence:
        out.append(f"{indent}    [{TIER_PT[e.tier]}] {e.statement}")
        if e.assumption:
            # A premissa e o que torna um T2 discutivel em vez de so incerto.
            out.append(f"{indent}       premissa: {e.assumption}")
    out.append(f"{indent}  Correcao: {f.fix}")
    if f.drill:
        out.append(f"{indent}  Treino: {f.drill}")
    # O erro e a decisao, nao o desfecho: o seek cai 8s antes da ancora.
    out.append(f"{indent}  Replay: pular para {mmss(f.seek_ms)}")
    return out


def render_report(
    facts: MatchFacts,
    report: CoachingReport,
    marks: list[Benchmark] | None = None,
    resolver: facts_render.NameResolver | None = None,
    trace: AiTrace | None = None,
) -> str:
    """O relatorio L5 completo, em texto."""
    ranqueados = report.ranked()
    linhas: list[str] = [
        facts_render.render(facts, (facts_render.Section.HEADER,), resolver).rstrip()
    ]

    if not ranqueados:
        linhas += [
            "",
            "Nenhum erro acima do limiar foi encontrado nesta partida.",
            "",
            "Isso nao quer dizer que ela foi perfeita — quer dizer que a telemetria "
            "sozinha nao enxergou nada grave. Partidas curtas, remakes e jogos "
            "tranquilos caem aqui com frequencia.",
        ]
    else:
        linhas += ["", "=" * 72, f"OS {min(3, len(ranqueados))} PRINCIPAIS", "=" * 72]
        for i, f in enumerate(ranqueados[:3], 1):
            linhas += ["", *render_finding(f, i)]

        if len(ranqueados) > 3:
            linhas += ["", "-" * 72, "TAMBEM VALE OLHAR", "-" * 72]
            for i, f in enumerate(ranqueados[3:], 4):
                linhas += ["", *render_finding(f, i)]

    if marks:
        linhas.append(bench.summarize(marks))

    if curva := advantage.summarize(facts):
        linhas.append(curva)

    linhas += [
        "",
        "=" * 72,
        f"patch {facts.patch} · parser v{facts.parser_version} · "
        f"{len(report.findings)} findings",
    ]
    if report.model_trace and trace is not None and trace.used_ai:
        modelos = ", ".join(f"{k} [{v}]" for k, v in sorted(report.model_trace.items()))
        linhas.append(f"modelos: {modelos}")
        passes = ", ".join(f"{k}={v}" for k, v in sorted(trace.by_analyst.items()))
        linhas.append(f"passes: {passes}")
        if trace.validation:
            linhas.append(f"validacao: {trace.validation}")
        for nome, motivo in sorted(trace.failures.items()):
            linhas.append(f"  analista {nome} falhou: {motivo}")
        if trace.head_coach_error:
            linhas.append(
                f"  head coach falhou ({trace.head_coach_error}); "
                "a selecao caiu no merge deterministico"
            )
        for v in trace.violations[:5]:
            linhas.append(f"  {v}")
    else:
        linhas.append(
            "SEM IA: este relatorio foi calculado inteiramente em Python, offline. "
            "Nenhum modelo foi consultado e nenhum dado saiu desta maquina."
        )
        if trace is not None and trace.note:
            linhas.append(f"motivo: {trace.note}")
        elif trace is not None and trace.failures:
            for nome, motivo in sorted(trace.failures.items()):
                linhas.append(f"  analista {nome} falhou: {motivo}")
    return "\n".join(linhas) + "\n"


def analyze(
    facts: MatchFacts,
    tier: str | None = None,
    history: list[MatchFacts] | None = None,
    resolver: facts_render.NameResolver | None = None,
) -> tuple[CoachingReport, str]:
    """MatchFacts -> (relatorio estruturado, texto renderizado). Caminho L5.

    Sincrono e sem rede de proposito: e o piso do produto, e o piso precisa
    funcionar sem nada. A versao com IA e `analyze_with_ai`, que reaproveita
    exatamente estas pecas.
    """
    tabela = BenchmarkTable(patch=facts.patch, history=history)
    marks = tabela.evaluate(facts, tier)
    achados = run_rules(facts, table=tabela, tier=tier)
    report = build_report(facts, achados)
    return report, render_report(facts, report, marks, resolver)


async def analyze_with_ai(
    facts: MatchFacts,
    router: ModelRouter,
    tier: str | None = None,
    history: list[MatchFacts] | None = None,
    resolver: facts_render.NameResolver | None = None,
    patch_db: PatchDB | None = None,
) -> tuple[CoachingReport, str, AiTrace]:
    """O pipeline completo: regras + quatro analistas + head coach + validador.

    A ORDEM IMPORTA, e ela e a razao desta funcao existir em vez de uma flag:

      1. As REGRAS rodam primeiro e sempre. Elas sao a rede de seguranca — se o
         modelo discordar do que esta medido, o modelo esta errado.
      2. Os analistas recebem as medicoes junto do pedido, o que transforma
         "invente findings" em "explique estes fatos".
      3. O VALIDADOR roda antes do merge, para que um finding descartado nunca
         chegue a ser escolhido para o topo.
      4. O merge deduplica os dois produtores com o mesmo criterio.

    Degrada em cada ponto: sem provedor, sem analista que responda, ou com o
    head coach falhando, o resultado continua sendo um relatorio valido — no
    pior caso exatamente o L5. Nunca levanta por falta de IA.
    """
    tabela = BenchmarkTable(patch=facts.patch, history=history)
    marks = tabela.evaluate(facts, tier)
    achados_regras = run_rules(facts, table=tabela, tier=tier)
    trace = AiTrace()

    if not router.available:
        report = build_report(facts, achados_regras)
        trace.note = "nenhum provedor de IA disponivel"
        return report, render_report(facts, report, marks, resolver, trace), trace

    packet = build_packet(
        facts,
        benchmarks=marks,
        rule_findings=achados_regras,
        blunders=find_blunders(facts),
        resolver=resolver,
    )

    analise = await run_analysts(router, packet)
    trace.failures = dict(analise.failures)
    trace.rejected = list(analise.rejected)
    trace.by_analyst = {k: len(v) for k, v in analise.by_analyst.items()}

    do_modelo = analise.findings
    entidades = analise.entities

    if do_modelo and patch_db is not None:
        validacao = validate_findings(
            do_modelo, facts.patch, patch_db, facts.duration_s * 1000, entidades
        )
        trace.validation = validacao.summary()
        trace.violations = [v.render() for v in validacao.violations]
        do_modelo = validacao.kept
        # O head coach so pode escolher entre o que sobreviveu. Filtrar o
        # indice ANTES de chama-lo e o que garante isso: ele devolve copias
        # com gravidade revisada, entao nao haveria como reconhecer um
        # descartado depois pelo objeto.
        aprovados = {id(f) for f in do_modelo}
        analise.by_analyst = {
            nome: [f for f in achados if id(f) in aprovados]
            for nome, achados in analise.by_analyst.items()
        }

    if do_modelo:
        escolhidos, erro = await run_head_coach(router, packet, analise)
        if erro:
            trace.head_coach_error = erro
        elif escolhidos:
            do_modelo = escolhidos

    for p in router.providers:
        trace.providers[p.profile.name] = p.profile.cost_class

    report = build_report(
        facts,
        achados_regras,
        model_trace={p.profile.name: p.profile.cost_class for p in router.providers},
        interpreted=do_modelo,
    )
    return report, render_report(facts, report, marks, resolver, trace), trace
