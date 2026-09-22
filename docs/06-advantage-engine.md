# §6 — Motor de Vantagem: avaliação no estilo de engine de xadrez

> Adicionado ao blueprint depois da etapa 1. Muda como a gravidade funciona em
> todo o sistema, então vem antes do `rules.py`.

## 6.1 A ideia

Uma engine de xadrez não diz "você jogou mal". Ela diz: a posição valia **+1.5**, você jogou Cf3, a
posição passou a valer **−0.8**, logo esse lance custou **2.3**. O erro fica **medido**, não opinado.

O mesmo vale para o LoL:

```
avaliar(estado)  ->  vantagem em ouro-equivalente  ->  probabilidade de vitória
erro             =   queda de probabilidade atribuível ao jogador
```

## 6.2 Por que isso muda a arquitetura

Sem o motor, `Finding.severity` seria um número que o modelo inventa — e modelos de linguagem são
notoriamente ruins em calibrar gravidade. Eles chamam tudo de importante, ou nada.

Com o motor, gravidade vira **perda de probabilidade de vitória medida** (T1/T2), e o modelo fica
apenas com o trabalho que faz bem: explicar *por que* aconteceu e o que fazer diferente.

Isso também é o que dá base objetiva para marcar "erro" e "erro crítico" na linha do tempo do
replay: não é uma opinião, é um número.

## 6.3 Unidade e pesos

Ouro é a unidade natural do LoL, como o peão é a do xadrez: tudo se converte nela. Cada peso inclui
o ouro direto **mais** o controle de mapa concedido.

| Termo | Peso (ouro-eq.) |
|---|---|
| Torre | 1.000 |
| Inibidor | 1.600 |
| Dragão | 650 cada |
| Alma do dragão | +3.000 |
| Barão | 1.800, decaindo linearmente em 3 min |
| Arauto | 450 |
| Grubs | 120 cada |
| Nível de time | 180 |

**Probabilidade de vitória** é uma logística sobre a vantagem, com escala que **cresce com o tempo**:

```
escala(t) = 3000 + 150 × minutos
wp        = sigmoid(vantagem / escala(t))
```

A mesma vantagem de ouro pesa menos no late game, porque o ouro total no mapa cresce — 5k aos 15 min
é uma fração muito maior da partida do que 5k aos 40.

## 6.4 Toda avaliação é decomponível

Regra de projeto: a avaliação **nunca** é um escalar sozinho. Uma engine de xadrez pode dizer só
"+1.5"; um coach precisa dizer *por que*:

```
decomposicao aos 20min:
   ouro=-5550 (-5550g)      dragoes=+2 (+1300g)
   xp=-2700 (-486g)         arauto=-1 (-450g)
   torres=-2 (-2000g)       grubs=-3 (-360g)
```

Sem isso, a avaliação vira um número mágico e deixa de poder virar evidência citável.

## 6.5 Atribuição: `involvement`

Nem toda queda é culpa do jogador. O motor separa três casos:

| | Significado | Correção que pede |
|---|---|---|
| `direct` | ele morreu, ou entregou o abate/objetivo | decisão individual |
| `positional` | aconteceu longe dele, e ele estava no lado errado do mapa | leitura de mapa / rotação |
| `team` | ele estava presente e mesmo assim deu errado | execução de luta |

*"Você morreu"* e *"seu time perdeu o barão enquanto você empurrava a top"* pedem correções
completamente diferentes. Colapsar os dois é o erro mais comum em ferramentas de coaching.

## 6.6 Calibração dos limiares

Limiar chutado produz "tudo é crítico" ou "nada é crítico" — os dois inúteis. Medimos a distribuição
real (`tools/calibrate_severity.py`, **1.013 erros em 160 jogadores**):

```
p50=5.8   p70=9.6   p80=11.8   p90=16.4   p95=19.1   p99=25.7   max=35.9
```

Alvo de projeto: **~1,5 críticos por jogador por partida**.

| Gravidade | Limiar | Resultado medido |
|---|---|---|
| sev1 | ≥ 1,5 pp | 5,8 / jogador |
| sev2 | ≥ 3,0 pp | 4,9 / jogador |
| sev3 | ≥ 6,0 pp | 3,1 / jogador |
| **sev4 (crítico)** | **≥ 11,0 pp** | **1,5 / jogador** ✓ |
| sev5 | ≥ 21,0 pp | 0,2 / jogador |

Os limiares anteriores (2/5/10/18/30) foram chutados e davam **0,5 críticos por jogador** — metade
das pessoas nunca veria um.

## 6.7 Limitações declaradas

- **Resolução temporal de ~1 minuto.** Os frames da timeline são de 60 em 60 segundos, então a
  janela de avaliação é de 45 s para cada lado. Sabemos *que* custou, com precisão de minuto, não o
  segundo exato. Por isso todo erro medido é **T2**, nunca T1.
- **Pesos são heurísticos.** São estimativas de ouro-equivalente, não ajustadas estatisticamente. O
  ajuste adequado exige uma amostra grande de partidas com resultado conhecido — exatamente o job de
  CI já planejado para os benchmarks (§4, L3). Enquanto não existe, um modelo **transparente e
  explicável** é preferível a um ajustado em 16 partidas, que daria overfit com cara de precisão.
- **Composição não entra.** Um time de late game com −3k aos 20 min está melhor do que o número
  sugere. Isso exige dados de escalonamento por campeão e fica para depois.

---

# §7 — Marcações e sessão de revisão

## 7.1 Duas fontes, uma linha do tempo

```python
class Mark(BaseModel):
    t_ms: int
    author: Literal["ai", "user"]
    kind: Literal["error", "critical", "note", "question", "good"]
    text: str
    severity: int | None      # só em marcações da IA
    wp_loss: float | None     # só em marcações da IA
```

As marcações da IA e as do jogador ficam **juntas de propósito**. O valor da revisão está em
comparar:

- onde a IA marcou **crítico** e o jogador não percebeu nada → ponto cego
- onde o jogador **sentiu** que errou e a métrica não viu → erro de execução que a telemetria não
  alcança (trade, combo), ou percepção equivocada

Os dois casos ensinam, e por motivos diferentes.

## 7.2 Retomar de onde parou

`ReviewSession` guarda `last_position_ms` e todas as marcações por `(match_id, puuid)`. Ao reabrir,
a linha do tempo volta inteira. Toda marcação tem `seek_ms = t_ms − 8s`: **o erro é a decisão, não o
desfecho.**

---

# §8 — Categorias de análise e o que cada uma exige

Pedido: análise de controle de wave, troca de dano, posicionamento, combo, movimentação e tomada de
decisão. Nem todas são alcançáveis pela mesma fonte de dados.

| Categoria | Viável hoje | Fonte | Nível |
|---|---|---|---|
| `wave` | ✅ | ritmo de CS + posição | T2 |
| `positioning` | ✅ | zonas + contexto de morte | T2 |
| `movement` | ✅ | série de posições por minuto | T2 |
| `decision` | ✅ | **motor de vantagem** (§6) | T1/T2 |
| `macro` / `objective` / `vision` / `recall` / `tempo` / `itemization` | ✅ | telemetria | T1/T2 |
| `trading` | ⚠️ Modo B/C | HP só existe **por minuto**; uma troca dura segundos | T3 |
| `combo` | ⚠️ Modo B/C | a timeline **não emite nenhum evento de uso de habilidade** | T3 |

**`trading` e `combo` não são omissão — o dado não existe na API.** A timeline tem
`SKILL_LEVEL_UP` (quando você sobe a habilidade), nunca o uso dela. E `championStats` traz vida, mas
amostrada de minuto em minuto, o que é inútil para julgar uma troca de 3 segundos.

As duas só ganham evidência real no caminho de visão (Modos B e C), lendo a barra de vida e os
cooldowns da HUD quadro a quadro — e mesmo lá permanecem **T3, inferidas**. Prometer análise de combo
a partir de telemetria seria inventar número.
