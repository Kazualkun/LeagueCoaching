# RiftCoach AI — Manual de Uso

> Guia completo: instalação, configuração, uso e as ferramentas de marcação durante o replay.
> Para a arquitetura técnica, veja [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. O que você precisa instalar

**Resposta curta: quase nada.**

| Item | Precisa? | Por quê |
|---|---|---|
| **League of Legends** | Sim, para o modo replay | O client é o único programa que reproduz `.rofl` |
| **uv** | Sim | Instala o Python e as dependências sozinho |
| Python | **Não** | O `uv` instala a versão certa para você |
| Ollama | Opcional | Só se quiser IA 100% local (precisa de GPU) |
| Player de replay | **Não** | Explicado abaixo |

### Sobre "programas que rodam replay de LoL"

Você perguntou se devemos usar um desses ou rodar o replay nós mesmos. A resposta é **nenhum dos
dois**:

**Não existe player de replay de LoL de terceiros.** O arquivo `.rofl` tem o conteúdo da partida
criptografado, e a chave não é recuperável depois. Os programas que aparecem em busca (ROFL Player e
similares) fazem uma coisa só: **abrem o client oficial** para você. Quem renderiza a partida é
sempre a Riot.

O RiftCoach faz o mesmo, só que melhor: controla o client oficial pela **Replay API** (uma API local
documentada pela Riot, na porta 2999). Quando você clica num erro marcado, o client pula sozinho para
aquele momento.

Consequência prática: **você não instala nada além do League de Legends que já tem.** E isso é o que
mantém tudo dentro das regras da Riot — a gente não decifra, não intercepta, não injeta nada.

> **Limite de prazo dos replays:** a Riot mantém replays baixáveis por cerca de 2 patches. Depois
> disso o arquivo não abre mais. O modo Telemetria (análise sem replay) continua funcionando para
> sempre, porque usa a API de partidas.

---

## 2. Instalação

### Passo 1 — instalar o uv

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**Mac / Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Passo 2 — baixar e rodar

```bash
git clone https://github.com/Kazualkun/LeagueCoaching.git
cd LeagueCoaching
uv run riftcoach doctor
```

O `uv` baixa o Python 3.13 e todas as dependências na primeira execução (leva ~30s). Não precisa
criar venv, não precisa `pip install`.

### Passo 3 — chave da API da Riot

1. Acesse [developer.riotgames.com](https://developer.riotgames.com) e faça login com sua conta Riot
2. Na página inicial, copie a **Development API Key** (começa com `RGAPI-`)
3. No terminal:

```bash
uv run riftcoach auth
```

Cole a chave quando pedir. Ela vai para o **cofre de credenciais do sistema** (Gerenciador de
Credenciais no Windows, Keychain no Mac), nunca para um arquivo.

> ⚠️ **A chave de desenvolvimento expira a cada 24 horas.** Se aparecer erro de chave, gere outra no
> portal e rode `riftcoach auth` de novo.
>
> **Solução permanente:** no portal, vá em *Register Product* → *Personal API Key*, descreva o
> projeto ("ferramenta pessoal de análise pós-jogo"). A Personal Key tem o mesmo limite de taxa e
> **não expira**. A aprovação leva alguns dias.

### Passo 4 — sincronizar os dados do patch

```bash
uv run riftcoach sync-patch
```

Baixa nomes e atributos de itens, runas e campeões do DataDragon (a CDN pública da Riot). Sem isso, o
relatório mostra `3076` em vez de `Colete Espinhoso`.

### Passo 5 — conferir

```bash
uv run riftcoach doctor --riot-id "SeuNome#TAG"
```

As quatro APIs devem aparecer em verde:

```
┌───────────────┬──────────┬────────┬────────────────┐
│ API           │ Host     │ Status │ Detalhe        │
├───────────────┼──────────┼────────┼────────────────┤
│ LOL-STATUS-V4 │ br1      │ ok     │ ok             │
│ ACCOUNT-V1    │ americas │ ok     │ SeuNome#BR1    │
│ MATCH-V5      │ americas │ ok     │ 1 registro(s)  │
│ LEAGUE-V4     │ br1      │ ok     │ 2 registro(s)  │
└───────────────┴──────────┴────────┴────────────────┘
```

> **Seu Riot ID** é o `Nome#TAG` que aparece no client — não o nome de invocador antigo. A tag nem
> sempre é `BR1`; pode ser qualquer coisa que você escolheu.

---

## 3. Configuração

Tudo tem padrão sensato. Só mexa se precisar.

### Pelo arquivo `.env` (copie de `.env.example`)

```ini
RIOT_PLATFORM=br1          # br1, na1, euw1, kr, las, lan...
LOCALE=pt_BR               # idioma dos nomes de item e do relatório
PRIVACY_MODE=relaxed       # strict = nada sai da sua máquina
```

### As opções que importam

| Opção | Valores | O que faz |
|---|---|---|
| `RIOT_PLATFORM` | `br1`, `na1`, `euw1`, `kr`... | Sua região. Define de onde as partidas vêm |
| `LOCALE` | `pt_BR`, `en_US`, `es_MX`... | Idioma dos nomes **e** do relatório. Os dois andam juntos |
| `PRIVACY_MODE` | `relaxed` / `strict` | `strict` **bloqueia qualquer provedor de nuvem**, antes de qualquer outra escolha |

> **Sobre `LOCALE`:** não é só cosmético. O validador confere os nomes de item que a IA escreve
> contra a tabela do patch. Banco em inglês + relatório em português faria todo item ser rejeitado
> como inventado. Por isso os dois são a mesma configuração.

### Escolhendo onde a IA roda

O assistente detecta seu hardware e recomenda. Resumo:

| Sua GPU | Recomendação | Comando |
|---|---|---|
| 24 GB+ (4090, 3090, 7900 XTX) | 100% local | `ollama pull qwen3:30b-a3b` |
| 12–16 GB (4070, 3080) | 100% local | `ollama pull qwen3:14b` |
| 8 GB | Local ou nuvem | `ollama pull qwen3:8b` |
| Apple Silicon 16 GB+ | 100% local | `ollama pull qwen3:14b` |
| Sem GPU / notebook | Nuvem grátis | Chave do Google AI Studio |

**Se tudo falhar, o relatório ainda sai.** Sem GPU, sem chave de IA, sem nada: você ainda recebe
percentis de referência, curva de vantagem, erros medidos, mapa de mortes e eficiência de recall —
tudo calculado localmente, sem modelo nenhum.

---

## 4. Usando

### Análise rápida (sem replay)

```bash
uv run riftcoach fetch "SeuNome#TAG" -n 5 -q 420
```

| Flag | Significado |
|---|---|
| `-n 5` | quantas partidas baixar |
| `-q 420` | fila: `420` ranked solo, `440` flex, `400` normal draft, `450` ARAM |

> **Só Summoner's Rift.** Arena (`CHERRY`) é rejeitada de propósito: naquele modo as métricas da Riot
> vêm zeradas, e analisar produziria números falsos com cara de medição.

### O relatório

```bash
uv run riftcoach analyze "SeuNome#TAG"
```

Analisa a sua partida mais recente e imprime o relatório. **Não precisa de GPU, nem de chave de IA,
nem de internet além da própria API da Riot** — tudo é calculado em Python nesta máquina.

| Flag | Significado |
|---|---|
| `-m BR1_123456` | analisar uma partida específica em vez da mais recente |
| `-l 3` | analisar a 3ª partida mais recente |
| `-q 420` | considerar só uma fila ao procurar a partida |
| `-o relatorio.txt` | gravar num arquivo em vez de imprimir |
| `--no-history` | não usar o seu próprio histórico como referência de percentil |

### Ligando a IA

```bash
uv run riftcoach models
```

Mostra o hardware detectado, quais provedores estão disponíveis e **por que os outros não estão**.
Este comando existe porque "a IA não rodou" precisa ter uma resposta: sem ele, um serviço fora do ar
e uma chave ausente são indistinguíveis do lado de fora.

Você tem três caminhos, e nenhum deles é obrigatório:

| Caminho | Como | Custo |
|---|---|---|
| **Local** | Instale o [Ollama](https://ollama.com) e rode `ollama pull qwen3:8b` (ou o modelo que o `models` recomendar para a sua placa) | nada sai da sua máquina |
| **Nuvem grátis** | Chave do [Google AI Studio](https://aistudio.google.com) em `GEMINI_API_KEY`, ou [Groq](https://console.groq.com) em `GROQ_API_KEY` | gratuito, com cota diária |
| **Nenhum** | Não faça nada | o relatório determinístico completo |

As chaves vão no cofre do sistema ou numa variável de ambiente — nunca em arquivo versionado.

```bash
riftcoach analyze "Nome#TAG"            # usa IA se houver; degrada sozinho se não
riftcoach analyze "Nome#TAG" --no-ai    # força o relatório determinístico
```

**Privacidade.** Com `PRIVACY_MODE=strict`, todo provedor de nuvem é eliminado **antes** de qualquer
outra consideração de roteamento — não é preferência, é filtro. Se sobrar só nuvem, o resultado é o
relatório sem IA, nunca um envio silencioso.

**Ordem de escolha.** Local vence nuvem gratuita, que vence paga. Não é só economia: modelo local não
tem cota, então um relatório em lote nunca queima a franquia diária de que uma pergunta interativa vai
precisar. A exceção é quando o local é lento demais (abaixo de ~15 tokens/s) e a tarefa é interativa —
aí a nuvem ganha.

**Sobre os percentis.** O relatório compara os seus números com uma população, e sempre diz **qual**
entre colchetes. São três fontes, da melhor para a pior:

| Fonte | Quando aparece |
|---|---|
| `amostra da Riot` | existe um parquet de benchmarks para o patch da partida |
| `seu próprio histórico, N partidas` | você tem pelo menos 10 partidas da mesma rota em cache |
| `modelo embarcado (não calibrado)` | nenhuma das outras — é um chute educado, e ele diz isso |

A terceira é o padrão no primeiro dia. Para sair dela rápido, rode `fetch -n 20` uma vez: o `analyze`
passa a usar o seu próprio histórico, que para rotas e campeões fora do meta é a comparação mais
honesta que existe.

> **Partidas muito curtas não geram percentil nenhum.** Num remake de 1:10 ninguém jogou o suficiente
> para ter um número que descreva o jogo, e "0 CS aos 10 minutos" seria uma medição que nunca
> aconteceu, não um percentil baixo.

### A interface web

```bash
uv run riftcoach web "SeuNome#TAG"
```

Abre o relatório no navegador: a curva de probabilidade de vitória com um marcador em cada erro, os
findings com o nível de evidência de cada afirmação, e os benchmarks com a fonte de cada percentil.

O servidor escuta **só em 127.0.0.1**. Não há login porque não há superfície remota.

| Flag | Significado |
|---|---|
| `-m BR1_123456` | uma partida específica |
| `-l 3` | a 3ª partida mais recente |
| `--no-ai` | força o relatório determinístico |
| `-p 8080` | outra porta |
| `--no-open` | não abrir o navegador sozinho (útil em servidor) |

### Revisão com replay sincronizado

1. Abra o League of Legends
2. No histórico de partidas, baixe o replay e dê play
3. Com o replay rodando, rode `riftcoach web "SeuNome#TAG"`

O RiftCoach detecta o replay e **sincroniza o relógio sozinho** — são três relógios diferentes
(timeline da Riot, arquivo de replay, vídeo) e eles não concordam. Clicar num erro faz o client pular
para lá.

> **Ele sempre pula 8 segundos ANTES do momento marcado.** Não é bug. O erro é a decisão, não a
> consequência — pular exatamente na sua morte mostra você morrendo, não o que causou.

**Se não houver replay rodando, o botão fica desabilitado e a página diz por quê.** O relatório
inteiro funciona sem ele; o replay é o acréscimo, não o produto.

#### Por que ele às vezes recusa

O RiftCoach verifica `GET https://127.0.0.1:2999/replay/playback` **antes de cada requisição** ao
client. Essa rota só existe enquanto um replay está rodando. Qualquer resultado ambíguo — 404,
timeout, conexão recusada, corpo inesperado — vira recusa.

Isso é deliberado e não é configurável. Durante uma partida ao vivo essa rota some enquanto o resto
da API local continua respondendo, então um 404 ali é indício de que pode haver partida em andamento.
Raciocínio completo em [COMPLIANCE.md](../COMPLIANCE.md).

---

## 5. Lendo o relatório

### A curva de vantagem

Funciona como a avaliação de uma engine de xadrez, mas em probabilidade de vitória:

```
min   ouro-eq    wp
  0        +0   50%  --------------------|--------------------
  9      -362   48%  -------------------|---------------------
 18     -3759   34%  --------------|--------------------------
 26    -11458   16%  ------|----------------------------------
 36    -26011    4%  --|--------------------------------------
```

Onde a linha despenca, algo caro aconteceu. E o relatório diz o quê.

### Erros medidos

```
18:15 [sev4] -11.1pp  direct      morreu na jungle superior própria, com RIFTHERALD_EM_32s
18:47 [sev4] -12.2pp  positional  perdeu RIFTHERALD; você estava em OWN_BASE
```

- **`-11.1pp`** = quanto de probabilidade de vitória aquilo custou. É medido, não opinião
- **`sev4`** = crítico (aparece em vermelho na linha do tempo)
- **`direct` / `positional` / `team`** = seu grau de envolvimento:

| | Significa | O que treinar |
|---|---|---|
| `direct` | você morreu / entregou o objetivo | decisão individual |
| `positional` | aconteceu longe, e você estava no lado errado do mapa | leitura de mapa, rotação |
| `team` | você estava lá e mesmo assim deu errado | execução de luta |

### Níveis de confiança — leia isto

Toda afirmação vem marcada com o quanto ela é confiável:

| Nível | Significa | Exemplo |
|---|---|---|
| **T1** | **Medido** direto dos dados da Riot | "CS@10 = 67" |
| **T2** | **Derivado**, com a premissa declarada | "estava empurrando (ritmo de CS + posição)" |
| **T3** | **Inferido** de vídeo | "a wave estava em slow push" |

**Por que isso existe:** a API da Riot **não tem** campo de estado de wave. Um coach que afirma com
certeza "você devia ter dado freeze" está chutando. O nosso diz *"seu ritmo de CS e sua posição
indicam que você estava empurrando (derivado) — se isso estiver certo, o freeze estava disponível"*.

Você consegue conferir. É exatamente esse o ponto.

### O que ainda não é analisável

Duas coisas dependem do modo replay/vídeo, porque **a telemetria não tem o dado**:

| Análise | Por quê |
|---|---|
| **Troca de dano** | A vida só é registrada **de minuto em minuto**; uma troca dura segundos |
| **Combo** | A timeline **não emite nenhum evento de uso de habilidade** |

Não é omissão nossa — o dado não existe na API. As duas vão sair pela leitura de tela no modo replay,
e mesmo lá ficam marcadas como **T3**.

---

## 6. Ferramentas de marcação durante o replay

### Layout da tela

```
┌──────────────────────────────────────────────────────────────────────────┐
│  RiftCoach · Garen TOP · BR1_3239179616 · patch 16.9 · DERROTA           │
├──────────────────────────────────────────────────────────────────────────┤
│  CURVA DE VANTAGEM                                          50%▁▄▆█▆▄▂▁  │
│  ━━━━━━━━━●━━━━━━━━━━━◆━━━━━━━━━━━━━●━━━━━━━━━━◆━━━━━━━━━━━━━━━━━━━━━━   │
│   0:00    7:24       18:15         25:21      32:03            35:16     │
│           ● erro    ◆ crítico    ▲ sua marcação                          │
├───────────────────────────────────────┬──────────────────────────────────┤
│  MARCAÇÕES                            │  DETALHE                         │
│                                       │                                  │
│  ◆ 18:15  CRÍTICO   −11.1pp   [IA]    │  Morreu na jungle superior       │
│  ◆ 18:47  CRÍTICO   −12.2pp   [IA]    │  própria, com Arauto a 32s       │
│  ● 25:21  erro      −8.0pp    [IA]    │                                  │
│  ▲ 26:40  anotação            [você]  │  Evidências:                     │
│  ● 32:03  erro      −5.2pp    [IA]    │   • morte D4  (T1, medido)       │
│                                       │   • nenhuma ward sua nos 60s (T1)│
│  [+ Marcar momento atual]             │   • wave empurrando (T2)         │
│  [⚑ Marcar como erro meu]             │                                  │
│  [? Tenho uma dúvida aqui]            │  ▶ Assistir  (pula p/ 18:07)     │
├───────────────────────────────────────┴──────────────────────────────────┤
│  ⏮  ⏪  ▶  ⏩  ⏭     18:15 / 35:16     velocidade 1x     [↻ Repetir]      │
└──────────────────────────────────────────────────────────────────────────┘
```

### As ferramentas

| Ferramenta | Atalho | O que faz |
|---|---|---|
| **Marcar momento** | `M` | Cria uma marcação no tempo atual do replay |
| **Marcar erro meu** | `E` | Marca como erro percebido por você |
| **Dúvida** | `?` | Marca um ponto para perguntar depois |
| **Marcar acerto** | `B` | Marca algo que você fez bem (serve para comparar) |
| **Anotar** | `N` | Escreve texto livre na marcação selecionada |
| **Próximo erro** | `→` | Pula para a próxima marcação da IA |
| **Repetir trecho** | `R` | Volta 8s e reproduz de novo |
| **Perguntar à IA** | `P` | Pergunta sobre o momento atual |

### Por que as marcações da IA e as suas ficam juntas

De propósito. O valor da revisão está em **comparar as duas**:

- Onde a **IA marcou crítico e você não percebeu nada** → ponto cego. É o mais valioso
- Onde **você sentiu que errou e a métrica não viu** → normalmente é erro de execução (trade, combo)
  que a telemetria não alcança, ou uma percepção equivocada. Os dois casos ensinam

### Retomar depois

Tudo é salvo automaticamente por partida. Ao reabrir, suas marcações, anotações e **a posição onde
você parou** voltam. Você pode revisar uma partida em várias sessões.

---

## 7. Perguntas frequentes

**Dá ban?** Não. O RiftCoach nunca roda durante partida ao vivo — é arquiteturalmente incapaz disso.
Antes de **cada** chamada ao client ele confere se um replay está rodando; se não estiver, recusa.
Detalhes em [COMPLIANCE.md](../COMPLIANCE.md).

**Meus dados saem da máquina?** Só se você escolher um provedor de nuvem. Com `PRIVACY_MODE=strict`,
provedores de nuvem são filtrados no roteador, antes de qualquer outra decisão.

**Preciso de GPU?** Não. Tiers gratuitos de nuvem funcionam bem, e o relatório estatístico funciona
sem nada.

**Por que a análise pede o replay?** Não pede. O modo Telemetria dá ~80% do valor só com o match ID.
O replay adiciona a revisão clicável e a análise de tela.

**Funciona com ARAM ou Arena?** Só Summoner's Rift. O parser não quebra em outros modos — ele recusa
explicitamente, porque analisar produziria métricas zeradas com aparência de medição.

**Por que os recalls são "inferidos"?** Porque a Riot não emite evento de recall. Deduzimos de
agrupamentos de compra (só dá para comprar na base) mais transições de posição. Por isso é T2.

**Analisou uma partida antiga e os nomes dos itens estão estranhos.** Rode
`uv run riftcoach sync-match-patches` — ele baixa os dados de **todos** os patches das suas partidas
em cache. O relatório sempre usa o patch em que a partida foi jogada, nunca o atual.

---

## 8. Se der errado

| Sintoma | Causa | Solução |
|---|---|---|
| `Unknown apikey` (401) | A chave foi substituída no portal | Copie a chave atual e rode `riftcoach auth` |
| `403 Forbidden` | Chave expirada (24h) | Gere outra e rode `riftcoach auth` |
| `Não encontrado` (404) | Riot ID errado | Confira `Nome#TAG` no client e a região no `.env` |
| `mapId 30 não é Summoner's Rift` | Partida de Arena | Use `-q 420` para filtrar ranked solo |
| `League client não está rodando` | Sem replay aberto | Abra o replay antes |
| `Parece haver uma partida ao vivo` | Proteção de conformidade | Encerre a partida — o RiftCoach nunca roda ao vivo |
| Itens aparecem como números | Patch não sincronizado | `uv run riftcoach sync-match-patches` |

Diagnóstico completo:

```bash
uv run riftcoach doctor --riot-id "SeuNome#TAG"
```

Ele testa cada API separadamente e mostra a mensagem original da Riot — testar em bloco esconderia
qual delas é o problema.
