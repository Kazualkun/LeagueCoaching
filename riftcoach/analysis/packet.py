"""O EvidencePacket: tudo o que um analista ve, e nada alem disso.

Duas regras montam este modulo, e as duas existem para que modelos de 8B
funcionem (docs/01-model-routing.md, pipeline P1):

1. CADA ANALISTA RECEBE SO A SUA FATIA. O analista de rota nao ganha nada
   vendo a lista de objetivos. Prompts longos e multiobjetivo sao exatamente
   onde modelos pequenos desabam; prompts curtos de objetivo unico sao onde
   eles ficam quase indistinguiveis dos grandes.

2. TODO NUMERO JA VEM CALCULADO. O modelo nunca soma, divide nem compara — se
   ele precisar fazer aritmetica para chegar a uma conclusao, a conta estava
   no lugar errado. Ele recebe o resultado e faz o unico trabalho que faz bem:
   explicar por que aconteceu e o que fazer diferente.

O pacote tambem carrega o que o motor de REGRAS ja mediu. Isso nao e
redundancia: e o que impede o modelo de "descobrir" com confianca algo que
contradiz a medicao, e o que faz ele complementar em vez de repetir.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from riftcoach.analysis.advantage import Blunder
from riftcoach.core.schema import Finding
from riftcoach.knowledge.benchmarks import Benchmark
from riftcoach.knowledge.benchmarks import summarize as summarize_benchmarks
from riftcoach.parse import render as facts_render
from riftcoach.parse.distill import mmss
from riftcoach.parse.facts import MatchFacts

# Analistas e a pergunta unica de cada um. A pergunta entra no prompt: um passe
# que sabe exatamente o que esta procurando produz findings muito melhores que
# um passe generico com a mesma entrada.
ANALYSTS: dict[str, str] = {
    "laning": "Como esta partida foi ganha ou perdida na rota, nos primeiros 15 minutos?",
    "macro": "O jogador estava no lugar certo do mapa nos momentos que decidiram a partida?",
    "economy": "O ouro e o tempo do jogador foram convertidos em forca de forma eficiente?",
    "fights": "Nas lutas, o jogador entrou nas certas e ficou de fora das erradas?",
}


@dataclass
class EvidencePacket:
    """Fatos ja destilados, prontos para virar prompt."""

    facts: MatchFacts
    benchmarks: list[Benchmark] = field(default_factory=list)
    rule_findings: list[Finding] = field(default_factory=list)
    blunders: list[Blunder] = field(default_factory=list)
    patch_notes: list[str] = field(default_factory=list)
    resolver: facts_render.NameResolver | None = None

    # ------------------------------------------------------------------
    # Blocos
    # ------------------------------------------------------------------

    def _measured_block(self) -> str:
        """O que ja esta MEDIDO. O modelo nao pode contradizer isto.

        Entregar as medicoes junto do pedido e o que transforma "invente
        findings" em "explique estes fatos" — que e uma tarefa muito mais facil
        e muito menos alucinavel.
        """
        if not self.blunders:
            return ""
        linhas = [
            "JA MEDIDO PELO MOTOR DE VANTAGEM (nao contradiga; complemente):",
            *(
                f"  {mmss(b.t_ms)} -{b.wp_loss:.0f}pp {b.kind} ({b.involvement}) — {b.detail}"
                for b in self.blunders[:6]
            ),
        ]
        return "\n".join(linhas)

    def _rules_block(self) -> str:
        if not self.rule_findings:
            return ""
        return "\n".join(
            [
                "O MOTOR DE REGRAS JA APONTOU (nao repita; aprofunde ou discorde):",
                *(
                    f"  [{f.category}] {f.claim}"
                    for f in sorted(self.rule_findings, key=lambda f: -f.severity)[:6]
                ),
            ]
        )

    def _patch_block(self) -> str:
        """Fatos de patch das entidades presentes NESTA partida.

        So o que a partida toca. Despejar a tabela inteira de itens gastaria o
        contexto todo e pioraria o resultado: o modelo passa a escolher entre
        400 itens em vez de raciocinar sobre os 6 que o jogador comprou.
        """
        if not self.patch_notes:
            return ""
        return "\n".join(
            [f"FATOS DO PATCH {self.facts.patch} RELEVANTES PARA ESTA PARTIDA:", *self.patch_notes]
        )

    def _benchmark_block(self) -> str:
        return summarize_benchmarks(self.benchmarks).strip()

    # ------------------------------------------------------------------
    # Montagem
    # ------------------------------------------------------------------

    def for_analyst(self, analyst: str) -> str:
        """A fatia de um analista. ~1,0 a 1,4k tokens."""
        partes = [
            facts_render.render_for_analyst(self.facts, analyst, self.resolver).strip(),
            self._measured_block(),
            self._benchmark_block() if analyst in ("laning", "economy") else "",
            self._patch_block() if analyst == "economy" else "",
        ]
        return "\n\n".join(p for p in partes if p)

    def for_head_coach(self, findings_por_analista: dict[str, list[Finding]]) -> str:
        """A entrada do head coach: cabecalho + o que cada analista achou.

        Ele NAO recebe a telemetria de novo. A tarefa dele e escolher e
        ordenar, nao reanalisar — e dar os dados crus junto o convidaria a
        produzir findings proprios, que e trabalho dos analistas e sairia sem
        a fatia certa de contexto.
        """
        blocos = [
            facts_render.render(self.facts, (facts_render.Section.HEADER,), self.resolver).strip(),
            self._measured_block(),
        ]
        for nome, achados in findings_por_analista.items():
            if not achados:
                continue
            linhas = [f"--- {nome.upper()} ---"]
            for i, f in enumerate(achados):
                linhas.append(
                    f"[{nome}:{i}] gravidade {f.severity} · {mmss(f.timestamp_ms)} · "
                    f"{f.category}\n    {f.claim}\n    correcao: {f.fix}"
                )
            blocos.append("\n".join(linhas))
        return "\n\n".join(b for b in blocos if b)


def build_patch_notes(facts: MatchFacts, resolver: facts_render.NameResolver | None) -> list[str]:
    """Nome e custo dos itens que o jogador realmente comprou.

    Sem resolver, devolve vazio em vez de ids crus: uma linha dizendo
    "item 3153 custa 3100" nao ajuda o modelo e ainda ocupa contexto.
    """
    if resolver is None:
        return []
    vistos: set[int] = set()
    notas: list[str] = []
    for _, item_id in facts.build_path:
        if item_id in vistos or item_id <= 0:
            continue
        vistos.add(item_id)
        nome = resolver.item(item_id)
        if not nome:
            continue
        custo = getattr(resolver, "item_cost", lambda _: None)(item_id)
        notas.append(f"- {nome}" + (f": custo total {custo}" if custo else ""))
    return notas


def build_packet(
    facts: MatchFacts,
    benchmarks: list[Benchmark],
    rule_findings: list[Finding],
    blunders: list[Blunder],
    resolver: facts_render.NameResolver | None = None,
) -> EvidencePacket:
    return EvidencePacket(
        facts=facts,
        benchmarks=benchmarks,
        rule_findings=rule_findings,
        blunders=blunders,
        patch_notes=build_patch_notes(facts, resolver),
        resolver=resolver,
    )
