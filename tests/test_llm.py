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


def test_three_schema_violations_burn_the_model_for_the_session() -> None:
    """O caso que mais importa: um modelo que nao sustenta o nosso JSON queima
    a cota diaria inteira em retentativas e nao produz nada."""
    b = ProviderBreaker(FakeClock())
    for _ in range(SCHEMA_STRIKES):
        b.record_schema_violation("groq:llama", "campo severity invalido")
    assert b.state("groq:llama") is BreakerState.BURNED
    assert not b.is_closed("groq:llama")
    assert "schema" in b.reason("groq:llama")


def test_a_burned_model_does_not_come_back_on_success() -> None:
    """O problema nao era disponibilidade, era capacidade. Sucesso em outra
    coisa nao prova que ele passou a sustentar o schema."""
    b = ProviderBreaker(FakeClock())
    for _ in range(SCHEMA_STRIKES):
        b.record_schema_violation("groq:llama")
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
    sem_chave = FakeProvider(
        _profile("groq"), [ProviderCallError("401", kind="fatal", status=401)]
    )
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
    p = FakeProvider(_profile("groq"), ["nao sou json"] * 5)
    r = _router(p)
    with pytest.raises(SchemaExhausted) as ex:
        await r.complete_validated(text_task("t", 500), "prompt", AnalystOutput)
    assert "sem IA" in (ex.value.hint or "") or "L5" in (ex.value.hint or "")
    assert r.guard.breaker.state("groq") is BreakerState.BURNED


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
    """Groq oferece `json_object`, que promete JSON sintatico e nao o NOSSO
    JSON. Tratar isso como garantia forte e o erro que faz um tier gratuito
    queimar a cota em retentativas."""
    respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
    )
    async with OpenAICompatProvider(cfg, "m", api_key="k") as p:
        out = await p.complete("oi", schema={"type": "object"})
    assert not out.schema_enforced

    ollama = next(c for c in default_providers() if c.name == "ollama")
    assert Capability.JSON_SCHEMA in ollama.caps


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
        return_value=httpx.Response(
            200, json={"data": [{"id": "a"}, {"id": "b"}]}
        )
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
    p.write_text(
        "providers:\n  - name: groq\n    text_model: modelo-novo\n", encoding="utf-8"
    )
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
            AnalystEvidence(
                tier="T1", timestamp_ms=600_000, statement="cs@10 = 40", assumption=""
            )
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


@pytest.mark.parametrize(
    "nome", ["_system", "laning", "macro", "economy", "fights", "head_coach"]
)
def test_every_prompt_exists_and_is_substantial(nome: str) -> None:
    texto = load_prompt(nome)
    assert len(texto) > 500, "prompt curto demais para orientar um modelo pequeno"


def test_the_system_prompt_states_the_closed_world_rule() -> None:
    """M1 de docs/04-knowledge-base.md, 4.5. Sem isto o modelo cita stats de
    itens de memoria, que estao errados desde o patch seguinte ao treino."""
    s = load_prompt("_system")
    assert "não tem" in s and "memória confiável" in s
    assert "T1" in s and "T2" in s and "T3" in s
