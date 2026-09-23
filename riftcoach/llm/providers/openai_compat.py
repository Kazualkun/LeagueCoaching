"""Um cliente, quatro provedores: ollama | groq | openrouter | gemini.

Todos os quatro falam o formato /chat/completions da OpenAI, entao nao ha
motivo para quatro clientes. O que muda entre eles e uma coisa so — COMO se
pede JSON estruturado — e e exatamente ai que mora a diferenca de
confiabilidade:

  Ollama    `format: <json-schema>`   decodificacao restrita. Garantia FORTE:
                                      o token que violaria o schema nao e nem
                                      amostrado.
  Gemini    `response_schema`         garantia FORTE, pelo mesmo motivo.
  Groq /    `response_format:         garantia FRACA: promete JSON sintatico,
  OpenRouter  {type: json_object}`    nao o NOSSO JSON. Vai do loop de reparo.

Tratar os tres casos como iguais e o erro que faz um tier gratuito queimar a
cota diaria inteira em retentativas. O `Completion.schema_enforced` carrega
essa distincao para cima, e o roteador decide o que fazer com ela.

Usamos httpx direto, e nao o SDK da openai: uma dependencia a menos, e o SDK
nao ajudaria em nada aqui porque o unico ponto divergente e justamente o campo
que ele abstrai.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from riftcoach.llm.base import (
    Capability,
    Completion,
    ProviderProfile,
    Usage,
)
from riftcoach.llm.catalog import ProviderConfig

DEFAULT_TIMEOUT = 120.0


class ProviderCallError(Exception):
    """Falha numa chamada, ja classificada para o breaker.

    A classificacao acontece AQUI, e nao no breaker, porque so aqui existe a
    resposta HTTP. Um breaker que recebe "deu erro" nao tem como saber se deve
    esperar a janela de cota ou recuar 30 segundos.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,  # "quota" | "transient" | "fatal"
        retry_after_s: float | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retry_after_s = retry_after_s
        self.status = status


class OpenAICompatProvider:
    """Cliente para qualquer endpoint compativel com /chat/completions."""

    def __init__(
        self,
        config: ProviderConfig,
        model: str,
        *,
        api_key: str | None = None,
        est_tok_per_s: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self._key = api_key
        self._client = client
        self._owns_client = client is None
        self.profile = ProviderProfile(
            # Identidade inclui o modelo: trocar o modelo do Ollama e trocar de
            # provedor do ponto de vista do breaker, porque a compatibilidade
            # com o schema e propriedade do modelo, nao do servidor.
            name=f"{config.name}:{model}",
            caps=config.caps,
            ctx_tokens=config.ctx_tokens,
            cost_class=config.cost_class,
            privacy=config.privacy,
            est_tok_per_s=est_tok_per_s,
            rate_limit=config.rate_limit,
            latency_preference=config.latency_preference,
        )

    async def __aenter__(self) -> OpenAICompatProvider:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._key:
            h["Authorization"] = f"Bearer {self._key}"
        if self.config.name == "openrouter":
            # O OpenRouter pede identificacao do app para a cota gratuita.
            h["HTTP-Referer"] = "https://github.com/Kazualkun/LeagueCoaching"
            h["X-Title"] = "RiftCoach AI"
        return h

    def _json_mode(self, schema: dict[str, Any] | None) -> tuple[dict[str, Any], bool]:
        """Como pedir JSON neste provedor. Devolve (campos, garantia_forte)."""
        if schema is None:
            return {}, False

        if Capability.JSON_SCHEMA in self.config.caps:
            if self.config.name == "ollama":
                # O Ollama nativo aceita o schema cru em `format`. Pelo shim
                # /v1 ele tambem aceita o formato da OpenAI; mandamos o da
                # OpenAI porque e o que o endpoint /v1 documenta.
                return (
                    {
                        "response_format": {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "resposta",
                                "schema": schema,
                                "strict": True,
                            },
                        }
                    },
                    True,
                )
            return (
                {
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "resposta",
                            "schema": schema,
                            "strict": self.config.schema_strict,
                        },
                    }
                },
                True,
            )

        if Capability.JSON_OBJECT in self.config.caps:
            return {"response_format": {"type": "json_object"}}, False
        return {}, False

    @staticmethod
    def _garante_a_palavra_json(mensagens: list[dict[str, Any]]) -> None:
        """O modo `json_object` exige a palavra "json" nas mensagens.

        E uma regra da OpenAI que os compativeis copiaram, e o Groq a aplica:
        sem a palavra, 400. Os nossos prompts sao em portugues e nem sempre a
        contem, entao uma falha de UMA PALAVRA derrubava a analise inteira — e
        ainda chegava ao usuario disfarcada de "nenhum provedor atende a
        tarefa", porque o 400 abria o disjuntor antes de alguem ler o corpo.
        """
        if any("json" in str(m.get("content", "")).lower() for m in mensagens):
            return
        for m in mensagens:
            if m.get("role") == "system":
                m["content"] = str(m["content"]) + "\n\nResponda somente com JSON."
                return
        mensagens.insert(0, {"role": "system", "content": "Responda somente com JSON."})

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        """Quantos segundos o provedor mandou esperar.

        `x-ratelimit-reset-tokens` estava faltando, e era justamente o que
        importava: num tier gratuito o teto que aperta e o de TOKENS, nao o de
        requisicoes. Sem ler esse cabecalho o 429 vinha sem prazo, o disjuntor
        assumia o recuo maximo, e uma pausa de tres segundos virava um minuto.

        Os valores vem com unidade — "3.09s", "659ms", "1m2s" — entao nao da
        para tirar o "s" e converter.
        """
        for header in (
            "retry-after",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-reset-requests",
        ):
            if (v := resp.headers.get(header)) is not None:
                segundos = _duracao_em_segundos(v)
                if segundos is not None:
                    return segundos
                try:
                    return float(v.rstrip("s"))
                except ValueError:
                    continue
        return None

    def _raise_for_status(self, resp: httpx.Response) -> None:
        if resp.is_success:
            return
        corpo = resp.text[:300]
        if resp.status_code == 429:
            raise ProviderCallError(
                f"cota atingida em {self.profile.name}: {corpo}",
                kind="quota",
                retry_after_s=self._retry_after(resp),
                status=429,
            )
        if resp.status_code in (401, 403):
            # Chave invalida nao melhora com retentativa. `fatal` faz o
            # roteador descartar o provedor em vez de gastar a sessao inteira
            # tentando.
            raise ProviderCallError(
                f"chave rejeitada por {self.profile.name}: {corpo}",
                kind="fatal",
                status=resp.status_code,
            )
        if resp.status_code == 404:
            raise ProviderCallError(
                f"modelo {self.model} nao existe em {self.config.name}: {corpo}",
                kind="fatal",
                status=404,
            )
        raise ProviderCallError(
            f"{self.profile.name} respondeu {resp.status_code}: {corpo}",
            kind="transient",
            status=resp.status_code,
        )

    async def complete(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        system: str | None = None,
        max_tokens: int = 2048,
    ) -> Completion:
        mensagens: list[dict[str, Any]] = []
        if system:
            mensagens.append({"role": "system", "content": system})
        mensagens.append({"role": "user", "content": prompt})

        campos_json, forte = self._json_mode(schema)
        if campos_json.get("response_format", {}).get("type") == "json_object":
            self._garante_a_palavra_json(mensagens)
        corpo: dict[str, Any] = {
            "model": self.model,
            "messages": mensagens,
            "max_tokens": max_tokens,
            **{k: v for k, v in self.config.options.items() if k != "num_ctx"},
            **campos_json,
        }

        inicio = time.monotonic()
        try:
            resp = await self.client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                json=corpo,
                headers=self._headers(),
            )
        except httpx.TimeoutException as e:
            raise ProviderCallError(
                f"{self.profile.name} nao respondeu a tempo", kind="transient"
            ) from e
        except httpx.HTTPError as e:
            # Conexao recusada e o caso do Ollama que nao esta rodando. E
            # transitorio de verdade: o usuario pode subir o servico e tentar
            # de novo sem mexer em nada.
            raise ProviderCallError(
                f"nao foi possivel falar com {self.profile.name}: {e}", kind="transient"
            ) from e

        self._raise_for_status(resp)
        decorrido = time.monotonic() - inicio

        try:
            dados = resp.json()
            texto = dados["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise ProviderCallError(
                f"{self.profile.name} devolveu resposta em formato inesperado: {resp.text[:200]}",
                kind="transient",
            ) from e

        uso = dados.get("usage") or {}
        return Completion(
            text=texto,
            provider=self.profile.name,
            usage=Usage(
                input_tokens=int(uso.get("prompt_tokens", 0) or 0),
                output_tokens=int(uso.get("completion_tokens", 0) or 0),
                elapsed_s=decorrido,
            ),
            schema_enforced=forte,
            remaining_tokens=_inteiro(resp.headers.get("x-ratelimit-remaining-tokens")),
        )

    async def list_models(self) -> list[str]:
        """Os ids que o provedor REALMENTE oferece agora.

        Isto existe porque ids de modelo morrem. Os defaults deste repositorio
        ja nasceram com dois obsoletos — o `llama-3.3-70b-versatile` foi
        descontinuado no Groq e o `gemini-2.5-flash` ficou geracoes para tras —
        e nenhuma revisao de codigo teria pego, porque a string continua
        parecendo certa.

        Lista vazia significa "nao consegui perguntar", e nao "nao ha modelo":
        quem chama precisa tratar os dois casos de forma diferente, senao um
        provedor sem rede vira um provedor sem modelos.
        """
        if self.config.needs_key and not self._key:
            return []
        try:
            resp = await self.client.get(
                f"{self.config.base_url.rstrip('/')}/models",
                headers=self._headers(),
                timeout=8.0,
            )
            if not resp.is_success:
                return []
            dados = resp.json()
        except (httpx.HTTPError, ValueError):
            return []

        itens = dados.get("data", dados) if isinstance(dados, dict) else dados
        if not isinstance(itens, list):
            return []
        return [
            str(m.get("id") or m.get("name") or "")
            for m in itens
            if isinstance(m, dict) and (m.get("id") or m.get("name"))
        ]

    async def healthy(self) -> bool:
        """O provedor responde?

        Para o Ollama isto responde "o servico esta rodando"; para a nuvem,
        "a chave vale". Os dois sao a mesma pergunta do ponto de vista do
        roteador: da para contar com este provedor agora?
        """
        if self.config.needs_key and not self._key:
            return False
        try:
            resp = await self.client.get(
                f"{self.config.base_url.rstrip('/')}/models",
                headers=self._headers(),
                timeout=5.0,
            )
            return resp.is_success
        except httpx.HTTPError:
            return False


def _duracao_em_segundos(texto: str) -> float | None:
    """ "3.09s", "659ms", "1m2s" -> segundos. None quando nao reconhece.

    Escrito a mao em vez de regex complicada porque o conjunto de formas e
    pequeno e conhecido, e porque errar aqui vira uma espera de um minuto onde
    bastavam tres segundos.
    """
    t = texto.strip().lower()
    if not t:
        return None
    total = 0.0
    numero = ""
    achou = False
    i = 0
    while i < len(t):
        c = t[i]
        if c.isdigit() or c == ".":
            numero += c
            i += 1
            continue
        if not numero:
            return None
        if t[i : i + 2] == "ms":
            total += float(numero) / 1000.0
            i += 2
        elif c == "s":
            total += float(numero)
            i += 1
        elif c == "m":
            total += float(numero) * 60.0
            i += 1
        elif c == "h":
            total += float(numero) * 3600.0
            i += 1
        else:
            return None
        numero = ""
        achou = True
    if numero:  # numero solto no fim: segundos, por convencao do Retry-After
        total += float(numero)
        achou = True
    return total if achou else None


def _inteiro(valor: str | None) -> int | None:
    """Cabecalho numerico, ou None. Cabecalho ausente nao e erro: a maioria dos
    provedores nao publica cota, e inventar zero ali faria o limitador parar
    de mandar para sempre."""
    if valor is None:
        return None
    try:
        return int(float(valor))
    except ValueError:
        return None
