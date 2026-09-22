"""O contrato de dados central.

Tudo no sistema e uma transformacao entre estes tipos. Congele-os primeiro; todo
o resto e substituivel (ver docs/ARCHITECTURE.md, secao 4).

Duas invariantes sao impostas pelo schema, nao por disciplina:

  1. Toda afirmacao carrega evidencia, e evidencia que nao seja T1 precisa
     declarar a premissa. Sem isso o coach mistura medicao com chute.
  2. Todo Finding carrega timestamp_ms. E a chave de juncao com o replay: sem
     ela o finding nao pode ser clicado, verificado nem pontuado.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Phase = Literal["early", "mid", "late"]

# As tres ultimas exigem dados que a telemetria NAO tem:
#   trading  — HP so existe por minuto; uma troca dura segundos
#   combo    — a timeline nao emite NENHUM evento de uso de habilidade
#   movement — parcial: posicao por minuto so mostra deslocamento grosseiro
# Elas so recebem evidencia real nos Modos B/C (replay/video), como T3.
# Ver docs/06-advantage-engine.md.
Category = Literal[
    "wave",
    "trading",
    "recall",
    "itemization",
    "vision",
    "objective",
    "positioning",
    "tempo",
    "macro",
    "decision",
    "movement",
    "combo",
]


class EvidenceTier(StrEnum):
    """Quao bem sabemos aquilo que estamos afirmando.

    Isto existe porque a API da Riot NAO tem campo de estado de wave, e wave
    management e o coaching de maior valor que existe. Um sistema que mistura
    silenciosamente CS medido com wave chutada produz conselho que o usuario nao
    pode verificar. Ver docs/02-data-pipeline.md, Transformacao 3.
    """

    T1_MEASURED = "T1"  # lido direto da telemetria da Riot
    T2_DERIVED = "T2"  # calculado da telemetria + premissa declarada
    T3_INFERRED = "T3"  # estimado por visao ou heuristica


EvidenceSource = Literal["timeline", "match", "vision", "patchdb", "benchmark"]


class Evidence(BaseModel):
    tier: EvidenceTier
    timestamp_ms: int = Field(ge=0)
    statement: str = Field(min_length=1)
    source: EvidenceSource
    assumption: str | None = None

    @model_validator(mode="after")
    def _assumption_required_when_not_measured(self) -> Evidence:
        """T2/T3 sem premissa declarada e exatamente o modo de falha que o
        sistema de niveis existe para impedir."""
        if self.tier is not EvidenceTier.T1_MEASURED and not self.assumption:
            raise ValueError(
                f"evidencia {self.tier.value} exige 'assumption' declarada: "
                f"{self.statement!r}"
            )
        return self


class Finding(BaseModel):
    category: Category
    phase: Phase
    severity: int = Field(ge=1, le=5, description="5 = custou a partida")
    timestamp_ms: int = Field(ge=0, description="ancora -> alvo do seek no replay")
    claim: str = Field(min_length=1, description="o que deu errado, em uma frase")
    evidence: list[Evidence] = Field(min_length=1)
    fix: str = Field(min_length=1, description="a acao alternativa concreta")
    drill: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    @property
    def best_tier(self) -> EvidenceTier:
        """O nivel mais forte que sustenta esta afirmacao.

        A UI ordena por isto: um finding lastreado em medicao vale mais que um
        lastreado em inferencia, mesmo com a mesma gravidade.
        """
        return min(e.tier for e in self.evidence)

    @property
    def seek_ms(self) -> int:
        """Onde pular no replay: 8s ANTES da ancora.

        O erro e a decisao, nao o desfecho. Pular para o instante exato da morte
        mostra a consequencia; o erro aconteceu 5-10s antes.
        Ver docs/03-vod-review.md, 3.7.
        """
        return max(0, self.timestamp_ms - 8_000)


MarkKind = Literal["error", "critical", "note", "question", "good"]


class Mark(BaseModel):
    """Uma marcacao na linha do tempo do replay.

    Dois autores, mesma estrutura:

      ai    — gerada pelo motor de vantagem, com gravidade MEDIDA
      user  — colocada a mao pelo jogador, com anotacao livre

    Ficam juntas de proposito. O valor da revisao esta em comparar: onde a IA
    marcou erro critico e o jogador nao percebeu nada, e onde o jogador sentiu
    que errou e a metrica nao viu. Os dois casos ensinam.
    """

    t_ms: int = Field(ge=0)
    author: Literal["ai", "user"]
    kind: MarkKind
    text: str = Field(min_length=1)
    category: Category | None = None
    severity: int | None = Field(default=None, ge=1, le=5)
    # Perda de probabilidade de vitoria, em pontos percentuais. So existe em
    # marcacoes da IA — o jogador marca o que sentiu, nao o que mediu.
    wp_loss: float | None = None
    resolved: bool = False

    @property
    def seek_ms(self) -> int:
        """8s antes: o erro e a decisao, nao o desfecho."""
        return max(0, self.t_ms - 8_000)


class ReviewSession(BaseModel):
    """Estado persistente de uma revisao, para poder retomar de onde parou.

    Guardado por (match_id, puuid). Ao reabrir, as marcacoes da IA e as do
    jogador voltam juntas, na mesma linha do tempo.
    """

    match_id: str
    puuid: str
    marks: list[Mark] = Field(default_factory=list)
    last_position_ms: int = 0
    completed: bool = False

    def timeline(self) -> list[Mark]:
        """Marcacoes em ordem cronologica — como a UI as desenha."""
        return sorted(self.marks, key=lambda m: m.t_ms)

    def criticals(self) -> list[Mark]:
        return [m for m in self.marks if m.kind == "critical"]

    def add(self, mark: Mark) -> None:
        self.marks.append(mark)


class CoachingReport(BaseModel):
    match_id: str
    patch: str = Field(description="ex.: '15.18.1' — fixa o relatorio a um meta")
    puuid: str
    parser_version: int
    locale: str = "pt_BR"
    model_trace: dict[str, str] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    top_three: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _top_three_must_index_findings(self) -> CoachingReport:
        n = len(self.findings)
        bad = [i for i in self.top_three if not 0 <= i < n]
        if bad:
            raise ValueError(f"top_three aponta para findings inexistentes: {bad}")
        if len(set(self.top_three)) != len(self.top_three):
            raise ValueError("top_three contem indices repetidos")
        return self

    def ranked(self) -> list[Finding]:
        """Gravidade primeiro, depois qualidade da evidencia, depois confianca."""
        return sorted(
            self.findings,
            key=lambda f: (-f.severity, f.best_tier.value, -f.confidence),
        )
