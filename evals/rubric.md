# O que significa "um bom finding"

Este documento é o contrato do harness de avaliação. Ele existe porque, sem um critério escrito
**antes** de olhar os resultados, "melhorou" vira opinião — e PRs de prompt viram discussão de gosto.

A regra do projeto: **PRs de prompt são revisados pelo delta nas avaliações, não por opinião.** Isto
aqui é o que torna essa regra aplicável.

---

## As quatro dimensões

Um finding é pontuado em quatro eixos independentes. Eles são independentes de propósito: um finding
pode estar factualmente correto e ser inútil, ou ser um conselho excelente ancorado no momento
errado, e essas duas falhas pedem correções completamente diferentes.

### 1. Fundamentação (`grounding`) — 0 ou 1, eliminatório

**Toda afirmação carrega evidência, e evidência não-medida declara a premissa.**

Este eixo é binário e elimina: um finding que falha aqui vale zero nos outros três, por melhor que
seja o texto. Não é rigor por rigor — um conselho que o jogador não consegue conferir não é conselho,
é palpite com formatação bonita.

Falha se:

- qualquer `Evidence` de nível `T2` ou `T3` vem sem `assumption` preenchida;
- a `assumption` é genérica ("derivado dos dados", "com base no contexto") em vez de dizer **o que**
  foi assumido;
- o finding cita um número que não aparece no contexto que ele recebeu;
- cita item, campeão ou runa que não existe no patch da partida.

O schema já impede os dois primeiros na construção, e o `FactValidator` pega o quarto. Este eixo
existe para medir quanto o modelo **tenta** violá-los — um modelo que produz três findings e tem dois
descartados pelo validador é pior que um que produz um só e passa.

### 2. Ancoragem (`anchor`) — 0 a 1

**O `timestamp_ms` aponta para a decisão, não para a consequência.**

O jogador clica no finding e o replay pula para 8 segundos antes dessa marca. Se a âncora estiver na
morte, ele vê a si mesmo morrendo — informação que ele já tem. Se estiver na decisão, ele vê o erro.

| Pontuação | Situação |
|---|---|
| 1.0 | dentro de ±10 s da âncora anotada no golden |
| 0.5 | dentro de ±45 s — mesma sequência de jogo, momento errado dentro dela |
| 0.0 | mais longe que isso, ou fora da duração da partida |

Fora da duração da partida também reprova o eixo 1: é um `BAD_ANCHOR`.

### 3. Correção (`match`) — 0 a 1

**O finding descreve o mesmo erro que o anotador humano descreveu?**

| Pontuação | Situação |
|---|---|
| 1.0 | mesma categoria e mesmo erro |
| 0.5 | categoria diferente, mesmo erro — ex.: anotado como `macro`, reportado como `positioning` |
| 0.0 | erro diferente, ou erro nenhum |

Categoria errada não é grave: as fronteiras entre `macro`, `positioning` e `decision` são
genuinamente borradas, e um jogador não liga para a etiqueta. Por isso vale meio ponto, não zero.

### 4. Utilidade (`actionability`) — 0 a 1

**O `fix` descreve uma ação alternativa concreta naquela situação.**

Este é o eixo mais difícil de automatizar e o mais importante para o produto. O teste operacional:
**se a `fix` serviria para qualquer partida de qualquer jogador, ela vale zero.**

| Pontuação | Situação |
|---|---|
| 1.0 | ação específica, com número ou referência concreta ("atravesse no recall antes dos 13:00, não depois") |
| 0.5 | direção certa, mas genérica ("tome mais cuidado com a visão perto do dragão") |
| 0.0 | truísmo ("melhore seu posicionamento", "não morra") |

O harness aproxima isto automaticamente procurando números, timestamps e nomes de zona na `fix`.
**A aproximação é grosseira e o `run.py` diz isso na saída.** Quando um anotador humano preenche
`actionability_manual` no golden, esse valor vence a heurística.

---

## Métricas agregadas

Por partida, e depois somadas:

| Métrica | O que mede | Por que importa |
|---|---|---|
| **precisão** | dos findings reportados, quantos casam com o golden | um coach que inventa erros perde a confiança na primeira vez |
| **cobertura** | dos erros anotados, quantos foram encontrados | um coach que não vê o erro que custou a partida não serve |
| **F1** | média harmônica das duas | o número único de comparação entre provedores |
| **ruído** | findings reportados sem correspondência, por partida | a queixa número um de ferramentas assim |
| **descarte** | fração eliminada pelo validador | mede quanto o modelo tenta alucinar |

**Precisão pesa mais que cobertura neste projeto.** Um finding errado custa mais credibilidade do que
dez findings certos constroem, e o relatório determinístico já garante um piso de cobertura. Por isso
o `run.py` também reporta F0.5, que pondera precisão ao dobro.

---

## A linha de base que todo provedor precisa bater

**O motor de regras determinístico (`--provider none`) é a linha de base.** Ele não usa modelo
nenhum, roda em milissegundos e é o que o usuário recebe se tudo falhar.

Um provedor que pontua **abaixo** da linha de base está ativamente piorando o produto e não deve ser
recomendado, por mais rápido ou barato que seja. Isto não é hipotético: modelos pequenos produzem
findings plausíveis e errados com muita facilidade, e "plausível e errado" é exatamente o que este
harness existe para pegar.

A matriz de compatibilidade publicada ordena por F1 e mostra a linha de base na mesma tabela,
sempre.

---

## Como anotar uma partida

Não precisa de Python. Crie `evals/golden/{match_id}.json`:

```json
{
  "match_id": "BR1_3239179616",
  "puuid": "...",
  "annotator": "seu-nick",
  "notes": "Garen top, derrota de 35 min. O jogo virou aos 18 min.",
  "findings": [
    {
      "category": "macro",
      "timestamp_ms": 1095000,
      "severity": 4,
      "summary": "Morreu na jungle de cima com o Arauto a 32s de nascer, sem visão no river.",
      "why_it_matters": "Perdeu o Arauto e a partida virou nessa janela.",
      "actionability_manual": null
    }
  ]
}
```

| Campo | O que pôr |
|---|---|
| `timestamp_ms` | **o momento da decisão**, não o da consequência |
| `severity` | 1 a 5. Reserve 5 para o que de fato custou a partida |
| `summary` | uma frase, do jeito que você explicaria para o jogador |
| `why_it_matters` | por que isso importou nesta partida específica |
| `actionability_manual` | deixe `null` para usar a heurística; preencha 0.0/0.5/1.0 se quiser julgar à mão |

### Anote o que importa, não tudo

**Três a seis findings por partida.** Uma anotação com vinte entradas transforma a métrica de
cobertura em ruído e pune modelos por não listarem coisas que nenhum jogador quereria ler.

Anote os erros que **você apontaria se estivesse revisando a partida com a pessoa**. Se você não
mencionaria, não anote.

### Anote o que a telemetria pode ver

A API da Riot não tem estado de wave, posição de ward, uso de habilidade nem cooldown. Um golden que
espera "deveria ter segurado o Flash para o gank" está pedindo ao sistema uma coisa que ele
arquiteturalmente não pode saber, e vai punir todo provedor igualmente — sem informar nada.

Se o erro só é visível no vídeo, ele pertence ao modo de visão (etapa 7), não a este corpus.
