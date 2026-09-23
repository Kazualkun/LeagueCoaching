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
    # Aberto por COTA (429) ou por falha de verdade? A diferenca decide se
    # vale esperar: cota estourada e o provedor pedindo ritmo, e esperar
    # resolve. Falha de verdade nao melhora com paciencia.
    por_cota: bool = False
    backoff_s: float = BACKOFF_START_S
    # EM QUAIS TAREFAS o schema falhou, e nao quantas vezes ao todo.
    #
    # A contagem simples estava errada de um jeito caro: ela era global e o
    # sucesso nunca a zerava, entao um modelo que acertava nove vezes e
    # tropecava em tres era descartado. Pior: as tres podiam ser do MESMO
    # analista tentando o mesmo prompt dificil, e ai o provedor morria para
    # os outros tres, que nem tinham sido tentados.
    #
    # Falhar em tres tarefas DIFERENTES e evidencia de que o modelo nao
    # sustenta o nosso schema. Falhar tres vezes na mesma e evidencia de que
    # aquele prompt e dificil.
    schema_falhou_em: set[str] = field(default_factory=set)
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
        e.por_cota = False
        # Sucesso e prova de que ele SABE fazer o nosso schema. Sem zerar aqui,
        # tropecos avulsos ao longo de uma sessao longa somariam ate o descarte
        # de um provedor que funciona.
        e.schema_falhou_em.clear()
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
        e.por_cota = True
        e.last_reason = f"cota atingida; liberado em {espera:.0f}s"

    def segundos_ate_fechar_por_cota(self, provider: str) -> float | None:
        """Quanto falta para reabrir, SE o motivo foi cota. None se nao foi.

        Existe porque tratar 429 como falha foi o que quebrou a fila dos
        analistas: o primeiro estourava a cota, o disjuntor abria, e quem
        esperava concluia "nao ha provedor" em vez de "espere tres segundos".
        """
        e = self._entries.get(provider)
        if e is None or not e.por_cota or e.state is not BreakerState.OPEN:
            return None
        return max(0.0, e.open_until - self._clock())

    def record_transient(self, provider: str, detail: str = "") -> None:
        """5xx, timeout, conexao recusada. Recuo exponencial."""
        e = self._entry(provider)
        if e.state is BreakerState.BURNED:
            return
        e.por_cota = False
        if e.state is BreakerState.HALF_OPEN:
            # A sondagem falhou: dobra antes de abrir de novo.
            e.backoff_s = min(e.backoff_s * 2, BACKOFF_MAX_S)
        e.state = BreakerState.OPEN
        e.open_until = self._clock() + e.backoff_s
        e.last_reason = f"falha transitoria ({detail or 'sem detalhe'}); recuo {e.backoff_s:.0f}s"

    def record_schema_violation(self, provider: str, task: str = "", detail: str = "") -> None:
        """O modelo devolveu algo que nao valida contra o nosso schema.

        So descarta o provedor quando ele falha em tarefas DIFERENTES — ver o
        comentario em `_Entry.schema_falhou_em`.
        """
        e = self._entry(provider)
        e.schema_falhou_em.add(task or "?")
        if len(e.schema_falhou_em) >= SCHEMA_STRIKES:
            e.state = BreakerState.BURNED
            e.last_reason = (
                f"este modelo nao sustenta o nosso schema de saida "
                f"(falhou em {len(e.schema_falhou_em)} tarefas diferentes); "
                f"descartado nesta sessao"
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
    """Dois baldes furados no mesmo provedor: requisicoes e tokens.

    OS DOIS VAZAM CONTINUAMENTE, e a versao anterior errava nisso. Ela tratava
    tokens como uma janela fixa que zerava de 60 em 60 segundos, e por isso
    esperava ate um minuto inteiro quando faltavam poucos tokens.

    O cabecalho do proprio Groq desmente a janela fixa:

        x-ratelimit-limit-tokens: 8000
        x-ratelimit-reset-tokens: 659ms

    659 milissegundos para repor o que uma chamada minuscula gastou — isso e
    reposicao continua, a 8000/60 = ~133 tokens por segundo. Com o modelo
    certo, esperar por 4.200 tokens custa ~32 s em vez de ate 60.
    """

    limit: RateLimit
    allowance: float  # requisicoes disponiveis
    tokens_livres: float  # tokens disponiveis
    last: float


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
            limit=limit,
            allowance=float(limit.requests),
            tokens_livres=float(limit.tokens if limit.tokens is not None else 0),
            last=agora,
        )

    def _refill(self, b: _Bucket) -> None:
        agora = self._clock()
        decorrido = agora - b.last
        b.last = agora
        b.allowance = min(
            float(b.limit.requests),
            b.allowance + decorrido * (b.limit.requests / b.limit.window_s),
        )
        if b.limit.tokens is not None:
            b.tokens_livres = min(
                float(b.limit.tokens),
                b.tokens_livres + decorrido * (b.limit.tokens / b.limit.window_s),
            )

    def has_budget(self, provider: str, tokens: int = 0) -> bool:
        """Provedor sem cota declarada tem orcamento infinito — e o caso do
        Ollama local, onde limitar seria so deixar a maquina ociosa."""
        b = self._buckets.get(provider)
        if b is None:
            return True
        self._refill(b)
        if b.allowance < 1.0:
            return False
        return not (b.limit.tokens is not None and tokens > b.tokens_livres)

    def consume(self, provider: str, tokens: int = 0) -> None:
        b = self._buckets.get(provider)
        if b is None:
            return
        self._refill(b)
        b.allowance = max(0.0, b.allowance - 1.0)
        if b.limit.tokens is not None:
            # Pode ficar negativo: uma chamada que gastou mais do que se
            # estimou deixa divida, e a divida atrasa a proxima. Zerar aqui
            # perdoaria o excesso e levaria a um 429 de verdade.
            b.tokens_livres -= tokens

    def sincronizar_tokens(self, provider: str, restantes: int) -> None:
        """Adota o saldo que o PROVEDOR informou.

        O balde local comeca cheio a cada execucao; o do provedor nao, porque
        e por minuto e compartilhado entre processos. Rodar a analise duas
        vezes seguidas fazia a segunda sair mandando com o balde local cheio e
        o real vazio, e levar 429 de cara.

        So aceita para MENOS. O cabecalho e lido depois da resposta, entao ele
        ja esta velho quando chega; deixa-lo aumentar o saldo desfaria um
        consumo que acabou de acontecer.
        """
        b = self._buckets.get(provider)
        if b is None or b.limit.tokens is None:
            return
        self._refill(b)
        b.tokens_livres = min(b.tokens_livres, float(max(0, restantes)))

    def seconds_until_budget(self, provider: str, tokens: int = 0) -> float:
        """Quanto falta ate caber uma chamada de `tokens`.

        CONSIDERA OS DOIS TETOS, e essa foi a falha: a versao anterior olhava
        so a fila de requisicoes e devolvia 0 sempre que sobrava requisicao —
        mesmo com a cota de TOKENS estourada. Quem esperava por esse numero
        nao esperava nada, tentava de novo na hora, e falhava igual.

        Os dois prazos se somam por `max`, nao por `min`: a chamada so cabe
        quando AMBOS os tetos liberam.
        """
        b = self._buckets.get(provider)
        if b is None:
            return 0.0
        self._refill(b)
        prazos: list[float] = []
        if b.allowance < 1.0:
            taxa = b.limit.requests / b.limit.window_s
            prazos.append((1.0 - b.allowance) / taxa)
        if b.limit.tokens is not None and tokens > b.tokens_livres:
            taxa = b.limit.tokens / b.limit.window_s
            prazos.append(max(0.0, (tokens - b.tokens_livres) / taxa))
        return max(prazos) if prazos else 0.0


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
