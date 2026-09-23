"""Os quatro analistas em paralelo, e o head coach que unifica.

POR QUE QUATRO PASSES PEQUENOS EM VEZ DE UM GRANDE — e a decisao que sustenta
toda a etapa 3 (docs/01-model-routing.md, P1):

1. E o que torna modelos de 8B viaveis. Cada analista ve ~1,2k tokens e
   responde a UMA pergunta. Prompt longo e multiobjetivo e exatamente onde
   modelo pequeno desaba; prompt curto de objetivo unico e onde ele fica quase
   indistinguivel do grande.
2. Paraleliza — quatro chamadas concorrentes terminam quase no tempo de uma.
3. Cotas de tier gratuito contam REQUISICOES, mas a capacidade e limitada por
   CONTEXTO. Cinco requisicoes pequenas cabem em todo tier gratuito; uma de
   30k tokens nao cabe em varios deles.
4. Falhas ficam isoladas: se o analista de lutas devolver JSON quebrado, voce
   refaz 1,2k tokens, nao a analise inteira.
5. Torna a avaliacao tratavel — da para pontuar findings de rota
   independentemente dos de macro e saber qual prompt regrediu.

O custo e um passe de merge e o risco de duplicata, que `analysis/merge.py` ja
resolve deduplicando em (categoria, t +/- 30s).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from riftcoach.analysis.packet import ANALYSTS, EvidencePacket
from riftcoach.core.errors import NoViableProvider, SchemaExhausted
from riftcoach.core.schema import (
    Category,
    Evidence,
    EvidenceTier,
    Finding,
    Phase,
)
from riftcoach.llm.router import ModelRouter, text_task

PROMPTS_DIR = Path(__file__).parent / "prompts"

# Estimativas de entrada por analista, em tokens. Servem para o roteador
# escolher um provedor com contexto suficiente — nao precisam ser exatas,
# precisam nao subestimar.
# CUSTO CONTRA A COTA, nao tamanho do prompt. A diferenca importa: o
# provedor reserva `max_tokens` de saida ao aceitar a chamada, entao uma
# estimativa so de entrada subestima em mais de 2 mil tokens e o limitador
# deixa passar chamadas que vao levar 429.
#
# Medido contra BR1_3285636947: entrada de 1.791 a 2.175 tokens, mais os
# 2.048 reservados de saida.
EST_TOKENS = {"laning": 4300, "macro": 3900, "economy": 4300, "fights": 3900}
EST_TOKENS_HEAD_COACH = 4500


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """Le um prompt do disco.

    Prompts sao ARQUIVOS, nao literais de string. Eles mudam o tempo todo, e
    mantelos como Markdown revisavel significa que um PR de prompt tem diff
    legivel e que contribuidor que nao programa em Python consegue abrir um.
    """
    caminho = PROMPTS_DIR / f"{name}.md"
    if not caminho.exists():
        raise FileNotFoundError(f"prompt ausente: {caminho}")
    return caminho.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# O schema de saida
# --------------------------------------------------------------------------


class AnalystEvidence(BaseModel):
    """Uma evidencia, como o modelo a escreve.

    `assumption` e OBRIGATORIA aqui, ao contrario do schema interno onde ela e
    opcional para T1. A diferenca e deliberada: JSON Schema nao expressa bem
    "obrigatorio se tier != T1", entao exigimos sempre e aceitamos vazio para
    T1. Assim a restricao vira decodificacao restrita de verdade, em vez de
    uma checagem que so falha depois de gastar a cota.
    """

    tier: Literal["T1", "T2", "T3"]
    timestamp_ms: int = Field(ge=0)
    statement: str = Field(min_length=1)
    assumption: str = Field(description="A premissa assumida. Deixe string vazia se tier for T1.")


class AnalystFinding(BaseModel):
    category: Category
    severity: int = Field(ge=1, le=5)
    timestamp_ms: int = Field(ge=0)
    claim: str = Field(min_length=1)
    evidence: list[AnalystEvidence] = Field(min_length=1)
    fix: str = Field(min_length=1)
    drill: str = ""
    # Por que o modelo DECLARA as entidades em vez de nos as extrairmos do
    # texto: uma entidade inventada nao casa com o automato do validador
    # justamente por ser inventada. Varrer acha o que existe; so a declaracao
    # revela o que o modelo acha que citou. Lista vazia e a resposta normal —
    # a maioria dos findings nao nomeia item nenhum.
    entities: list[str] = Field(
        default_factory=list,
        description=(
            "Itens, campeoes e runas que voce nomeou nos textos acima. "
            "Lista vazia se voce nao nomeou nenhum."
        ),
    )


class AnalystOutput(BaseModel):
    findings: list[AnalystFinding] = Field(default_factory=list, max_length=3)


class HeadCoachPick(BaseModel):
    """O head coach escolhe por REFERENCIA, nao reescrevendo.

    Devolver o id do finding em vez do texto e o que garante que as evidencias
    sobrevivam intactas com os niveis e as premissas originais. Se ele pudesse
    reescrever, a evidencia deixaria de ser confiavel exatamente no passe que
    tem menos contexto para sustenta-la.
    """

    ref: str = Field(description="o id entre colchetes, ex.: 'laning:0'")
    severity: int = Field(ge=1, le=5, description="gravidade revisada")
    reason: str = Field(default="", description="por que este entra")


class HeadCoachOutput(BaseModel):
    keep: list[HeadCoachPick] = Field(default_factory=list, max_length=8)
    top_three: list[str] = Field(default_factory=list, max_length=3)


# --------------------------------------------------------------------------
# Conversao para o schema interno
# --------------------------------------------------------------------------

_TIER = {
    "T1": EvidenceTier.T1_MEASURED,
    "T2": EvidenceTier.T2_DERIVED,
    "T3": EvidenceTier.T3_INFERRED,
}


def _phase_of(t_ms: int) -> Phase:
    from riftcoach.analysis.rules import phase_of

    return phase_of(t_ms)


@dataclass
class ConversionResult:
    findings: list[Finding] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    # Entidades declaradas, por indice do finding NA LISTA `findings`. O
    # validador precisa desse alinhamento, entao o indice acompanha a lista
    # que sobreviveu, nao a que o modelo emitiu.
    entities: dict[int, list[str]] = field(default_factory=dict)


def to_findings(saida: AnalystOutput, duration_ms: int, source_label: str) -> ConversionResult:
    """Converte a saida do modelo em `Finding`, descartando o que nao se sustenta.

    Descartar o finding e NAO a resposta inteira: um analista que produziu dois
    findings bons e um sem premissa entregou dois findings bons. Derrubar o
    passe todo por causa do terceiro seria jogar fora trabalho valido e ainda
    gastar outra chamada.
    """
    out = ConversionResult()
    for f in saida.findings:
        if not 0 <= f.timestamp_ms <= duration_ms:
            # BAD_ANCHOR: um finding fora da partida nao pode ser clicado nem
            # conferido no replay, entao ele nao e utilizavel mesmo que o texto
            # esteja certo.
            out.rejected.append(f"{source_label}: timestamp {f.timestamp_ms} fora da partida")
            continue

        evidencias: list[Evidence] = []
        problema = ""
        for e in f.evidence:
            tier = _TIER[e.tier]
            premissa = e.assumption.strip() or None
            if tier is not EvidenceTier.T1_MEASURED and not premissa:
                problema = f"evidencia {e.tier} sem premissa declarada"
                break
            if not 0 <= e.timestamp_ms <= duration_ms:
                problema = f"evidencia ancorada em {e.timestamp_ms}, fora da partida"
                break
            evidencias.append(
                Evidence(
                    tier=tier,
                    timestamp_ms=e.timestamp_ms,
                    statement=e.statement,
                    source="timeline",
                    assumption=premissa,
                )
            )

        if problema or not evidencias:
            out.rejected.append(f"{source_label}: {problema or 'sem evidencia'}")
            continue

        if f.entities:
            out.entities[len(out.findings)] = [e.strip() for e in f.entities if e.strip()]
        out.findings.append(
            Finding(
                category=f.category,
                phase=_phase_of(f.timestamp_ms),
                severity=f.severity,
                timestamp_ms=f.timestamp_ms,
                claim=f.claim,
                evidence=evidencias,
                fix=f.fix,
                drill=f.drill.strip() or None,
                # Menor que a confianca das regras de proposito. Um finding do
                # modelo e uma interpretacao; um da regra e uma medicao. O
                # ranqueamento usa isto para desempatar a favor do medido.
                confidence=0.6,
            )
        )
    return out


# --------------------------------------------------------------------------
# Execucao
# --------------------------------------------------------------------------


@dataclass
class AnalysisResult:
    findings: list[Finding] = field(default_factory=list)
    by_analyst: dict[str, list[Finding]] = field(default_factory=dict)
    model_trace: dict[str, str] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    # Indice em `findings` -> entidades que o modelo declarou. Alinhado com a
    # lista concatenada, que e a mesma que o validador recebe.
    entities: dict[int, list[str]] = field(default_factory=dict)

    @property
    def any_succeeded(self) -> bool:
        return bool(self.findings)


async def run_analyst(
    router: ModelRouter, packet: EvidencePacket, name: str
) -> tuple[str, AnalystOutput | None, str]:
    """Um analista. Devolve (nome, saida, erro)."""
    tarefa = text_task(f"{name}_analyst", EST_TOKENS.get(name, 1400))
    system = load_prompt("_system")
    prompt = (
        f"{load_prompt(name)}\n\n"
        f"PERGUNTA: {ANALYSTS[name]}\n\n"
        f"=== DADOS DA PARTIDA ===\n{packet.for_analyst(name)}\n"
    )
    try:
        saida = await router.complete_validated(tarefa, prompt, AnalystOutput, system=system)
    except (NoViableProvider, SchemaExhausted) as e:
        return name, None, e.message
    return name, saida, ""


# Quanto uma chamada custa contra a cota, com folga. Medido contra o Groq:
# "Used 4233, Requested 5465" para um passe de analista — ou seja, entrada mais
# a reserva de saida mais o overhead do proprio schema.
CUSTO_POR_PASSE = 5_500

# Quantas chamadas o formato de quatro analistas faz: os quatro mais o head
# coach.
PASSES_DO_FORMATO_LONGO = 5


def _cabe_em_quatro_passes(router: ModelRouter) -> bool:
    """A cota do provedor escolhido aguenta o formato de quatro analistas?

    O Mixture-of-Analysts foi desenhado para modelo LOCAL, onde uma chamada a
    mais nao custa nada alem de tempo. Num tier gratuito com teto de tokens
    POR MINUTO a conta muda: o Groq da 8.000/min e cada passe pede 5.465,
    entao cabe UMA chamada por minuto e as cinco viram cinco minutos de
    espera. Medido, nao estimado.

    Sem cota declarada — o Ollama local — a resposta e sempre sim.
    """
    for p in router.providers:
        limite = p.profile.rate_limit
        if limite is None or limite.tokens is None:
            return True
        if limite.tokens >= CUSTO_POR_PASSE * PASSES_DO_FORMATO_LONGO:
            return True
    return False


async def _passe_completo(
    router: ModelRouter, packet: EvidencePacket, duracao_ms: int
) -> AnalysisResult:
    """Os quatro angulos numa chamada so.

    TRES FINDINGS, E NAO OITO, e o numero saiu de uma medicao que custou caro
    descobrir. Contra o Groq, este passe consome:

        entrada 4.661  +  saida reservada 2.048  =  6.709   (teto: 8.000)

    Pedir oito findings exigiria perto de 3.500 tokens de saida, e o total
    passaria de 8.161 — nao cabe. Sem espaco, o modelo responde ate o teto e o
    JSON sai CORTADO NO MEIO: o segundo finding chega sem o campo `fix`, a
    validacao recusa, e a analise inteira se perde por truncamento.

    Entrega menos que quatro passes dedicados — e entrega, o que os quatro nao
    fazem quando a cota nao alcanca. Tres findings de tres angulos diferentes,
    somados aos deterministicos, valem mais que zero por erro de cota.
    """
    resultado = AnalysisResult()
    tarefa = text_task("analista_completo", CUSTO_POR_PASSE)
    prompt = f"{load_prompt('completo')}\n\n=== DADOS DA PARTIDA ===\n{packet.for_completo()}\n"
    try:
        completo = await router.complete_validated(
            tarefa, prompt, AnalystOutput, system=load_prompt("_system")
        )
    except (NoViableProvider, SchemaExhausted) as e:
        resultado.failures["completo"] = e.message
        return resultado

    conv = to_findings(completo, duracao_ms, "completo")
    resultado.entities.update(dict(conv.entities.items()))
    resultado.by_analyst["completo"] = conv.findings
    resultado.findings.extend(conv.findings)
    resultado.rejected.extend(conv.rejected)
    return resultado


async def run_analysts(router: ModelRouter, packet: EvidencePacket) -> AnalysisResult:
    """Os quatro, UM DE CADA VEZ.

    Eram em paralelo, e medido contra o tier gratuito do Groq isso saia pior.
    O gargalo la nao e latencia, e cota: 8.000 tokens por minuto, ~4.200 por
    passe. Quatro chamadas simultaneas somam 16 mil, todas levam 429, e as que
    esperam acordam juntas para disputar a mesma cota de novo. Nas medicoes,
    dois dos quatro analistas desistiam.

    Em serie, o limitador espaca as chamadas pelo tempo exato que falta e os
    quatro passam. O relogio de parede nao piora — com teto de tokens ele ja
    era a soma, nao o maximo — e some a disputa.

    Contra um Ollama local a serie tambem nao custa: e um servidor de modelo
    so, que enfileira internamente de qualquer jeito.

    Um analista que falha continua nao derrubando os outros: o erro fica na
    lista de falhas e o relatorio sai com os angulos que deram certo. Um
    relatorio com tres angulos e util; nenhum relatorio nao e.

    E ONDE A COTA NAO DA PARA QUATRO, VIRA UM. Ver `_cabe_em_quatro_passes`:
    quatro chamadas so fazem sentido onde chamada e barata em tempo e de graca
    em cota, que e o caso do modelo local. Num tier gratuito apertado, o
    formato certo e uma passada que responde as quatro perguntas.
    """
    duracao_ms = packet.facts.duration_s * 1000
    resultado = AnalysisResult()

    if not _cabe_em_quatro_passes(router):
        return await _passe_completo(router, packet, duracao_ms)

    for nome_do_analista in ANALYSTS:
        try:
            saida: tuple[str, AnalystOutput | None, str] = await run_analyst(
                router, packet, nome_do_analista
            )
        except Exception as e:
            resultado.failures[nome_do_analista] = str(e)
            continue
        nome, dados, erro = saida
        if dados is None:
            resultado.failures[nome] = erro
            continue
        conv = to_findings(dados, duracao_ms, nome)
        # O deslocamento e o que mantem o alinhamento com a lista concatenada.
        # Errar aqui faria o validador acusar as entidades de um finding contra
        # o texto de outro, que e o tipo de bug que passa despercebido porque a
        # saida continua plausivel.
        base = len(resultado.findings)
        for i, entidades in conv.entities.items():
            resultado.entities[base + i] = entidades
        resultado.by_analyst[nome] = conv.findings
        resultado.findings.extend(conv.findings)
        resultado.rejected.extend(conv.rejected)

    return resultado


async def run_head_coach(
    router: ModelRouter, packet: EvidencePacket, analise: AnalysisResult
) -> tuple[list[Finding], str]:
    """Unifica. Devolve (findings escolhidos, erro).

    Falhar aqui NAO e fatal: `merge.py` ja deduplica e ranqueia
    deterministicamente. O head coach melhora a selecao; ele nao e condicao
    para existir relatorio.
    """
    if not analise.findings:
        return [], "nenhum analista produziu findings"

    indice: dict[str, Finding] = {}
    for nome, achados in analise.by_analyst.items():
        for i, f in enumerate(achados):
            indice[f"{nome}:{i}"] = f

    tarefa = text_task("head_coach", EST_TOKENS_HEAD_COACH)
    prompt = (
        f"{load_prompt('head_coach')}\n\n"
        f"=== O QUE OS ANALISTAS ACHARAM ===\n"
        f"{packet.for_head_coach(analise.by_analyst)}\n"
    )
    try:
        escolha = await router.complete_validated(
            tarefa, prompt, HeadCoachOutput, system=load_prompt("_system")
        )
    except (NoViableProvider, SchemaExhausted) as e:
        return [], e.message

    escolhidos: list[Finding] = []
    for pick in escolha.keep:
        base = indice.get(pick.ref)
        if base is None:
            # Referencia inventada. Ignorar em silencio e o certo: e o modo de
            # falha esperado de um modelo pequeno, e o merge deterministico
            # cobre o resto.
            continue
        escolhidos.append(base.model_copy(update={"severity": pick.severity}))
    return escolhidos, ""
