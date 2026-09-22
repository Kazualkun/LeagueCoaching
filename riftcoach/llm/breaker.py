"""Circuit breaker e limitador de taxa, por provedor.

O breaker e CIENTE DO TIPO DE FALHA, e essa e a decisao inteira do modulo. Um
breaker generico trata tudo como "deu erro, tenta de novo daqui a pouco", e as
tres falhas que realmente acontecem pedem respostas opostas:

  429 / cota          esperar ate a janela resetar. Tentar antes gasta a cota
                      de novo e costuma reiniciar a punicao.
  5xx / timeout       recuo exponencial. Costuma ser transitorio.
  schema violado 3x   desistir DESTE modelo para sempre nesta sessao.

O terceiro caso importa mais do que parece: um modelo de tier gratuito que nao
sustenta o nosso JSON vai queimar a cota diaria inteira do usuario em
retentativas e nao produzir nada. Detectar em tres tentativas e descartar e o
que separa "o RiftCoach nao funcionou hoje" de "o modelo X nao serve, usei o Y".

Ver docs/01-model-routing.md, 1.5.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from riftcoach.config import data_dir
from riftcoach.llm.base import Clock, RateLimit, monotonic

# Recuo para falhas transitorias: 30s, dobrando ate o teto.
BACKOFF_START_S = 30.0
BACKOFF_MAX_S = 300.0

# Violacoes de schema toleradas antes de descartar o provedor. Tres porque o
# loop de reparo ja tem duas tentativas: se nem com os erros de validacao
# realimentados literalmente o modelo acerta, ele nao sustenta o schema.
SCHEMA_STRIKES = 3


class BreakerState(StrEnum):
    CLOSED = "closed"  # passando
    OPEN = "open"  # bloqueado
    HALF_OPEN = "half_open"  # deixa UMA sondagem passar
    BURNED = "burned"  # descartado nesta sessao, nao volta


@dataclass
class _Entry:
    state: BreakerState = BreakerState.CLOSED
    open_until: float = 0.0
    backoff_s: float = BACKOFF_START_S
    schema_violations: int = 0
    last_reason: str = ""


class ProviderBreaker:
    """Estado por provedor. Em memoria: o processo e curto e a cota nao e."""

    def __init__(self, clock: Clock = monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, _Entry] = {}

    def _entry(self, provider: str) -> _Entry:
        return self._entries.setdefault(provider, _Entry())

    def state(self, provider: str) -> BreakerState:
        e = self._entry(provider)
        if e.state is BreakerState.OPEN and self._clock() >= e.open_until:
            # A janela venceu: uma sondagem passa. Se ela falhar, o recuo
            # dobra; se passar, o breaker fecha de vez.
            e.state = BreakerState.HALF_OPEN
        return e.state

    def is_closed(self, provider: str) -> bool:
        """O roteador pergunta isto. HALF_OPEN conta como disponivel — e assim
        que a sondagem acontece."""
        return self.state(provider) in (BreakerState.CLOSED, BreakerState.HALF_OPEN)

    def reason(self, provider: str) -> str:
        return self._entry(provider).last_reason

    # ------------------------------------------------------------------
    # Registro de resultado
    # ------------------------------------------------------------------

    def record_success(self, provider: str) -> None:
        e = self._entry(provider)
        if e.state is BreakerState.BURNED:
            # Descartado por schema nao volta por sucesso em outra coisa: o
            # problema nao era disponibilidade.
            return
        e.state = BreakerState.CLOSED
        e.backoff_s = BACKOFF_START_S
        e.open_until = 0.0
        e.last_reason = ""

    def record_quota(self, provider: str, retry_after_s: float | None) -> None:
        """429 ou cota estourada.

        Sem `Retry-After`, esperamos a janela inteira em vez de recuar pouco:
        num tier gratuito, errar para menos custa mais uma requisicao perdida e
        as vezes reinicia a punicao.
        """
        e = self._entry(provider)
        if e.state is BreakerState.BURNED:
            return
        espera = retry_after_s if retry_after_s and retry_after_s > 0 else BACKOFF_MAX_S
        e.state = BreakerState.OPEN
        e.open_until = self._clock() + espera
        e.last_reason = f"cota atingida; liberado em {espera:.0f}s"

    def record_transient(self, provider: str, detail: str = "") -> None:
        """5xx, timeout, conexao recusada. Recuo exponencial."""
        e = self._entry(provider)
        if e.state is BreakerState.BURNED:
            return
        if e.state is BreakerState.HALF_OPEN:
            # A sondagem falhou: dobra antes de abrir de novo.
            e.backoff_s = min(e.backoff_s * 2, BACKOFF_MAX_S)
        e.state = BreakerState.OPEN
        e.open_until = self._clock() + e.backoff_s
        e.last_reason = f"falha transitoria ({detail or 'sem detalhe'}); recuo {e.backoff_s:.0f}s"

    def record_schema_violation(self, provider: str, detail: str = "") -> None:
        """O modelo devolveu algo que nao valida contra o nosso schema."""
        e = self._entry(provider)
        e.schema_violations += 1
        if e.schema_violations >= SCHEMA_STRIKES:
            e.state = BreakerState.BURNED
            e.last_reason = (
                f"este modelo nao sustenta o nosso schema de saida "
                f"({e.schema_violations} violacoes); descartado nesta sessao"
            )
            log_compatibility(provider, "schema_exhausted", detail)

    def burn(self, provider: str, reason: str) -> None:
        """Descarte explicito — usado quando falta chave de API, por exemplo.
        Sem isto, o roteador tentaria o mesmo provedor sem chave a cada tarefa.
        """
        e = self._entry(provider)
        e.state = BreakerState.BURNED
        e.last_reason = reason

    def diagnose(self) -> dict[str, str]:
        """Por que cada provedor esta fora. Vira a `hint` de NoViableProvider —
        e um erro de roteamento que nao diz o que falta e so um 500 bonito."""
        return {
            nome: e.last_reason or e.state.value
            for nome, e in self._entries.items()
            if e.state is not BreakerState.CLOSED
        }


# --------------------------------------------------------------------------
# Limitador
# --------------------------------------------------------------------------


@dataclass
class _Bucket:
    limit: RateLimit
    allowance: float
    last: float
    spent_tokens: int = 0
    window_started: float = 0.0


class RateLimiter:
    """Token bucket por provedor, aplicado antes de sair da maquina."""

    def __init__(self, clock: Clock = monotonic) -> None:
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}

    def register(self, provider: str, limit: RateLimit | None) -> None:
        if limit is None:
            return
        agora = self._clock()
        self._buckets[provider] = _Bucket(
            limit=limit, allowance=float(limit.requests), last=agora, window_started=agora
        )

    def _refill(self, b: _Bucket) -> None:
        agora = self._clock()
        decorrido = agora - b.last
        b.last = agora
        taxa = b.limit.requests / b.limit.window_s
        b.allowance = min(float(b.limit.requests), b.allowance + decorrido * taxa)
        if agora - b.window_started >= b.limit.window_s:
            b.window_started = agora
            b.spent_tokens = 0

    def has_budget(self, provider: str, tokens: int = 0) -> bool:
        """Provedor sem cota declarada tem orcamento infinito — e o caso do
        Ollama local, onde limitar seria so deixar a maquina ociosa."""
        b = self._buckets.get(provider)
        if b is None:
            return True
        self._refill(b)
        if b.allowance < 1.0:
            return False
        return not (b.limit.tokens is not None and b.spent_tokens + tokens > b.limit.tokens)

    def consume(self, provider: str, tokens: int = 0) -> None:
        b = self._buckets.get(provider)
        if b is None:
            return
        self._refill(b)
        b.allowance = max(0.0, b.allowance - 1.0)
        b.spent_tokens += tokens

    def seconds_until_budget(self, provider: str) -> float:
        b = self._buckets.get(provider)
        if b is None:
            return 0.0
        self._refill(b)
        if b.allowance >= 1.0:
            return 0.0
        taxa = b.limit.requests / b.limit.window_s
        return (1.0 - b.allowance) / taxa


# --------------------------------------------------------------------------
# Matriz de compatibilidade
# --------------------------------------------------------------------------


def compat_log_path() -> Path:
    return data_dir() / "compat.jsonl"


def log_compatibility(provider: str, outcome: str, detail: str = "") -> None:
    """Registra um veredito sobre um modelo, em append.

    A matriz publicada de "quais modelos gratuitos realmente funcionam" e,
    provavelmente, o artefato mais util que o projeto vai produzir para os
    usuarios — e ela so existe se cada instalacao registrar os proprios
    resultados. Ninguem consegue testar trinta provedores sozinho.

    NAO grava conteudo de prompt nem de resposta: so o veredito. Este arquivo
    precisa poder ser colado numa issue sem revisao.
    """
    import time as _time

    linha = {
        "ts": int(_time.time()),
        "provider": provider,
        "outcome": outcome,
        "detail": detail[:200],
    }
    try:
        with compat_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    except OSError:
        # Registro de telemetria local nunca derruba uma analise.
        pass


@dataclass
class Guard:
    """Breaker + limitador juntos, que e como o roteador sempre os usa."""

    breaker: ProviderBreaker = field(default_factory=ProviderBreaker)
    limiter: RateLimiter = field(default_factory=RateLimiter)

    def available(self, provider: str, tokens: int = 0) -> bool:
        return self.breaker.is_closed(provider) and self.limiter.has_budget(provider, tokens)
