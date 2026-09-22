# Como contribuir com o RiftCoach AI

🇧🇷 Português · [🇺🇸 English](CONTRIBUTING.en.md)

Obrigado por considerar contribuir. Este documento existe para que o seu primeiro PR seja fácil de
abrir e fácil de revisar.

**A contribuição mais valiosa deste projeto não é código.** Se você joga em elo alto, pule direto
para [Escrevendo princípios](#escrevendo-princípios) — um arquivo Markdown bem escrito sobre mecânica
de wave melhora todos os relatórios que a ferramenta gera, para sempre.

---

## Índice

- [Regras inegociáveis](#regras-inegociáveis)
- [Escrevendo princípios](#escrevendo-princípios) ← comece aqui se você não é dev
- [Relatando um finding ruim](#relatando-um-finding-ruim)
- [Ambiente de desenvolvimento](#ambiente-de-desenvolvimento)
- [Contribuindo com código](#contribuindo-com-código)
- [A disciplina de níveis de evidência](#a-disciplina-de-níveis-de-evidência)
- [Commits e PRs](#commits-e-prs)
- [Licenciamento da sua contribuição](#licenciamento-da-sua-contribuição)

---

## Regras inegociáveis

Estas quatro não estão em discussão. PRs que as violam são fechados, mesmo que o código seja bom.

1. **Nada que rode durante uma partida ao vivo.** Sem exceções, sem "mas e se for só um overlay
   passivo". O intertravamento é arquitetural (`replay/guard.py`) e falha fechado. Ver
   [COMPLIANCE.md](COMPLIANCE.md).
2. **Nenhuma funcionalidade que exija pagamento.** Toda capacidade precisa de um caminho local e um
   caminho gratuito na nuvem. Se a sua feature só funciona com uma chave paga, ela não entra.
3. **Nada de scraping de sites terceiros** (op.gg, u.gg, porofessor, lolalytics). Só API oficial da
   Riot e DataDragon. Isso não é preciosismo: scraping viola os termos, quebra sozinho e é
   desnecessário — a seção 4.3 da documentação mostra como gerar os mesmos dados de fontes
   autorizadas.
4. **Toda afirmação da IA precisa estar ancorada e citada.** Se o coach não consegue citar a
   evidência que sustenta uma frase, isso é um bug, não uma limitação aceitável.

---

## Escrevendo princípios

`knowledge/principles/` é a principal superfície de contribuição do projeto. São arquivos Markdown
puros. **Você não precisa saber Python, nem clonar o repositório** — dá para criar o arquivo direto
pela interface do GitHub.

### O formato

Cada arquivo tem um frontmatter com as chaves de recuperação, seguido do texto:

```markdown
---
id: waves-bounce-mechanics
applies_to: {roles: [TOP, MIDDLE], phases: [early, mid]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error]
tier: T1_PRINCIPLE
---

Uma wave dá bounce quando a wave inimiga acumula massa suficiente para empurrar de volta sozinha...
```

| Campo | O que é | Valores |
|---|---|---|
| `id` | Identificador único, em kebab-case. Convenção: `{pasta}-{assunto}`. | `waves-slow-push` |
| `applies_to.roles` | Rotas em que o princípio se aplica. Omita para "todas". | `TOP` `JUNGLE` `MIDDLE` `BOTTOM` `UTILITY` |
| `applies_to.phases` | Fases da partida. | `early` `mid` `late` |
| `triggers` | Gatilhos estruturados emitidos pelo parser. É o filtro rígido que roda **antes** da busca semântica. | ver lista abaixo |
| `tier` | Sempre `T1_PRINCIPLE` para este corpus. | — |

Gatilhos que o motor de regras emite hoje (`riftcoach/analysis/rules.py`, constante `TRIGGERS`):

```
wave_proxy=PUSHING_TO_ENEMY   wave_proxy=HOLDING_MID   wave_proxy=HELD_IN_OWN_HALF
recall_error   objective_window   death_cluster   vision_gap
gold_hoarding   itemization_gap   tempo_loss   lane_deficit
role=TOP role=JUNGLE role=MIDDLE role=BOTTOM role=UTILITY
phase=early phase=mid phase=late
```

Errar um gatilho não quebra nada — ele só deixa de ser recuperado naquele caso. Errar o `id` (repetir
um existente) quebra. Na dúvida, abra o PR mesmo assim e a revisão ajusta.

### O que faz um bom princípio

Escreva para alguém que **já sabe jogar** e quer entender o porquê. O público é um jogador de Ouro
lendo sobre um erro que acabou de cometer.

- **Um conceito por arquivo.** `waves/freezing.md` fala de freeze. Não fale de bounce junto.
- **Dê os números.** "Aproximadamente três minions caster de vantagem" é útil. "Uma wave um pouco
  maior" não é.
- **Diga quando o princípio está errado.** Todo princípio de LoL tem exceção, e a exceção é metade do
  valor. Um arquivo que diz "sempre congele quando estiver atrás" é pior que nenhum arquivo.
- **Independente de patch.** Se vira mentira no próximo patch, não é princípio — é fato de patch, e
  esses vêm do DataDragon automaticamente.
- **Sem jargão sem definição.** Na primeira vez que usar "prio", explique.

Tamanho bom: 200 a 600 palavras. Arquivos maiores são fatiados na recuperação e perdem contexto.

### Onde colocar

```
knowledge/principles/
├── waves/        slow-push, freezing, crashing-and-recall, bounce-mechanics
├── trading/      stance-and-spacing, minion-aggro, level-spike-windows
├── macro/        tempo-and-prio, objective-setup, crossmap-and-tradeoffs
├── economy/      recall-thresholds, death-timers
├── vision/       deep-vs-defensive
└── fights/       target-selection
```

Não achou a pasta certa? Crie uma e diga no PR por quê.

---

## Relatando um finding ruim

**Este é o relato de bug mais útil que o projeto pode receber.** Se o coach te deu um conselho errado,
abra uma issue com o template *"Finding ruim"*.

Como cada afirmação carrega a evidência e o nível dela, esses relatos são de fato diagnosticáveis —
dá para ver se o erro foi de medição (bug no parser), de premissa (heurística T2 errada) ou de
julgamento (prompt ruim). Inclua:

- O relatório completo, copiado e colado (ele não contém PUUID nem nada identificável).
- Qual finding está errado e **por que** — a sua leitura da situação.
- O match ID, se você não se importar de compartilhar.

---

## Ambiente de desenvolvimento

O projeto usa [uv](https://docs.astral.sh/uv/). Ele instala o Python certo e resolve tudo a partir do
`uv.lock`, então a sua máquina fica igual à do CI.

```bash
git clone https://github.com/Kazualkun/LeagueCoaching.git
cd LeagueCoaching

uv sync --all-extras --dev     # instala Python 3.11+ e todas as dependências
uv run pytest                  # testes rodam sem rede, em segundos
uv run ruff check .
uv run mypy
```

**Os testes não tocam a rede.** As fixtures em `tests/fixtures/*.json.gz` são partidas reais
anonimizadas. Se um teste seu precisa de rede, ele está no lugar errado — use `respx` para simular a
API da Riot, como em `tests/test_cache_and_client.py`.

Para rodar contra as suas próprias partidas você precisa de uma chave da Riot (gratuita, em
[developer.riotgames.com](https://developer.riotgames.com)):

```bash
uv run riftcoach auth                      # guarda no cofre do SO, nunca em arquivo
uv run riftcoach doctor                    # confere a configuração inteira
uv run riftcoach fetch "Nome#TAG" -n 5
uv run riftcoach analyze "Nome#TAG"        # relatório da última partida, sem IA
```

---

## Contribuindo com código

### Boas primeiras issues

Mais ou menos em ordem de dependência:

- **`parse/waves.py`** — melhore o proxy de estado da wave. Hoje são quatro estados grosseiros. Quem
  deixar isso significativamente mais preciso melhora a categoria de coaching mais valiosa do
  produto.
- **`parse/deaths.py`** — mais contexto tático por morte.
- **`llm/providers/`** — adicione um provedor gratuito. A interface é uma única classe.
- **`vision/rois.py`** — regiões de recorte da HUD para resoluções diferentes de 1080p e ultrawide.
- **`evals/golden/`** — anote uma partida. Não precisa de código, e é a contribuição de maior
  alavancagem que existe hoje: o corpus tem **duas** anotações, as duas geradas por máquina.
  Enquanto for assim, a precisão medida é um piso do corpus, não da ferramenta.
  Ver [`evals/golden/README.md`](evals/golden/README.md).
- **`knowledge/benchmarks/`** — a tabela embarcada hoje é pequena. Rodar o job de amostragem e abrir
  PR com um parquet de patch novo melhora todo percentil que o relatório mostra.

### O que o CI exige

Três comandos, todos obrigatórios:

```bash
uv run ruff check .    # lint + ordenação de imports
uv run mypy            # strict em core/, parse/ e riot/
uv run pytest
```

`mypy --strict` roda só em `core/`, `parse/` e `riot/` de propósito: é onde moram os bugs silenciosos
de correção. O resto do código é tipado, mas não sob `strict`.

### Fronteiras de camada que a revisão faz cumprir

- **`parse/` não importa nada de LLM. `llm/` não importa nada da Riot.** A camada de destilação tem
  que ser testável com zero rede e zero modelos. Se essa fronteira se mantiver, o parser continua
  correto enquanto tudo acima dele muda.
- **Métricas são calculadas em Python, nunca pelo modelo.** Se você se pegar pedindo ao LLM para
  somar, dividir ou comparar números, o cálculo pertence a `parse/` ou `analysis/rules.py`.
- **Prompts são arquivos** (`analysis/prompts/*.md`), não literais de string enterrados no código.

### Estilo

- Português no código e nos comentários, seguindo o que já está lá. **Sem acentos em docstrings e
  comentários de código** — os arquivos `.md` usam acentuação normal, o código não.
- Comentários explicam **por quê**, não o quê. O código já diz o quê. Olhe `analysis/advantage.py`
  para o tom.
- Linha de até 100 colunas (`ruff` reclama sozinho).
- Testes com nome de frase: `test_same_lead_matters_less_late`, não `test_wp_2`. Um teste é um
  argumento sobre o comportamento correto.

---

## A disciplina de níveis de evidência

Esta é a regra que mais gera pedido de mudança na revisão, então vale ler antes de escrever código.

Toda afirmação carrega um nível:

| Nível | Significa | Exemplo |
|---|---|---|
| `T1_MEASURED` | Lido direto da telemetria da Riot. | "Você morreu às 14:22" |
| `T2_DERIVED` | Calculado a partir da telemetria **mais uma premissa declarada**. | "A wave estava empurrando — derivado do ritmo de CS e da posição" |
| `T3_INFERRED` | Estimado por visão ou heurística. | "Você tinha Flash em cooldown — lido da HUD" |

O schema **impõe** isso: um `Evidence` com tier T2 ou T3 sem `assumption` preenchida levanta
`ValidationError` na hora da construção. Isso não é burocracia. A API da Riot não tem campo de estado
de wave, e wave management é o coaching de maior valor que existe — um sistema que mistura
silenciosamente CS medido com wave chutada produz conselho que o usuário não pode verificar.

Na prática, ao adicionar uma métrica, pergunte: *isso está literalmente num campo da resposta da
Riot?* Se sim, T1. Se você calculou a partir de campos da Riot assumindo alguma coisa, T2 — **e
escreva a premissa que você assumiu**, em português, na `assumption`. Não escreva "derivado dos
dados".

---

## Commits e PRs

- **Um assunto por PR.** Um PR que melhora `waves.py` e de quebra reformata `render.py` leva pedido
  de divisão.
- Mensagem de commit no imperativo, explicando o porquê quando não for óbvio. Português ou inglês,
  tanto faz.
- **PRs de prompt são revisados pelo delta nas avaliações, não por opinião.** Rode
  `uv run python evals/run.py` e cole o resultado antes/depois. Se a sua mudança melhorou o número,
  ela entra; se piorou, não entra, mesmo que o texto novo pareça melhor.
- **PRs de princípio são revisados por jogadores**, não por devs. Espere discussão sobre o conteúdo
  de LoL. Isso é o processo funcionando.
- Adicione teste para todo comportamento novo em `core/`, `parse/` e `riot/`. Nas outras camadas, use
  o bom senso.

---

## Licenciamento da sua contribuição

Ao abrir um PR você concorda em licenciar a contribuição sob a licença da parte do repositório que
ela toca:

- **Código** → [AGPL-3.0](LICENSE). Ela torna a promessa de "100% gratuito" exigível: quem rodar um
  RiftCoach modificado como serviço hospedado precisa publicar as mudanças.
- **`knowledge/principles/`** → [CC-BY-SA-4.0](knowledge/principles/LICENSE), para que o corpus de
  coaching possa circular independentemente do código.

Não pedimos CLA e não vamos pedir.

---

RiftCoach AI não é endossado pela Riot Games e não reflete as visões ou opiniões da Riot Games ou de
qualquer pessoa oficialmente envolvida na produção ou gestão das propriedades da Riot Games.
