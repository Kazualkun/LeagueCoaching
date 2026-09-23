"""Testes do roteamento de modelos.

Tres invariantes valem mais que todo o resto aqui, e sao as que, se quebrarem,
quebram em silencio:

  1. privacy_mode=strict NUNCA deixa passar um provedor de nuvem.
  2. Falta de provedor NUNCA vira um erro mudo — a hint tem que nomear a acao
     que resolve.
  3. Falha de IA NUNCA derruba o relatorio: o pior caso e o L5.

O relogio e injetado em tudo que depende de tempo. Testar breaker com sleep
real deixaria a suite lenta e intermitente, que sao as duas formas de um teste
deixar de ser rodado.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from riftcoach.analysis.analysts import (
    AnalystEvidence,
    AnalystFinding,
    AnalystOutput,
    load_prompt,
    to_findings,
)
from riftcoach.core.errors import NoViableProvider, SchemaExhausted
from riftcoach.llm.base import (
    Capability,
    ProviderProfile,
    RateLimit,
    TaskSpec,
)
from riftcoach.llm.breaker import (
    BACKOFF_START_S,
    SCHEMA_STRIKES,
    BreakerState,
    ProviderBreaker,
    RateLimiter,
)
from riftcoach.llm.catalog import (
    default_providers,
    load_providers,
    resolve_text_model,
    text_model_for,
)
from riftcoach.llm.hardware import _tier_for
from riftcoach.llm.providers.openai_compat import OpenAICompatProvider, ProviderCallError
from riftcoach.llm.router import ModelRouter, RoutingPolicy, _extract_json, text_task

TEXT = frozenset({Capability.TEXT})


class FakeClock:
    """Relogio controlado pelo teste."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _profile(
    name: str,
    *,
    cost: str = "free_cloud",
    privacy: str = "leaves_machine",
    speed: float = 50.0,
    ctx: int = 32_768,
    caps: frozenset[Capability] = TEXT,
    latency: str | None = None,
) -> ProviderProfile:
    return ProviderProfile(
        name=name,
        caps=caps,
        ctx_tokens=ctx,
        cost_class=cost,  # type: ignore[arg-type]
        privacy=privacy,  # type: ignore[arg-type]
        est_tok_per_s=speed,
        latency_preference=latency,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------


def test_a_fresh_provider_is_usable() -> None:
    b = ProviderBreaker(FakeClock())
    assert b.is_closed("groq")
    assert b.state("groq") is BreakerState.CLOSED


def test_quota_blocks_until_the_window_resets() -> None:
    """Num tier gratuito, tentar antes da janela costuma gastar a cota de novo
    e as vezes reiniciar a punicao."""
    c = FakeClock()
    b = ProviderBreaker(c)
    b.record_quota("groq", retry_after_s=60.0)
    assert not b.is_closed("groq")

    c.advance(59)
    assert not b.is_closed("groq")

    c.advance(2)
    assert b.is_closed("groq"), "passada a janela, uma sondagem precisa poder passar"


def test_quota_without_retry_after_waits_long_not_short() -> None:
    """Errar para menos custa mais uma requisicao perdida; errar para mais
    custa um pouco de vazao. A assimetria decide o padrao."""
    c = FakeClock()
    b = ProviderBreaker(c)
    b.record_quota("gemini", retry_after_s=None)
    c.advance(120)
    assert not b.is_closed("gemini")


def test_transient_failures_back_off_exponentially() -> None:
    c = FakeClock()
    b = ProviderBreaker(c)

    b.record_transient("ollama", "connection refused")
    c.advance(BACKOFF_START_S + 1)
    assert b.state("ollama") is BreakerState.HALF_OPEN

    # A sondagem falha: o recuo dobra antes de reabrir.
    b.record_transient("ollama", "de novo")
    c.advance(BACKOFF_START_S + 1)
    assert not b.is_closed("ollama"), "o recuo tinha que ter dobrado"
    c.advance(BACKOFF_START_S + 1)
    assert b.is_closed("ollama")


def test_success_resets_the_backoff() -> None:
    c = FakeClock()
    b = ProviderBreaker(c)
    b.record_transient("ollama")
    c.advance(BACKOFF_START_S + 1)
    b.record_success("ollama")
    assert b.state("ollama") is BreakerState.CLOSED

    b.record_transient("ollama")
    c.advance(BACKOFF_START_S + 1)
    assert b.is_closed("ollama"), "o recuo devia ter voltado ao inicial"


def test_falhar_o_schema_em_tarefas_diferentes_queima_o_modelo() -> None:
    """O caso que mais importa: um modelo que nao sustenta o nosso JSON queima
    a cota diaria inteira em retentativas e nao produz nada."""
    b = ProviderBreaker(FakeClock())
    for tarefa in ("laning_analyst", "macro_analyst", "fights_analyst"):
        b.record_schema_violation("groq:llama", tarefa, "campo severity invalido")
    assert b.state("groq:llama") is BreakerState.BURNED
    assert not b.is_closed("groq:llama")
    assert "schema" in b.reason("groq:llama")


def test_falhar_tres_vezes_na_MESMA_tarefa_nao_queima() -> None:
    """A regra antiga contava violacoes, nao tarefas — e era cara: os tres
    tropecos podiam ser do MESMO analista insistindo no mesmo prompt dificil,
    e o provedor morria para os outros tres, que nem tinham sido tentados.

    Tres falhas na mesma tarefa sao evidencia de que aquele prompt e dificil.
    Tres em tarefas diferentes sao evidencia sobre o modelo.
    """
    b = ProviderBreaker(FakeClock())
    for _ in range(SCHEMA_STRIKES + 2):
        b.record_schema_violation("groq:llama", "laning_analyst")
    assert b.state("groq:llama") is not BreakerState.BURNED
    assert b.is_closed("groq:llama")


def test_um_sucesso_limpa_os_tropecos_anteriores() -> None:
    """Sucesso e prova de que ele SABE fazer o nosso schema. Sem zerar, falhas
    avulsas ao longo de uma sessao longa somariam ate descartar um provedor
    que funciona."""
    b = ProviderBreaker(FakeClock())
    b.record_schema_violation("groq:llama", "laning_analyst")
    b.record_schema_violation("groq:llama", "macro_analyst")
    b.record_success("groq:llama")
    b.record_schema_violation("groq:llama", "fights_analyst")
    assert b.is_closed("groq:llama"), "a contagem recomecou depois do sucesso"


def test_a_burned_model_does_not_come_back_on_success() -> None:
    """O problema nao era disponibilidade, era capacidade. Sucesso em outra
    coisa nao prova que ele passou a sustentar o schema."""
    b = ProviderBreaker(FakeClock())
    for tarefa in ("a", "b", "c"):
        b.record_schema_violation("groq:llama", tarefa)
    b.record_success("groq:llama")
    assert b.state("groq:llama") is BreakerState.BURNED


def test_diagnose_names_what_is_wrong() -> None:
    b = ProviderBreaker(FakeClock())
    b.record_quota("gemini", 60.0)
    b.burn("groq", "sem chave")
    d = b.diagnose()
    assert "cota" in d["gemini"]
    assert d["groq"] == "sem chave"


# --------------------------------------------------------------------------
# Limitador
# --------------------------------------------------------------------------


def test_a_provider_without_declared_quota_is_unlimited() -> None:
    """E o caso do Ollama local: limitar seria so deixar a maquina ociosa."""
    lim = RateLimiter(FakeClock())
    lim.register("ollama", None)
    for _ in range(1000):
        lim.consume("ollama")
    assert lim.has_budget("ollama")


def test_the_bucket_runs_out_and_refills_over_time() -> None:
    c = FakeClock()
    lim = RateLimiter(c)
    lim.register("groq", RateLimit(requests=3, window_s=60.0))

    for _ in range(3):
        assert lim.has_budget("groq")
        lim.consume("groq")
    assert not lim.has_budget("groq")

    c.advance(21)  # 3 req / 60s = 1 a cada 20s
    assert lim.has_budget("groq")


def test_it_says_how_long_until_budget() -> None:
    c = FakeClock()
    lim = RateLimiter(c)
    lim.register("groq", RateLimit(requests=2, window_s=60.0))
    lim.consume("groq")
    lim.consume("groq")
    espera = lim.seconds_until_budget("groq")
    assert 0 < espera <= 30


def test_rate_limit_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        RateLimit(requests=0, window_s=60.0)


# --------------------------------------------------------------------------
# Politica de roteamento
# --------------------------------------------------------------------------


def test_strict_privacy_blocks_every_cloud_provider() -> None:
    """A invariante mais importante do modulo. Nao e desempate: e filtro, e
    esvaziar o pool e um resultado aceitavel."""
    p = RoutingPolicy(privacy_mode="strict")
    assert not p.allows(_profile("gemini", privacy="leaves_machine"))
    assert p.allows(_profile("ollama", cost="local", privacy="local_only"))


def test_relaxed_privacy_allows_cloud() -> None:
    p = RoutingPolicy(privacy_mode="relaxed")
    assert p.allows(_profile("gemini"))


def test_local_is_preferred_over_free_cloud() -> None:
    """Nao e so economia: local nao tem cota, entao trabalho em lote nunca
    queima a franquia que o caminho interativo precisa."""
    p = RoutingPolicy()
    t = text_task("relatorio", 1200)
    local = _profile("ollama", cost="local", privacy="local_only")
    nuvem = _profile("groq")
    assert p.rank(local, t) < p.rank(nuvem, t)


def test_a_slow_local_loses_for_interactive_work() -> None:
    """Uma espera de 90 segundos numa pergunta de follow-up e funcionalidade
    quebrada; 90 segundos num relatorio em lote esta de bom tamanho."""
    p = RoutingPolicy()
    lento = _profile("ollama", cost="local", privacy="local_only", speed=5.0)
    nuvem = _profile("groq", speed=300.0, latency="interactive")

    lote = text_task("relatorio", 1200, interactive=False)
    assert p.rank(lento, lote) < p.rank(nuvem, lote), "em lote, o local ganha"

    agora = text_task("followup", 800, interactive=True)
    assert p.rank(nuvem, agora) < p.rank(lento, agora), "interativo inverte"


def test_a_fast_local_keeps_winning_even_interactively() -> None:
    p = RoutingPolicy()
    rapido = _profile("ollama", cost="local", privacy="local_only", speed=80.0)
    nuvem = _profile("groq", speed=300.0)
    agora = text_task("followup", 800, interactive=True)
    assert p.rank(rapido, agora) < p.rank(nuvem, agora)


def test_throughput_breaks_ties() -> None:
    p = RoutingPolicy()
    t = text_task("relatorio", 1200)
    devagar = _profile("a", speed=20.0)
    rapido = _profile("b", speed=200.0)
    assert p.rank(rapido, t) < p.rank(devagar, t)


# --------------------------------------------------------------------------
# Capacidades e contexto
# --------------------------------------------------------------------------


def test_a_task_needs_every_capability_it_asks_for() -> None:
    so_texto = _profile("groq", caps=frozenset({Capability.TEXT}))
    visao = TaskSpec(
        name="vod", requires=frozenset({Capability.TEXT, Capability.VISION}), est_input_tokens=1000
    )
    assert not so_texto.supports(visao)


def test_context_must_fit_input_plus_output() -> None:
    """Dimensionar so pela entrada faz o modelo truncar no meio do JSON — a
    falha mais cara possivel, porque gasta a cota sem produzir nada."""
    pequeno = _profile("mini", ctx=8_000)
    tarefa = text_task("grande", 7_000)
    assert tarefa.min_ctx_tokens > 8_000
    assert not pequeno.supports(tarefa)


# --------------------------------------------------------------------------
# Selecao no roteador
# --------------------------------------------------------------------------


class FakeProvider:
    """Provedor de mentira.

    Duas formas de programar a resposta:

      respostas   uma fila, consumida em ordem
      by_prompt   {trecho do prompt: resposta} — necessario quando os quatro
                  analistas compartilham o provedor e o teste precisa que um
                  deles falhe sem afetar os outros
    """

    def __init__(
        self,
        profile: ProviderProfile,
        respostas: list[str | Exception] | None = None,
        by_prompt: dict[str, str] | None = None,
    ) -> None:
        self.profile = profile
        self._respostas = list(respostas or [])
        self._by_prompt = by_prompt or {}
        self.chamadas = 0

    async def complete(self, prompt, *, schema=None, system=None, max_tokens=2048):  # type: ignore[no-untyped-def]
        from riftcoach.llm.base import Completion, Usage

        self.chamadas += 1
        r: str | Exception = '{"findings": []}'
        for trecho, resposta in self._by_prompt.items():
            if trecho in prompt:
                r = resposta
                break
        else:
            if self._respostas:
                r = self._respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return Completion(
            text=r,
            provider=self.profile.name,
            usage=Usage(input_tokens=100, output_tokens=50, elapsed_s=1.0),
            schema_enforced=True,
        )

    async def healthy(self) -> bool:
        return True

    async def __aexit__(self, *exc: object) -> None:
        return None


def _router(*providers: FakeProvider, privacy: str = "relaxed") -> ModelRouter:
    return ModelRouter(
        providers=list(providers),  # type: ignore[arg-type]
        policy=RoutingPolicy(privacy_mode=privacy),
    )


def test_no_provider_raises_with_an_actionable_hint() -> None:
    """Um erro de roteamento que nao diz o que falta e so um 500 bonito."""
    r = _router()
    with pytest.raises(NoViableProvider) as ex:
        r.select(text_task("laning_analyst", 1400))
    assert ex.value.hint
    assert "sem IA" in ex.value.hint or "relatorio" in ex.value.hint.lower()


def test_strict_mode_empties_the_pool_instead_of_leaking() -> None:
    nuvem = FakeProvider(_profile("gemini"), [])
    r = _router(nuvem, privacy="strict")
    assert not r.available
    with pytest.raises(NoViableProvider) as ex:
        r.select(text_task("laning_analyst", 1400))
    assert "strict" in (ex.value.hint or "")


@pytest.mark.asyncio
async def test_a_failing_provider_fails_over_to_the_next() -> None:
    ruim = FakeProvider(
        _profile("groq", speed=10.0),
        [ProviderCallError("500", kind="transient")],
    )
    bom = FakeProvider(_profile("gemini", speed=5.0), ["tudo certo"])
    r = _router(ruim, bom)

    out = await r.complete(text_task("t", 500), "oi")
    assert out.text == "tudo certo"
    assert ruim.chamadas == 1 and bom.chamadas == 1


@pytest.mark.asyncio
async def test_a_bad_key_burns_the_provider_instead_of_retrying() -> None:
    """401 nao melhora com retentativa. Sem o descarte, o roteador gastaria a
    sessao inteira batendo na mesma porta."""
    sem_chave = FakeProvider(_profile("groq"), [ProviderCallError("401", kind="fatal", status=401)])
    bom = FakeProvider(_profile("gemini", speed=1.0), ["ok"])
    r = _router(sem_chave, bom)

    await r.complete(text_task("t", 500), "oi")
    assert r.guard.breaker.state("groq") is BreakerState.BURNED


@pytest.mark.asyncio
async def test_validated_output_repairs_then_succeeds() -> None:
    """Realimentar os erros do pydantic literalmente funciona melhor que
    'formato invalido': o erro ja esta na linguagem do schema."""
    p = FakeProvider(
        _profile("groq"),
        [
            '{"findings": [{"category": "wave", "severity": 99}]}',  # invalido
            '{"findings": []}',  # valido
        ],
    )
    r = _router(p)
    out = await r.complete_validated(text_task("t", 500), "prompt", AnalystOutput)
    assert out.findings == []
    assert p.chamadas == 2


@pytest.mark.asyncio
async def test_a_model_that_never_validates_is_given_up_on() -> None:
    """Uma tarefa que nao valida desiste dela — e NAO condena o provedor.

    Condenar aqui foi o que fez um analista dificil derrubar os outros tres.
    """
    p = FakeProvider(_profile("groq"), ["nao sou json"] * 5)
    r = _router(p)
    with pytest.raises(SchemaExhausted) as ex:
        await r.complete_validated(text_task("t", 500), "prompt", AnalystOutput)
    assert "sem IA" in (ex.value.hint or "") or "L5" in (ex.value.hint or "")
    assert r.guard.breaker.is_closed("groq"), "o provedor continua servindo outras tarefas"


@pytest.mark.asyncio
async def test_um_analista_dificil_nao_derruba_os_outros() -> None:
    """O sintoma real: `laning` falhava o schema tres vezes e os analistas
    seguintes liam "nenhum provedor atende a tarefa"."""
    p = FakeProvider(_profile("groq"), ["nao sou json"] * 3 + ['{"findings": []}'])
    r = _router(p)
    with pytest.raises(SchemaExhausted):
        await r.complete_validated(text_task("laning_analyst", 500), "p", AnalystOutput)

    # O proximo analista tem de conseguir rodar.
    out = await r.complete_validated(text_task("fights_analyst", 500), "p", AnalystOutput)
    assert out.findings == []


@pytest.mark.asyncio
async def test_speed_is_learned_from_real_calls() -> None:
    """Sem medir, o roteador nunca sai do palpite inicial e a regra de latencia
    interativa nunca dispara — ela depende de saber se o local e lento."""
    p = FakeProvider(_profile("groq", speed=0.0), ["ok"])
    r = _router(p)
    await r.complete(text_task("t", 500), "oi")
    assert p.profile.est_tok_per_s > 0


# --------------------------------------------------------------------------
# Extracao de JSON
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bruto",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '```\n{"a": 1}\n```',
        'Claro! Aqui esta:\n{"a": 1}',
    ],
)
def test_json_survives_the_wrapping_models_add(bruto: str) -> None:
    """Provedores de garantia fraca embrulham em cerca ou prosa mesmo
    instruidos a nao fazer. Recortar e mais barato que gastar uma retentativa
    inteira com um JSON que estava correto dentro da cerca."""
    import json

    assert json.loads(_extract_json(bruto)) == {"a": 1}


# --------------------------------------------------------------------------
# O cliente HTTP de verdade
# --------------------------------------------------------------------------


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    return next(c for c in default_providers() if c.name == "groq")


@respx.mock
@pytest.mark.asyncio
async def test_the_client_parses_a_normal_response(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"findings": []}'}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 40},
            },
        )
    )
    async with OpenAICompatProvider(cfg, "llama-3.3-70b-versatile", api_key="k") as p:
        out = await p.complete("oi")
    assert out.text == '{"findings": []}'
    assert out.usage.input_tokens == 900


@respx.mock
@pytest.mark.asyncio
async def test_a_429_is_classified_as_quota_with_retry_after(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(429, headers={"retry-after": "42"}, text="slow down")
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        with pytest.raises(ProviderCallError) as ex:
            await p.complete("oi")
    assert ex.value.kind == "quota"
    assert ex.value.retry_after_s == 42.0


@respx.mock
@pytest.mark.asyncio
async def test_a_401_is_fatal_not_transient(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )
    async with OpenAICompatProvider(cfg, "m", api_key="errada") as p:
        with pytest.raises(ProviderCallError) as ex:
            await p.complete("oi")
    assert ex.value.kind == "fatal"


@respx.mock
@pytest.mark.asyncio
async def test_a_500_is_transient(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(503, text="upstream down")
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        with pytest.raises(ProviderCallError) as ex:
            await p.complete("oi")
    assert ex.value.kind == "transient"


@respx.mock
@pytest.mark.asyncio
async def test_strong_schema_providers_are_marked_as_such(cfg) -> None:  # type: ignore[no-untyped-def]
    """Quem tem `json_schema` promete o NOSSO JSON; quem so tem `json_object`
    promete JSON sintatico e nada mais.

    O Groq mudou de lado: hoje ele tem `json_schema`, e isso foi conferido
    contra a API de verdade com `AnalystOutput` — `$defs` e tudo — que volta
    valido de primeira. Enquanto este teste afirmava o contrario, o roteador
    pedia `json_object` e levava 400, porque o Groq exige a palavra "json" nas
    mensagens nesse modo.
    """
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        out = await p.complete("oi", schema={"type": "object"})
    assert out.schema_enforced, "groq passou a ter json_schema"

    for nome in ("ollama", "gemini", "groq"):
        c = next(x for x in default_providers() if x.name == nome)
        assert Capability.JSON_SCHEMA in c.caps

    # Quem so tem `json_object` continua sendo garantia fraca.
    fraco = next(c for c in default_providers() if c.name == "mistral")
    assert Capability.JSON_SCHEMA not in fraco.caps


def test_o_groq_nao_pede_strict_no_schema() -> None:
    """`strict` do jeito da OpenAI exige `additionalProperties: false` em todo
    objeto; o schema do pydantic nao e assim, e o Groq responde 400 — conferido
    contra a API: `/$defs/AnalystEvidence: additionalProperties:false must be
    set on every object`."""
    groq = next(c for c in default_providers() if c.name == "groq")
    assert groq.schema_strict is False

    ollama = next(c for c in default_providers() if c.name == "ollama")
    assert ollama.schema_strict is True


@respx.mock
@pytest.mark.asyncio
async def test_modo_json_fraco_garante_a_palavra_json() -> None:
    """O modo `json_object` exige a palavra "json" nas mensagens — regra da
    OpenAI que os compativeis copiaram. Os nossos prompts sao em portugues e
    nem sempre a contem, e a falta de UMA PALAVRA derrubava a analise inteira
    com um 400 que chegava ao usuario disfarcado de "nenhum provedor atende a
    tarefa"."""
    capturado: dict[str, object] = {}

    def registrar(request: httpx.Request) -> httpx.Response:
        capturado.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    respx.post("https://api.mistral.ai/v1/chat/completions").mock(side_effect=registrar)
    fraco = next(c for c in default_providers() if c.name == "mistral")
    async with OpenAICompatProvider(fraco, "m", api_key="k") as p:
        await p.complete("analise a partida", schema={"type": "object"})

    mensagens = capturado["messages"]
    assert isinstance(mensagens, list)
    assert any("json" in str(m.get("content", "")).lower() for m in mensagens)


# --------------------------------------------------------------------------
# Catalogo
# --------------------------------------------------------------------------


def test_defaults_work_without_any_config_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Quem nunca abriu providers.yaml precisa receber exatamente os padroes."""
    assert load_providers(tmp_path / "nao-existe.yaml") == default_providers()


def test_the_model_id_follows_the_hardware_tier() -> None:
    ollama = next(c for c in default_providers() if c.name == "ollama")
    assert text_model_for(ollama, "tier1") == "qwen3:8b"
    assert text_model_for(ollama, "tier3") == "qwen3:30b-a3b"
    assert text_model_for(ollama, "none") == ""


def test_cloud_models_do_not_depend_on_local_hardware() -> None:
    gemini = next(c for c in default_providers() if c.name == "gemini")
    assert text_model_for(gemini, "tier1") == text_model_for(gemini, "tier3")


# --------------------------------------------------------------------------
# Resolucao de modelo contra o que o provedor realmente oferece
# --------------------------------------------------------------------------
#
# Esta secao existe por causa de uma falha concreta: dois dos tres ids padrao
# deste repositorio nasceram obsoletos. Nenhuma revisao de codigo pegaria —
# a string continua parecendo valida depois de morta.


def _groq() -> object:
    return next(c for c in default_providers() if c.name == "groq")


def test_the_preferred_model_is_used_when_it_still_exists() -> None:
    cfg = _groq()
    escolhido, nota = resolve_text_model(
        cfg,  # type: ignore[arg-type]
        "none",
        ["openai/gpt-oss-120b", "outro-modelo"],
    )
    assert escolhido == "openai/gpt-oss-120b"
    assert nota == "", "sem substituicao, sem aviso"


def test_a_dead_model_id_falls_back_to_the_family() -> None:
    """O caso real: o id preferido foi descontinuado e o provedor oferece
    outro da mesma familia."""
    cfg = _groq()
    escolhido, nota = resolve_text_model(
        cfg,  # type: ignore[arg-type]
        "none",
        ["openai/gpt-oss-20b", "whisper-large-v3"],
    )
    assert escolhido == "openai/gpt-oss-20b"
    assert "nao esta mais disponivel" in nota


def test_the_substitution_is_always_explained() -> None:
    """Um relatorio pior sem explicacao vira um relato de bug impossivel de
    diagnosticar."""
    cfg = _groq()
    _, nota = resolve_text_model(cfg, "none", ["modelo-que-nao-casa-com-nada"])  # type: ignore[arg-type]
    assert nota, "substituir em silencio e o pior dos dois mundos"


def test_resolution_is_deterministic() -> None:
    """Dois computadores com a mesma conta precisam escolher o mesmo modelo,
    senao os relatorios deixam de ser comparaveis."""
    cfg = _groq()
    lista = ["openai/gpt-oss-20b", "openai/gpt-oss-120b-alt", "llama-4"]
    primeiro, _ = resolve_text_model(cfg, "none", lista)  # type: ignore[arg-type]
    segundo, _ = resolve_text_model(cfg, "none", list(reversed(lista)))  # type: ignore[arg-type]
    assert primeiro == segundo


def test_not_being_able_to_ask_keeps_the_preference() -> None:
    """Lista vazia significa 'nao consegui perguntar', nao 'nao ha modelo'.
    Confundir os dois transformaria uma queda de rede em 'provedor sem
    modelos', e a mensagem de erro apontaria para o lugar errado."""
    cfg = _groq()
    escolhido, nota = resolve_text_model(cfg, "none", [])  # type: ignore[arg-type]
    assert escolhido == "openai/gpt-oss-120b"
    assert nota == ""


def test_openrouter_picks_any_free_model() -> None:
    """Fixar um id no OpenRouter garantiria que ele quebrasse: sao centenas, e
    so o sufixo ':free' e estavel."""
    cfg = next(c for c in default_providers() if c.name == "openrouter")
    escolhido, _ = resolve_text_model(
        cfg,  # type: ignore[arg-type]
        "none",
        ["algum/modelo-pago", "outro/modelo:free"],
    )
    assert escolhido.endswith(":free")


@respx.mock
@pytest.mark.asyncio
async def test_the_client_lists_models(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        assert await p.list_models() == ["a", "b"]


@respx.mock
@pytest.mark.asyncio
async def test_listing_models_degrades_instead_of_raising(cfg) -> None:  # type: ignore[no-untyped-def]
    respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(500, text="boom")
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        assert await p.list_models() == []


@pytest.mark.asyncio
async def test_listing_models_without_a_key_asks_nothing(cfg) -> None:  # type: ignore[no-untyped-def]
    """Sem chave nao ha o que perguntar, e gastar uma requisicao para
    descobrir isso seria so latencia."""
    async with OpenAICompatProvider(cfg, "m", api_key=None) as p:
        assert await p.list_models() == []


def test_yaml_override_merges_instead_of_replacing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Quem so quer trocar o id do modelo nao deveria precisar redeclarar caps,
    url e cota junto — e se precisasse, redeclararia errado."""
    pytest.importorskip("yaml")
    p = tmp_path / "providers.yaml"
    p.write_text("providers:\n  - name: groq\n    text_model: modelo-novo\n", encoding="utf-8")
    groq = next(c for c in load_providers(p) if c.name == "groq")
    padrao = next(c for c in default_providers() if c.name == "groq")
    assert groq.text_model == "modelo-novo"
    assert groq.caps == padrao.caps
    assert groq.base_url == padrao.base_url


def test_broken_yaml_falls_back_instead_of_crashing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Perder a personalizacao e ruim; nao conseguir analisar a partida e pior."""
    p = tmp_path / "providers.yaml"
    p.write_text("isto: [nao: fecha", encoding="utf-8")
    assert load_providers(p) == default_providers()


# --------------------------------------------------------------------------
# Hardware
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gb", "esperado"),
    [(0, "none"), (6, "none"), (8, "tier1"), (12, "tier2"), (16, "tier2"), (24, "tier3")],
)
def test_the_tier_table_matches_what_the_readme_promises(gb: float, esperado: str) -> None:
    """Prometer qwen3:8b numa placa de 8 GB e entregar 'use a nuvem' e pior do
    que nunca ter prometido."""
    assert _tier_for(gb) == esperado


# --------------------------------------------------------------------------
# Conversao da saida do analista
# --------------------------------------------------------------------------


def _saida(**kw: object) -> AnalystOutput:
    base: dict[str, object] = {
        "category": "wave",
        "severity": 3,
        "timestamp_ms": 600_000,
        "claim": "voce perdeu a wave",
        "evidence": [
            AnalystEvidence(tier="T1", timestamp_ms=600_000, statement="cs@10 = 40", assumption="")
        ],
        "fix": "farme a wave antes de rotacionar",
    }
    base.update(kw)
    return AnalystOutput(findings=[AnalystFinding(**base)])  # type: ignore[arg-type]


def test_a_valid_finding_converts() -> None:
    r = to_findings(_saida(), duration_ms=2_000_000, source_label="laning")
    assert len(r.findings) == 1
    assert not r.rejected


def test_derived_evidence_without_an_assumption_is_dropped() -> None:
    """A regra que o sistema inteiro existe para impor. Um T2 sem premissa e
    conselho que o usuario nao consegue verificar."""
    r = to_findings(
        _saida(
            evidence=[
                AnalystEvidence(
                    tier="T2", timestamp_ms=600_000, statement="a wave empurrava", assumption=""
                )
            ]
        ),
        duration_ms=2_000_000,
        source_label="laning",
    )
    assert r.findings == []
    assert "premissa" in r.rejected[0]


def test_derived_evidence_with_an_assumption_survives() -> None:
    r = to_findings(
        _saida(
            evidence=[
                AnalystEvidence(
                    tier="T2",
                    timestamp_ms=600_000,
                    statement="a wave empurrava",
                    assumption="derivado do ritmo de CS e da posicao por minuto",
                )
            ]
        ),
        duration_ms=2_000_000,
        source_label="laning",
    )
    assert len(r.findings) == 1
    assert r.findings[0].evidence[0].assumption


def test_a_finding_anchored_outside_the_match_is_dropped() -> None:
    """Um finding que nao da para clicar no replay perde a propriedade que faz
    este projeto diferente."""
    r = to_findings(_saida(timestamp_ms=9_000_000), duration_ms=2_000_000, source_label="macro")
    assert r.findings == []
    assert "fora da partida" in r.rejected[0]


@pytest.mark.parametrize("ts", [0, 2, 9, 999])
def test_a_finding_anchored_in_the_first_second_is_dropped(ts: int) -> None:
    """Visto numa revisao real: o modelo pos 2 e 9 em `timestamp_ms` (minutos,
    ou nada), e tres marcacoes "criticas" apareceram no segundo zero do
    replay, antes de o jogo comecar."""
    r = to_findings(_saida(timestamp_ms=ts), duration_ms=2_000_000, source_label="macro")
    assert r.findings == []
    assert "milissegundo" in r.rejected[0]


def test_the_schema_tells_the_model_the_unit() -> None:
    """Os dados chegam em mm:ss e o campo pede ms. A unidade precisa estar no
    schema que o modelo preenche, nao so no nome do campo."""
    esquema = json.dumps(AnalystOutput.model_json_schema(), ensure_ascii=False)
    assert "milissegundos" in esquema


def test_one_bad_finding_does_not_discard_the_good_ones() -> None:
    """Um analista que produziu dois bons e um ruim entregou dois bons.
    Derrubar o passe inteiro jogaria trabalho valido fora e gastaria outra
    chamada."""
    bom = _saida().findings[0]
    ruim = _saida(timestamp_ms=9_000_000).findings[0]
    r = to_findings(
        AnalystOutput(findings=[bom, ruim]), duration_ms=2_000_000, source_label="fights"
    )
    assert len(r.findings) == 1
    assert len(r.rejected) == 1


def test_declared_entities_are_kept_aligned_with_the_surviving_findings() -> None:
    """Se o alinhamento quebrar, o validador confere as entidades de um finding
    contra o texto de outro — e a saida continua plausivel, entao ninguem
    percebe."""
    ruim = _saida(timestamp_ms=9_000_000).findings[0]
    bom = _saida(entities=["Botas de Velocidade"]).findings[0]
    r = to_findings(
        AnalystOutput(findings=[ruim, bom]), duration_ms=2_000_000, source_label="economy"
    )
    assert len(r.findings) == 1
    assert r.entities == {0: ["Botas de Velocidade"]}


def test_model_findings_are_less_confident_than_measured_ones() -> None:
    """Um finding do modelo e uma interpretacao; um da regra e uma medicao. O
    ranqueamento usa isso para desempatar a favor do medido."""
    from riftcoach.analysis.rules import evaluate as run_rules
    from riftcoach.parse.distill import distill
    from tests.test_distill import load

    match, tl = load("sr_ranked_35min")
    facts = distill(match, tl, match["info"]["participants"][0]["puuid"])
    das_regras = max(f.confidence for f in run_rules(facts))
    do_modelo = to_findings(_saida(), 2_000_000, "laning").findings[0].confidence
    assert do_modelo < das_regras


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nome", ["_system", "laning", "macro", "economy", "fights", "head_coach"])
def test_every_prompt_exists_and_is_substantial(nome: str) -> None:
    texto = load_prompt(nome)
    assert len(texto) > 500, "prompt curto demais para orientar um modelo pequeno"


def test_the_system_prompt_states_the_closed_world_rule() -> None:
    """M1 de docs/04-knowledge-base.md, 4.5. Sem isto o modelo cita stats de
    itens de memoria, que estao errados desde o patch seguinte ao treino."""
    s = load_prompt("_system")
    assert "não tem" in s and "memória confiável" in s
    assert "T1" in s and "T2" in s and "T3" in s


# --------------------------------------------------------------------------
# O caminho de CPU
# --------------------------------------------------------------------------


def test_ram_sobrando_sem_gpu_ainda_da_um_caminho_local() -> None:
    """O bug que este teste existe para impedir contradizia a premissa do
    projeto: uma maquina com placa integrada fraca e 16 GB de RAM era
    classificada como "nao ha modelo local possivel, use a nuvem" — quando um
    4B roda perfeitamente bem na CPU com essa RAM."""
    from riftcoach.llm.hardware import _tier_for

    assert _tier_for(vram_gb=0.0, ram_gb=16.0) == "cpu"
    assert _tier_for(vram_gb=2.0, ram_gb=16.0) == "cpu"


def test_a_gpu_sempre_ganha_da_ram() -> None:
    """VRAM e uma ordem de grandeza mais rapida. RAM e plano B, nunca
    preferencia."""
    from riftcoach.llm.hardware import _tier_for

    assert _tier_for(vram_gb=8.0, ram_gb=64.0) == "tier1"
    assert _tier_for(vram_gb=24.0, ram_gb=8.0) == "tier3"


def test_ram_insuficiente_continua_mandando_para_a_nuvem() -> None:
    from riftcoach.llm.hardware import CPU_REQUIREMENT_GB, _tier_for

    assert _tier_for(vram_gb=0.0, ram_gb=CPU_REQUIREMENT_GB - 0.1) == "none"
    assert _tier_for(vram_gb=0.0, ram_gb=0.0) == "none"


def test_o_tier_de_cpu_escolhe_um_modelo_menor() -> None:
    """Na CPU o gargalo e a banda de memoria: um 8B entrega 2-3 tokens/s e um
    4B entrega 4-6. A diferenca entre lento e inutilizavel mora ai."""
    from riftcoach.llm.catalog import OLLAMA_TEXT_BY_TIER

    assert OLLAMA_TEXT_BY_TIER["cpu"] == "qwen3:4b"
    assert "none" not in OLLAMA_TEXT_BY_TIER


def test_a_descricao_avisa_que_vai_ser_lento() -> None:
    """Dizer so "da para rodar local" faria a pessoa esperar vinte minutos
    achando que travou."""
    from riftcoach.llm.hardware import Hardware

    hw = Hardware(
        gpu_name="Intel(R) HD Graphics 4000",
        vram_gb=2.0,
        unified_memory=False,
        tier="cpu",
        ollama_up=False,
        ram_gb=16.0,
        avx2=False,
    )
    texto = hw.describe()
    assert "16 GB de RAM" in texto
    assert "AVX2" in texto, "sem AVX2 a velocidade cai pela metade; a pessoa precisa saber"
    assert "CPU" in texto


def test_cache_antigo_sem_ram_e_refeito(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Um cache gravado pela versao que so conhecia VRAM manteria a maquina
    classificada como "sem opcao local" para sempre."""
    import json as _json

    from riftcoach.llm import hardware as hw_mod

    caminho = tmp_path / "hardware.json"
    caminho.write_text(
        _json.dumps({"gpu_name": "iGPU", "vram_gb": 1.0, "unified_memory": False, "tier": "none"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(hw_mod, "cache_path", lambda: caminho)
    monkeypatch.setattr(hw_mod, "probe_gpu", lambda: ("iGPU", 1.0, False))
    monkeypatch.setattr(hw_mod, "_probe_ram_gb", lambda: 16.0)
    monkeypatch.setattr(hw_mod, "_probe_avx2", lambda: True)

    async def sem_ollama(base_url: str = "") -> tuple[bool, tuple[str, ...]]:
        return False, ()

    monkeypatch.setattr(hw_mod, "probe_ollama", sem_ollama)
    hw = asyncio.run(hw_mod.probe())
    assert hw.tier == "cpu"
    assert hw.ram_gb == 16.0


# --------------------------------------------------------------------------
# Cota de tokens
# --------------------------------------------------------------------------


def test_a_cota_de_tokens_repoe_continuamente() -> None:
    """A versao anterior tratava tokens como janela fixa que zerava de 60 em
    60 s, e por isso mandava esperar um minuto inteiro por poucos tokens.

    O proprio Groq desmente a janela fixa: `x-ratelimit-reset-tokens: 659ms`
    para uma chamada minuscula e reposicao continua, a 8000/60 por segundo.
    """
    from riftcoach.llm.base import RateLimit
    from riftcoach.llm.breaker import RateLimiter

    agora = [0.0]
    lim = RateLimiter(clock=lambda: agora[0])
    lim.register("groq", RateLimit(requests=1000, window_s=60.0, tokens=8000))

    assert lim.has_budget("groq", 4200)
    lim.consume("groq", 4200)
    assert not lim.has_budget("groq", 4200), "8000 - 4200 nao cabe outra de 4200"

    # A 133 tokens/s, faltam ~3 s para repor os 400 que faltam.
    espera = lim.seconds_until_budget("groq", 4200)
    assert 2.0 < espera < 4.0, f"esperado ~3s de reposicao continua, veio {espera}"

    agora[0] += espera
    assert lim.has_budget("groq", 4200)


def test_gastar_mais_do_que_o_estimado_vira_divida() -> None:
    """Zerar o excesso perdoaria o gasto e levaria a um 429 de verdade."""
    from riftcoach.llm.base import RateLimit
    from riftcoach.llm.breaker import RateLimiter

    agora = [0.0]
    lim = RateLimiter(clock=lambda: agora[0])
    lim.register("groq", RateLimit(requests=1000, window_s=60.0, tokens=8000))

    lim.consume("groq", 12_000)  # estourou o balde inteiro
    assert not lim.has_budget("groq", 1)
    # A divida atrasa: 4000 de excesso a 133/s sao ~30 s so para voltar a zero.
    assert lim.seconds_until_budget("groq", 1) > 25.0


def test_sem_cota_de_tokens_declarada_nada_bloqueia() -> None:
    """O Ollama local nao tem cota, e limitar ali seria so deixar a maquina
    ociosa."""
    from riftcoach.llm.base import RateLimit
    from riftcoach.llm.breaker import RateLimiter

    lim = RateLimiter(clock=lambda: 0.0)
    lim.register("ollama", RateLimit(requests=1000, window_s=60.0))
    lim.consume("ollama", 999_999)
    assert lim.has_budget("ollama", 999_999)


def test_duracao_com_unidade_vira_segundos() -> None:
    """O provedor manda "3.09s", "659ms", "1m2s". Tirar o "s" e converter
    transformava 659ms em 659 segundos — onze minutos de espera por meio
    segundo de cota."""
    from riftcoach.llm.providers.openai_compat import _duracao_em_segundos

    assert _duracao_em_segundos("3.09s") == pytest.approx(3.09)
    assert _duracao_em_segundos("659ms") == pytest.approx(0.659)
    assert _duracao_em_segundos("1m2s") == pytest.approx(62.0)
    assert _duracao_em_segundos("30") == pytest.approx(30.0)  # Retry-After cru
    assert _duracao_em_segundos("") is None
    assert _duracao_em_segundos("depois") is None


def test_429_por_cota_e_espera_nao_e_falha() -> None:
    """Tratar 429 como falha de provedor quebrava a fila dos analistas: o
    primeiro estourava a cota, o disjuntor abria, e os seguintes concluiam
    "nao ha provedor" em vez de "espere tres segundos"."""
    from riftcoach.llm.breaker import ProviderBreaker

    agora = [0.0]
    cb = ProviderBreaker(clock=lambda: agora[0])
    cb.record_quota("groq", 3.0)

    assert not cb.is_closed("groq")
    falta = cb.segundos_ate_fechar_por_cota("groq")
    assert falta is not None and falta == pytest.approx(3.0)

    agora[0] += 3.0
    assert cb.is_closed("groq"), "passado o prazo, volta a servir"


def test_falha_de_verdade_nao_vira_espera() -> None:
    """Paciencia nao conserta 500 nem conexao recusada."""
    from riftcoach.llm.breaker import ProviderBreaker

    cb = ProviderBreaker(clock=lambda: 0.0)
    cb.record_transient("groq", "connection refused")
    assert cb.segundos_ate_fechar_por_cota("groq") is None


def test_o_balde_local_adota_o_saldo_real_do_provedor() -> None:
    """O balde local comeca cheio a cada execucao; o do provedor nao, porque e
    por minuto e compartilhado entre processos. Rodar a analise duas vezes
    seguidas fazia a segunda sair mandando com o balde cheio e o real vazio."""
    from riftcoach.llm.base import RateLimit
    from riftcoach.llm.breaker import RateLimiter

    lim = RateLimiter(clock=lambda: 0.0)
    lim.register("groq", RateLimit(requests=1000, window_s=60.0, tokens=8000))
    assert lim.has_budget("groq", 4200)

    lim.sincronizar_tokens("groq", 900)
    assert not lim.has_budget("groq", 4200), "o provedor disse que so restam 900"


def test_o_saldo_informado_nunca_aumenta_o_balde() -> None:
    """O cabecalho e lido DEPOIS da resposta, entao ja chega velho. Deixa-lo
    subir o saldo desfaria um consumo que acabou de acontecer."""
    from riftcoach.llm.base import RateLimit
    from riftcoach.llm.breaker import RateLimiter

    lim = RateLimiter(clock=lambda: 0.0)
    lim.register("groq", RateLimit(requests=1000, window_s=60.0, tokens=8000))
    lim.consume("groq", 7000)  # restam 1000

    lim.sincronizar_tokens("groq", 8000)  # cabecalho velho, de antes da chamada
    assert not lim.has_budget("groq", 4200), "o saldo nao pode voltar a subir"


def test_provedor_sem_cabecalho_de_cota_nao_e_afetado() -> None:
    """A maioria nao publica cota, e inventar zero ali faria o limitador parar
    de mandar para sempre."""
    from riftcoach.llm.providers.openai_compat import _inteiro

    assert _inteiro(None) is None
    assert _inteiro("nao sou numero") is None
    assert _inteiro("7912") == 7912


# --------------------------------------------------------------------------
# Quatro passes ou um so
# --------------------------------------------------------------------------


def test_cota_apertada_troca_quatro_passes_por_um() -> None:
    """O Mixture-of-Analysts foi desenhado para modelo LOCAL, onde uma chamada
    a mais nao custa nada alem de tempo.

    Num tier gratuito com teto de tokens POR MINUTO a conta muda: medido
    contra o Groq, 8.000/min e 5.465 por passe — cabe UMA chamada por minuto,
    e as cinco viram cinco minutos de espera.
    """
    from riftcoach.analysis.analysts import (
        CUSTO_POR_PASSE,
        PASSES_DO_FORMATO_LONGO,
        _cabe_em_quatro_passes,
    )
    from riftcoach.llm.base import RateLimit

    class _FakeRouter:
        def __init__(self, tokens: int | None) -> None:
            self.providers = [
                type(
                    "P",
                    (),
                    {
                        "profile": type(
                            "Perfil",
                            (),
                            {"rate_limit": RateLimit(requests=100, window_s=60.0, tokens=tokens)},
                        )()
                    },
                )()
            ]

    apertado = _FakeRouter(8_000)
    assert not _cabe_em_quatro_passes(apertado)  # type: ignore[arg-type]

    folgado = _FakeRouter(CUSTO_POR_PASSE * PASSES_DO_FORMATO_LONGO)
    assert _cabe_em_quatro_passes(folgado)  # type: ignore[arg-type]

    # Sem cota declarada (o Ollama local) sempre cabe.
    assert _cabe_em_quatro_passes(_FakeRouter(None))  # type: ignore[arg-type]


def test_o_teto_de_findings_cabe_na_cota() -> None:
    """Medido: entrada 4.661 + saida reservada 2.048 = 6.709 contra um teto de
    8.000. Pedir oito findings exigiria ~3.500 de saida e passaria de 8.161 —
    a resposta sairia cortada no meio, sem o campo `fix` do segundo finding, e
    a validacao descartaria a analise inteira."""
    from riftcoach.analysis.analysts import AnalystOutput

    assert AnalystOutput.model_fields["findings"].metadata[0].max_length == 3


def test_o_prompt_do_passe_unico_existe() -> None:
    """Prompt ausente so aparece em tempo de execucao, e ai ja gastou a cota
    para descobrir."""
    from riftcoach.analysis.analysts import load_prompt

    texto = load_prompt("completo")
    for palavra in ("Rota", "Macro", "Economia", "Lutas"):
        assert palavra in texto, f"o passe unico precisa cobrir {palavra}"
    assert "TRES" in texto, "o teto de tres precisa estar no prompt, nao so no schema"
