"""Pontuacao de um relatorio contra a anotacao humana.

Implementa `evals/rubric.md`. Se os dois divergirem, o documento vence e este
arquivo esta com bug — a rubrica e o contrato, isto e a execucao dela.

O ponto do modulo inteiro: tornar "melhorou" verificavel. Sem um numero
calculado por um criterio escrito ANTES de olhar o resultado, um PR de prompt
vira discussao de gosto, e o projeto perde a unica forma que tem de saber se
mudou para melhor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from riftcoach.core.schema import CoachingReport, EvidenceTier, Finding

# Janelas de ancoragem, em milissegundos. Ver rubric.md, eixo 2.
ANCHOR_EXACT_MS = 10_000
ANCHOR_NEAR_MS = 45_000

# Premissas que nao declaram nada. Um T2 com "derivado dos dados" e
# formalmente valido e materialmente inutil: o usuario nao consegue discordar
# de uma premissa que nao foi dita.
VAGUE_ASSUMPTIONS = (
    "derivado dos dados",
    "com base no contexto",
    "com base nos dados",
    "a partir do contexto",
    "inferido do contexto",
    "dados da partida",
)
MIN_ASSUMPTION_CHARS = 25

# Sinais de que uma `fix` e concreta: numero, horario, zona do mapa, ou um
# termo que so faz sentido numa situacao especifica.
_CONCRETE = re.compile(
    r"\d|OWN_|ENEMY_|NEUTRAL_|\briver\b|\bpit\b|\bwave\b|\brecall\b|\bward\b",
    re.IGNORECASE,
)
# Frases que servem para qualquer partida de qualquer jogador.
_TRUISMS = (
    "melhore seu posicionamento",
    "tome mais cuidado",
    "jogue com mais atencao",
    "nao morra",
    "seja mais cuidadoso",
    "preste atencao",
    "evite morrer",
)


@dataclass
class GoldenFinding:
    category: str
    timestamp_ms: int
    severity: int
    summary: str
    why_it_matters: str = ""
    actionability_manual: float | None = None


@dataclass
class Golden:
    match_id: str
    puuid: str
    findings: list[GoldenFinding] = field(default_factory=list)
    annotator: str = ""
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> Golden:
        dados = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            match_id=dados["match_id"],
            puuid=dados["puuid"],
            annotator=dados.get("annotator", ""),
            notes=dados.get("notes", ""),
            findings=[GoldenFinding(**f) for f in dados.get("findings", [])],
        )


@dataclass
class FindingScore:
    """A pontuacao de um finding, nos quatro eixos da rubrica."""

    grounding: float = 0.0
    anchor: float = 0.0
    match: float = 0.0
    actionability: float = 0.0
    matched_golden: int | None = None
    reason: str = ""

    @property
    def total(self) -> float:
        """Fundamentacao e ELIMINATORIA: zero nela zera o resto.

        Nao e severidade por severidade. Um finding bem escrito sobre uma
        premissa nao declarada e pior que nenhum finding, porque o usuario nao
        tem como saber que precisa desconfiar dele.
        """
        if self.grounding <= 0:
            return 0.0
        return (self.anchor + self.match + self.actionability) / 3.0


def score_grounding(f: Finding) -> tuple[float, str]:
    """Eixo 1. Binario.

    A checagem de premissa AUSENTE e, hoje, inalcancavel por qualquer `Finding`
    valido: o schema recusa um T2 sem `assumption`, e o pydantic revalida a
    evidencia aninhada ao montar o finding. Ela fica aqui como defesa em
    profundidade — se um dia a validacao for relaxada, o harness continua
    reprovando em vez de passar a dar nota.

    A checagem que REALMENTE pega coisa e a de premissa generica logo abaixo:
    "derivado dos dados" satisfaz o schema e nao informa nada.
    """
    for e in f.evidence:
        if e.tier is EvidenceTier.T1_MEASURED:
            continue
        premissa = (e.assumption or "").strip()
        if not premissa:
            return 0.0, f"evidencia {e.tier.value} sem premissa"
        baixo = premissa.lower()
        if any(v in baixo for v in VAGUE_ASSUMPTIONS) and len(premissa) < 60:
            return 0.0, f"premissa generica: {premissa!r}"
        if len(premissa) < MIN_ASSUMPTION_CHARS:
            return 0.0, f"premissa curta demais: {premissa!r}"
    return 1.0, ""


def score_anchor(reported_ms: int, golden_ms: int) -> float:
    """Eixo 2."""
    delta = abs(reported_ms - golden_ms)
    if delta <= ANCHOR_EXACT_MS:
        return 1.0
    if delta <= ANCHOR_NEAR_MS:
        return 0.5
    return 0.0


def score_actionability(f: Finding, manual: float | None = None) -> float:
    """Eixo 4, aproximado.

    A aproximacao e GROSSEIRA e o run.py diz isso na saida. Ela nao entende a
    frase; ela procura sinais de que a frase e sobre ESTA partida. Quando um
    anotador humano preenche `actionability_manual`, o valor dele vence.
    """
    if manual is not None:
        return manual

    texto = f"{f.fix} {f.drill or ''}".strip()
    baixo = texto.lower()
    if any(t in baixo for t in _TRUISMS) and len(texto) < 120:
        return 0.0
    if not _CONCRETE.search(texto):
        return 0.0
    # Uma frase curta raramente contem uma acao alternativa de verdade; ela
    # costuma ser a versao imperativa do proprio erro ("nao va ali sozinho").
    return 1.0 if len(texto) >= 80 else 0.5


def _category_agrees(reported: str, golden: str) -> float:
    if reported == golden:
        return 1.0
    # As fronteiras entre estas tres sao genuinamente borradas, e o jogador nao
    # liga para a etiqueta. Meio ponto, nao zero.
    vizinhas = {"macro", "positioning", "decision", "objective", "tempo"}
    if reported in vizinhas and golden in vizinhas:
        return 0.5
    lane = {"wave", "trading", "recall", "itemization"}
    if reported in lane and golden in lane:
        return 0.5
    return 0.0


@dataclass
class MatchScore:
    """O resultado de uma partida."""

    match_id: str
    reported: int = 0
    golden: int = 0
    matched: int = 0
    scores: list[FindingScore] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    dropped_by_validator: int = 0
    error: str = ""

    @property
    def precision(self) -> float:
        """Dos findings reportados, quantos casam com o golden — ponderado
        pela qualidade, nao so pela existencia do casamento."""
        if not self.reported:
            return 0.0
        return sum(s.total for s in self.scores) / self.reported

    @property
    def recall(self) -> float:
        return self.matched / self.golden if self.golden else 0.0

    @property
    def noise(self) -> int:
        """Findings sem correspondencia. A queixa numero um de ferramentas
        assim, e o numero que os agregados escondem."""
        return sum(1 for s in self.scores if s.matched_golden is None)

    def f_beta(self, beta: float = 1.0) -> float:
        p, r = self.precision, self.recall
        if p + r == 0:
            return 0.0
        b2 = beta * beta
        return (1 + b2) * p * r / (b2 * p + r)


def score_report(report: CoachingReport, golden: Golden, dropped: int = 0) -> MatchScore:
    """Casa os findings reportados com os anotados e pontua.

    Casamento guloso, do melhor para o pior: cada golden e consumido por no
    maximo um reportado. Sem isso, tres findings quase iguais sobre a mesma
    morte casariam todos com a mesma anotacao e a precisao ficaria inflada
    exatamente no caso que ela deveria punir.
    """
    resultado = MatchScore(
        match_id=golden.match_id,
        reported=len(report.findings),
        golden=len(golden.findings),
        dropped_by_validator=dropped,
    )

    candidatos: list[tuple[float, int, int, FindingScore]] = []
    for i, f in enumerate(report.findings):
        fundamento, motivo = score_grounding(f)
        for j, g in enumerate(golden.findings):
            ancora = score_anchor(f.timestamp_ms, g.timestamp_ms)
            if ancora == 0.0:
                continue
            s = FindingScore(
                grounding=fundamento,
                anchor=ancora,
                match=_category_agrees(f.category, g.category),
                actionability=score_actionability(f, g.actionability_manual),
                matched_golden=j,
                reason=motivo,
            )
            candidatos.append((s.total, i, j, s))

    candidatos.sort(key=lambda c: -c[0])
    usados_reportados: set[int] = set()
    usados_golden: set[int] = set()
    por_reportado: dict[int, FindingScore] = {}

    for _total, i, j, s in candidatos:
        if i in usados_reportados or j in usados_golden:
            continue
        usados_reportados.add(i)
        usados_golden.add(j)
        por_reportado[i] = s

    for i, f in enumerate(report.findings):
        if i in por_reportado:
            resultado.scores.append(por_reportado[i])
            continue
        fundamento, motivo = score_grounding(f)
        resultado.scores.append(
            FindingScore(
                grounding=fundamento,
                reason=motivo or "sem correspondencia no golden",
            )
        )

    resultado.matched = len(usados_golden)
    resultado.missed = [
        f"{g.timestamp_ms // 60000}:{(g.timestamp_ms // 1000) % 60:02d} [{g.category}] {g.summary}"
        for j, g in enumerate(golden.findings)
        if j not in usados_golden
    ]
    return resultado


@dataclass
class ProviderScore:
    """O agregado de um provedor sobre o corpus inteiro."""

    provider: str
    matches: list[MatchScore] = field(default_factory=list)

    @property
    def ok(self) -> list[MatchScore]:
        return [m for m in self.matches if not m.error]

    @property
    def precision(self) -> float:
        return _media(m.precision for m in self.ok)

    @property
    def recall(self) -> float:
        return _media(m.recall for m in self.ok)

    @property
    def f1(self) -> float:
        return _media(m.f_beta(1.0) for m in self.ok)

    @property
    def f05(self) -> float:
        """Precisao pesa o dobro. Um finding errado custa mais credibilidade do
        que dez certos constroem, e o relatorio L5 ja garante um piso de
        cobertura."""
        return _media(m.f_beta(0.5) for m in self.ok)

    @property
    def noise_per_match(self) -> float:
        return _media(float(m.noise) for m in self.ok)

    @property
    def drop_rate(self) -> float:
        total = sum(m.reported + m.dropped_by_validator for m in self.ok)
        if not total:
            return 0.0
        return sum(m.dropped_by_validator for m in self.ok) / total

    @property
    def failures(self) -> int:
        return sum(1 for m in self.matches if m.error)

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "matches": len(self.matches),
            "failures": self.failures,
            "precision": round(self.precision, 3),
            "recall": round(self.recall, 3),
            "f1": round(self.f1, 3),
            "f05": round(self.f05, 3),
            "noise_per_match": round(self.noise_per_match, 2),
            "drop_rate": round(self.drop_rate, 3),
        }


def _media(valores: object) -> float:
    lista = list(valores)  # type: ignore[call-overload]
    return sum(lista) / len(lista) if lista else 0.0
