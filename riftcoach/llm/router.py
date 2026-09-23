"""O roteador: escolhe um provedor por RESTRICAO, nunca por nome.

    pool = provedores que atendem os requisitos, cabem no contexto, nao estao
           com o breaker aberto e ainda tem orcamento
    escolha = o de menor rank segundo a politica

A politica, em ordem de prioridade (docs/01-model-routing.md, 1.5):

  1. PRIVACIDADE. `privacy_mode=strict` elimina tudo que sai da maquina, antes
     de qualquer outra consideracao. Nao e um criterio de desempate — e um
     filtro, e se ele esvaziar o pool o resultado e o relatorio L5, nao uma
     excecao.
  2. CUSTO. local < free_cloud < paid. Local primeiro nao e so economia: local
     nao tem cota, entao trabalho em lote nunca queima a franquia diaria de
     que o caminho interativo precisa.
  3. LATENCIA. Para tarefa `interactive`, inverte quando o local e lento. Uma
     espera de 90 segundos numa pergunta de follow-up e funcionalidade
     quebrada; 90 segundos num relatorio em lote esta de bom tamanho.
  4. VAZAO MEDIDA, como desempate.

E o roteador NUNCA levanta excecao por falta de provedor sem dizer o que falta.
`NoViableProvider.hint` carrega o diagnostico de cada provedor descartado —
"Ollama fora do ar", "cota do Gemini estourada", "chave do Groq ausente". Um
erro de roteamento silencioso e indistinguivel de um bug.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from riftcoach.config import settings
from riftcoach.core.errors import NoViableProvider, SchemaExhausted
from riftcoach.llm.base import (
    Capability,
    Completion,
    ProviderProfile,
    TaskSpec,
)
from riftcoach.llm.breaker import Guard, log_compatibility
from riftcoach.llm.catalog import ProviderConfig, load_providers, resolve_text_model
from riftcoach.llm.hardware import Hardware, probe
from riftcoach.llm.providers.openai_compat import (
    OpenAICompatProvider,
    ProviderCallError,
)

T = TypeVar("T", bound=BaseModel)

COST_ORDER = {"local": 0, "free_cloud": 1, "paid": 2}

# Tentativas de validacao antes de desistir do provedor. Duas de reparo mais a
# original: se nem com os erros do pydantic realimentados literalmente o modelo
# produz o schema, ele nao produz.
SCHEMA_ATTEMPTS = 3

# Quantas vezes esperar pela cota antes de desistir, e o teto de cada
# espera.
#
# Oito, e nao tres, porque os quatro analistas mais o head coach formam
# uma FILA: cada um espera a vez do anterior. No tier gratuito do Groq —
# 8.000 tokens por minuto, ~4.200 por passe — isso da uns dois minutos e
# meio de fila. Com tres esperas o ultimo da fila desistia, e o relatorio
# saia faltando um angulo sem que nada explicasse por que.
#
# Esperar aqui nao gasta CPU: sao `sleep` do tamanho exato que falta.
ESPERAS_POR_COTA = 8
TETO_ESPERA_S = 65.0


@dataclass
class RoutingPolicy:
    privacy_mode: str = "relaxed"
    prefer: str = "cost"  # cost | speed | quality

    def allows(self, p: ProviderProfile) -> bool:
        """O filtro de privacidade. Primeiro, e sem excecao."""
        return not (self.privacy_mode == "strict" and p.leaves_machine)

    def rank(self, p: ProviderProfile, task: TaskSpec) -> tuple[float, ...]:
        custo = float(COST_ORDER.get(p.cost_class, 9))

        if task.latency_class == "interactive":
            if p.is_slow_for_interactive:
                # Empurra o local lento para tras de tudo. Sem isto, a
                # preferencia por custo escolheria um modelo de 5 tok/s para
                # responder uma pergunta de follow-up.
                custo += 10.0
            if p.latency_preference == "interactive":
                custo -= 0.5

        if self.prefer == "speed":
            custo -= 0.25 * min(p.est_tok_per_s / 100.0, 1.0)

        # Vazao como desempate: mais rapido primeiro, entao negativo.
        return (custo, -p.est_tok_per_s)


@dataclass
class Route:
    """O que foi escolhido, e por que. Vai para o `model_trace` do relatorio."""

    provider: OpenAICompatProvider
    task: TaskSpec

    @property
    def name(self) -> str:
        return self.provider.profile.name


@dataclass
class ModelRouter:
    """Monta o pool de provedores e serve tarefas.

    Construir com `ModelRouter.create()`, que sonda o hardware e resolve as
    chaves. O construtor cru existe para os testes, que injetam provedores
    falsos sem tocar em rede nem em disco.
    """

    providers: list[OpenAICompatProvider] = field(default_factory=list)
    policy: RoutingPolicy = field(default_factory=RoutingPolicy)
    guard: Guard = field(default_factory=Guard)
    hardware: Hardware | None = None
    # Provedor -> por que o modelo usado nao e o preferido. O usuario precisa
    # saber: um relatorio pior sem explicacao vira um relato de bug impossivel
    # de diagnosticar.
    substitutions: dict[str, str] = field(default_factory=dict)
    _notes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for p in self.providers:
            self.guard.limiter.register(p.profile.name, p.profile.rate_limit)

    # ------------------------------------------------------------------
    # Construcao
    # ------------------------------------------------------------------

    @classmethod
    async def create(
        cls,
        configs: list[ProviderConfig] | None = None,
        hardware: Hardware | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> ModelRouter:
        hw = hardware or await probe()
        policy = RoutingPolicy(privacy_mode=settings.privacy_mode)
        guard = Guard()
        providers: list[OpenAICompatProvider] = []
        notas: dict[str, str] = {}
        substituicoes: dict[str, str] = {}

        for cfg in configs or load_providers():
            if cfg.name == "ollama":
                if not hw.ollama_up:
                    notas[cfg.name] = (
                        "Ollama nao esta respondendo em "
                        f"{cfg.base_url} — inicie o servico, ou use um tier gratuito"
                    )
                    continue
                if hw.tier == "none":
                    notas[cfg.name] = f"{hw.describe()} — nenhum modelo local cabe nesta maquina"
                    continue

            chave = cfg.resolve_key()
            if cfg.needs_key and not chave:
                notas[cfg.name] = (
                    f"sem chave: defina {cfg.api_key_name.upper()}_API_KEY "  # type: ignore[union-attr]
                    f"ou guarde no cofre do SO"
                )
                continue

            # Pergunta ao provedor o que ele tem HOJE, em vez de confiar num id
            # gravado no codigo. Ids de modelo morrem, e a string continua
            # parecendo valida depois de morta — ver o comentario em catalog.py.
            async with OpenAICompatProvider(cfg, "", api_key=chave, client=client) as sonda:
                disponiveis = await sonda.list_models()
            modelo, nota = resolve_text_model(cfg, hw.tier, disponiveis)
            if nota:
                substituicoes[cfg.name] = nota

            if not modelo:
                notas[cfg.name] = "nenhum modelo de texto disponivel"
                continue

            providers.append(OpenAICompatProvider(cfg, modelo, api_key=chave, client=client))

        r = cls(
            providers=providers,
            policy=policy,
            guard=guard,
            hardware=hw,
        )
        r._notes = notas
        r.substitutions = substituicoes
        return r

    # ------------------------------------------------------------------
    # Selecao
    # ------------------------------------------------------------------

    def _pool(self, task: TaskSpec) -> list[OpenAICompatProvider]:
        return [
            p
            for p in self.providers
            if self.policy.allows(p.profile)
            and p.profile.supports(task)
            and self.guard.available(p.profile.name, task.est_input_tokens)
        ]

    def select(self, task: TaskSpec) -> OpenAICompatProvider:
        pool = self._pool(task)
        if not pool:
            raise NoViableProvider(
                f"nenhum provedor atende a tarefa '{task.name}'",
                hint=self._diagnose(task),
            )
        return min(pool, key=lambda p: self.policy.rank(p.profile, task))

    async def select_esperando(self, task: TaskSpec) -> OpenAICompatProvider:
        """Como `select`, mas ESPERA quando o unico impedimento e a cota.

        Num tier gratuito, "voce gastou os 8 mil tokens deste minuto" nao e
        uma falha: e o provedor pedindo para ir mais devagar. Tratar isso como
        erro foi o que fez quatro analistas virarem "nenhum provedor atende a
        tarefa" — uma mensagem que nao tem nada a ver com a causa e nao sugere
        a acao certa, que era simplesmente aguardar alguns segundos.

        A espera e limitada: passar de uma janela inteira significa que o
        problema nao e ritmo, e ai falhar rapido vale mais que insistir.
        """
        for _ in range(ESPERAS_POR_COTA):
            try:
                return self.select(task)
            except NoViableProvider:
                espera = self._espera_por_cota(task)
                if espera is None:
                    raise
                await asyncio.sleep(min(espera, TETO_ESPERA_S))
        return self.select(task)

    def _espera_por_cota(self, task: TaskSpec) -> float | None:
        """Quantos segundos ate algum provedor ter cota — None se o problema
        for outro.

        A distincao e o ponto: so vale esperar quando o provedor esta apto em
        tudo menos orcamento. Se ele nao tem a capacidade exigida, ou se o
        disjuntor abriu, esperar nao muda nada.
        """
        prazos: list[float] = []
        for p in self.providers:
            perfil = p.profile
            if not (self.policy.allows(perfil) and perfil.supports(task)):
                continue

            # Disjuntor aberto POR COTA tambem e espera, e nao beco sem saida.
            # Tratar o 429 como falha de provedor foi o que quebrou a fila dos
            # analistas: o primeiro estourava a cota, o disjuntor abria, e os
            # seguintes concluiam "nao ha provedor" em vez de "espere tres
            # segundos".
            por_cota = self.guard.breaker.segundos_ate_fechar_por_cota(perfil.name)
            if por_cota is not None:
                prazos.append(por_cota)
                continue
            if not self.guard.breaker.is_closed(perfil.name):
                continue
            if self.guard.limiter.has_budget(perfil.name, task.est_input_tokens):
                continue  # tem cota: o impedimento e outro
            prazos.append(
                self.guard.limiter.seconds_until_budget(perfil.name, task.est_input_tokens)
            )
        return min(prazos) if prazos else None

    def _diagnose(self, task: TaskSpec) -> str:
        """Por que cada provedor esta fora. Isto vira a mensagem que o usuario
        le, entao precisa nomear a acao que resolve."""
        linhas: list[str] = []
        for nome, motivo in self._notes.items():
            linhas.append(f"  {nome}: {motivo}")
        for p in self.providers:
            perfil = p.profile
            if not self.policy.allows(perfil):
                linhas.append(f"  {perfil.name}: privacy_mode=strict bloqueia provedores de nuvem")
            elif not perfil.supports(task):
                faltando = task.requires - perfil.caps
                if faltando:
                    linhas.append(f"  {perfil.name}: nao tem {', '.join(sorted(faltando))}")
                else:
                    linhas.append(
                        f"  {perfil.name}: contexto de {perfil.ctx_tokens} tokens e "
                        f"menor que os {task.min_ctx_tokens} necessarios"
                    )
            elif not self.guard.breaker.is_closed(perfil.name):
                linhas.append(f"  {perfil.name}: {self.guard.breaker.reason(perfil.name)}")
            elif not self.guard.limiter.has_budget(perfil.name, task.est_input_tokens):
                espera = self.guard.limiter.seconds_until_budget(perfil.name, task.est_input_tokens)
                linhas.append(f"  {perfil.name}: cota local esgotada, libera em {espera:.0f}s")

        if not linhas:
            return (
                "Nenhum provedor de IA esta configurado. O relatorio sem IA continua "
                "funcionando — rode `riftcoach analyze` normalmente."
            )
        return "Provedores indisponiveis:\n" + "\n".join(linhas)

    @property
    def available(self) -> bool:
        """Ha algum provedor utilizavel? A CLI usa isto para decidir entre o
        caminho com IA e o L5, sem precisar provocar uma excecao."""
        return bool([p for p in self.providers if self.policy.allows(p.profile)]) and any(
            self.guard.breaker.is_closed(p.profile.name) for p in self.providers
        )

    def describe(self) -> str:
        if not self.providers:
            return "nenhum provedor de IA disponivel — relatorio sem IA (L5)"
        return " · ".join(f"{p.profile.name} [{p.profile.cost_class}]" for p in self.providers)

    # ------------------------------------------------------------------
    # Execucao com validacao
    # ------------------------------------------------------------------

    async def complete(
        self, task: TaskSpec, prompt: str, *, system: str | None = None
    ) -> Completion:
        """Uma chamada, com failover entre provedores.

        Cada falha classifica o provedor no breaker e tenta o proximo. So
        levanta quando o pool inteiro se esgota — e ai com o diagnostico.
        """
        tentados: list[str] = []
        while True:
            try:
                provider = self.select(task)
            except NoViableProvider:
                if tentados:
                    raise NoViableProvider(
                        f"todos os provedores falharam na tarefa '{task.name}'",
                        hint=self._diagnose(task),
                    ) from None
                raise

            nome = provider.profile.name
            tentados.append(nome)
            self.guard.limiter.consume(nome, task.est_input_tokens)
            try:
                out = await provider.complete(prompt, system=system)
            except ProviderCallError as e:
                self._record_failure(nome, e)
                continue

            self.guard.breaker.record_success(nome)
            self._learn_speed(provider, out)
            return out

    async def complete_validated(
        self, task: TaskSpec, prompt: str, model: type[T], *, system: str | None = None
    ) -> T:
        """Gera e valida contra um modelo pydantic, com reparo.

        NUNCA fazemos parsing de texto livre. Quando o provedor tem garantia
        forte de schema, uma tentativa basta; quando nao tem, realimentamos os
        erros de validacao LITERALMENTE — dizer ao modelo "campo severity:
        Input should be less than or equal to 5" funciona muito melhor que
        "formato invalido", porque o erro ja esta na linguagem do schema.
        """
        tentados: set[str] = set()
        esquema = model.model_json_schema()

        for tentativa in range(SCHEMA_ATTEMPTS):
            provider = await self.select_esperando(task)
            nome = provider.profile.name
            tentados.add(nome)
            self.guard.limiter.consume(nome, task.est_input_tokens)

            try:
                out = await provider.complete(prompt, schema=esquema, system=system)
            except ProviderCallError as e:
                self._record_failure(nome, e)
                continue

            self.guard.breaker.record_success(nome)
            self._learn_speed(provider, out)

            try:
                validado = model.model_validate_json(_extract_json(out.text))
            except (ValidationError, ValueError) as e:
                self.guard.breaker.record_schema_violation(nome, str(e)[:200])
                if tentativa < SCHEMA_ATTEMPTS - 1:
                    prompt = _repair_prompt(prompt, out.text, e)
                continue

            log_compatibility(nome, "ok", task.name)
            return validado

        raise SchemaExhausted(
            f"nenhum provedor sustentou o schema de '{task.name}' em {SCHEMA_ATTEMPTS} tentativas",
            hint=(
                f"Provedores tentados: {', '.join(sorted(tentados))}. "
                "O relatorio sem IA continua disponivel e nao depende disto. "
                "O resultado foi registrado em ~/.riftcoach/compat.jsonl."
            ),
        )

    def _record_failure(self, nome: str, e: ProviderCallError) -> None:
        if e.kind == "quota":
            self.guard.breaker.record_quota(nome, e.retry_after_s)
        elif e.kind == "fatal":
            self.guard.breaker.burn(nome, e.message)
            log_compatibility(nome, "fatal", e.message)
        else:
            self.guard.breaker.record_transient(nome, e.message)

    @staticmethod
    def _learn_speed(provider: OpenAICompatProvider, out: Completion) -> None:
        """Mede a vazao real e guarda no perfil.

        Sem isto o roteador nunca sai do palpite inicial, e a regra de latencia
        interativa (item 3 da politica) nunca dispara — ela depende de saber se
        o local e lento, e isso so se descobre usando.
        """
        medido = out.usage.tok_per_s
        if medido <= 0:
            return
        anterior = provider.profile.est_tok_per_s
        # Media movel: uma chamada atipica nao deve redefinir a classificacao.
        novo = medido if anterior <= 0 else 0.7 * anterior + 0.3 * medido
        provider.profile = ProviderProfile(
            name=provider.profile.name,
            caps=provider.profile.caps,
            ctx_tokens=provider.profile.ctx_tokens,
            cost_class=provider.profile.cost_class,
            privacy=provider.profile.privacy,
            est_tok_per_s=novo,
            rate_limit=provider.profile.rate_limit,
            latency_preference=provider.profile.latency_preference,
        )

    async def aclose(self) -> None:
        for p in self.providers:
            await p.__aexit__()


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------


def _extract_json(texto: str) -> str:
    """Tira o JSON de uma resposta que pode vir embrulhada.

    Provedores com garantia fraca devolvem ```json ... ``` ou prosa em volta,
    mesmo instruidos a nao fazer isso. Recortar aqui e mais barato que gastar
    uma retentativa inteira com um JSON que estava correto dentro da cerca.
    """
    t = texto.strip()
    if t.startswith("```"):
        linhas = t.splitlines()
        t = "\n".join(linhas[1:-1] if linhas[-1].strip().startswith("```") else linhas[1:])
        t = t.strip()
    inicio = min(
        (i for i in (t.find("{"), t.find("[")) if i >= 0),
        default=-1,
    )
    if inicio > 0:
        t = t[inicio:]
    return t


def _repair_prompt(original: str, resposta: str, erro: Exception) -> str:
    """Realimenta os erros de validacao LITERALMENTE.

    A tentacao e reescrever o erro em linguagem natural. Nao faca: a mensagem
    do pydantic ja nomeia o campo e a restricao violada, que e exatamente a
    informacao que o modelo precisa para corrigir.
    """
    if isinstance(erro, ValidationError):
        detalhe = json.dumps(
            [
                {"campo": ".".join(str(p) for p in d["loc"]), "erro": d["msg"]}
                for d in erro.errors()[:8]
            ],
            ensure_ascii=False,
            indent=2,
        )
    else:
        detalhe = str(erro)[:400]

    return (
        f"{original}\n\n"
        "--- CORRECAO NECESSARIA ---\n"
        "A sua resposta anterior foi rejeitada pelo validador de schema.\n\n"
        f"Voce respondeu:\n{resposta[:800]}\n\n"
        f"Erros:\n{detalhe}\n\n"
        "Responda de novo com o JSON corrigido. Somente o JSON, sem cercas de "
        "codigo e sem texto em volta."
    )


def text_task(name: str, est_input_tokens: int, interactive: bool = False) -> TaskSpec:
    """Atalho para as tarefas de texto, que sao quase todas."""
    return TaskSpec(
        name=name,
        requires=frozenset({Capability.TEXT}),
        est_input_tokens=est_input_tokens,
        latency_class="interactive" if interactive else "batch",
    )
