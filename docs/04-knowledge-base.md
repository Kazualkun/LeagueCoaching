# §4 — Base de Conhecimento: Fine-Tuning vs. RAG / Prompts Dinâmicos

🇧🇷 Português · [🇺🇸 English](en/04-knowledge-base.md)

## 4.1 Veredito

**RAG + montagem dinâmica de prompt. Sem fine-tune de conhecimento. Nem agora, nem depois.**

Um LoRA pequeno para *formatação de saída* é uma otimização defensável na v2 e está discutido na
§4.6, mas ele é uma otimização de latência/tokens, não um mecanismo de conhecimento, e o projeto
entrega sem ele.

---

## 4.2 Por que o fine-tuning perde — cinco razões independentes

Qualquer uma delas basta. Juntas, encerram a discussão.

### R1 — A cadência de atualização é matematicamente incompatível

Patches saem a cada duas semanas, mais ou menos. Um ciclo de QLoRA que de fato melhore alguma coisa
é: curar o dataset do delta → treinar → avaliar contra uma suíte de regressão → quantizar → converter
para GGUF → publicar → todo usuário baixa vários GB de novo. Isso são dias de trabalho do mantenedor
e gigabytes de banda, num relógio de duas semanas, para sempre, num projeto voluntário. O
equivalente no RAG é um único HTTP GET no `versions.json` do DataDragon, que termina em menos de um
segundo.

### R2 — Fine-tuning ensina comportamento, não fatos

Essa é a razão fundamental. Fine-tuning muda de forma confiável *como* um modelo responde — tom,
formato, estrutura, enquadramento da tarefa. É uma maneira pouco confiável e com perdas de instalar
*fatos*, e os fatos que ele instala ficam impossíveis de corrigir sem retreinar. Nosso conhecimento
volátil é inteiramente factual: stats de itens, custos, caminhos de build, ratios de campeão,
coeficientes de timer de morte, tempos de respawn de objetivo. Fatos pertencem ao contexto, onde são
auditáveis, fixados no patch e corrigíveis editando uma linha.

### R3 — O dataset não existe, e não dá para construí-lo de forma limpa

"Fazer fine-tune com dados de decisão de elo alto" pressupõe um corpus rotulado de pares
`(estado de jogo → decisão correta)`. Esse dataset público não existe. Construí-lo significa:

- **Raspar VODs de coaching / comentários** — um problema de direitos que o projeto não pode
  absorver, e os rótulos são prosa, não decisões; ou
- **Minerar replays de Challenger** — você obtém *o que* jogadores fortes fizeram, sem
  contrafactual e sem rótulo causal. Rotular por resultado é irremediavelmente confundido: um
  jogador de Challenger morrer às 14:22 não é evidência de que morrer às 14:22 é correto; ou
- **Destilar de um modelo maior** — o que limita a qualidade ao teto do professor, não acrescenta
  nenhum conhecimento que o professor já não tivesse, e é estritamente pior do que simplesmente
  *chamar o professor*, que é o que nosso tier gratuito na nuvem já faz.

Só essa razão encerra o debate. O gargalo nunca foi o treinamento; sempre foram os rótulos.

### R4 — Um LoRA não pode ser implantado no caminho da nuvem

C2 exige que o app funcione num notebook fraco via Gemini ou Groq. Você não consegue entregar um
LoRA para esses endpoints. Então um fine-tune produz **dois perfis de qualidade divergentes**,
exigindo dois conjuntos de prompts, duas suítes de avaliação e dois conjuntos de relatos de bug —
para a *minoria* de usuários que tem GPU. O custo de manutenção cai exatamente onde o benefício não
está.

### R5 — Conhecimento no prompt é depurável; conhecimento nos pesos não é

Quando o coach diz algo errado, o RAG permite imprimir o contexto recuperado exato e ver a linha
ruim. Com um fine-tune, "por que ele disse que o Eclipse dá omnivamp" não tem resposta aquém de uma
auditoria de treinamento. Para um projeto open source que depende de relatos de bug da comunidade,
depurabilidade *é* a funcionalidade.

### O que perdemos ao não fazer fine-tune, honestamente

O fine-tuning compraria genuinamente: prompts mais curtos (o schema e a voz do coach poderiam ser
embutidos, economizando ~900 tokens por chamada), tom mais consistente e melhor aderência a JSON em
modelos pequenos. Isso é real. Vale mais ou menos 15% de latência e um pouco de polimento — contra as
cinco razões acima. As restrições de saída estruturada (§1.5) já resolvem a parte da aderência a JSON
de graça.

---

## 4.3 A arquitetura de conhecimento — três camadas

A palavra "RAG" sugere uma coisa só. Na prática precisamos de três, com volatilidades diferentes e
mecânicas de recuperação diferentes:

```
L1  PRINCÍPIOS         independente de patch   curado por humanos   busca semântica       ~120 chunks
L2  FATOS DE PATCH     a cada ~2 semanas       sync automático      consulta determinística ~400 itens
L3  BENCHMARKS         a cada patch            gerado por CI        consulta determinística ~2 MB parquet
```

### L1 — Corpus de princípios (`knowledge/principles/*.md`)

Fundamentos independentes de patch. Eles mudam numa escala de anos, não de duas semanas:

```
knowledge/principles/
├── waves/slow-push.md          # o que cria, timing do canhão, quando é correto
├── waves/freezing.md           # o limiar de ~3 casters, quando quebrar
├── waves/crashing-and-recall.md
├── waves/bounce-mechanics.md
├── trading/stance-and-spacing.md
├── trading/minion-aggro.md
├── trading/level-spike-windows.md   # 2, 3, 6, 11, 16 e o porquê
├── macro/tempo-and-prio.md
├── macro/objective-setup.md         # a janela de visão de 60-90s antes do nascimento
├── macro/crossmap-and-tradeoffs.md
├── economy/recall-thresholds.md     # breakpoints de componente, não de item completo
├── economy/death-timers.md          # a fórmula BRW de verdade
├── vision/deep-vs-defensive.md
└── fights/target-selection.md
```

Cada arquivo tem frontmatter com chaves de recuperação:

```markdown
---
id: waves-slow-push
applies_to: {roles: [TOP, MIDDLE, BOTTOM], phases: [early, mid]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error, roam_window]
tier: T1_PRINCIPLE
---
Um slow push é criado matando a wave inimiga ligeiramente mais devagar do que ela chega...
```

**Esta é a principal superfície de contribuição do projeto.** Um jogador de Mestre que não sabe
escrever Python consegue escrever `waves/bounce-mechanics.md`, e isso é uma contribuição
materialmente mais valiosa do que a maioria dos PRs de código. Projete o repositório para que esse
seja o PR mais fácil possível de abrir (§5).

### L2 — Fatos de patch (sync automático, manutenção humana zero)

Fontes, em ordem de preferência:

```python
DDRAGON = "https://ddragon.leagueoflegends.com"
#   /api/versions.json                     -> ["15.18.1", "15.17.1", ...]
#   /cdn/{v}/data/en_US/item.json          -> stats, ouro, from/into, tags
#   /cdn/{v}/data/en_US/champion/{c}.json  -> stats base, crescimento por nível, ratios
#   /cdn/{v}/data/en_US/runesReforged.json
#   /cdn/{v}/data/en_US/summoner.json      -> cooldowns de feitiços de invocador

CDRAGON = "https://raw.communitydragon.org/latest"
#   /plugins/rcp-be-lol-game-data/global/default/v1/items.json   -> mais rico que o DDragon
#   /plugins/rcp-be-lol-game-data/global/default/v1/perks.json
```

O DataDragon é a base autoritativa e versionada. O CommunityDragon preenche lacunas (ele traz campos
que o DDragon omite e é atualizado mais rápido), mas é mantido pela comunidade e só expõe `latest` de
forma confiável — então: **DDragon para tudo que precise ser fixado no patch, CDragon apenas para
enriquecimento.**

### Locale: uma decisão com consequência direta no validador

O DataDragon serve os mesmos dados em vários idiomas (`en_US`, `pt_BR`, ...). A escolha do locale
**não é cosmética**, porque o `FactValidator` (§4.5) casa nomes de entidade por texto exato contra a
tabela do patch. Se o banco for carregado em `en_US` e o modelo escrever "Companheiro de Luden", todo
item vira `HALLUCINATED_ENTITY` e o relatório inteiro é descartado.

Regra: **o locale do banco de patch, o locale injetado no prompt e o idioma de saída do coach têm que
ser o mesmo.** Trate como uma configuração única:

```python
# config.py
LOCALE = "pt_BR"          # controla DDragon, prompts, validador e UI — sempre juntos
```

Duas armadilhas concretas:

1. **Os nomes de campeão no Match-v5 vêm sempre em inglês** (`championName: "Orianna"`,
   `"MonkeyKing"` para o Wukong). Eles são identificadores, não texto localizado — resolva-os pela
   chave numérica contra o `champion.json` do locale escolhido antes de renderizar, e nunca os passe
   crus para um prompt em português.
2. **Carregue o índice Aho-Corasick com os dois locales** e normalize para o do usuário. Modelos
   treinados majoritariamente em inglês vão escrever "Luden's Companion" mesmo instruídos em
   português; reconhecer o nome em inglês e corrigir para o nome em pt-BR é muito melhor do que
   rejeitar um finding correto.

Normalizado em SQLite:

```sql
CREATE TABLE item  (patch TEXT, item_id INT, name TEXT, gold_total INT, gold_base INT,
                    stats_json TEXT, builds_from TEXT, builds_into TEXT, tags TEXT,
                    PRIMARY KEY (patch, item_id));
CREATE TABLE champion (patch TEXT, key INT, name TEXT, stats_json TEXT, spells_json TEXT,
                    PRIMARY KEY (patch, key));
CREATE TABLE rune  (patch TEXT, perk_id INT, name TEXT, tree TEXT, desc TEXT,
                    PRIMARY KEY (patch, perk_id));
CREATE TABLE patch_diff (from_patch TEXT, to_patch TEXT, entity_type TEXT, entity_id INT,
                    field TEXT, old_value TEXT, new_value TEXT);
```

### O truque das notas de patch — faça diff do DataDragon em vez de raspar as notas

O instinto de todo mundo é raspar a página de notas de patch da Riot. Ela é HTML, é reestruturada
regularmente, é prosa que ainda exige um LLM para interpretar (risco de alucinação), e raspá-la fica
numa zona cinzenta dos termos de uso.

**Em vez disso: compare duas versões do DataDragon e gere o changelog você mesmo.**

```python
# riftcoach/knowledge/patchdiff.py
def diff_patches(old: str, new: str) -> list[PatchChange]:
    """Diff estrutural de item.json / champion.json entre duas versões do DDragon.
    Produz registros de mudança gerados por máquina e literalmente verdadeiros."""
    # -> PatchChange(entity="Companheiro de Luden", field="stats.AP",
    #                old=100, new=95, patch="15.18.1")
```

Propriedades que isso entrega e que o scraping não entrega:

- **Literalmente verdadeiro.** É um diff dos arquivos de dados autoritativos, não a interpretação de
  um texto.
- **Completo.** Pega mudanças silenciosas que a Riot não documentou.
- **Legível por máquina.** Alimenta diretamente os prompts e o validador.
- **Manutenção zero.** Nenhum seletor para consertar quando a página de notas for redesenhada.
- **Retroativo.** Todas as versões históricas estão na CDN, então você gera qualquer diff sob
  demanda.

Renderizado como fatia de prompt:

```
MUDANÇAS DO PATCH 15.18.1 RELEVANTES PARA ESTA PARTIDA:
- Companheiro de Luden: AP 100 -> 95, custo total 3200 -> 3100
- Orianna: armadura base 20 -> 22
- Dragão Infernal: AD/AP bônus 4% -> 3%
```

Somente mudanças que tocam entidades *efetivamente presentes na partida* são injetadas. Em geral, 0 a
4 linhas.

### L3 — Benchmarks

Percentis de `cs@10`, `gd@10`, `visão/min`, `dpm`, `mortes` e `time_dead_pct`, por
`(rota, elo, patch_major)`.

**Não raspe op.gg/u.gg.** Viola os termos, é frágil e é desnecessário. Gere você mesmo:

- Uma GitHub Action rodada pelo mantenedor amostra partidas aleatórias por elo através da API da Riot
  (`league-v4` → `match-v5`), agrega em percentis e publica
  `knowledge/benchmarks/{patch_major}.parquet` (~2 MB) como asset de release.
- Só agregados são publicados — sem PUUIDs, sem linhas por jogador. Em conformidade e sem problema
  de privacidade.
- Plano B: percentis calculados a partir das últimas 50 partidas do próprio usuário, para que a
  funcionalidade funcione desde o primeiro dia e para rotas fora do meta que a tabela embarcada não
  cobre.

Benchmarks são o que transforma "seu CS estava baixo" em "61 CS@10 te coloca no percentil 28 para mid
Esmeralda, onde a mediana é 68" — a diferença entre uma observação e uma meta treinável.

---

## 4.4 Mecânica de recuperação — em boa parte *sem* embeddings

O erro comum é embutir tudo e fazer busca vetorial. **A maior parte da nossa recuperação é uma
consulta com chave conhecida.** Dado o `MatchFacts`, sabemos exatamente quais itens foram comprados,
quais campeões jogaram, qual rota, qual elo. Nada precisa ser *buscado*.

| Camada | Mecanismo | Por quê |
|---|---|---|
| L2 fatos de patch | `SELECT ... WHERE patch=? AND item_id IN (...)` | As chaves exatas são conhecidas. Embeddings seriam estritamente piores e poderiam recuperar o item errado. |
| L3 benchmarks | Consulta em parquet por `(rota, elo)` | Mesma coisa. |
| L1 princípios | **Híbrido: filtro por tag de gatilho → ranqueamento por embedding** | Aqui é genuinamente difuso. |

Recuperação de L1, concretamente:

```python
# riftcoach/knowledge/retrieve.py
def retrieve_principles(facts: MatchFacts, analyst: str, k: int = 4) -> list[Chunk]:
    # 1. Filtro rígido pelos gatilhos estruturados emitidos pelo parser:
    #    {"wave_proxy=PUSHING_TO_ENEMY", "recall_error", "role=MIDDLE", "phase=early"}
    #    Normalmente corta de 120 chunks para ~15.
    # 2. Embute a pergunta do analista + os findings já encontrados, ranqueia os
    #    sobreviventes por cosseno.
    # 3. Retorna o top-k, com teto rígido de 700 tokens no total.
```

Stack: **`fastembed` (ONNX, BGE-small-en-v1.5, ~130 MB, CPU) + `sqlite-vec`.**

Justificativa: o `fastembed` não precisa de PyTorch. Isso importa muito — exigir uma instalação do
torch transforma um `uv sync` de 30 segundos numa saga de vários gigabytes com compatibilidade de
CUDA no Windows, que é exatamente o usuário para quem este projeto existe. O `sqlite-vec` é uma
única extensão no arquivo SQLite que já temos. Nada de Chroma (dependências transitivas pesadas),
nada de serviço vetorial externo (viola C2/C3), nada de `sentence-transformers` (arrasta o torch
junto).

Com 120 chunks o índice é trivialmente pequeno; daria para resolver por força bruta em numpy. Use o
`sqlite-vec` mesmo assim, para que o corpus possa crescer até milhares de chunks contribuídos pela
comunidade sem precisar de reescrita.

---

## 4.5 Aplicação anti-obsolescência — três mecanismos

O RAG fornece os fatos certos. Estes três garantem que o modelo *use* eles.

### M1 — System prompt de mundo fechado

```
Você só pode afirmar valores numéricos se eles aparecerem literalmente no bloco
FATOS DE PATCH. Se um número de que você precisa estiver ausente, diga "(valor exato
não consta no contexto)" e raciocine qualitativamente. Você não tem memória confiável
das stats atuais de itens; seus dados de treino são anteriores a este patch. Nunca
nomeie um item, runa ou habilidade de campeão que não apareça no contexto fornecido.
```

### M2 — `FactValidator` (pós-geração, determinístico)

```python
# riftcoach/knowledge/validator.py
def validate(report: CoachingReport, patch_db: PatchDB) -> list[Violation]:
    """Roda em TODO relatório gerado antes de ele chegar à interface."""
    # 1. NER simplificado: casa todos os nomes de item/campeão/runa com um autômato
    #    de Aho-Corasick construído a partir do banco de patch (rápido, exato, sem modelo).
    # 2. Qualquer entidade fora de patch_db[report.patch] -> HALLUCINATED_ENTITY
    # 3. Qualquer entidade válida num patch ANTIGO mas já removida -> STALE_ENTITY  (a
    #    captura de maior valor: "builde Eco de Luden" / "rushe Divino Despedaçador")
    # 4. Números adjacentes a uma entidade, comparados com o banco -> STALE_STAT
    # 5. Qualquer Finding com timestamp_ms fora de [0, duração] -> BAD_ANCHOR
```

Violações disparam uma regeneração com as violações realimentadas. Se ainda assim falhar, o finding
é descartado e o modelo é registrado em `compat.jsonl` (§1.5). **Nunca mostre um finding não
validado.**

O mecanismo 3 é o que mais se paga. Um modelo treinado antes do patch vai recomendar itens removidos
com toda a confiança. Essa é a falha que mais destrói credibilidade num coach de LoL, e uma varredura
de Aho-Corasick contra uma tabela de 400 linhas pega praticamente tudo em microssegundos.

### M3 — Fixação e expiração de patch

`CoachingReport.patch` é armazenado. A interface mostra *"gerado para o patch 15.18.1"* e, se o patch
atual já tiver avançado, um aviso: *"esta análise é anterior ao patch 15.19 — o conselho de
itemização pode estar desatualizado."* Barato, honesto, e impede que o histórico apodreça em
silêncio.

---

## 4.6 O único fine-tune que talvez valha a pena (v2, opcional)

Um **LoRA de estilo/formato**, explicitamente *não* um LoRA de conhecimento:

- **Dados de treino:** ~2.000 pares `(EvidencePacket → CoachingReport validado)` colhidos de
  execuções do Gemini Flash que passaram limpas pelo `FactValidator`, mais notas de qualidade dadas
  por humanos.
- **O que ele ensina:** o schema de saída, a voz do coach, a calibração de gravidade, o hábito de
  citar níveis de evidência. Tudo independente de patch.
- **O que ele nunca pode ensinar:** nenhuma stat de item, nenhum número de campeão, nenhuma
  afirmação sobre "o meta atual".
- **Método:** Unsloth + QLoRA, rank 16, sobre Qwen3-8B. Cerca de 2 horas de GPU.
- **Retorno:** permite que o tier de 8 GB descarte o preâmbulo de schema de ~900 tokens e se comporte
  como o tier de 14B.
- **Proteção:** o modelo com fine-tune passa pelo *mesmo* `FactValidator`. Se um LoRA de estilo
  começar a alucinar itens, o validador pega e nós deletamos o LoRA.

Só entregue isso depois que o harness de avaliação (§5) puder provar que ficou melhor. Sem essa
prova, é um passivo que reintroduz todos os problemas da §4.2.
