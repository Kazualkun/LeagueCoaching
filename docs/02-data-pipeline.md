# §2 — Pipeline de Dados e Otimização de Tokens

🇧🇷 Português · [🇺🇸 English](en/02-data-pipeline.md)

## 2.1 O problema, quantificado

Uma partida de 32 minutos no Summoner's Rift:

| Artefato | Tamanho bruto | ~Tokens |
|---|---|---|
| `match-v5/matches/{id}` | ~120 KB | ~30.000 |
| `match-v5/matches/{id}/timeline` | 1,8–3,2 MB | **450.000–800.000** |

A timeline são 32 frames (um por 60 s) com 10 `participantFrames` cada, mais 800–2.000 eventos. O
volume não está na parte interessante — está nos arrays `victimDamageReceived` /
`victimDamageDealt` (todo `CHAMPION_KILL` carrega o detalhamento completo de dano por fonte, muitas
vezes 20+ objetos), em `championStats` (todo frame carrega 20 atributos de combate para os 10
jogadores) e no ruído de `ITEM_DESTROYED`/`ITEM_UNDO`.

**Meta: ≤ 4.500 tokens para o pacote de evidências completo.** Isso é uma redução de ~150×, e ela
precisa *aumentar* o sinal útil, não apenas encolher o payload.

---

## 2.2 O que realmente extraímos (lista exata de campos)

### De `match-v5/matches/{id}` — `info.participants[]`

Manter, para o jogador em foco e o oponente de rota:

```
puuid, championName, teamPosition, teamId, win, champLevel
kills, deaths, assists
totalMinionsKilled, neutralMinionsKilled          # soma -> cs
goldEarned, goldSpent
totalDamageDealtToChampions, damageDealtToTurrets, damageDealtToObjectives
totalDamageTaken, totalHealsOnTeammates, totalTimeSpentDead
visionScore, wardsPlaced, wardsKilled, visionWardsBoughtInGame, detectorWardsPlaced
item0..item6                                      # -> nomes resolvidos + ordem final da build
summoner1Id, summoner2Id
perks.styles[].selections[].perk, perks.statPerks  # -> nomes de runa resolvidos
challenges.*                                      # SELETIVAMENTE — veja abaixo
```

`challenges` é uma mina de ouro que a maioria dos parsers ignora. A Riot já calcula dezenas das
métricas que você derivaria mal por conta própria. Pegue estas e pule a derivação:

```
laneMinionsFirst10Minutes, maxCsAdvantageOnLaneOpponent, maxLevelLeadLaneOpponent
earlyLaningPhaseGoldExpAdvantage, laningPhaseGoldExpAdvantage
controlWardsPlaced, wardTakedowns, visionScorePerMinute, stealthWardsPlaced
killParticipation, teamDamagePercentage, damageTakenOnTeamPercentage
goldPerMinute, damagePerMinute
turretPlatesTaken, takedownsFirstXMinutes
soloKills, killsNearEnemyTurret, deathsByEnemyChamps
skillshotsDodged, skillshotsHit, enemyChampionImmobilizations
dodgeSkillShotsSmallWindow, saveAllyFromDeath
epicMonsterSteals, dragonTakedowns, baronTakedowns, riftHeraldTakedowns
immobilizeAndKillWithAlly, pickKillWithAlly
```

Para os **outros oito jogadores**, mantenha apenas: `championName`, `teamPosition`, `teamId`,
`kills`, `deaths`, `assists`, `goldEarned`, `totalDamageDealtToChampions`, `item0..6`. Todo o resto é
descartado. Só esse filtro de perspectiva já é um corte de ~4×.

### De `timeline` — `info.frames[].participantFrames`

Mantenha **apenas para o jogador em foco, o oponente de rota e os dois junglers**, e somente estes
campos:

```
totalGold, xp, level, minionsKilled, jungleMinionsKilled, position{x,y}
```

**Descarte explicitamente**: `currentGold` (ruidoso, derivável nos pontos de recall),
`championStats` (20 campos × 10 jogadores × 32 frames = ~6.400 números de valor de coaching quase
nulo), `damageStats` (mantenha só o agregado de fim de jogo vindo de `match`),
`timeEnemySpentControlled`.

Para os seis jogadores restantes, mantenha apenas `totalGold`, e agregue imediatamente para um total
por time por frame. O *diferencial* de ouro por time ao longo do tempo é de alto sinal (mostra as
viradas de tempo); dez curvas individuais de ouro não são.

### De `timeline` — `info.frames[].events[]`

| Tipo de evento | Ação |
|---|---|
| `CHAMPION_KILL` | **Manter, enriquecido.** Descarte os **quatro** arrays de dano inteiros — mas antes reduza-os a `top_damage_source: championName` e `damage_share: float`. Uma string e um float substituem ~6 KB. Ver 2.2.1. |
| `ITEM_PURCHASED` | Manter só para foco + oponente. Resolva `itemId` → nome (§4). Colapse em um **caminho de build com timestamps**. |
| `ITEM_SOLD` / `ITEM_DESTROYED` / `ITEM_UNDO` | **Descartar**, exceto: um `ITEM_UNDO` até 3 s após uma compra significa que a compra nunca aconteceu — aplique e descarte os dois. `ITEM_DESTROYED` de um componente é só uma conclusão de item; já está implícito no item completo. |
| `SKILL_LEVEL_UP` | **Colapsar em uma string**: `"Q>W>E>Q>Q>R>..."`. 18 eventos → 1 campo. |
| `LEVEL_UP` | **Descartar** — totalmente derivável de `participantFrames.level`. |
| `WARD_PLACED` / `WARD_KILL` | Agregue em contagens por tipo e por blocos de 5 minutos. Mantenha eventos individuais **apenas** dentro de ±60 s do nascimento de um objetivo (essa é a fatia treinável). |
| `BUILDING_KILL` | Manter todos. ~11–22 eventos, cada um de alto valor. |
| `TURRET_PLATE_DESTROYED` | Manter todos (≤ 25 eventos; ouro de plate é sinal real de tempo no early). |
| `ELITE_MONSTER_KILL` | Manter todos. Enriqueça com `monsterType`, `monsterSubType` (elemento do dragão). |
| `DRAGON_SOUL_GIVEN`, `FEAT_UPDATE` | Manter. |
| `OBJECTIVE_BOUNTY_PRESTART/START` | Manter — explica oscilações de ouro que o modelo atribuiria mal. |
| `CHAMPION_SPECIAL_KILL` | Manter apenas `FIRST_BLOOD` e multikills. |
| `PAUSE_END` | Manter — define o verdadeiro `t=0` para calibração de relógio (§3). |
| `GAME_END` | Manter. |
| Todo o resto | Descartar. |

---

## 2.2.1 Medições reais (8 partidas de SR, `tools/inspect_timeline.py`)

Este bloco substitui as estimativas. Partida de referência: `BR1_3239179616`,
35 min, ranked solo, patch 16.9.

| Onde está o peso | Medido |
|---|---|
| Timeline bruta | 926 KB ≈ **237.000 tokens** |
| `CHAMPION_KILL` | **82% de todos os bytes de evento** (462 KB de 563 KB, 77 abates) |
| Um único `CHAMPION_KILL` | **7.104 B**, dos quais ~6.000 B são arrays de dano |
| `participantFrames` | 39% da timeline |
| ├ `championStats` | 43% de cada frame de jogador |
| └ `damageStats` | 33% de cada frame de jogador |

**Correção ao blueprint original: `CHAMPION_KILL` carrega QUATRO arrays de dano, não dois.**

```
victimDamageReceived          victimTeamfightDamageReceived
victimDamageDealt             victimTeamfightDamageDealt
```

Os dois `Teamfight*` não estavam previstos e sozinhos respondem por ~3,8 KB dos 7,1 KB do evento.
Dropar apenas os dois primeiros deixaria mais da metade do desperdício de pé. Os quatro saem;
o que sobra de útil é `killerId`, `victimId`, `assistingParticipantIds`, `position`, `bounty`,
`shutdownBounty`, `killStreakLength`, `timestamp`.

**Também descoberto:**

- `gameVersion` vem com **quatro componentes** (`16.9.772.8292`), não `16.9.1`. Precisa ser
  normalizado para `major.minor` antes de consultar o DataDragon.
- `participantFrames` tem `goldPerSecond` e `timeEnemySpentControlled`, ambos descartáveis.
- Os 13 `challenges` que o pipeline usa estão **todos presentes** em SR (queue 420), e **todos
  ausentes ou zerados** em Arena (queue 1750, `mapId` 30, `gameMode` CHERRY) — o parser precisa
  rejeitar `mapId != 11` logo na entrada em vez de produzir métricas silenciosamente zeradas.

Dropar `championStats` + `damageStats` + os quatro arrays de dano, sozinho, tira
**~30% da timeline** antes de qualquer filtro de perspectiva.

---

## 2.3 As três transformações que fazem o trabalho de verdade

### Transformação 1 — Coordenada → zona nomeada do mapa

Posições brutas são `x, y` em aproximadamente `[0, 15000]`. **Um LLM não consegue raciocinar sobre
`(7432, 8891)`.** Ele consegue raciocinar brilhantemente sobre `"MID_RIVER"`. Essa transformação
simultaneamente corta tokens (~9 caracteres → um enum curto) *e* é a maior melhoria isolada de
precisão do pipeline.

```python
# riftcoach/parse/zones.py
MAP_MAX = 14870      # extensão do mundo no SR usada pelas posições do match-v5

class MapZone(str, Enum):
    BLUE_BASE = "BLUE_BASE"; RED_BASE = "RED_BASE"
    TOP_LANE_BLUE = "TOP_BLUE"; TOP_LANE_MID = "TOP_MID"; TOP_LANE_RED = "TOP_RED"
    MID_LANE_BLUE = "MID_BLUE"; MID_LANE_MID = "MID_MID"; MID_LANE_RED = "MID_RED"
    BOT_LANE_BLUE = "BOT_BLUE"; BOT_LANE_MID = "BOT_MID"; BOT_LANE_RED = "BOT_RED"
    TOP_RIVER = "TOP_RIVER"; BOT_RIVER = "BOT_RIVER"
    BARON_PIT = "BARON_PIT"; DRAGON_PIT = "DRAGON_PIT"
    BLUE_TOP_JUNGLE = "BLUE_TOPJG"; BLUE_BOT_JUNGLE = "BLUE_BOTJG"
    RED_TOP_JUNGLE = "RED_TOPJG"; RED_BOT_JUNGLE = "RED_BOTJG"
    BLUE_BUFF_BLUE = "BLUE_BLUEBUFF"; BLUE_BUFF_RED = "BLUE_REDBUFF"
    RED_BUFF_BLUE = "RED_BLUEBUFF"; RED_BUFF_RED = "RED_REDBUFF"

# Consulta por polígono, não por grade: as rotas são faixas diagonais e uma grade
# quadrada rotula river/jungle errado o tempo todo. STRtree do shapely pré-construída,
# ~3 us por consulta.
_TREE = build_zone_index(ZONE_POLYGONS)

def to_zone(x: int, y: int) -> MapZone: ...

def side_relative(zone: MapZone, team_id: int) -> str:
    """'ENEMY_JUNGLE_TOPSIDE' / 'OWN_JUNGLE_BOTSIDE' — SEMPRE apresente isso ao
    modelo em vez de BLUE/RED. O modelo nunca deveria precisar lembrar de que lado
    o jogador em foco está; esse é exatamente o tipo de contabilidade que ele erra."""
```

O passo `side_relative` não é cosmético. Todo erro que um LLM comete sobre "você se
sobre-estendeu" se origina de ele perder a noção de qual metade do mapa é perigosa. Pré-resolver
isso elimina o modo de falha por completo.

### Transformação 2 — Enriquecimento de mortes

Um `CHAMPION_KILL` bruto tem ~2 KB e diz quase nada de treinável. Cada morte vira **uma linha de ~35
tokens** carregando o contexto tático completo, calculado em Python a partir dos frames ao redor:

```python
class DeathContext(BaseModel):
    n: int                          # número da morte
    t: str                          # "14:22"
    zone: str                       # "ENEMY_JUNGLE_TOPSIDE" (relativo ao lado)
    killers: list[str]              # ["LeeSin", "Ahri"]
    top_damage_source: str
    gold_swing: int                 # shutdown entregue + bounty perdida + ouro ganho pelo inimigo
    death_timer_s: float            # fórmula BRW, tabela fixada no patch (§4)
    allies_within_2000u: int        # do participantFrames mais próximo
    enemies_unaccounted: int        # inimigos NÃO visíveis na última leitura do minimapa (T3, só visão)
    objective_window: str | None    # "DRAKE_SPAWN_IN_38s" | "BARON_UP" | None
    wave_proxy: str                 # "PUSHING_TO_ENEMY" (T2) — ver Transformação 3
    had_vision: bool                # ward própria viva num raio de 1600u em t (T2)
    gold_at_death: int              # currentGold: morreu segurando 1400 de ouro não gasto?
    summs_available: list[str]      # flash/tp rastreados por último uso + cooldown do patch (T2)
```

Renderizado:

```
D3 14:22 ENEMY_JG_TOPSIDE killers=LeeSin,Ahri dmg=Ahri swing=-450 timer=23s
   allies=0 obj=DRAKE_IN_38s wave=PUSHING_TO_ENEMY vision=NO gold_held=1150 summs=[]
```

Nove mortes ≈ 315 tokens, e *cada uma* delas é diretamente treinável. Essa é a estrutura de maior
sinal-por-token do sistema inteiro — um modelo que recebe essas linhas produz conselho específico e
correto ("três das suas nove mortes foram na jungle superior inimiga sem ward e sem flash, todas com
um dragão a menos de 60 s de nascer") sem precisar fazer nenhuma inferência que poderíamos ter feito
por ele.

`summs_available` merece uma nota: a timeline da Riot não tem evento de "uso de feitiço de
invocador". Rastrear Flash por saltos de posição em `CHAMPION_KILL`? Não é confiável. **Resposta
honesta: esse campo é T3 e só é preenchido nos Modos B/C, a partir da visão da HUD.** No Modo A ele é
`None`, e o prompt nunca pode afirmar estado de feitiço só com telemetria. Marque, não invente.

### Transformação 3 — Proxy de estado da wave (e seus limites honestos)

Não existe dado de wave na API da Riot. Ponto final. O que é derivável:

```python
def wave_proxy(frames, pid, t_ms) -> tuple[str, str]:
    """Retorna (estado, premissa). SEMPRE EvidenceTier.T2 ou T3 — nunca T1."""
    # cs_rate: delta de minionsKilled na janela de 60s ao redor
    # faixa de posição: metade própria / meio / metade inimiga da rota
    # Um jogador na faixa da metade inimiga com cs_rate alto está quase certamente
    # empurrando; na faixa da metade própria com taxa perto de zero, ou está de
    # freeze ou está roamando.
```

Quatro estados: `PUSHING_TO_ENEMY`, `HOLDING_MID`, `HELD_IN_OWN_HALF`, `NOT_IN_LANE`. Esse é o limite
honesto de resolução da telemetria. Discriminar de verdade entre freeze/slow push/crash exige ver a
contagem de minions, o que significa visão (Modos B/C) — e mesmo lá continua sendo T3.

**Não passe pano nisso.** Um coach que afirma com confiança "você deveria ter dado freeze aqui" com
base em evidência que ele não tem é pior do que um que diz "seu ritmo de CS e sua posição sugerem que
você estava empurrando numa rota com visão inimiga (derivado, não medido) — se isso estiver certo, o
freeze estava disponível". O segundo é confiável; o primeiro garante ao projeto uma reputação de
alucinação.

---

## 2.4 A representação intermediária `MatchFacts`

```python
# riftcoach/parse/facts.py
class LaneSnapshot(BaseModel):
    """Deltas por minuto entre foco e oponente. Deltas, nunca absolutos: o modelo se
    importa com a diferença, e deltas comprimem muito melhor (inteiros pequenos,
    muitos zeros)."""
    minute: int
    gd: int          # diferença de ouro, arredondada para múltiplos de 25
    xpd: int         # diferença de xp, arredondada para múltiplos de 50
    csd: int         # diferença de cs
    zone: str        # zona do jogador em foco, relativa ao lado

class RecallEvent(BaseModel):
    t: str
    gold_on_back: int
    bought: list[str]              # nomes de item resolvidos
    wave_proxy_before: str
    seconds_lost: float            # tempo do recall até voltar à rota (T2, por posições)
    was_forced: bool               # precedido por morte ou proxy de <25% de hp

class ObjectiveEvent(BaseModel):
    t: str; kind: str; subtype: str | None; taken_by_team: bool
    contested: bool                # algum abate de campeão em ±25 s
    prep_wards_60s: int            # wards próprias perto do pit nos 60 s anteriores
    team_gold_diff_at: int
    focus_player_zone: str         # onde o jogador ESTAVA quando isso aconteceu?

class MatchFacts(BaseModel):
    # identidade
    match_id: str; patch: str; queue: str; duration_s: int; win: bool
    focus: PlayerSummary; opponent: PlayerSummary
    team: list[PlayerSummary]; enemy: list[PlayerSummary]   # reduzidos
    # métricas principais derivadas (TODAS calculadas em Python — nível T1)
    cs_at_10: int; cs_at_14: int; csd_at_10: int; gd_at_10: int; gd_at_14: int; xpd_at_10: int
    dpm: float; damage_share: float; kill_participation: float; gold_per_min: float
    vision_score_per_min: float; control_wards: int
    time_dead_s: int; time_dead_pct: float
    plates_taken: int; solo_kills: int
    # séries
    lane_series: list[LaneSnapshot]          # 0..18, depois a cada 3 min
    team_gold_diff_series: list[int]         # por minuto, partida inteira
    # eventos
    deaths: list[DeathContext]
    kills_by_focus: list[KillContext]        # mesmo formato, invertido
    recalls: list[RecallEvent]
    objectives: list[ObjectiveEvent]
    build_path: list[tuple[str, str]]        # [("06:12", "Tomo Perdido"), ...]
    skill_order: str                         # "Q>W>E>Q>Q>R>..."
    runes: RuneSet; summoners: tuple[str, str]
    # derivado de visão, só Modos B/C
    observations: list[FrameObservation] = []
```

### Afinamento das séries

`lane_series` é por minuto de 0 a 18 min, depois a cada 3 minutos. Justificativa: decisões de fase de
rota estão na escala de minutos; decisões de late game estão na escala de eventos e já são capturadas
por `objectives`/`deaths`. Uma partida de 40 minutos rende 18 + 8 = 26 snapshots, não 40.

Arredonde com agressividade. `gd: 1247` → `gd: 1250`. O julgamento do modelo é idêntico e você
economiza caracteres ao longo de centenas de números. Nunca arredonde *timestamps* — eles são chaves
de junção.

---

## 2.5 Renderizando para tokens

**Não mande JSON para o modelo.** JSON gasta ~40% dos tokens com chaves, aspas e nomes de campo
repetidos. Renderize a IR em um formato compacto, tabular, com cabeçalho declarado uma única vez:

```
=== PARTIDA 15.18.1 | RANKED_SOLO | 32:14 | DERROTA ===
VOCÊ: Orianna MEIO   | 7/9/11  | 221cs | 612 DPM | 28.1% dano | KP 64%
ADV:  Syndra MEIO    | 11/4/8  | 248cs | 744 DPM | 33.0% dano

ROTA (vs Syndra) — gd/xpd/csd, sua zona
min  1   2   3   4   5   6   7   8   9  10  11  12  13  14
gd   0 -25 -50   0 -75 -150 -125 -300 -275 -450 -400 -625 -900 -1100
xpd  0   0 -50 -50 -100 -150 -100 -250 -200 -350 -300 -500 -700 -850
csd  0  -2  -3  -1  -4  -6  -5  -9  -8 -13 -12 -16 -21 -24
zona MID MID MID MID MID MID MID MID MID MID MID OWN OWN OWN

BENCHMARKS (MEIO, ESMERALDA)     você    p50    p90
cs@10                              61     68     79
gd@10                            -450    +12   +680
visão/min                        0.71   0.88   1.24
tempo morto                     14.2%   9.8%   6.1%

MORTES (9)
D1 08:41 OWN_MID_LANE killers=Syndra dmg=Syndra swing=-300 timer=14s allies=0
   obj=- wave=PUSHING_TO_ENEMY vision=NO gold_held=700
D2 12:03 ENEMY_JG_BOTSIDE killers=Syndra,Viego dmg=Viego swing=-450 timer=18s
   allies=0 obj=DRAKE_IN_51s wave=HOLDING_MID vision=NO gold_held=150
...

RECALLS (6)
R1 05:12 ouro=780 comprou=[Tomo Perdido] wave=PUSHING_TO_ENEMY perdeu=19s forçado=NÃO
R2 09:30 ouro=1290 comprou=[Botas, Sentinela de Controle] wave=HELD_IN_OWN_HALF perdeu=24s forçado=SIM(morte)
...

OBJETIVOS (11)
14:22 DRAGÃO/INFERNAL inimigo contestado=SIM prep_wards=0 teamgd=-1400 você_estava=OWN_MID_LANE
...

BUILD  06:12 Tomo Perdido | 09:31 Botas | 11:48 Companheiro de Luden | ...
SKILLS Q>W>E>Q>Q>R>Q>W>Q>R>...
RUNAS  Eletrocutar / Impacto Súbito / Globo Ocular / Caçador de Tesouros // Fluxo de Mana / Transcendência
```

### Medido (160 destilações reais, `tools/try_distill.py`)

| | por partida | vs. bruto |
|---|---|---|
| match + timeline bruto | ~214.000 tokens | — |
| `MatchFacts` (JSON) | ~4.080 tokens | 52x |
| **renderizado (texto)** | **~1.275 tokens** | **168x** |

O ganho do texto sobre o JSON é de **3,4x** — acima do 2,5x estimado. Vale por linha: cabeçalho
declarado uma vez e linhas posicionais, em vez de repetir nome de campo em cada registro.

Por analista, numa partida de 35 min com 9 mortes e 12 recalls:

```
laning   706 tokens      macro   502      economy  543
fights   398 tokens      head_coach 43    SOMA   2.192 (5 passes)
```

Cada passe cabe folgado em 8k de contexto e em qualquer tier gratuito.

**Armadilha encontrada na medição:** a seção de objetivos chegou a **578 tokens** — a maior de
todas — porque o marcador `contested` só comparava o *tempo* entre objetivo e abate. Numa partida
de 35 min com 77 abates, quase todo objetivo tem um abate a menos de 25s, então 26 de 28 vinham
marcados como disputados e o sinal virava ruído constante. Exigir proximidade também no **espaço**
(2.500 unidades) derrubou a seção para 362 tokens e tornou o marcador significativo. O teste
`test_contested_is_not_always_true` trava a regressão.

### Orçamento medido

| Componente | Tokens |
|---|---|
| System prompt + schema de saída | ~900 |
| `MatchFacts` renderizado | ~1.100 |
| Dados de patch dos itens/runas em jogo (§4) | ~500 |
| Princípios recuperados (top-4 chunks de RAG) | ~700 |
| Tabela de benchmarks | ~150 |
| **Total por passe de analista** | **~1.100–1.600** (cada analista vê só suas fatias relevantes) |
| **Análise completa de 5 passes** | **~6.500 entrada / ~2.500 saída** |

Confortavelmente dentro de todo tier gratuito e dentro de um modelo local de 8 GB com 8k de contexto.

---

## 2.6 Notas de implementação

### Cache — a decisão de engenharia de maior alavancagem

Dados de partida concluída são **imutáveis**. Guarde em cache permanentemente:

```python
# riftcoach/riot/cache.py — SQLite, zstd nível 10
# CREATE TABLE raw (key TEXT PRIMARY KEY, payload BLOB, fetched_at INT, schema_v INT)
```

Uma timeline de 2,5 MB comprime para ~180 KB (JSON é extremamente compressível). Um histórico de 500
partidas dá ~90 MB. Em troca: reanálise é instantânea, desenvolver o parser custa zero chamadas de
API, o harness de avaliação roda offline, e o limite de 20 req/s da chave de desenvolvimento deixa de
importar depois da primeira busca.

Guarde o **`MatchFacts` destilado separadamente**, com chave `(match_id, puuid, parser_version)`,
para que uma mudança no parser invalide apenas a camada derivada e nunca refaça o download.

### Limitação de taxa

Chaves de desenvolvimento: 20 req/s, 100 req/2 min, **por valor de roteamento**, e elas **expiram a
cada 24 horas** — um problema de UX de primeira classe, não uma nota de rodapé. Detecte o `403` com o
corpo de expiração de chave e mostre uma mensagem específica de "sua chave de desenvolvimento
expirou, gere outra em developer.riotgames.com" com link direto. Oriente os usuários a solicitar uma
Personal API Key (sem expiração) depois que passarem da fase de teste.

Implemente um token bucket de duas janelas por `(região, chave)` e respeite o `Retry-After` e os
headers `X-Rate-Limit-Count` / `X-App-Rate-Limit` da resposta em vez de chutar:

```python
class RiotLimiter:
    # janelas lidas de X-App-Rate-Limit: "20:1,100:120"
    # um 429 fecha um portão rígido até o Retry-After passar; todas as corrotinas
    # aguardam o mesmo portão
```

### Correção do parser

`parser_version` entra na chave de cache **e** no `CoachingReport`, para que o usuário consiga
entender por que o relatório de ontem difere do de hoje. Fixtures de referência: versione ~10
timelines anonimizadas (`tests/fixtures/`, com PUUIDs limpos) cobrindo uma partida de ARAM, um
remake, uma partida de 60 minutos, uma partida com desconexão e uma em que o jogador em foco trocou
de rota. Faça snapshot-test de `MatchFacts` contra elas. Cada um desses casos de borda vai quebrar o
parser em produção se você não fizer isso — especialmente trocas de rota, em que `teamPosition` não é
confiável e o "oponente de rota" precisa ser resolvido por *proximidade de posição durante os minutos
2 a 8*, não pela string de função.
