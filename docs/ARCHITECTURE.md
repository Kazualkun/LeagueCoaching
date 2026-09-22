# RiftCoach AI — Blueprint de Arquitetura

> Coaching de League of Legends pós-partida. 100% gratuito, 100% compatível com as regras da Riot,
> self-hosted.
> Status: blueprint de projeto v1.0 · Patch de referência: 15.x/16.x · Última revisão: 2026-09-21

🇧🇷 Português · [🇺🇸 English](en/ARCHITECTURE.md)

---

## 0. A tese em um parágrafo

Quase todo "coach de LoL com IA" falha pelo mesmo motivo: ele empurra um JSON de 2 MB do
`match-v5/timeline` (ou um fluxo de screenshots brutos) para um LLM genérico e pede que ele "dê
coaching". O modelo então faz, ao mesmo tempo, **percepção**, **aritmética**, **memorização de
fatos** e **julgamento** — e ele é ruim nos três primeiros. O RiftCoach inverte isso. O Python faz
percepção e aritmética de forma determinística, um banco de dados de patch versionado fornece os
fatos, e o LLM é usado **apenas para julgamento sobre um pacote de evidências pré-digerido, citado e
de ~4 mil tokens.** Essa inversão sozinha é o que faz um modelo local de 8B produzir conselhos de
nível Diamante em vez de besteira dita com confiança, e é o que permite que a coisa toda rode em um
tier gratuito.

---

## 1. Restrições inegociáveis

| # | Restrição | Consequência arquitetural |
|---|---|---|
| C1 | **Zero assistência em tempo real.** Nenhum overlay, alerta ou inferência durante partida ao vivo. | A porta 2999 só é acessada quando `GET /replay/playback` retorna 200 (modo replay). Uma partida ao vivo devolve 404 nessa rota → abortar imediatamente. Ver `ReplayGuard`, §6. |
| C2 | **Custo zero obrigatório.** | Todo caminho tem uma implementação local (Ollama) e uma gratuita na nuvem. Nenhuma funcionalidade pode existir apenas atrás de uma chave paga. |
| C3 | **Chave é do usuário.** | Nenhum servidor operado pelo RiftCoach, nenhum proxy, nenhuma telemetria. Chaves ficam no cofre de credenciais do SO, nunca no repositório. |
| C4 | **Nunca afirmar um fato desatualizado.** | Fatos numéricos de itens/campeões são injetados de um banco fixado no patch; um validador pós-geração rejeita qualquer entidade que o banco não conheça. §4. |
| C5 | **Toda afirmação é ancorada.** | Todo `Finding` carrega `timestamp_ms` + `evidence[]` + `evidence_tier`. Texto sem âncora é bug. |

---

## 2. Decomposição do sistema

```
                          +-------------------------------------------+
                          |          CAMADA DE INGESTÃO               |
                          |  riot/match-v5 · riot/timeline · .rofl    |
                          |  metadados · arquivo de vídeo · URL       |
                          +---------------------+---------------------+
                                                | bruto, imutável, cache eterno (SQLite+zstd)
                          +---------------------v---------------------+
                          |       CAMADA DE DESTILAÇÃO  (§2)          |
                          |  filtro de perspectiva -> zonas ->        |
                          |  resumo por fase -> métricas derivadas -> |
                          |  MatchFacts IR   (~500k tok -> ~4k tok)   |
                          +---------------------+---------------------+
                                                |
              +---------------------------------+---------------------------------+
              |                                 |                                 |
   +----------v----------+       +--------------v--------------+     +------------v-----------+
   | CONHECIMENTO   §4   |       |  PERCEPÇÃO          §3      |     |  BENCHMARKS       §4   |
   | banco de patch      |       |  VLM/OCR em frames          |     | percentis por rota/elo |
   | (diff do DDragon) + |       |  *amostrados*, só onde a    |     | (parquet embarcado +   |
   | corpus de princípios|       |  telemetria é cega          |     |  histórico do usuário) |
   +----------+----------+       +--------------+--------------+     +------------+-----------+
              +---------------------------------+---------------------------------+
                                                |  EvidencePacket (tipado, citado)
                          +---------------------v---------------------+
                          |       CAMADA DE RACIOCÍNIO  (§1)          |
                          |  Mistura de Analistas: lane · macro ·     |
                          |  economia · lutas  ->  merge head_coach   |
                          |  via ModelRouter (Ollama <-> nuvem free)  |
                          +---------------------+---------------------+
                                                | List[Finding] (validado por schema)
                          +---------------------v---------------------+
                          |     CAMADA DE APRESENTAÇÃO  (§3, §5)      |
                          |  SPA React · linha do tempo de momentos · |
                          |  clique -> seek na Replay API / <video>   |
                          +-------------------------------------------+
```

---

## 3. Registro de decisões (a versão curta)

| Pergunta | Decisão | Motivo em uma linha |
|---|---|---|
| Modelo de raciocínio textual | **Qwen3-14B / 30B-A3B local · Gemini 2.5 Flash na nuvem** | Melhor obediência a instruções por GB de VRAM; o tier gratuito do Flash é o único com RPD alto *e* vídeo nativo. |
| Modelo de visão | **Qwen2.5-VL-7B local · Gemini Flash na nuvem** | O Qwen2.5-VL é o único VLM aberto com OCR confiável de texto pequeno de HUD *e* ancoragem espacial. |
| Papel da visão | **Somente percepção, nunca julgamento** | Isola o elo mais fraco; o raciocínio permanece em um modelo de texto trocável. |
| Fonte primária de evidência | **Timeline do Match-v5, não pixels** | A timeline é verdade absoluta e é gratuita; a visão só cobre os pontos cegos dela. |
| Formato do prompt | **4 passes especialistas pequenos + 1 merge** | Modelos de 8B desabam em prompts longos multiobjetivo; passes pequenos também cabem no RPM dos tiers gratuitos. |
| Revisão de VOD | **Opção B+ — momentos pré-processados controlando a Replay API do client** | Latência zero, custo zero, risco de conformidade zero; UX estritamente melhor que captura de tela em streaming. |
| Conhecimento | **RAG + prompts dinâmicos. Sem fine-tune de conhecimento.** | Patches saem a cada 2 semanas; não existe dataset licenciável de decisões de elo alto; e um LoRA não pode ser implantado no caminho da nuvem. |
| Fonte das notas de patch | **Diff automático do DataDragon entre versões** | Verdade gerada por máquina; sem scraping de HTML, sem alucinação. |
| Backend | **Python 3.11 + FastAPI + uv** | Domina o ecossistema de CV/dados; o `uv` transforma a instalação no Windows em um comando. |
| Frontend | **SPA Vite + React servida pelo FastAPI (v1) → shell Tauri (v2)** | Velocidade para contribuidores agora, instalador de 10 MB depois. Streamlit descartado: não consegue fazer uma linha do tempo navegável. |
| Cliente da Riot | **Cliente `httpx` próprio** | Dados de partida são imutáveis → cache permanente é o maior ganho isolado, e wrappers atrapalham isso. |
| Cliente de LLM | **Um único `AsyncOpenAI` contra 5 `base_url`s diferentes** | Ollama, Groq, OpenRouter, Cerebras e Gemini falam OpenAI-compat. Um só caminho de código. |

Raciocínio completo por seção:

- [§1 — Modelo e Motor de Inferência](01-model-routing.md)
- [§2 — Pipeline de Dados e Otimização de Tokens](02-data-pipeline.md)
- [§3 — Revisão Interativa de VOD](03-vod-review.md)
- [§4 — Base de Conhecimento](04-knowledge-base.md)
- [§5 — Stack Técnica e Repositório](05-stack-and-repo.md)

---

## 4. O contrato de dados central

Tudo no sistema é uma transformação entre quatro tipos. Congele esses primeiro; todo o resto é
substituível.

```python
# riftcoach/core/schema.py
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class EvidenceTier(str, Enum):
    T1_MEASURED = "T1"   # lido direto da telemetria da Riot, ex.: "CS@10 = 61"
    T2_DERIVED  = "T2"   # calculado da telemetria + premissa declarada, ex.: "perdeu prio às 14:10"
    T3_INFERRED = "T3"   # estimado por visão ou heurística, ex.: "a wave estava em slow push"

class Evidence(BaseModel):
    tier: EvidenceTier
    timestamp_ms: int
    statement: str                      # "Morreu no MID_RIVER 38s antes do Dragão nascer"
    source: Literal["timeline", "match", "vision", "patchdb", "benchmark"]
    assumption: str | None = None       # OBRIGATÓRIO quando tier != T1

class Finding(BaseModel):
    category: Literal["wave", "trading", "recall", "itemization",
                      "vision", "objective", "positioning", "tempo", "macro"]
    phase: Literal["early", "mid", "late"]
    severity: int = Field(ge=1, le=5)   # 5 = custou a partida
    timestamp_ms: int                   # âncora -> alvo do seek no replay
    claim: str                          # o que deu errado, em uma frase
    evidence: list[Evidence] = Field(min_length=1)   # afirmações sem âncora são rejeitadas
    fix: str                            # a ação alternativa concreta
    drill: str | None = None            # exercício prático para a próxima partida
    confidence: float = Field(ge=0, le=1)

class CoachingReport(BaseModel):
    match_id: str
    patch: str                          # ex.: "15.18.1" — fixa o relatório a um meta
    puuid: str
    model_trace: dict[str, str]         # {"laning": "ollama/qwen3:14b", "merge": "gemini-2.5-flash"}
    findings: list[Finding]
    top_three: list[int]                # índices em findings — a única coisa mostrada primeiro
```

**Por que `EvidenceTier` existe.** A restrição é "alta acionabilidade *e* precisão". Conselho de wave
management é o coaching de maior valor que existe, e é também justamente aquilo que a API da Riot
não consegue medir — não existe campo de estado da wave. Um sistema que mistura silenciosamente
contagens de CS medidas com estados de wave chutados produz conselhos que o usuário não pode
confiar nem verificar. O sistema de níveis obriga o modelo a dizer *"seu ritmo de CS e sua posição
indicam que você estava em slow push (T2)"* em vez de *"você estava em slow push"*. É o mecanismo de
precisão mais barato do projeto, e é o que torna o harness de avaliação (§5) pontuável.

**Por que todo `Finding` carrega `timestamp_ms`.** É a chave de junção entre a camada de raciocínio e
o replay. Um finding sem timestamp não pode ser clicado, não pode ser verificado pelo usuário e não
pode ser pontuado pelo harness de avaliação. Force isso no nível do schema, para que nenhuma mudança
de prompt consiga regredir.

---

## 5. Modos de processamento

| Modo | Entradas | Evidência disponível | Custo | Observações |
|---|---|---|---|---|
| **A. Telemetria** (padrão) | `matchId` | T1/T2 | grátis, ~15 s | 80% do valor. Funciona sem GPU, sem replay, sem vídeo. |
| **B. Replay Sincronizado** | `matchId` + `.rofl` + client do League rodando | T1/T2 + câmera/minimapa (T3) | grátis, ~60 s | O modo interativo principal. §3. |
| **C. VOD em vídeo** | arquivo/URL de vídeo (+ `matchId` opcional) | T1/T2 se houver match, senão só T3 | grátis, 2–10 min | Para conteúdo de coaching, streams ou contas que você não possui. |

O Modo A precisa ser completo e excelente **sozinho**. B e C são enriquecimento. Qualquer projeto em
que o valor central dependa de um replay ou de uma GPU viola C2.

---

## 6. `ReplayGuard` — o intertravamento de conformidade

Este é o único componente que conversa com `127.0.0.1:2999`. Ele é deliberadamente pequeno,
deliberadamente sem graça, e falha fechado.

```python
# riftcoach/replay/guard.py
import httpx
from riftcoach.core.errors import LiveGameRefused

LIVE_CLIENT_CERT = "certs/riotgames.pem"   # a Riot publica isso; NÃO use verify=False

class ReplayGuard:
    """Permite I/O com o client local SOMENTE enquanto um REPLAY está rodando.

    `GET /replay/playback` existe exclusivamente no modo replay. Durante uma partida
    ao vivo essa rota devolve 404 enquanto `/liveclientdata/*` continua respondendo —
    então um 404 aqui é sinal positivo de que uma partida ao vivo pode estar em
    andamento. Nós recusamos, incondicionalmente.
    """
    BASE = "https://127.0.0.1:2999"

    async def assert_replay_mode(self, client: httpx.AsyncClient) -> float:
        try:
            r = await client.get(f"{self.BASE}/replay/playback", timeout=2.0)
        except httpx.ConnectError:
            raise LiveGameRefused("O client do League não está rodando.")
        if r.status_code == 404:
            raise LiveGameRefused(
                "Parece haver uma partida ao vivo em andamento. O RiftCoach nunca roda "
                "durante partidas ao vivo — inicie um replay e tente de novo."
            )
        r.raise_for_status()
        return r.json()["length"]
```

Todo ponto de chamada envolve seu trabalho em `async with guard.session() as s:`, que revalida antes
de **cada** requisição, não uma vez por sessão — o usuário pode sair do replay via alt-tab e cair na
seleção de campeões no meio da análise, e um job em lote de 5 minutos precisa perceber isso.

**Posição de política.** A Riot documenta tanto a Live Client Data API quanto a Replay API e permite
ferramentas construídas sobre elas; o que ela proíbe é software que automatiza a jogabilidade ou
expõe informação que o jogador não poderia obter de outra forma. O RiftCoach não toca em nenhuma
dessas superfícies durante o jogo ao vivo. Publique essa posição no `COMPLIANCE.md` e no README,
porque *"isso dá ban?"* é a primeira pergunta de todo usuário em potencial, e uma resposta
específica e verificável é um recurso de crescimento.

---

## 7. O que este projeto deliberadamente *não* faz

Listado para que contribuidores parem de propor de novo:

- **Nada de decodificar frames de `.rofl`.** O conteúdo de chunks/keyframes é criptografado e a
  chave não é recuperável de um arquivo de replay pessoal depois do fato. Decodificadores da
  comunidade existem, são incompletos e quebram na maioria dos patches. Lemos os *metadados* do
  `.rofl` (`statsJson`) para identificar a partida e entregamos o arquivo ao client para reprodução.
  Quem promete telemetria de posição a partir de um `.rofl` bruto está enganado.
- **Nada de overlay ao vivo.** Nunca. Ver C1.
- **Nada de scraping de op.gg / u.gg / porofessor.** Viola os termos e é frágil. Os benchmarks vêm
  de amostragem agregada da API da Riot feita por nós mesmos (§4).
- **Nada de fine-tune de conhecimento.** Ver [§4](04-knowledge-base.md) para o argumento completo.
- **Nada de serviço hospedado.** Ser self-hosted é o que mantém C2 e C3 verdadeiros para sempre.
