"""O contrato do roteador de modelos.

REGRA CENTRAL, E ELA E A RAZAO DESTE MODULO EXISTIR: o roteador nunca raciocina
sobre NOMES de modelo. Todo provedor declara capacidades, toda tarefa declara
requisitos, e o roteador resolve a restricao.

Ids de modelo envelhecem — `qwen3:14b` e `gemini-2.5-flash` vao estar errados
em um ano. Se o roteador falasse deles, trocar de modelo seria mudanca de
codigo. Falando de `Capability`, e edicao de YAML.

Ver docs/01-model-routing.md, 1.5.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol


class Capability(StrEnum):
    """O que um provedor sabe fazer. Tarefas pedem, provedores oferecem."""

    TEXT = "text"
    VISION = "vision"
    VIDEO_NATIVE = "video"
    # Decodificacao restrita por JSON Schema de verdade (Ollama `format`,
    # Gemini `response_schema`). NAO inclui `json_object` do OpenAI, que so
    # promete "algum JSON" — esse caso vive em JSON_OBJECT.
    JSON_SCHEMA = "json_schema"
    JSON_OBJECT = "json_object"
    LONG_CTX_32K = "ctx32k"


CostClass = Literal["local", "free_cloud", "paid"]
Privacy = Literal["local_only", "leaves_machine"]
LatencyClass = Literal["batch", "interactive"]

# Abaixo disto, um modelo local nao serve para tarefa interativa. Uma espera de
# 90 segundos numa pergunta de follow-up e funcionalidade quebrada; 90 segundos
# num relatorio em lote esta de bom tamanho.
SLOW_LOCAL_TOK_PER_S = 15.0

# Folga de contexto para a saida. O contexto precisa caber a entrada E a
# resposta; dimensionar so pela entrada faz o modelo truncar no meio do JSON,
# que e a falha mais cara possivel porque consome a cota inteira sem produzir
# nada aproveitavel.
CTX_HEADROOM = 1.4


@dataclass(frozen=True)
class RateLimit:
    """Cota declarada de um provedor, aplicada NO CLIENTE.

    Aplicar do nosso lado em vez de descobrir por 429 nao e delicadeza: um 429
    num tier gratuito costuma custar a janela inteira, entao chegar la ja e a
    falha. O bucket existe para nunca chegar.
    """

    requests: int
    window_s: float
    tokens: int | None = None

    def __post_init__(self) -> None:
        if self.requests <= 0 or self.window_s <= 0:
            raise ValueError("RateLimit precisa de requests e window_s positivos")


@dataclass(frozen=True)
class ProviderProfile:
    """Tudo o que o roteador precisa saber sobre um provedor."""

    name: str  # "ollama:qwen3:14b" — identidade, nao criterio de escolha
    caps: frozenset[Capability]
    ctx_tokens: int
    cost_class: CostClass
    privacy: Privacy
    # Medido na primeira execucao e guardado em cache. Zero significa "ainda
    # nao medido", e o roteador trata isso como desconhecido em vez de lento —
    # penalizar um provedor por falta de medicao o deixaria nunca ser escolhido
    # e portanto nunca medido.
    est_tok_per_s: float = 0.0
    rate_limit: RateLimit | None = None
    latency_preference: LatencyClass | None = None

    def supports(self, task: TaskSpec) -> bool:
        return task.requires <= self.caps and self.ctx_tokens >= task.min_ctx_tokens

    @property
    def leaves_machine(self) -> bool:
        return self.privacy == "leaves_machine"

    @property
    def is_slow_for_interactive(self) -> bool:
        return 0.0 < self.est_tok_per_s < SLOW_LOCAL_TOK_PER_S


@dataclass(frozen=True)
class TaskSpec:
    """O que uma tarefa exige. Nunca menciona provedor."""

    name: str  # "laning_analyst"
    requires: frozenset[Capability]
    est_input_tokens: int
    latency_class: LatencyClass = "batch"

    @property
    def min_ctx_tokens(self) -> int:
        return int(self.est_input_tokens * CTX_HEADROOM)


@dataclass
class Usage:
    """Contabilidade de uma chamada. Alimenta o limitador e a medicao de
    velocidade — sem ela o roteador nunca sai do palpite inicial."""

    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_s: float = 0.0

    @property
    def tok_per_s(self) -> float:
        return self.output_tokens / self.elapsed_s if self.elapsed_s > 0 else 0.0


@dataclass
class Completion:
    text: str
    provider: str
    usage: Usage = field(default_factory=Usage)
    # True quando a saida veio de decodificacao restrita por schema, e nao de
    # um pedido educado no prompt. O loop de reparo usa isto para decidir se
    # vale tentar de novo: um provedor com garantia forte que errou o schema
    # nao vai acertar na segunda.
    schema_enforced: bool = False
    # Quanto de cota de TOKENS o provedor diz que ainda resta nesta janela.
    #
    # Existe porque o nosso balde local comeca cheio a cada execucao, e o do
    # provedor nao: ele e por minuto e compartilhado entre processos. Rodar a
    # analise duas vezes seguidas fazia a segunda sair mandando com o balde
    # local cheio e o real vazio — e levar 429 de cara. Com este numero, o
    # balde local passa a acompanhar a realidade em vez de adivinha-la.
    remaining_tokens: int | None = None


class Provider(Protocol):
    """A interface que um provedor implementa. Uma classe, dois metodos.

    Deliberadamente pequena: 'adicione um provedor gratuito' e uma das boas
    primeiras issues do projeto, e ela so continua boa se a superficie for
    esta."""

    profile: ProviderProfile

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, object] | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> Completion: ...

    async def healthy(self) -> bool: ...


class Clock(Protocol):
    """Relogio injetavel.

    O breaker e o limitador sao maquinas de estado sobre o tempo; testa-los com
    sleeps reais tornaria a suite lenta e intermitente, que sao as duas
    maneiras de um teste deixar de ser rodado.
    """

    def __call__(self) -> float: ...


def monotonic() -> float:
    """Relogio padrao. Monotonico de proposito: o relogio de parede pode andar
    para tras num ajuste de NTP e reabrir um breaker cedo demais."""
    return time.monotonic()
