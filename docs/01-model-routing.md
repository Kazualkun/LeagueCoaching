# §1 — Seleção de Modelo e Motor de Inferência

🇧🇷 Português · [🇺🇸 English](en/01-model-routing.md)

## 1.1 O enquadramento que todo mundo erra

O instinto é escolher "o melhor modelo". A jogada correta é **decompor a carga de trabalho em quatro
tarefas com requisitos radicalmente diferentes** e rotear cada uma separadamente, porque elas quase
não têm nada em comum:

| Tarefa | O que realmente exige | Contexto | Tolerância a modelo fraco |
|---|---|---|---|
| **J1 — Percepção** (ler a HUD, localizar ícones no minimapa) | OCR de texto de 11px, ancoragem espacial, saída de bbox | minúsculo (1 imagem + 200 tok) | **Baixa** — um valor de ouro lido errado envenena tudo depois |
| **J2 — Aritmética** (CS@10, diferença de ouro, timers de morte) | *nada* — isso é Python | — | não se aplica, nunca dê isso a um modelo |
| **J3 — Memorização de fatos** (o Eclipse ainda dá omnivamp?) | *nada* — isso é consulta a banco | — | não se aplica, nunca dê isso a um modelo |
| **J4 — Julgamento** (dar recall às 12:40 foi certo?) | obediência a instruções, saída estruturada, raciocínio causal sobre ~3k tok | 8–32k | **Média** — um modelo de 8B é genuinamente utilizável *se* J1–J3 já estiverem resolvidos |

Tirar J2 e J3 do modelo é o que derruba o requisito de hardware. Um modelo de 14B a quem se pede
"analise esta partida" falha. O mesmo 14B a quem se pede *"aqui estão 12 fatos medidos e 4 constantes
do patch; ranqueie os três maiores erros e cite quais fatos sustentam cada um"* acerta. **O nível de
qualidade exigido do modelo é definido por quanto trabalho você se recusa a entregar a ele.**

---

## 1.2 Modelos locais — a recomendação

### Raciocínio textual (J4) — carga principal

| Tier | VRAM | Modelo | Tag Ollama | Por quê |
|---|---|---|---|---|
| T0 | nenhuma / <6 GB | *(só nuvem)* | — | Nem tente. Um modelo de 3B produz conselho pior do que conselho nenhum. |
| T1 | 8 GB | **Qwen3-8B** (Q4_K_M) | `qwen3:8b` | Melhor seguidor de instruções abaixo de 10B; forte aderência a JSON. Alternativa: `qwen2.5:7b-instruct`. |
| T2 | 12–16 GB | **Qwen3-14B** (Q4_K_M) | `qwen3:14b` | O ponto ideal. ~9 GB de pesos + ~3 GB de KV em 32k. Alternativas: `phi4:14b`, `gemma3:12b`. |
| T3 | 24 GB+ | **Qwen3-30B-A3B** (Q4_K_M) | `qwen3:30b-a3b` | MoE: qualidade de ~30B na velocidade de ~3B de parâmetros ativos. Roda a 40+ tok/s numa 3090/4090 e degrada com elegância quando parcialmente descarregado para a RAM — excepcionalmente bom para um projeto open source de hardware de consumidor. |
| Mac | 16 GB unificada | `qwen3:14b` | | Metal via Ollama; 32 GB+ → `qwen3:30b-a3b`. |

**Por que Qwen em vez de Llama 3.x nesta tarefa.** Três razões concretas, não "impressões":

1. **Aderência a saída estruturada.** Nosso pipeline inteiro é JSON de `list[Finding]`. Qwen2.5/Qwen3
   sustentam um schema ao longo de saídas longas de forma sensivelmente melhor que Llama 3.1/3.2 no
   mesmo tamanho, e isso é a diferença entre um app funcional e um loop de retentativas.
2. **Modo de raciocínio (Qwen3).** O `/think` dá um traço de raciocínio com orçamento que podemos
   *descartar antes do parsing*. Ranquear erros por gravidade se beneficia disso, e pagamos apenas
   com computação local.
3. **Estabilidade em contexto longo (32k).** O passe de merge concatena a saída de quatro analistas.
   O Llama 3.1 8B degrada visivelmente na segunda metade de um contexto de 16k; o Qwen3 se sustenta.

**A armadilha do Ollama que você precisa tratar no primeiro dia.** O `num_ctx` padrão do Ollama é
4096 e ele **trunca silenciosamente** em vez de dar erro. Seu pacote de evidências cuidadosamente
destilado de 4k tokens mais um system prompt de 1,5k vai ser decapitado sem aviso, e o modelo vai dar
coaching sobre metade da partida. Sempre defina explicitamente, e valide a janela de contexto
reportada pelo modelo na inicialização:

```python
options = {"num_ctx": 32768, "temperature": 0.3, "top_p": 0.9, "repeat_penalty": 1.05}
```

A temperatura baixa é deliberada: queremos que o modelo *selecione e ranqueie* as evidências
fornecidas, não que invente. Criatividade aqui é defeito.

### Visão (J1)

**`qwen2.5vl:7b` (Ollama) é o padrão local.** O Llama 3.2 Vision 11B é a alternativa óbvia e é a
escolha errada para este domínio: ele é comparativamente fraco em OCR de texto pequeno e denso, que é
*exatamente* nossa carga de trabalho (contador de ouro, contador de CS, o relógio 00:00, tooltips de
item em 1080p). O Qwen2.5-VL foi treinado com objetivos explícitos de OCR de documentos e ancoragem
espacial, e consegue devolver bounding boxes, que é o que precisamos para localizar ícones no
minimapa. O Qwen2-VL (a versão citada no briefing) é o antecessor dele — usável, mas o 2.5 é um
upgrade direto no mesmo tamanho. Em 8 GB, caia para `qwen2.5vl:3b` só para OCR de relógio/HUD e
mande o trabalho de minimapa para a nuvem.

**Restrição crítica em J1: nunca deixe o VLM fazer J4.** A única saída permitida ao VLM é uma
`FrameObservation`:

```python
class FrameObservation(BaseModel):
    game_clock_s: int | None            # OCR do timer da HUD
    camera_zone: MapZone | None         # para onde o jogador estava olhando
    minimap_allies: list[MapZone]       # detecção de ícones
    minimap_enemies_visible: list[MapZone]
    hud: HudState | None                # ouro, cs, nível, hp%, ult_pronta
    wave_state: Literal["freeze","slow_push","fast_push","crashing","bounced","unknown"]
    confidence: float
```

Tudo que ele emite é carimbado como `EvidenceTier.T3_INFERRED`. Ele é um sensor, não um coach.

---

## 1.3 Tiers gratuitos na nuvem — a recomendação

| Provedor | Use para | Formato do tier gratuito | Veredito |
|---|---|---|---|
| **Google AI Studio — Gemini 2.5 Flash / 2.0 Flash** | **Padrão na nuvem. Texto + o *único* bom caminho para VOD.** | RPM/RPD generosos, contexto enorme | **Principal.** Exclusivamente: ingere um *arquivo de vídeo direto*, com amostragem nativa de ~1 fps, e responde com timestamps reais. Essa capacidade sozinha reduz o Modo C de um pipeline de extração de frames para uma única chamada de API. |
| **Groq** | Follow-ups interativos sensíveis a latência | RPM alto, RPD baixo, OpenAI-compat | **Secundário.** A inferência é dramaticamente mais rápida que qualquer outra coisa; a cota diária pequena o torna errado para lote e certo para "pergunte algo sobre este momento". |
| **OpenRouter (modelos `:free`)** | Transbordo / escolha do usuário | Por modelo, volátil | **Terciário.** A disponibilidade de modelos muda o tempo todo — trate como configurado pelo usuário, nunca como padrão. |
| **Mistral** | Quando Google/Groq não abrem no país | Tier gratuito, sediada na UE | **Quarta opção.** Está no catálogo por DISPONIBILIDADE geográfica, não por ser melhor. |
| ~~Cerebras~~ | — | **deixou de ser gratuita** | Virou trial de US$5 com cartão (verificado em set/2026). Removida das recomendações. |

Os quatro falam o schema de Chat Completions da OpenAI (o Gemini via sua base URL OpenAI-compat),
então uma única instância `AsyncOpenAI` com `base_url` + `api_key` trocados cobre toda a superfície
de nuvem. Use o SDK **nativo** `google-genai` apenas para o caminho de upload de vídeo, porque a
Files API e os tipos de parte de vídeo não têm equivalente OpenAI-compat.

**Cotas de tiers gratuitos mudam constantemente.** Não as fixe em código. Distribua um
`providers.yaml` com os limites declarados, deixe os usuários editarem, e aplique do lado do cliente
com um token bucket, para que uma mudança de cota degrade em um failover elegante em vez de um
crash.

---

## 1.4 Os dois pipelines

### Pipeline P1 — Telemetria pós-partida, só texto (Modo A)

```
MatchFacts IR (§2)
      │
      ├─► KnowledgeAssembler: injeta dados de patch dos itens/runas desta build (§4)
      ├─► BenchmarkAssembler: injeta percentis de rota/elo para este jogador (§4)
      │
      ▼
 EvidencePacket  (~3,5–4,5k tokens, totalmente tipado, todo número pré-calculado)
      │
      ├──────────────┬──────────────┬──────────────┐   ← rodam em PARALELO
      ▼              ▼              ▼              ▼
 laning_analyst  macro_analyst  economy_analyst  fight_analyst
 (~1,4k tok in)  (~1,2k)        (~1,0k)          (~1,3k)
      │              │              │              │
      └──────────────┴──────┬───────┴──────────────┘
                            ▼
                   head_coach (unifica, deduplica, ranqueia)   ~2k tok in
                            ▼
                   CoachingReport → FactValidator (§4.5) → UI
```

**Por que quatro passes pequenos em vez de um grande — esta é a decisão que sustenta a §1.**

1. **É o que torna modelos de 8B viáveis.** Cada analista vê ~1,2k tokens e responde a uma única
   pergunta. Prompts longos e multiobjetivo são exatamente onde modelos pequenos desabam; prompts
   curtos de objetivo único são onde eles ficam quase indistinguíveis dos grandes.
2. **Paraleliza.** Quatro chamadas concorrentes contra o Groq terminam mais ou menos no tempo de uma.
3. **Cotas de tier gratuito são contadas em requisições, mas a *capacidade* é limitada por
   contexto.** Cinco requisições pequenas cabem confortavelmente em todo tier gratuito; uma
   requisição de 30k tokens não cabe em alguns deles.
4. **Falhas ficam isoladas.** Se o `fight_analyst` devolver JSON malformado, você refaz 1,2k tokens,
   não a análise inteira.
5. **Torna o harness de avaliação tratável.** Você pontua findings de lane independentemente dos de
   macro e sabe qual prompt regrediu.

O custo é um passe de merge e o risco de findings duplicados — tratado por deduplicação em
`(category, timestamp_ms ± 30s)` no `head_coach`, com o merge *ranqueando* em vez de reescrever,
para que as citações de evidência sobrevivam intactas.

### Pipeline P2 — Revisão multimodal de VOD (Modos B/C)

A percepção-chave: **quando existe um `matchId` correspondente, você quase não precisa de visão.** A
timeline já entrega abates, itens, wards, objetivos, ouro e posição-por-minuto como verdade
absoluta. A visão só é necessária para as cinco coisas que a telemetria não enxerga:

1. Para onde a câmera estava apontada (proxy de consciência de mapa)
2. Estado da wave / posicionamento dos minions
3. Uso de habilidades e cooldowns entre os limites de frame de 60 s
4. Se um inimigo estava *visivelmente* no minimapa antes de uma morte
5. Tudo, quando não existe `matchId` nenhum (Modo C sem correspondência)

Então a visão roda em **frames amostrados nos momentos treináveis pré-identificados**, nunca em
streaming:

```
CoachableMoment[] (da §2, ex.: 9 mortes + 4 lutas de objetivo + 3 erros de recall)
      │
      ▼  por momento, extrai 3 frames em t-5s, t-1s, t+2s  (seek preciso com PyAV)
      │
      ▼  recorta ROIs fixas: minimapa / ouro-HUD / cs-HUD / relógio  ← normalizadas por resolução
      │
      ├─► RapidOCR (ONNX, CPU, ~15 ms/recorte) para relógio + números  ← barato, determinístico
      └─► VLM só para ícones do minimapa + estado da wave           ← caro, amostrado
      │
      ▼
 FrameObservation[]  →  mesclado ao MatchFacts como evidência T3
      │
      ▼
 P1 roda sem mudanças, agora com evidência mais rica
```

**Orçamento:** 16 momentos × 3 frames = 48 frames, mas só ~16 chegam ao VLM (o frame `t-1s` de cada
momento). A ~800 tokens de imagem cada, isso dá ~13k tokens de imagem para uma partida inteira — uma
chamada ao Gemini Flash, ou ~40 s num Qwen2.5-VL-7B local. Compare com captura ingênua a 1 fps de uma
partida de 30 minutos: 1.800 frames, ~1,4 milhão de tokens de imagem. **Redução de ~100×, e saída
melhor**, porque cada frame sobrevivente é um que a timeline já indicou ser decisivo.

**Atalho do Modo C.** Se o provedor na nuvem for o Gemini e o usuário forneceu um vídeo, pule a
extração de frames por completo: suba o arquivo e peça observações nos timestamps dos momentos em
uma única chamada. Cai de volta no pipeline de frames em qualquer outro provedor.

---

## 1.5 O sistema de failover / híbrido

### Declaração de capacidades, não nomes de modelo

O roteador nunca raciocina sobre nomes de modelo. Todo provedor declara capacidades; toda tarefa
declara requisitos; o roteador resolve a restrição.

```python
# riftcoach/llm/base.py
class Capability(str, Enum):
    TEXT = "text"; VISION = "vision"; VIDEO_NATIVE = "video"
    JSON_SCHEMA = "json_schema"; LONG_CTX_32K = "ctx32k"

@dataclass(frozen=True)
class ProviderProfile:
    name: str                       # "ollama:qwen3:14b"
    caps: frozenset[Capability]
    ctx_tokens: int
    est_tok_per_s: float            # medido na primeira execução, em cache
    cost_class: Literal["local", "free_cloud", "paid"]
    rate_limit: RateLimit | None    # token bucket, aplicado no cliente
    privacy: Literal["local_only", "leaves_machine"]

@dataclass(frozen=True)
class TaskSpec:
    name: str                       # "laning_analyst"
    requires: frozenset[Capability]
    est_input_tokens: int
    latency_class: Literal["batch", "interactive"]
```

### Detecção de hardware (só na primeira execução, salva em `~/.riftcoach/hardware.json`)

```python
def probe() -> HardwareTier:
    # 1. NVIDIA: pynvml.nvmlDeviceGetMemoryInfo -> VRAM total
    # 2. AMD/Intel: não exige torch — lê `wmic path win32_VideoController`
    #    no Windows, /sys/class/drm no Linux
    # 3. Apple: platform.machine() == "arm64" -> memória unificada via sysctl hw.memsize
    # 4. Ollama acessível? GET http://127.0.0.1:11434/api/tags
    # 5. Quais das tags recomendadas já foram baixadas?
```

Mapeie VRAM → tier usando a tabela da §1.2, **subtraindo a folga para o cache KV de 32k de contexto**
(cerca de 2–4 GB para um 14B em Q4). Classificar apenas pelos pesos é o erro clássico: o modelo
carrega, aí estoura a memória ou vaza para a RAM lá pelo token 6.000, e o usuário culpa o app.

### Algoritmo de seleção

```python
async def select(self, task: TaskSpec) -> ProviderProfile:
    pool = [p for p in self.providers
            if task.requires <= p.caps
            and p.ctx_tokens >= task.est_input_tokens * 1.4      # folga para a saída
            and self.breaker.is_closed(p.name)                    # §1.6
            and self.limiter.has_budget(p.name)]
    if not pool:
        raise NoViableProvider(task, self._diagnose())            # mensagem acionável, nunca um 500
    return min(pool, key=lambda p: self.policy.rank(p, task))
```

A ordenação padrão de `policy.rank`, por prioridade:

1. **Respeite a configuração de privacidade do usuário.** `privacy_mode=strict` filtra tudo que seja
   `leaves_machine`, ponto final, antes de qualquer outra consideração.
2. **`cost_class`:** `local` < `free_cloud` < `paid`. Local primeiro não é só questão de custo — ele
   não tem cota, então trabalho em lote nunca queima a franquia diária de que o caminho interativo
   precisa.
3. **Classe de latência:** para tarefas `interactive`, inverta a ordem quando o local tiver
   `est_tok_per_s < 15`. Uma espera de 90 segundos para uma pergunta de follow-up é uma
   funcionalidade quebrada; 90 segundos para um relatório em lote está de bom tamanho.
4. **Throughput medido** como critério de desempate.

### Escada de degradação (explícita e visível ao usuário)

```
L0  modelo local T3, 5 passes locais, visão local        "Local completo"
L1  texto local + visão na nuvem                          "Híbrido — visão na nuvem"
L2  texto na nuvem + visão na nuvem                       "Nuvem"
L3  texto na nuvem, visão DESATIVADA                      "Reduzido — sem enriquecimento de VOD"
L4  só telemetria, passe único unificado, contexto 8k     "Mínimo"
L5  relatório determinístico: métricas + benchmarks + regras   "Sem IA disponível"
```

**L5 é obrigatório e não é um prêmio de consolação.** Se todos os provedores estiverem fora e todas
as cotas queimadas, o app ainda renderiza CS@10 contra o percentil do elo, curvas de diferença de
ouro, mapas de calor de local de morte, eficiência de recall e disparos do motor de regras ("você
comprou Sentinela de Controle em apenas 2 dos 9 recalls"). Aproximadamente 40% do valor do produto é
calculado na §2 e não precisa de modelo nenhum. Entregar o L5 primeiro também significa que o resto
do app é testável antes de qualquer modelo estar conectado.

### Circuit breaker + limitação de taxa

```python
class ProviderBreaker:
    """Por provedor, três estados, ciente do tipo de falha."""
    # 429 / cota           -> ABERTO até a janela de cota resetar (lê Retry-After; senão
    #                         recua até o reset declarado em providers.yaml)
    # 5xx / timeout        -> ABERTO 30s, exponencial até 5min, sondagens em MEIO_ABERTO
    #                         com a tarefa mais barata da fila
    # violação de schema x3 -> ABERTO permanentemente NESTA SESSÃO, com aviso:
    #                         este modelo não sustenta nosso schema de saída; pare de
    #                         queimar cota com ele
```

O terceiro caso importa mais do que parece. Um modelo de tier gratuito que não consegue emitir nosso
JSON de forma confiável vai queimar a cota diária inteira do usuário em retentativas e não produzir
nada. Detecte em três tentativas, descarte, diga ao usuário qual modelo falhou e por quê, e faça
failover. Registre em `~/.riftcoach/compat.jsonl` para que o projeto possa publicar uma matriz de
compatibilidade real construída com dados da comunidade.

### Saída estruturada, por provedor

Nunca faça parsing de texto livre. Em ordem de confiabilidade:

1. **Ollama** — `format: <json-schema>` (decodificação nativa restrita por JSON Schema). Garantia
   forte.
2. **Gemini** — `response_schema` + `response_mime_type="application/json"`. Garantia forte.
3. **Groq / OpenRouter** — `response_format={"type":"json_object"}` + schema no prompt. Garantia
   fraca → envolva em um loop de validar-e-reparar com Pydantic, máximo 2 tentativas, depois o
   breaker.

```python
async def generate_validated[T: BaseModel](self, spec, prompt, model: type[T]) -> T:
    for attempt in range(3):
        raw = await self._call(spec, prompt, schema=model.model_json_schema())
        try:
            return model.model_validate_json(raw)
        except ValidationError as e:
            prompt = repair_prompt(prompt, raw, e)      # devolve os erros literalmente
            self.breaker.record_schema_violation(spec.provider)
    raise SchemaExhausted(spec.provider)
```

---

## 1.6 Configuração padrão concreta

```yaml
# config/providers.yaml — padrões distribuídos, editáveis pelo usuário
routing:
  privacy_mode: relaxed          # strict = nunca sai da máquina
  prefer: cost                   # cost | speed | quality

providers:
  - name: ollama
    base_url: http://127.0.0.1:11434/v1
    cost_class: local
    privacy: local_only
    models:
      text:   {tier1: qwen3:8b, tier2: qwen3:14b, tier3: qwen3:30b-a3b}
      vision: {tier1: qwen2.5vl:3b, tier2: qwen2.5vl:7b, tier3: qwen2.5vl:7b}
    options: {num_ctx: 32768, temperature: 0.3}

  - name: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_ref: keyring:riftcoach/gemini      # nunca inline
    cost_class: free_cloud
    privacy: leaves_machine
    caps: [text, vision, video, json_schema, ctx32k]
    models: {text: gemini-2.5-flash, vision: gemini-2.5-flash, video: gemini-2.5-flash}

  - name: groq
    base_url: https://api.groq.com/openai/v1
    api_key_ref: keyring:riftcoach/groq
    cost_class: free_cloud
    caps: [text, json_schema, ctx32k]
    latency_preference: interactive
```

IDs de modelo ficam em configuração precisamente porque envelhecem. O contrato do roteador é com
`Capability`, nunca com uma string — trocar para o modelo do ano que vem é uma edição de YAML, não
uma mudança de código.
