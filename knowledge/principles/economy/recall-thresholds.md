---
id: economy-recall-thresholds
applies_to: {roles: [TOP, JUNGLE, MIDDLE, BOTTOM, UTILITY], phases: [early, mid]}
triggers: [recall_error, gold_hoarding, itemization_gap, wave_proxy=PUSHING_TO_ENEMY]
tier: T1_PRINCIPLE
---

# Limiares de recall — componentes, não itens completos

O erro de economia mais comum e mais caro do jogo é esperar o item completo.

## Ouro no bolso não faz nada

Um jogador com 1450 de ouro guardado tem exatamente a mesma força de um jogador com 0. A diferença só
existe depois da compra. Enquanto o ouro está parado, ele não te dá dano, nem vida, nem velocidade —
e se você morrer carregando, parte dele vira bounty para o inimigo.

Por isso o limiar de recall **não é o preço do item completo**. É o preço do próximo componente que
muda alguma coisa na sua rota.

## Como escolher o limiar

Pergunte, nesta ordem:

1. **Qual é o próximo componente que muda a minha troca?** Não o item final — o pedaço. Costuma ser
   um componente de dano, um de sustain, ou as botas.
2. **O que eu perco ficando para completar?** Conte concretamente: minions, XP, e o risco de estar na
   rota carregando ouro.
3. **A wave me deixa voltar de graça agora?** Se você acabou de crashar uma wave grande na torre
   inimiga, o recall custa quase nada. Se a wave está no meio da rota, ele custa muito.

Quando (1) já está pago e (3) diz que a janela é agora, volte. Não espere mais 300 de ouro.

## O recall bom quase sempre é um recall de wave, não de ouro

Esta é a inversão que faz a maior diferença na prática: **o momento do recall é decidido pela wave, e
o que você compra é decidido pelo ouro** — não o contrário.

Um recall com a wave crashada na torre inimiga te devolve à rota sem perder nada. O mesmo recall
feito noventa segundos antes custa uma wave inteira, e nenhuma compra compensa isso no early.

## Sentinela de Controle

Comprar uma sentinela de controle quase nunca é errado. Ela é barata, ocupa espaço no inventário mas
não na economia, e é a compra com melhor retorno por ouro do jogo inteiro para todas as rotas — não
só para o suporte.

Um padrão que aparece muito nos relatórios: nove recalls, duas sentinelas compradas. Isso não é um
erro de visão, é um erro de checklist. A sentinela entra em **todo** recall em que sobrar ouro.

## Quando esperar está certo

- **Falta muito pouco para um pico real.** Se faltam 150 de ouro para um item que muda o seu padrão
  de luta e a rota está segura, uma wave a mais pode valer.
- **Você acabou de morrer.** O ouro de respawn já foi pago em tempo — aproveite e complete.
- **Um objetivo nasce em breve.** Voltar tarde demais para chegar é pior que voltar com menos ouro.
  Neste caso, o timer manda; ver [`macro-objective-setup`](../macro/objective-setup.md).
- **Você não tem como voltar à rota.** Se a wave vai crashar na sua torre de qualquer jeito e você
  perderia tudo, não volte — segure e resolva primeiro.
