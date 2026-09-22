"""FactValidator: a varredura deterministica que roda em TODO relatorio.

O README promete que o modelo nunca e responsavel pelos fatos. Este modulo e
onde essa promessa deixa de ser uma intencao e vira codigo.

A captura que mais se paga e a STALE_ENTITY. Um modelo treinado antes do patch
vai recomendar itens removidos com toda a confianca — "builde Eco de Luden",
"rushe Divino Despedacador" — e essa e a falha que mais destroi credibilidade
num coach de LoL. O jogador abre a loja, o item nao existe, e nada mais que o
relatorio disser importa.

Aho-Corasick e a estrutura certa aqui e nao e preciosismo: o banco guarda
varios patches em dois locales, entao a tabela de nomes chega facil a milhares
de entradas. Um automato varre o texto em O(tamanho do texto), independente de
quantos nomes existam.

Duas checagens, com mecanismos diferentes:

  STALE_ENTITY          o automato ACHOU a entidade, e ela nao existe no patch
                        da partida. Detectavel sem ajuda do modelo.
  HALLUCINATED_ENTITY   o modelo DECLAROU ter citado uma entidade, e ela nao
                        existe em patch nenhum. Precisa da declaracao: uma
                        entidade inventada nao casa com o automato justamente
                        por ser inventada, entao varrer nao a encontraria.

Ver docs/04-knowledge-base.md, 4.5.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum

from riftcoach.core.schema import CoachingReport, Finding
from riftcoach.knowledge.sync import PatchDB


class ViolationKind(StrEnum):
    HALLUCINATED_ENTITY = "HALLUCINATED_ENTITY"
    STALE_ENTITY = "STALE_ENTITY"
    BAD_ANCHOR = "BAD_ANCHOR"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"


@dataclass(frozen=True)
class Violation:
    kind: ViolationKind
    finding_index: int
    detail: str

    @property
    def fatal(self) -> bool:
        """Esta violacao descarta o finding, ou so o marca?

        Uma ancora ruim e fatal porque o finding deixa de ser clicavel — e um
        finding que nao da para conferir no replay perde a propriedade que faz
        este projeto diferente. Entidade obsoleta tambem: recomendar item que
        nao existe e pior que nao recomendar nada.
        """
        return self.kind is not ViolationKind.UNSUPPORTED_CLAIM

    def render(self) -> str:
        return f"[{self.kind.value}] finding #{self.finding_index}: {self.detail}"


# --------------------------------------------------------------------------
# Aho-Corasick
# --------------------------------------------------------------------------


class _Node:
    __slots__ = ("children", "fail", "outputs")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.fail: _Node | None = None
        self.outputs: list[str] = []


class EntityScanner:
    """Automato de multiplos padroes sobre os nomes de entidade conhecidos.

    Construido uma vez por relatorio e reutilizado em todos os campos de texto.
    Casamento em minusculas: os nomes vem do DataDragon com capitalizacao
    propria e o modelo nao a respeita de forma confiavel.
    """

    def __init__(self, names: dict[str, str]) -> None:
        self._kinds = names
        self._root = _Node()
        for nome in names:
            no = self._root
            for ch in nome:
                no = no.children.setdefault(ch, _Node())
            no.outputs.append(nome)
        self._build_failure_links()

    def _build_failure_links(self) -> None:
        fila: deque[_Node] = deque()
        self._root.fail = self._root
        for filho in self._root.children.values():
            filho.fail = self._root
            fila.append(filho)

        while fila:
            atual = fila.popleft()
            for ch, filho in atual.children.items():
                fila.append(filho)
                alvo = atual.fail
                while alvo is not None and alvo is not self._root and ch not in alvo.children:
                    alvo = alvo.fail
                destino = (alvo.children.get(ch) if alvo else None) or self._root
                filho.fail = destino if destino is not filho else self._root
                # Herdar as saidas do link de falha e o que faz o automato
                # reportar padroes que TERMINAM aqui mas comecaram antes — sem
                # isso, "Botas" dentro de "Botas de Velocidade" sumiria.
                filho.outputs.extend(filho.fail.outputs)

    def scan(self, text: str) -> set[str]:
        """Todos os nomes conhecidos que aparecem no texto."""
        achados: set[str] = set()
        no = self._root
        for ch in text.lower():
            while no is not self._root and ch not in no.children:
                no = no.fail or self._root
            no = no.children.get(ch, self._root)
            if no.outputs:
                achados.update(no.outputs)
        return achados

    def kind_of(self, name: str) -> str:
        return self._kinds.get(name.lower(), "")

    def knows(self, name: str) -> bool:
        return name.lower() in self._kinds

    def __len__(self) -> int:
        return len(self._kinds)


# --------------------------------------------------------------------------
# Validacao
# --------------------------------------------------------------------------


@dataclass
class ValidationResult:
    violations: list[Violation] = field(default_factory=list)
    kept: list[Finding] = field(default_factory=list)
    dropped: list[Finding] = field(default_factory=list)
    # True quando nao tinhamos dados do patch da partida. O relatorio precisa
    # dizer isso: "nao validado" e uma informacao diferente de "validado e
    # limpo", e apresentar as duas do mesmo jeito seria mentir por omissao.
    skipped: bool = False

    @property
    def clean(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        if self.skipped:
            return "validacao de patch pulada: banco sem dados deste patch"
        if self.clean:
            return "validado contra o patch: nenhuma violacao"
        por_tipo: dict[str, int] = {}
        for v in self.violations:
            por_tipo[v.kind.value] = por_tipo.get(v.kind.value, 0) + 1
        detalhe = ", ".join(f"{k}={n}" for k, n in sorted(por_tipo.items()))
        return f"{len(self.dropped)} finding(s) descartado(s) na validacao ({detalhe})"


def _texts_of(f: Finding) -> list[str]:
    """Os campos que o modelo escreveu, e que portanto podem conter invencao."""
    partes = [f.claim, f.fix, f.drill or ""]
    partes.extend(e.statement for e in f.evidence)
    partes.extend(e.assumption or "" for e in f.evidence)
    return [p for p in partes if p]


def validate_findings(
    findings: list[Finding],
    patch: str,
    db: PatchDB,
    duration_ms: int,
    declared_entities: dict[int, list[str]] | None = None,
) -> ValidationResult:
    """A varredura, sobre uma lista crua de findings.

    Este e o ponto de entrada que o pipeline usa, e nao a versao que recebe um
    `CoachingReport`, por um motivo de ordem: validar ANTES do merge significa
    que um finding descartado nunca chega a ser escolhido para o top tres. Se
    validassemos depois, o relatorio poderia abrir com um buraco.

    `declared_entities` mapeia indice NESTA lista -> nomes que o modelo disse
    ter citado.
    """
    resultado = ValidationResult()

    if not db.knows_patch(patch):
        # Sem dados do patch nao da para distinguir "item removido" de "item
        # que nunca sincronizamos". Acusar o segundo descartaria findings bons,
        # entao pulamos e dizemos que pulamos.
        resultado.skipped = True
        resultado.kept = list(findings)
        return resultado

    do_patch = db.entity_names(patch)
    de_todos = db.entity_names(None)
    scanner = EntityScanner(de_todos)
    declaradas = declared_entities or {}

    for i, f in enumerate(findings):
        problemas: list[Violation] = []

        if not 0 <= f.timestamp_ms <= duration_ms:
            problemas.append(
                Violation(
                    ViolationKind.BAD_ANCHOR,
                    i,
                    f"timestamp {f.timestamp_ms} fora de [0, {duration_ms}]",
                )
            )

        texto = " \n ".join(_texts_of(f))
        for nome in scanner.scan(texto):
            if nome not in do_patch:
                problemas.append(
                    Violation(
                        ViolationKind.STALE_ENTITY,
                        i,
                        f"cita '{nome}' ({scanner.kind_of(nome)}), que nao existe no patch {patch}",
                    )
                )

        for nome in declaradas.get(i, []):
            if nome and not scanner.knows(nome):
                problemas.append(
                    Violation(
                        ViolationKind.HALLUCINATED_ENTITY,
                        i,
                        f"declarou ter citado '{nome}', que nao existe em patch nenhum",
                    )
                )

        resultado.violations.extend(problemas)
        if any(p.fatal for p in problemas):
            resultado.dropped.append(f)
        else:
            resultado.kept.append(f)

    return resultado


def validate(
    report: CoachingReport,
    db: PatchDB,
    duration_ms: int,
    declared_entities: dict[int, list[str]] | None = None,
) -> ValidationResult:
    """A mesma varredura, sobre um relatorio ja montado.

    Existe para o caso de auditar um relatorio guardado — a interface mostra
    "gerado para o patch X" e, se o patch atual avancou, revalidar responde se
    o conselho de itemizacao ainda vale (docs/04-knowledge-base.md, M3).
    """
    return validate_findings(
        list(report.findings), report.patch, db, duration_ms, declared_entities
    )


def build_scanner(db: PatchDB, patch: str | None = None) -> EntityScanner:
    return EntityScanner(db.entity_names(patch))
