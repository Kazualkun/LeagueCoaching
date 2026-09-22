<div align="center">

# RiftCoach AI

**Um coach de League of Legends gratuito e open source que analisa suas partidas e diz o que corrigir.**

Sem assinatura. Sem cadastro. Nenhum dado sai da sua máquina, a menos que você mande.
Roda localmente na sua GPU, ou em tiers gratuitos de APIs na nuvem se você não tiver uma.

🇧🇷 Português · [🇺🇸 English](README.en.md)

[Manual completo](docs/MANUAL.md) · [Começando](#começando) · [Como funciona](#como-funciona) · [Isso dá ban?](#isso-dá-ban-não) · [Como contribuir](#como-contribuir) · [Arquitetura](docs/ARCHITECTURE.md)

</div>

---

## O que ele faz

Você termina uma partida. Cola o match ID (ou clica nela no seu histórico). Noventa segundos depois,
você recebe algo assim:

> **#1 · gravidade 5 · 14:22 · posicionamento**
> Você morreu na jungle superior inimiga 38 segundos antes do Dragão nascer, sem nenhuma ward num
> raio de 1600 unidades e com o Flash em cooldown.
> **Evidências:** morte D2 *(T1, medido)* · nenhuma ward sua perto do pit nos 60 s anteriores *(T1)* ·
> a wave estava empurrando na direção da torre inimiga *(T2, derivado do ritmo de CS + posição)*
> **Correção:** com um dragão a menos de um minuto de nascer, seu trabalho às 14:00 é visão no river
> de baixo, não uma invade no lado de cima. Atravesse o mapa no recall *antes* do objetivo, não
> depois.
> **Treino:** nas próximas 3 partidas — quando o timer de um objetivo chegar em 60 s, olhe onde você
> está. Se estiver no lado errado do mapa, o erro já aconteceu.
> `[▶ Assistir isso no replay]`

Clique no botão e o seu client do League pula o replay para 14:14 — oito segundos *antes* da morte,
porque o erro é a decisão, não a consequência.

### As marcações aparecem dentro do replay

Abra o replay no client do League e o RiftCoach desenha por cima dele — sincronizado com o relógio
do próprio replay.

![O cartão do erro, alguns segundos antes de ele acontecer](docs/img/overlay-cartao-critico.jpg)

O cartão entra **antes** do erro, com uma barrinha contando os segundos que faltam. Essa é a decisão
de design mais importante do overlay: se ele só aparecesse no instante marcado, você leria o
diagnóstico depois de já ter visto o desfecho — a resposta antes da pergunta. Aparecendo antes, você
lê, olha, e vê acontecer.

Na faixa do topo ficam todas as marcações da partida. Erro crítico é mais largo e vermelho; as suas
anotações têm cor própria. No minimapa, um anel acompanha onde você estava.

E você marca as suas, com a janela do jogo na frente:

| Atalho | O que faz |
|---|---|
| `Ctrl+Alt+E` | marcar um erro seu |
| `Ctrl+Alt+N` | uma anotação |
| `Ctrl+Alt+G` | algo que você fez bem |
| `Ctrl+Alt+Q` | uma dúvida para rever depois |
| `Ctrl+Alt+S` | pular para a **próxima** marcação |
| `Ctrl+Alt+R` | voltar para **onde você parou** da última vez |
| `Ctrl+Alt+H` | esconder o overlay |

**As suas marcações ficam salvas.** Ao reabrir a mesma partida, elas voltam junto com as da IA — e é
comparando as duas que se aprende mais: onde a IA marcou crítico e você não sentiu nada é ponto
cego; onde você sentiu que errou e a medição não viu costuma ser troca de dano, combo ou
posicionamento fino, coisas que a telemetria da Riot simplesmente não registra.

### Quatro formas de usar

| Modo | O que você precisa | O que você recebe |
|---|---|---|
| **Telemetria** | Um match ID | Análise completa. Sem GPU, sem replay, sem download. |
| **Overlay no replay** | Match ID + client do League com o replay aberto | As marcações desenhadas por cima do jogo, no momento certo. |
| **Replay Sincronizado** | Match ID + `.rofl` + client do League | Relatório no navegador com clique-para-pular. |
| **VOD em vídeo** | Um arquivo de vídeo ou URL | Analise qualquer partida gravada, inclusive as que você não jogou. |

---

## Começando

**Não sabe o que é terminal? Não precisa saber. Você não vai ver um.**

1. [**Baixe o ZIP**](https://github.com/Kazualkun/LeagueCoaching/archive/refs/heads/main.zip) e extraia
2. Dois cliques em **`RiftCoach.bat`**

Na primeira vez ele baixa o que falta (leva cerca de um minuto, com o progresso na tela). Depois
disso abre direto nesta janela:

![A janela pedindo a chave da Riot](docs/img/janela-chave.png)

Um passo de cada vez, sem jargão, sempre dizendo qual é o próximo. Pode fechar no meio — ao reabrir,
continua de onde parou, sem repetir pergunta que você já respondeu.

No fim, você escolhe como revisar:

![A tela final, com as duas formas de revisar](docs/img/janela-pronto.png)

<details>
<summary><b>Prefere terminal?</b></summary>

```bash
git clone https://github.com/Kazualkun/LeagueCoaching.git
cd LeagueCoaching

uv run riftcoach gui                         # a mesma janela
uv run riftcoach start                       # o assistente, em texto
uv run riftcoach analyze "SeuNome#TAG"       # direto ao relatório
uv run riftcoach web "SeuNome#TAG"           # no navegador
uv run riftcoach overlay "SeuNome#TAG"       # marcações por cima do replay
uv run riftcoach marcacoes BR1_123 --riot-id "SeuNome#TAG"   # exportar as marcações
uv run riftcoach doctor                      # o que falta, e por quê
```

O `uv` instala o Python e as dependências na primeira execução.

</details>

Dois comandos que valem conhecer antes de qualquer coisa dar errado:

```bash
uv run riftcoach doctor     # o que está configurado, o que falta, e por quê
uv run riftcoach models     # quais provedores de IA existem — e por que os outros não
```

**Para o modo replay e o overlay**, há dois passos a mais, e nenhum dos dois é adivinhável:

```bash
uv run riftcoach enable-replay-api    # adiciona EnableReplayApi=1 no game.cfg, com backup
uv run riftcoach pin-cert             # fixa o certificado do client (com um replay aberto)
```

1. **A Replay API do League vem desligada de fábrica.** O comando acima liga, com backup do arquivo.
2. **O jogo precisa estar em "Sem bordas".** Em tela cheia exclusiva o Windows não deixa nada
   aparecer por cima — não é limitação do RiftCoach, é de como o modo funciona. Troque em
   Configurações → Vídeo → Modo de janela.

**Onde a IA vai rodar** — o `models` detecta seu hardware e recomenda:

| Sua GPU | Recomendação |
|---|---|
| 24 GB+ (4090, 3090, 7900 XTX) | `ollama pull qwen3:30b-a3b` — 100% local, nada sai do seu PC |
| 12–16 GB (4070, 3080, 4060 Ti 16GB) | `ollama pull qwen3:14b` — 100% local |
| 8 GB | `ollama pull qwen3:8b` — local, ou use um tier gratuito na nuvem para resultados melhores |
| Apple Silicon 16 GB+ | `ollama pull qwen3:14b` |
| Sem GPU / notebook | Chave gratuita do Google AI Studio → Gemini Flash. Funciona bem. |

**Se nenhuma opção de IA estiver disponível, o relatório sai do mesmo jeito** — percentis de
referência, curva de vantagem, erros medidos com o custo em probabilidade de vitória, eficiência
de recall. Tudo calculado em Python, nesta máquina. O rodapé sempre diz qual dos dois você
recebeu.

---

## Isso dá ban? Não.

Essa é a primeira pergunta que todo mundo faz, então aqui vai a resposta específica.

O RiftCoach **nunca roda durante uma partida ao vivo.** Ele é arquiteturalmente incapaz disso:

- O único componente que conversa com o client do League (`ReplayGuard`) checa
  `GET https://127.0.0.1:2999/replay/playback` antes de **cada requisição**. Esse endpoint só existe
  enquanto um replay está rodando. Se ele retornar 404 — que é o que acontece durante uma partida ao
  vivo — o RiftCoach se recusa a continuar e explica o motivo.
- Não existe alerta ao vivo, automação, simulação de input nem leitura de memória.
- Todo o resto usa a API oficial Match-v5, em partidas que já acabaram.

**E o overlay?** Ele desenha por cima do **replay**, nunca de uma partida ao vivo — e não por
disciplina, mas porque não há caminho: ele não conhece a porta 2999, pede tudo ao mesmo
`ReplayGuard`, e numa partida ao vivo o guard recusa antes da primeira requisição. É um treinador
desenhando por cima do vídeo de domingo, e a Riot publica a Replay API exatamente para isso.
Raciocínio inteiro em [COMPLIANCE.md](COMPLIANCE.md#o-overlay-sobre-o-replay).

A Riot documenta e permite tanto a Live Client Data API quanto a Replay API. O que ela proíbe é
software que automatiza a jogabilidade ou revela informação que você não teria de outra forma. O
RiftCoach não faz nenhuma das duas coisas, em momento algum. Raciocínio completo em
[COMPLIANCE.md](COMPLIANCE.md).

---

## Como funciona

A maioria dos projetos de "coach com IA" joga uma timeline de 2 MB dentro de um LLM e torce. Isso
falha, porque o modelo acaba fazendo percepção, aritmética, memorização de fatos e julgamento tudo
de uma vez — e ele é ruim nos três primeiros.

O RiftCoach faz os três primeiros em Python e entrega ao modelo apenas o quarto:

```
 timeline de 2,5 MB  (~600.000 tokens)
        │
        ▼  filtro de perspectiva · coordenadas -> zonas nomeadas do mapa · resumos por fase
        ▼  cada métrica calculada em Python · mortes enriquecidas com contexto tático
        │
  MatchFacts  (~1.100 tokens)   +  dados do patch via DataDragon  +  benchmarks por elo
        │
        ▼  quatro passes especialistas pequenos, rodando em paralelo:
        ▼  lane · macro · economia · lutas   ->   o head coach unifica e ranqueia
        │
  CoachingReport — toda afirmação com timestamp, citada e marcada com o nível de certeza
        │
        ▼  validada contra o patch atual, depois renderizada
```

Duas ideias fazem a maior parte do trabalho:

**Níveis de evidência.** Toda afirmação é marcada como `T1` (medido diretamente dos dados da Riot),
`T2` (derivado, com a premissa declarada) ou `T3` (inferido a partir de vídeo). A API da Riot não tem
nenhum campo de estado da wave, então um coach que afirma com confiança "você deveria ter dado
freeze" está chutando. O nosso diz: *"seu ritmo de CS e sua posição indicam que você estava
empurrando (derivado) — se isso estiver certo, o freeze estava disponível."* Você consegue conferir.
É exatamente esse o ponto.

**O modelo nunca é responsável pelos fatos.** Stats de itens, custos e números de campeões são
injetados a partir de um banco de dados fixado no patch, construído comparando versões do
DataDragon. Depois, um validador varre a saída e rejeita qualquer item ou campeão que não exista no
seu patch — então ele nunca vai te mandar buildar algo que foi removido seis patches atrás.

Detalhamento técnico completo: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

---

## Status do projeto

> **Utilizável hoje, com ou sem IA.** O pipeline de dados funciona ponta a ponta. Com um modelo
> disponível — local ou em tier gratuito — quatro analistas especialistas rodam em paralelo sobre os
> fatos já medidos e um head coach unifica. Sem nenhum modelo, o mesmo comando entrega o relatório
> determinístico completo. O rodapé sempre diz qual dos dois você recebeu.
>
> `riftcoach web` abre o relatório no navegador com a curva de vantagem e, se houver um replay
> rodando, clicar num finding faz o client do League pular para lá.

| Etapa | Status |
|---|---|
| 0 · Cliente da Riot + cache + fixtures | ✅ |
| 1 · Destilação da timeline → `MatchFacts` + render | ✅ |
| **6 · Motor de vantagem** (avaliação estilo xadrez) | ✅ |
| 2 · Sync do DataDragon | ✅ |
| **2 · Benchmarks + relatório sem IA** (`riftcoach analyze`) | ✅ |
| **3 · Roteador de modelos + passes dos analistas** | ✅ |
| **4 · Harness de avaliação + matriz de compatibilidade** | ✅ |
| **5 · Interface web + controle do replay** | ✅ |
| **7 · Visão: geometria da HUD, amostragem, OCR e sincronia** | ✅ |
| 7 · Visão: leitura do minimapa por VLM | ☐ |
| **Modo B validado contra o client real** | ✅ |
| 7 · Empacotamento Tauri | ☐ |

**576 testes**, `ruff` e `mypy --strict` limpos. Números medidos em 16 partidas reais de SR:

| | |
|---|---|
| Redução de tokens | **168x** (~214.000 → ~1.275 por partida) |
| Compressão do cache | 19x (timeline de 2,5 MB → 180 KB) |
| Erros críticos detectados | 1,5 por jogador por partida (calibrado em 1.013 amostras) |

---

## Como contribuir

**Você não precisa ser dev Python para fazer a contribuição mais valiosa deste projeto.**

### Se você é um jogador de elo alto — escreva conhecimento de coaching

`knowledge/principles/` é Markdown puro. Um arquivo bem escrito sobre mecânica de bounce ou postura
de trade melhora todos os relatórios que a ferramenta gera, para sempre. Isso vale mais do que a
maioria dos PRs de código.

```markdown
---
id: waves-bounce-mechanics
applies_to: {roles: [TOP, MIDDLE], phases: [early]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error]
---
Uma wave dá bounce quando...
```

Comece pelo [`CONTRIBUTING.md`](CONTRIBUTING.md) → *"Escrevendo princípios"*.

### Se você quer melhorar a saída da IA — edite os prompts

`analysis/prompts/*.md` são arquivos Markdown, não strings enterradas no código. Mude um, rode
`uv run python evals/run.py`, e o harness te diz se você melhorou ou piorou contra 30 partidas
anotadas por humanos. PRs de prompt são bem-vindos e são revisados pelo delta nas avaliações, não
por opinião.

### Se você é dev Python

Boas primeiras issues, mais ou menos em ordem de dependência:

- **`parse/waves.py`** — melhore o proxy de estado da wave. Hoje são quatro estados grosseiros. Quem
  deixar isso significativamente mais preciso melhora a categoria de coaching mais valiosa do
  produto.
- **`parse/deaths.py`** — mais contexto tático por morte.
- **`llm/providers/`** — adicione um provedor gratuito. A interface é uma única classe.
- **`vision/rois.py`** — regiões de recorte da HUD para resoluções diferentes de 1080p e ultrawide.
- **`evals/golden/`** — anote uma partida. Não precisa de código, valor enorme.

### Se você só quer ajudar agora

Rode nas suas próprias partidas e abra uma issue quando o conselho estiver errado. Anexe o
relatório. Findings ruins são os relatos de bug mais úteis que este projeto pode receber — e, como
toda afirmação carrega sua evidência e seu nível, eles são de fato diagnosticáveis.

### Regras inegociáveis

1. **Nada que rode durante uma partida ao vivo.** Inegociável, sem exceções, sem "mas e se for só um
   overlay". PRs que mexem nisso são fechados.
2. **Nenhuma funcionalidade que exija pagamento.** Toda capacidade precisa de um caminho local e um
   caminho gratuito na nuvem.
3. **Nada de scraping de sites terceiros** (op.gg, u.gg, porofessor). Só API da Riot e DataDragon.
4. **Toda afirmação da IA precisa estar ancorada e citada.** Se ela não consegue citar a evidência,
   é um bug.

---

## Perguntas frequentes

**Preciso de GPU?** Não. Os tiers gratuitos na nuvem funcionam bem, e o relatório estatístico sem IA
funciona sem absolutamente nada.

**Meus dados saem da minha máquina?** Só se você escolher um provedor na nuvem. Configure
`privacy_mode: strict` e isso é aplicado no nível do roteador — provedores na nuvem são filtrados
antes de qualquer outra consideração.

**Ele lê arquivos de replay `.rofl`?** Ele lê os metadados. O conteúdo da partida é criptografado e a
chave não é recuperável depois — quem afirma extrair dados de posição de um `.rofl` bruto está
enganado. Usamos a timeline do Match-v5 para os dados e o client do League para a reprodução, o que
entrega estritamente mais.

**Vai ficar desatualizado depois do próximo patch?** Não. Os dados do patch são sincronizados do
DataDragon automaticamente e um validador rejeita qualquer coisa que não exista no seu patch. É por
isso que não existe modelo fine-tunado — [o argumento completo está aqui](docs/04-knowledge-base.md).

**Funciona para ARAM / Arena?** Summoner's Rift primeiro. O parser lida com outras filas sem quebrar,
mas os princípios de coaching são específicos do SR.

---

## Licença

Código: **AGPL-3.0**. Corpus de coaching (`knowledge/principles/`): **CC-BY-SA-4.0**.

RiftCoach AI não é endossado pela Riot Games e não reflete as visões ou opiniões da Riot Games ou de
qualquer pessoa oficialmente envolvida na produção ou gestão das propriedades da Riot Games. League
of Legends e Riot Games são marcas registradas ou marcas comerciais da Riot Games, Inc.
