# Posição de Conformidade com a Riot Games

🇧🇷 Português · [🇺🇸 English](COMPLIANCE.en.md)

Este documento existe porque *"isso vai me dar ban?"* é a primeira pergunta que todo usuário em
potencial faz, e porque uma resposta vaga não vale nada. Aqui está a resposta específica.

## A regra da qual estamos ficando do lado certo

A Riot proíbe software de terceiros que automatize a jogabilidade ou revele informação que o jogador
não poderia obter de outra forma durante uma partida. Ela publica e permite a Riot Web API, a Live
Client Data API e a Replay API para ferramentas de terceiros legítimas.

O RiftCoach não automatiza nada e não roda durante partidas ao vivo, ponto.

## O que o RiftCoach acessa

| Superfície | Quando | Por que está tudo certo |
|---|---|---|
| `match-v5/matches/*` e `/timeline` | Depois que a partida acabou | API pública oficial, em partidas concluídas, com a chave do próprio usuário. |
| DataDragon / CommunityDragon | A qualquer momento | CDNs públicas de assets estáticos. |
| `127.0.0.1:2999/replay/*` | **Somente durante a reprodução de um replay** | Replay API documentada; as rotas não existem fora do modo replay. |
| `127.0.0.1:2999/liveclientdata/*` | **Somente durante a reprodução de um replay**, atrás da mesma verificação | Lendo o estado de um replay, não o de uma partida ao vivo. |
| Arquivos `.rofl` | Em disco, depois da partida | Apenas metadados. |

## O que o RiftCoach nunca faz

- Rodar, inferir, alertar ou exibir qualquer coisa durante uma partida ao vivo
- Sobrepor qualquer coisa a uma partida ao vivo
- Simular input, automatizar ações ou interagir com o processo do jogo
- Ler memória do jogo ou injetar código
- Revelar informação indisponível ao jogador (nada de dados sob fog of war, nada de cooldowns
  inimigos a partir de estado oculto, nada de tracking de jungle durante partida ao vivo)
- Fazer scraping de sites terceiros (op.gg, u.gg, porofessor) ou violar os termos deles
- Transmitir dados do usuário para qualquer lugar que o usuário não tenha configurado explicitamente

## O overlay sobre o replay

O RiftCoach desenha marcações por cima da janela do jogo **enquanto um replay está sendo
reproduzido**. Como essa é a pergunta mais direta que alguém pode fazer sobre compatibilidade com as
regras, aqui está a resposta inteira.

**A distinção é entre partida ao vivo e gravação, e ela não é retórica — é estrutural.**

| | Partida ao vivo | Replay |
|---|---|---|
| A partida já terminou? | Não | Sim, e o resultado já está no histórico |
| Dá vantagem competitiva? | Daria | Não há partida acontecendo para vencer |
| O que a Riot oferece para isso? | Nada | A Replay API, documentada, com controle de câmera e de tempo |
| O RiftCoach consegue chegar lá? | **Não** — `ReplayGuard` recusa | Sim |

O overlay é um processo comum do Windows desenhando numa janela transparente sua. Ele **não toca no
processo do jogo**: não injeta código, não lê memória, não desenha dentro do motor gráfico, não
envia input. Ele lê o relógio do replay pela Replay API oficial — a mesma que já era usada para o
botão de pular — e desenha ao lado.

Um treinador de futebol pausando o vídeo de domingo e desenhando por cima é a analogia exata, e a
Riot publica a Replay API justamente para esse uso.

**O intertravamento continua sendo o mesmo, e continua sendo o único caminho.** O overlay não abre
socket, não conhece a porta 2999 e não monta URL: ele pede ao `ReplayController`, que pede ao
`ReplayGuard`, que confere `GET /replay/playback` **antes de cada requisição**. Numa partida ao vivo
essa rota devolve 404 e o overlay nunca chega a existir. Nada foi afrouxado para essa funcionalidade
entrar.

Sobre os atalhos de marcação (`Ctrl+Alt+E` e companhia): o RiftCoach **lê** o estado do teclado pelo
sistema operacional, somente enquanto a janela do League está em primeiro plano, e **nunca envia**
tecla ou clique para lugar nenhum. É a mesma coisa que um gravador de tela com atalho global.

## O mecanismo de aplicação

Isso não é uma declaração de política — é aplicado em código, em um único lugar.

`riftcoach/replay/guard.py` é o **único** módulo autorizado a abrir uma conexão com
`127.0.0.1:2999`. Antes de cada requisição, ele dispara:

```
GET https://127.0.0.1:2999/replay/playback
```

Essa rota existe **somente** enquanto um replay está rodando. Durante uma partida ao vivo ela
retorna 404 enquanto `/liveclientdata/*` continua respondendo — ou seja, um 404 aqui é um sinal
positivo de que uma partida ao vivo pode estar em andamento, e nós recusamos incondicionalmente.

Três propriedades tornam isso confiável:

1. **Falha fechado.** Erro de conexão, 404, timeout, corpo inesperado — todos levantam
   `LiveGameRefused`. Não existe caminho de código em que um resultado ambíguo siga adiante.
2. **Revalida antes de cada requisição, não uma vez por sessão.** O usuário pode sair do replay via
   alt-tab e cair na seleção de campeões no meio da análise. Um job de cinco minutos precisa
   perceber isso.
3. **É garantido pela arquitetura, não pela disciplina.** A verificação está na camada de
   transporte. Um contribuidor não consegue burlar acidentalmente escrevendo uma funcionalidade
   nova, porque não existe outro cliente.

Uma regra de import-linter no CI quebra o build se qualquer módulo fora de `riftcoach/replay/`
importar `httpx` e referenciar a porta 2999.

## TLS

A Live Client Data API usa o certificado autoassinado da Riot. Nós distribuímos o `riotgames.pem`
publicado pela Riot e fazemos pinning contra ele, em vez de usar `verify=False`. Não custa nada e
significa que um MITM local não consegue alimentar o app com estado de jogo fabricado.

## Chaves de API e tratamento de dados

- Os usuários fornecem a própria chave da API da Riot. Não existe servidor, proxy ou telemetria
  operados pelo RiftCoach.
- As chaves são guardadas no cofre de credenciais do sistema operacional (Gerenciador de Credenciais
  do Windows / Keychain do macOS / Secret Service), nunca em um arquivo no repositório.
- Os dados das partidas ficam em cache local na máquina do usuário e nunca são transmitidos a lugar
  algum, exceto para um provedor de LLM que o usuário configurou explicitamente. `privacy_mode:
  strict` desabilita isso por completo e é aplicado no nível do roteador, antes da seleção de
  provedor.
- O dataset de benchmarks publicado pelo projeto contém apenas percentis agregados. Nenhum PUUID,
  nenhuma linha por jogador, nenhum dado identificável.

## Política para contribuidores

PRs que introduzam funcionalidade durante partida ao vivo são fechados sem revisão. Isso inclui
overlays ao vivo, alertas ao vivo, HUDs ao vivo "somente leitura" e qualquer coisa que leia estado do
client fora do modo replay — não importa como seja apresentado. Essa restrição é o que torna o
projeto seguro de recomendar, e ela não é negociável por nenhuma funcionalidade.

O overlay de replay não é exceção a essa regra: ele passa pelo mesmo `ReplayGuard`, e é justamente
por isso que ele pôde existir. Um PR que desenhe na tela sem passar por ali é a mesma coisa que um
PR de partida ao vivo, ainda que hoje só rode em replay.

## Aviso legal

RiftCoach AI não é endossado pela Riot Games e não reflete as visões ou opiniões da Riot Games ou de
qualquer pessoa oficialmente envolvida na produção ou gestão das propriedades da Riot Games. League
of Legends e Riot Games são marcas registradas ou marcas comerciais da Riot Games, Inc.

Se a Riot Games solicitar uma mudança neste projeto, abra uma issue e nós vamos atender.
