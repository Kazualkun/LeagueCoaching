# §5 — Stack Técnica e Estrutura do Repositório

🇧🇷 Português · [🇺🇸 English](en/05-stack-and-repo.md)

## 5.1 Linguagem e runtime

**Backend em Python 3.11+.** Não é um debate real: PyAV, RapidOCR, pandas/polars, o ecossistema da
Riot, o ferramental de ML e todo contribuidor capaz de escrever um parser vivem lá. O 3.11 é o piso
por causa de `ExceptionGroup`/`TaskGroup` (o fan-out dos analistas da §1 é um `TaskGroup`) e dos
generics modernos.

**`uv` para gerenciar dependências e o próprio Python.** Essa é uma decisão de distribuição, não de
gosto. Nossos usuários são jogadores de League no Windows, muitos sem Python nenhum. O `uv` instala o
próprio interpretador, resolve em segundos e produz um `uv.lock` versionado. Ele reduz "instale o
Python 3.11, crie um venv, rode pip install, torça para o CUDA bater" a:

```
uv run riftcoach
```

Essa diferença vale mais adoção do que qualquer funcionalidade.

---

## 5.2 Framework de backend — FastAPI

Escolhido por três propriedades de que este app especificamente precisa:

1. **Async nativo.** A carga é fan-out de I/O — API da Riot, Ollama, LLMs na nuvem, o client local na
   2999 — tudo concorrente. Os quatro passes de analista rodam num `TaskGroup`.
2. **WebSockets.** A análise leva de 15 a 90 s. Transmitir progresso (`buscando timeline →
   destilando → analista de rota concluído → ...`) é a diferença entre "funcionando" e "travado".
3. **Pydantic é o framework.** Todo o nosso contrato de dados (§ARCHITECTURE.4) são modelos Pydantic;
   o schema da API, o schema de saída do LLM e a IR interna são os mesmos objetos. Sem duplicação.

**Rejeitados:** Flask (sem async, ergonomia ruim de WS), Django (centrado em ORM, enorme para um app
local), Streamlit *como app principal* (veja abaixo).

---

## 5.3 Frontend — em fases

### v1: SPA Vite + React + TypeScript, buildada em arquivos estáticos, servida pelo FastAPI, aberta no navegador do sistema

### v2: Shell Tauri envolvendo a mesma SPA, com o backend Python como sidecar

**Por que não Streamlit.** É o "caminho rápido" óbvio e é uma armadilha para *este* app. A UX central
é uma linha do tempo navegável com marcadores clicáveis de findings, controlando um replay externo,
com um elemento `<video>` e progresso em streaming. O modelo de execução do Streamlit re-executa o
script a cada interação; isso briga com cada um desses requisitos. Ele é excelente para o **console
de desenvolvimento/depuração** — inspecionar `MatchFacts`, comparar prompts, examinar o parser — então
mantenha-o lá: `tools/inspector/` é um app Streamlit, e esse é um uso genuinamente bom dele.

**Por que não Electron.** ~150 MB de linha de base e um runtime Node entregue a todo usuário, para
uma webview que o sistema operacional já tem.

**Por que navegador primeiro, Tauri depois.** Funciona desde o primeiro dia sem toolchain extra,
mantém viável o deploy headless/servidor (analisar num servidor doméstico, ver pelo celular) e faz
com que o Tauri seja uma mudança de empacotamento depois, não uma reescrita. O Tauri v2 dá um
instalador de ~10 MB usando a webview do sistema e tem suporte de primeira classe a sidecar para
distribuir o binário Python — mas acrescenta uma toolchain Rust no CI e assinatura específica por
plataforma. Isso é problema de v2, não bloqueio de v1.

---

## 5.4 Seleção de bibliotecas

| Necessidade | Escolha | Justificativa |
|---|---|---|
| **Cliente da API da Riot** | **Próprio, sobre `httpx.AsyncClient`** | Dados de partida são imutáveis, então um cache permanente em zstd/SQLite é o maior ganho disponível (§2.6) — e wrappers atrapalham camadas de cache. A superfície de API que usamos são 5 endpoints. Também dá controle exato do limitador de duas janelas e da UX de expiração da chave de desenvolvimento. O `pulsefire` é a melhor opção de terceiros se você discordar; o `riotwatcher` é síncrono e portanto errado aqui. |
| **HTTP** | `httpx` + `tenacity` | Um cliente async para Riot, DDragon e `127.0.0.1:2999`. |
| **Cache / BD** | `sqlite3` + `aiosqlite` + `zstandard` + `sqlite-vec` | Um arquivo, sem daemon, sem Docker. Timeline de 2,5 MB → ~180 KB. |
| **Validação / IR** | `pydantic` v2 | O contrato de dados. |
| **Clientes de LLM** | `openai` (async) para todos os provedores OpenAI-compat; `google-genai` **apenas** para upload de vídeo; `ollama` opcional para gerenciar modelos | Um caminho de código para cinco provedores (§1.3). |
| **Embeddings** | `fastembed` (ONNX CPU) | **Sem dependência de PyTorch.** Inegociável pelo atrito de instalação no Windows (§4.4). |
| **Vídeo** | `av` (PyAV) | Seek preciso em processo; ~15× mais rápido que subprocessos de ffmpeg por frame. |
| **OCR** | `rapidocr-onnxruntime` | ONNX, CPU, sem Paddle/torch. Resolve o relógio da HUD e os números de forma determinística — nunca pague um VLM para ler um contador de ouro de 4 dígitos. |
| **Imagens** | `Pillow` + `numpy` | Recortes de ROI, normalização de resolução. |
| **TTS (opcional)** | `piper-tts` | MIT, ONNX, CPU, offline, ~50 MB. |
| **Segredos** | `keyring` | Gerenciador de Credenciais do Windows / Keychain do macOS / Secret Service. **Nunca** um dotfile. |
| **Configuração** | `pydantic-settings` + `providers.yaml` | Config tipada, override por env, lista de provedores editável pelo usuário. |
| **Jobs** | `asyncio` + uma tabela `jobs` no SQLite | Sem Celery, sem Redis. Um app local monousuário não precisa de broker. |
| **CLI** | `typer` + `rich` | `riftcoach analyze NA1_123456` precisa funcionar headless, para usuários avançados e para CI. |
| **Testes** | `pytest`, `pytest-asyncio`, `respx`, `syrupy` | `respx` mocka o HTTP da Riot/LLM; `syrupy` faz snapshot de `MatchFacts` contra fixtures de referência. |
| **Qualidade** | `ruff`, `mypy --strict` em `core/` e `parse/` | O parser é onde moram os bugs silenciosos de correção; tipifique com rigor. |
| **Empacotamento** | `uv` + `hatchling`; PyInstaller para o lançador de um clique da v1 | |

---

## 5.5 Estrutura do repositório

```
riftcoach-ai/
├── README.md                      # pt-BR
├── README.en.md
├── COMPLIANCE.md                  # a posição frente aos termos da Riot — documento principal
├── COMPLIANCE.en.md
├── CONTRIBUTING.md
├── LICENSE                        # AGPL-3.0 (ver 5.7)
├── pyproject.toml
├── uv.lock
├── .env.example                   # NUNCA chaves reais
├── .gitignore                     # *.key, .env, *.rofl, data/, !.env.example
│
├── riftcoach/
│   ├── __main__.py                # `uv run riftcoach` -> sobe a API + abre o navegador
│   ├── cli.py                     # typer: analyze / sync-patch / bench / doctor
│   ├── config.py                  # pydantic-settings, integração com keyring
│   │
│   ├── core/
│   │   ├── schema.py              # Finding, Evidence, CoachingReport  <- O contrato
│   │   ├── errors.py              # LiveGameRefused, NoViableProvider, SchemaExhausted
│   │   └── zones.py               # enum MapZone + índice de polígonos + side_relative()
│   │
│   ├── riot/
│   │   ├── client.py              # httpx + limitador de duas janelas + UX de chave expirada
│   │   ├── cache.py               # SQLite + zstd, permanente para recursos imutáveis
│   │   └── models.py              # views tipadas finas sobre o JSON bruto da Riot
│   │
│   ├── parse/                     # ===== §2 =====
│   │   ├── distill.py             # timeline -> MatchFacts  (a redução de 150x)
│   │   ├── facts.py               # a IR MatchFacts
│   │   ├── deaths.py              # enriquecimento de DeathContext
│   │   ├── economy.py             # recalls, caminho de build, timings de spike
│   │   ├── objectives.py          # ObjectiveEvent + análise da janela de preparação
│   │   ├── waves.py               # wave_proxy (T2) — honesto sobre seus limites
│   │   ├── moments.py             # MomentBuilder -> CoachableMoment[]
│   │   └── render.py              # MatchFacts -> o formato de texto compacto (NÃO json)
│   │
│   ├── knowledge/                 # ===== §4 =====
│   │   ├── sync.py                # DDragon/CDragon -> SQLite
│   │   ├── patchdiff.py           # diff estrutural entre versões -> changelog
│   │   ├── retrieve.py            # filtro de gatilhos -> fastembed -> sqlite-vec
│   │   ├── benchmarks.py          # percentis em parquet + fallback do próprio histórico
│   │   └── validator.py           # FactValidator: Aho-Corasick para entidades e stats
│   │
│   ├── llm/                       # ===== §1 =====
│   │   ├── base.py                # Capability, ProviderProfile, TaskSpec
│   │   ├── router.py              # select(), escada de degradação, política de privacidade
│   │   ├── breaker.py             # circuit breaker + token bucket
│   │   ├── hardware.py            # detecção de VRAM -> tier (incluindo folga de cache KV)
│   │   └── providers/
│   │       ├── openai_compat.py   # ollama | groq | openrouter | cerebras | gemini-compat
│   │       └── gemini_native.py   # SOMENTE o caminho de upload de vídeo
│   │
│   ├── analysis/
│   │   ├── packet.py              # montagem do EvidencePacket
│   │   ├── prompts/               # ---- o diretório de maior rotatividade do repo ----
│   │   │   ├── laning.md
│   │   │   ├── macro.md
│   │   │   ├── economy.md
│   │   │   ├── fights.md
│   │   │   ├── head_coach.md
│   │   │   └── followup.md
│   │   ├── analysts.py            # o fan-out de 4 vias em TaskGroup
│   │   ├── merge.py               # dedup em (category, t +/- 30s), ranqueia, top_three
│   │   └── rules.py               # motor de regras determinístico — sustenta o L5 (sem IA)
│   │
│   ├── replay/                    # ===== §3 =====
│   │   ├── guard.py               # ReplayGuard — o intertravamento de conformidade
│   │   ├── controller.py          # seek_to(), câmera, calibração de relógio
│   │   ├── rofl.py                # só metadados; nunca decodifica chunks
│   │   └── sinks.py               # ClientReplaySink | VideoFileSink | BrowserVideoSink
│   │
│   ├── vision/
│   │   ├── sampler.py             # 3 frames por momento via seek preciso do PyAV
│   │   ├── rois.py                # regiões de recorte de HUD/minimapa normalizadas
│   │   ├── ocr.py                 # RapidOCR: relógio, ouro, cs
│   │   └── vlm.py                 # extração de FrameObservation — só percepção
│   │
│   └── api/
│       ├── app.py                 # FastAPI + mount da SPA estática
│       ├── routes.py              # /analyze /report/{id} /seek /followup /doctor
│       └── ws.py                  # streaming de progresso
│
├── web/                           # Vite + React + TS
│   ├── src/
│   │   ├── components/Timeline.tsx        # curva de diferença de ouro + marcadores
│   │   ├── components/FindingCard.tsx     # claim / evidência(nível) / correção / [Pular]
│   │   ├── components/ReplayBridge.ts     # POST /seek  ou  <video>.currentTime
│   │   └── components/SetupWizard.tsx     # entrada de chave, detecção de hardware, pull
│   └── package.json
│
├── knowledge/                     # ---- CONTRIBUA AQUI, sem precisar de Python ----
│   ├── principles/                # markdown + frontmatter (ver §4.3)
│   └── benchmarks/                # {patch}.parquet, gerado pelo CI
│
├── evals/                         # ===== a camada de credibilidade =====
│   ├── fixtures/                  # ~30 partidas anonimizadas, PUUIDs limpos
│   ├── golden/                    # findings esperados, anotados por humanos
│   ├── rubric.md                  # o que significa "um bom finding"
│   ├── run.py                     # pontua cada provedor -> matriz de compatibilidade
│   └── results/                   # histórico versionado; regressões ficam visíveis nos PRs
│
├── tools/
│   ├── inspector/                 # console Streamlit de dev: inspeciona IR, compara prompts
│   └── sample_matches.py          # job do mantenedor: gera o parquet de benchmarks
│
├── tests/
│   ├── fixtures/                  # timelines de referência: remake, ARAM, troca de rota, 60min
│   └── test_distill.py            # snapshots syrupy de MatchFacts
│
├── docs/
│   ├── ARCHITECTURE.md            # pt-BR
│   ├── 01-model-routing.md
│   ├── 02-data-pipeline.md
│   ├── 03-vod-review.md
│   ├── 04-knowledge-base.md
│   ├── 05-stack-and-repo.md
│   └── en/                        # versões em inglês de todos os documentos acima
│
└── .github/workflows/
    ├── ci.yml                     # ruff + mypy + pytest
    ├── patch-sync.yml             # diário: detecta nova versão do DDragon -> abre PR do diff
    ├── benchmarks.yml             # por patch: amostra partidas -> parquet como asset
    └── evals.yml                  # noturno: roda avaliações nos tiers gratuitos -> publica matriz
```

### Notas estruturais

- **`parse/` não importa nada de LLM e `llm/` não importa nada da Riot.** A camada de destilação
  precisa ser testável com zero rede e zero modelos. Se essa fronteira se mantiver, o parser continua
  correto.
- **`analysis/prompts/*.md` são arquivos, não literais de string.** Prompts mudam o tempo todo;
  mantê-los como Markdown revisável significa que um PR de prompt tem um diff legível e que
  contribuidores que não programam em Python conseguem abrir um.
- **`analysis/rules.py` não é um detalhe secundário.** Ele é o relatório L5 sem IA (§1.5) e a fonte
  dos gatilhos determinísticos que guiam a recuperação do RAG (§4.4). Construa primeiro — é o
  caminho mais rápido para um produto funcional e é o que torna tudo acima dele testável.
- **`evals/` é versionado, incluindo os resultados.** Um projeto open source de IA sem harness de
  avaliação não pode fazer afirmações sobre precisão, e não consegue saber se um PR de prompt
  ajudou. A matriz publicada de compatibilidade de provedores ("quais modelos gratuitos realmente
  funcionam") é também, na prática, o artefato mais útil que o projeto vai produzir para seus
  usuários.

---

## 5.6 Segredos — com um exemplo real

Uma chave de desenvolvimento da Riot foi encontrada em texto puro na raiz do projeto durante a
montagem do esqueleto (`.txt`). Essa é exatamente a falha que esta seção previne. Regras:

1. Chaves vão para o cofre do SO via `keyring.set_password("riftcoach", "riot", chave)`.
2. `.env` está no gitignore; apenas `.env.example` com valores de exemplo é versionado.
3. `*.txt` na raiz do repositório **não** é seguro por padrão — use gitignore com agressividade e
   adicione um hook de `pre-commit` rodando `detect-secrets` ou `gitleaks`.
4. O assistente de configuração grava a chave no cofre e oferece apagar o arquivo de origem.
5. Chaves de desenvolvimento expiram a cada 24 h. Detecte o `403` de expiração, mostre um link
   direto para `developer.riotgames.com` e oriente os usuários a solicitar uma Personal API Key.

Como essa chave está agora num arquivo de texto puro em um caminho no Desktop, **gere uma nova**
antes de qualquer outra coisa.

---

## 5.7 Licenciamento

**AGPL-3.0** para a aplicação. Ela torna a promessa de "100% gratuito" exigível: qualquer um que rode
um RiftCoach modificado como serviço hospedado precisa publicar suas mudanças, o que impede o
desfecho ruim mais provável — alguém empacotar isso num SaaS pago e superar o original em marketing.

**CC-BY-SA-4.0** para `knowledge/principles/`, para que o corpus de coaching possa circular
independentemente do código.

Aviso padrão da Riot no README: *RiftCoach AI não é endossado pela Riot Games e não reflete as visões
ou opiniões da Riot Games ou de qualquer pessoa oficialmente envolvida na produção ou gestão das
propriedades da Riot Games.*

---

## 5.8 Ordem de construção (o plano de verdade)

A ordem de dependências que chega mais rápido a um produto utilizável, com cada etapa sendo útil por
conta própria:

| Etapa | Entrega | Por que nesta ordem |
|---|---|---|
| **0** | Cliente `riot/` + cache + fixtures de referência | Tudo depende disso; as fixtures tornam o resto testável offline. |
| **1** | `parse/` → `MatchFacts` + `render.py` + testes de snapshot | O trabalho difícil, de alto valor e independente de modelo. Bem feito, tudo acima dele fica fácil. |
| **2** | `knowledge/sync.py` + `benchmarks` + `rules.py` → **relatório L5** | **Um produto genuinamente útil, sem IA nenhuma.** Publique isso. |
| **3** | Roteador `llm/` + os 4+1 passes de `analysis/` | Agora o LLM tem entrada limpa e um plano B que já funciona. |
| **4** | `evals/` + matriz de compatibilidade | Antes de adicionar funcionalidades, prove as que você já tem. |
| **5** | SPA `web/` + navegação de `replay/` | A UX principal, sobre uma fundação que já está correta. |
| **6** | Enriquecimento por `vision/`, empacotamento Tauri, LoRA de estilo opcional | Polimento. |

A etapa 2 é a que se deve resistir a pular. Um relatório analítico determinístico que funciona sem
GPU, sem chave de API e sem modelo é ao mesmo tempo um produto real e a rede de segurança que mantém
o sistema inteiro honesto.
