"""Perguntar sobre a partida, e receber resposta em texto.

Separado dos analistas por uma diferenca de natureza: um analista PROCURA o
que esta errado e devolve JSON validado; aqui o jogador ja sabe o que quer
saber, e o que ele quer de volta e uma frase, nao um schema.

O CONTEXTO VAI INTEIRO, e isso foi medido antes de decidir. A partida
completa renderiza em ~1.227 tokens:

    header 42 · lane 192 · deaths 129 · kills 174 · recalls 278
    objectives 217 · build 99 · vision 49 · tempo 41

Recortar so a secao "relacionada" economizaria uns 900 tokens e pioraria as
respostas, porque pergunta de jogador atravessa secoes o tempo todo: "por que
eu morri as 18:15" costuma se explicar pelo recall anterior, pelo item que
faltava ou pelo objetivo que estava nascendo. Com pergunta e sistema, uma
chamada fica em ~2.000 tokens — contra o teto de 8.000/min do Groq, da umas
tres perguntas por minuto.
"""

from __future__ import annotations

from dataclasses import dataclass

from riftcoach.analysis.analysts import load_prompt
from riftcoach.core.errors import RiftCoachError
from riftcoach.core.schema import Finding
from riftcoach.llm.router import ModelRouter, text_task
from riftcoach.parse import render as facts_render
from riftcoach.parse.distill import mmss
from riftcoach.parse.facts import MatchFacts

# Medido: contexto ~1.227 + prompt de sistema ~450 + pergunta. A estimativa
# alimenta o limitador de cota, entao errar para mais e melhor que para menos:
# subestimar faz o roteador mandar uma chamada que nao cabe e levar 429.
CUSTO_ESTIMADO = 2_000

# A pergunta e entrada de usuario e entra num prompt: sem teto, uma colagem
# acidental de dez mil caracteres estoura a cota da janela inteira.
LIMITE_DA_PERGUNTA = 600


@dataclass(frozen=True)
class Resposta:
    texto: str
    provedor: str


def _momento_em_foco(finding: Finding | None, momento_ms: int | None) -> str:
    """De onde a pergunta esta sendo feita.

    Vai separado do resto do contexto de proposito: sem isto o modelo recebe a
    partida inteira e nao sabe de qual dos cinco erros o jogador esta falando
    quando ele pergunta so "por que isso foi ruim?".

    Duas formas porque ha duas origens. Na pagina do relatorio a pergunta nasce
    grudada num finding e o contexto pode ser rico. No overlay ela nasce de um
    replay parado num instante qualquer — ali so existe o relogio, e mandar o
    relogio ja resolve, porque a timeline da partida esta toda no contexto.
    """
    if finding is not None:
        return "\n".join(
            [
                "=== MOMENTO EM FOCO (a pergunta e sobre este) ===",
                f"{mmss(finding.timestamp_ms)} · {finding.category} · gravidade {finding.severity}",
                f"o que o sistema apontou: {finding.claim}",
                f"correcao sugerida: {finding.fix}",
            ]
        )
    if momento_ms is not None:
        return (
            "=== MOMENTO EM FOCO (a pergunta e sobre este) ===\n"
            f"o jogador esta com o replay parado em {mmss(momento_ms)}"
        )
    return ""


def montar_prompt(
    facts: MatchFacts,
    pergunta: str,
    *,
    finding: Finding | None = None,
    momento_ms: int | None = None,
    resolver: facts_render.NameResolver | None = None,
) -> str:
    """O prompt completo. Separado de `responder` para poder ser medido."""
    partes = [
        "=== DADOS DA PARTIDA ===",
        facts_render.render(facts, None, resolver).strip(),
        _momento_em_foco(finding, momento_ms),
        "=== PERGUNTA DO JOGADOR ===",
        pergunta.strip(),
    ]
    return "\n\n".join(p for p in partes if p)


async def responder(
    router: ModelRouter,
    facts: MatchFacts,
    pergunta: str,
    *,
    finding: Finding | None = None,
    momento_ms: int | None = None,
    resolver: facts_render.NameResolver | None = None,
) -> Resposta:
    """Responde uma pergunta sobre esta partida.

    `interactive=True` na tarefa nao e detalhe: o roteador ordena os provedores
    por latencia nessa classe. Quem esta esperando uma resposta na tela tolera
    muito menos que um passe de analise que roda sozinho.
    """
    texto = pergunta.strip()
    if not texto:
        raise RiftCoachError(
            "pergunta vazia",
            hint="Escreva o que voce quer saber sobre a partida.",
        )
    if len(texto) > LIMITE_DA_PERGUNTA:
        raise RiftCoachError(
            f"pergunta longa demais ({len(texto)} caracteres, o limite e {LIMITE_DA_PERGUNTA})",
            hint="Pergunte uma coisa de cada vez — respostas ficam melhores assim.",
        )

    tarefa = text_task("pergunta", CUSTO_ESTIMADO, interactive=True)
    saida = await router.complete(
        tarefa,
        montar_prompt(facts, texto, finding=finding, momento_ms=momento_ms, resolver=resolver),
        system=load_prompt("pergunta"),
    )
    return Resposta(texto=saida.text.strip(), provedor=saida.provider)
