# Partidas anotadas

**Anotar uma partida é uma das contribuições mais valiosas deste projeto, e não precisa de Python.**

Cada arquivo aqui é o gabarito de uma partida: os erros que um revisor humano apontaria. É contra
eles que `evals/run.py` pontua todo provedor de IA — e é o que torna aplicável a regra de que **PRs
de prompt são revisados pelo delta nas avaliações, não por opinião**.

O formato e os critérios estão em [`../rubric.md`](../rubric.md).

## O estado atual, sem rodeios

| | |
|---|---|
| Perspectivas anotadas | 2 |
| Anotadores humanos | 0 |
| Partidas distintas | 1 |

As duas anotações existentes têm `"annotator": "seed"`. **Elas não foram feitas por um revisor de elo
alto.** Foram derivadas do que a telemetria mostra sem ambiguidade — mortes em janela de objetivo,
ouro parado na hora da morte, objetivos perdidos sem visão prévia — e servem para o harness rodar,
não para julgar a qualidade de um coach.

Isso tem uma consequência que aparece direto na saída do `run.py`: com poucas anotações, **todo
finding correto que o anotador não escreveu conta como ruído**. A precisão medida hoje é um piso do
corpus, não uma medida da ferramenta.

Substituir as sementes por anotações de gente que joga é o passo que falta.

## Como contribuir

1. Escolha uma partida. Se for sua, `riftcoach fetch "Nome#TAG" -n 5` põe ela no cache; se for de uma
   fixture, ela já está em `tests/fixtures/`.
2. Assista ao replay e anote os erros que **você apontaria revisando com a pessoa**. Três a seis por
   partida — ver a rubrica sobre por que mais que isso piora a métrica.
3. Crie `{match_id}-{campeao}.json` seguindo o formato da rubrica.
4. Rode `uv run python evals/run.py --detail` e veja onde o sistema concorda e discorda de você.
5. Abra o PR. **Discordância é informação**, não erro: se o motor acha algo que você não anotou e
   você acha que ele está certo, isso é um relato de que a sua anotação ficou incompleta; se está
   errado, é um bug diagnosticável.

### Anote o que a telemetria pode ver

A API da Riot não expõe estado de wave, posição de ward, uso de habilidade nem cooldown. Um gabarito
que espera "deveria ter segurado o Flash" pune todos os provedores igualmente e não informa nada.
Erros que só aparecem no vídeo pertencem ao modo de visão (etapa 7).

### Sobre privacidade

Os arquivos aqui carregam um **prefixo** de PUUID, não o valor completo — o suficiente para achar o
jogador dentro da partida, e não o suficiente para identificá-lo fora dela. Mantenha assim.
