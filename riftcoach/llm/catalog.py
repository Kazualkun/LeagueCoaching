"""Catalogo de provedores: os padroes distribuidos, e o override do usuario.

IDS DE MODELO FICAM EM CONFIGURACAO PRECISAMENTE PORQUE ENVELHECEM. O contrato
do roteador e com `Capability`, nunca com uma string — trocar para o modelo do
ano que vem precisa ser uma edicao de arquivo, nao uma mudanca de codigo.

Os padroes vivem aqui em Python, e nao num YAML obrigatorio, por um motivo
pratico: o app precisa funcionar sem nenhum arquivo de configuracao e sem
nenhuma dependencia extra. O YAML e opcional e so sobrescreve — quem nunca
abrir `~/.riftcoach/providers.yaml` recebe exatamente estes valores.

Ver docs/01-model-routing.md, 1.6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from riftcoach.config import data_dir, settings
from riftcoach.llm.base import Capability, CostClass, LatencyClass, Privacy, RateLimit
from riftcoach.llm.hardware import Tier

# Modelo por tier de hardware. Chave "none" nao existe de proposito: sem VRAM
# suficiente nao ha modelo local, e forcar um seria entregar 2 tokens/s.
OLLAMA_TEXT_BY_TIER: dict[Tier, str] = {
    "tier1": "qwen3:8b",
    "tier2": "qwen3:14b",
    "tier3": "qwen3:30b-a3b",
}
OLLAMA_VISION_BY_TIER: dict[Tier, str] = {
    "tier1": "qwen2.5vl:3b",
    "tier2": "qwen2.5vl:7b",
    "tier3": "qwen2.5vl:7b",
}


@dataclass(frozen=True)
class ProviderConfig:
    """Um provedor configurado, antes de virar `ProviderProfile`.

    A separacao existe porque o perfil depende de coisas que so sao conhecidas
    em runtime: qual modelo o hardware aguenta, se a chave existe, qual a
    velocidade medida.
    """

    name: str
    base_url: str
    cost_class: CostClass
    privacy: Privacy
    caps: frozenset[Capability]
    ctx_tokens: int = 32_768
    # None = nao exige chave (o Ollama local). Quando preenchido, e o nome
    # usado tanto na env var quanto no cofre do SO.
    api_key_name: str | None = None
    # PREFERENCIA, nao exigencia. Se o provedor nao oferecer mais este id,
    # `resolve_text_model` cai para `model_prefers`. Ver o comentario grande
    # em DEFAULTS abaixo.
    text_model: str = ""
    vision_model: str = ""
    # Trechos de id, em ordem de preferencia, usados quando `text_model` nao
    # existe mais. Sao deliberadamente vagos: familias de modelo duram anos,
    # ids exatos duram meses.
    model_prefers: tuple[str, ...] = ()
    rate_limit: RateLimit | None = None
    latency_preference: LatencyClass | None = None
    options: dict[str, Any] = field(default_factory=dict)

    def resolve_key(self) -> str | None:
        if self.api_key_name is None:
            return None
        return settings.resolve_provider_key(self.api_key_name)

    @property
    def needs_key(self) -> bool:
        return self.api_key_name is not None


# --------------------------------------------------------------------------
# Os padroes
# --------------------------------------------------------------------------
#
# IDS DE MODELO AQUI SAO PREFERENCIA, NAO EXIGENCIA — e isso nao e cautela
# teorica. Na primeira versao deste arquivo, DOIS dos tres ids ja nasceram
# obsoletos: o `llama-3.3-70b-versatile` tinha sido descontinuado no Groq e o
# `gemini-2.5-flash` estava geracoes atras. Nenhuma revisao de codigo pegaria
# isso, porque a string continua parecendo perfeitamente valida.
#
# Entao o roteador pergunta ao provedor o que ele tem (`/v1/models`) e usa
# `model_prefers` para escolher quando a preferencia sumiu. Familias de modelo
# duram anos; ids exatos duram meses.
#
# As cotas declaradas abaixo sao CONSERVADORAS de proposito. Elas sao aplicadas
# no cliente, e num tier gratuito chegar no 429 costuma custar a janela inteira
# — entao errar para baixo custa um pouco de vazao, e errar para cima custa o
# dia do usuario. Os numeros reais mudam sem aviso; por isso ficam aqui, em
# configuracao, e nao espalhados no codigo.

TEXT_CAPS = frozenset(
    {Capability.TEXT, Capability.JSON_SCHEMA, Capability.JSON_OBJECT, Capability.LONG_CTX_32K}
)


def default_providers() -> list[ProviderConfig]:
    return [
        ProviderConfig(
            name="ollama",
            base_url=settings.ollama_base_url,
            cost_class="local",
            privacy="local_only",
            # Ollama tem decodificacao restrita por JSON Schema de verdade
            # (`format`), que e a garantia mais forte da lista.
            caps=TEXT_CAPS | {Capability.VISION},
            ctx_tokens=32_768,
            api_key_name=None,
            rate_limit=None,  # local nao tem cota; limitar seria so ociosidade
            options={"temperature": 0.3, "num_ctx": 32_768},
        ),
        ProviderConfig(
            name="gemini",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            cost_class="free_cloud",
            privacy="leaves_machine",
            caps=TEXT_CAPS | {Capability.VISION, Capability.VIDEO_NATIVE},
            ctx_tokens=1_000_000,
            api_key_name="gemini",
            text_model="gemini-flash-latest",
            vision_model="gemini-flash-latest",
            # "flash" primeiro: o relatorio e trabalho em lote de contexto
            # curto, exatamente onde o modelo rapido empata com o caro.
            model_prefers=("flash-latest", "flash", "gemini"),
            rate_limit=RateLimit(requests=10, window_s=60.0),
            options={"temperature": 0.3},
        ),
        ProviderConfig(
            name="groq",
            base_url="https://api.groq.com/openai/v1",
            cost_class="free_cloud",
            privacy="leaves_machine",
            # Sem JSON_SCHEMA: o Groq oferece `json_object`, que promete "algum
            # JSON" e nao o NOSSO JSON. A diferenca decide se o roteador confia
            # na saida ou entra no loop de validar-e-reparar.
            caps=frozenset({Capability.TEXT, Capability.JSON_OBJECT, Capability.LONG_CTX_32K}),
            ctx_tokens=32_768,
            api_key_name="groq",
            text_model="openai/gpt-oss-120b",
            model_prefers=("gpt-oss-120b", "gpt-oss", "llama", "instruct"),
            rate_limit=RateLimit(requests=25, window_s=60.0),
            # O Groq e rapido o bastante para ser a escolha certa em tarefa
            # interativa mesmo quando existe um local disponivel.
            latency_preference="interactive",
            options={"temperature": 0.3},
        ),
        ProviderConfig(
            name="mistral",
            base_url="https://api.mistral.ai/v1",
            cost_class="free_cloud",
            privacy="leaves_machine",
            # Existe aqui por DISPONIBILIDADE, nao por ser melhor: e sediada na
            # UE, entao atende paises onde o Google AI Studio nao abre. Um
            # usuario sem alternativa cai no relatorio sem IA — que funciona,
            # mas e menos do que ele poderia ter.
            caps=frozenset({Capability.TEXT, Capability.JSON_OBJECT, Capability.LONG_CTX_32K}),
            ctx_tokens=32_768,
            api_key_name="mistral",
            text_model="",
            model_prefers=("small-latest", "small", "mistral"),
            rate_limit=RateLimit(requests=15, window_s=60.0),
            options={"temperature": 0.3},
        ),
        ProviderConfig(
            name="openrouter",
            base_url="https://openrouter.ai/api/v1",
            cost_class="free_cloud",
            privacy="leaves_machine",
            caps=frozenset({Capability.TEXT, Capability.JSON_OBJECT, Capability.LONG_CTX_32K}),
            ctx_tokens=32_768,
            api_key_name="openrouter",
            text_model="",
            # O OpenRouter tem centenas de ids e os gratuitos rodam. Fixar um
            # so garantiria que ele quebrasse; o sufixo ":free" e o que
            # importa e ele e estavel.
            model_prefers=(":free",),
            rate_limit=RateLimit(requests=15, window_s=60.0),
            options={"temperature": 0.3},
        ),
    ]


# --------------------------------------------------------------------------
# Override do usuario
# --------------------------------------------------------------------------


def config_path() -> Path:
    return data_dir() / "providers.yaml"


def _apply_override(base: ProviderConfig, bruto: dict[str, Any]) -> ProviderConfig:
    """Sobrescreve campo a campo, mantendo o que o usuario nao mencionou.

    Merge e nao substituicao: quem so quer trocar o id do modelo nao deveria
    precisar redeclarar caps, url e cota junto — e se precisasse, redeclararia
    errado.
    """
    mudancas: dict[str, Any] = {}
    for campo in ("base_url", "text_model", "vision_model", "ctx_tokens", "api_key_name"):
        if campo in bruto:
            mudancas[campo] = bruto[campo]
    if "caps" in bruto:
        mudancas["caps"] = frozenset(Capability(c) for c in bruto["caps"])
    if "options" in bruto and isinstance(bruto["options"], dict):
        mudancas["options"] = {**base.options, **bruto["options"]}
    if "rate_limit" in bruto:
        rl = bruto["rate_limit"]
        mudancas["rate_limit"] = (
            RateLimit(
                requests=int(rl["requests"]),
                window_s=float(rl.get("window_s", 60.0)),
                tokens=rl.get("tokens"),
            )
            if rl
            else None
        )
    return ProviderConfig(**{**base.__dict__, **mudancas})


def load_providers(path: Path | None = None) -> list[ProviderConfig]:
    """Padroes, com o YAML do usuario aplicado por cima se existir.

    Sem pyyaml instalado ou sem arquivo, devolve os padroes — que e o caminho
    de todo mundo que nunca editou configuracao. YAML malformado tambem cai
    nos padroes em vez de derrubar a analise: perder a personalizacao e ruim,
    nao conseguir analisar a partida e pior.
    """
    padroes = default_providers()
    caminho = path or config_path()
    if not caminho.exists():
        return padroes

    try:
        import yaml
    except ImportError:
        return padroes

    try:
        dados = yaml.safe_load(caminho.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError, ValueError):
        # yaml.YAMLError NAO e subclasse de ValueError — capturar so ValueError
        # deixava um providers.yaml malformado derrubar a analise inteira, que
        # e exatamente o oposto do que esta funcao promete.
        return padroes
    if not isinstance(dados, dict):
        return padroes

    por_nome = {p.name: p for p in padroes}
    for bruto in dados.get("providers", []) or []:
        if not isinstance(bruto, dict) or "name" not in bruto:
            continue
        nome = str(bruto["name"])
        if nome in por_nome:
            por_nome[nome] = _apply_override(por_nome[nome], bruto)
    return list(por_nome.values())


def text_model_for(cfg: ProviderConfig, tier: Tier) -> str:
    """O id PREFERIDO para texto, sem consultar o provedor.

    O Ollama e o unico cujo modelo depende do hardware: os provedores de nuvem
    rodam o mesmo modelo para todo mundo.
    """
    if cfg.name == "ollama":
        return cfg.text_model or OLLAMA_TEXT_BY_TIER.get(tier, "")
    return cfg.text_model


def resolve_text_model(cfg: ProviderConfig, tier: Tier, available: list[str]) -> tuple[str, str]:
    """Escolhe o modelo contra o que o provedor REALMENTE oferece.

    Devolve (id escolhido, nota). A nota e vazia quando a preferencia valeu, e
    explica a substituicao quando nao valeu — o usuario precisa saber que esta
    rodando em outro modelo, senao um relatorio pior fica sem explicacao.

    `available` vazio significa "nao consegui perguntar", nao "nao ha modelo":
    nesse caso mantemos a preferencia e deixamos a chamada falhar com a
    mensagem do provedor, que e mais informativa que um palpite nosso.
    """
    preferido = text_model_for(cfg, tier)
    if not available:
        return preferido, ""
    if preferido and preferido in available:
        return preferido, ""

    for trecho in cfg.model_prefers:
        # Ordenado para ser deterministico: sem isso, dois computadores com a
        # mesma conta escolheriam modelos diferentes e os relatorios nao seriam
        # comparaveis.
        casaram = sorted(m for m in available if trecho in m)
        if casaram:
            escolhido = casaram[0]
            motivo = (
                f"'{preferido}' nao esta mais disponivel; usando '{escolhido}'"
                if preferido
                else f"usando '{escolhido}'"
            )
            return escolhido, motivo

    escolhido = sorted(available)[0]
    return escolhido, (
        f"nenhuma preferencia casou com o que {cfg.name} oferece; usando '{escolhido}'"
    )
