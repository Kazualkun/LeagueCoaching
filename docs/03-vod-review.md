# §3 — Arquitetura de Revisão Interativa de VOD

🇧🇷 Português · [🇺🇸 English](en/03-vod-review.md)

## 3.1 Os dois projetos candidatos

### Opção A — Captura de tela + fala-para-fala em tempo real

Capturar continuamente a tela enquanto o usuário assiste, transmitir frames + áudio para uma API
multimodal em tempo real, e deixar a IA falar por cima do replay como um coach sentado do lado.

### Opção B — Mapeamento de timestamps pré-processados + vínculo com eventos da timeline

Analisar a partida offline, produzir uma lista de momentos treináveis com timestamp, e deixar o
usuário navegar por eles com o replay pulando para cada um.

---

## 3.2 Avaliação

| Critério | Opção A | Opção B |
|---|---|---|
| **Conformidade (C1)** | **Reprova.** Captura de tela não consegue distinguir com segurança um replay de uma partida ao vivo, e a arquitetura inteira é "olhe a tela e dê conselhos" — exatamente o formato que a Riot proíbe. Mesmo com trava, fica a um bug de distância de virar um coach ao vivo. | **Aprova estruturalmente.** Nada é capturado; o app controla um replay por uma API documentada que só existe no modo replay. |
| **Custo (C2)** | **Reprova.** Sessões multimodais em tempo real são o produto de inferência mais caro que existe e não há tier gratuito relevante. Fala-para-fala local em tempo real numa GPU de consumidor, junto com um client de jogo rodando, não é viável. | **Aprova.** Uma análise em lote, ~6,5k tokens, feita uma vez. |
| **Economia de tokens** | 1 fps por 30 min = ~1.800 frames ≈ 1,4 M tokens de imagem, mais áudio. | ~16 frames amostrados ≈ 13 mil tokens de imagem. **~100× mais barato.** |
| **Latência** | Precisa responder em <1 s enquanto o jogo roda. Em hardware local, o primeiro token de um VLM leva 2–5 s. Inutilizável. | Zero — o comentário já está escrito antes de o usuário clicar. |
| **Qualidade do conselho** | **Pior.** O modelo só vê pixels: precisa adivinhar ouro, adivinhar cooldowns, adivinhar o que aconteceu 4 minutos atrás. Não tem verdade absoluta nem tempo para raciocinar. | **Melhor.** Toda afirmação é lastreada em telemetria exata, ranqueada por gravidade ao longo da partida inteira, com o benefício da retrospectiva — o modelo sabe que a luta aos 24:00 foi perdida por causa da ward que não foi colocada aos 22:30. |
| **Revisibilidade** | Fala efêmera. Não dá para reler, compartilhar, comparar ou avaliar. | Um `CoachingReport` persistente e citável. Pontuável por um harness de avaliação. Compartilhável com um amigo ou um coach de verdade. |
| **Piso de hardware** | GPU com folga enquanto um jogo renderiza. | Roda num notebook sem GPU. |

A Opção A perde em todos os eixos que importam, e perde de forma categórica nas duas restrições
rígidas.

Há uma coisa real que a Opção A tem e a B não: **a sensação de um coach do seu lado.** Isso vale a
pena capturar — e dá para ter sem a arquitetura, como mostrado abaixo.

---

## 3.3 Escolha final — Opção B+, "revisão sincronizada de replay"

**Opção B como núcleo, com três acréscimos que recuperam a interatividade da A a custo zero:**

1. **O app controla o replay, não o usuário.** Clicar em um finding não apenas mostra um timestamp —
   ele faz um `POST` para a Replay API do client e o jogo pula para lá, pausado, com a câmera no
   campeão relevante. É essa funcionalidade que dá a sensação de "ao vivo".
2. **Perguntas de follow-up com escopo.** A qualquer momento o usuário pode perguntar "por que isso
   foi ruim?" — uma *única* chamada pequena ao LLM com a fatia de evidência de ±30 s como contexto.
   ~600 tokens, ~2 s no Groq. Interativo, mas requisição/resposta, não streaming.
3. **TTS local opcional.** O Piper (ONNX, CPU, ~50 MB, licença MIT) lê o finding em voz alta quando o
   replay pula para ele. Grátis, offline, instantâneo. Entrega a experiência de "coach falando com
   você" sem nada do custo de tempo real.

O resultado é estritamente melhor que a Opção A: parece um coach, é mais barato, é mais preciso, e
não pode banir ninguém.

---

## 3.4 A Replay API — mecânica

Quando o client do LoL reproduz um `.rofl`, ele expõe uma REST API local em `https://127.0.0.1:2999`:

| Endpoint | Método | Payload / retorno |
|---|---|---|
| `/replay/playback` | GET/POST | `{length, paused, seeking, speed, time}` — `time` está em **segundos, float** |
| `/replay/render` | GET/POST | modo/posição de câmera, FOV, fog of war, contornos, profundidade de campo, visibilidade da HUD |
| `/replay/sequence` | GET/POST | sequências de câmera com keyframes roteirizados |
| `/liveclientdata/allgamedata` | GET | placar completo, itens, scores, lista de eventos **no tempo atual do replay** |
| `/liveclientdata/playerlist` | GET | itens/scores/runas por jogador |

TLS: o endpoint usa o certificado autoassinado da Riot. **Não use `verify=False`** — distribua o
`riotgames.pem` publicado pela Riot e faça pinning. Não custa nada e impede que um MITM local
alimente seu app com dados arbitrários.

### Calibração de relógio (a parte que vai te morder)

Três relógios diferentes estão em jogo e eles não concordam entre si:

- **Relógio da timeline** — ms desde o início da partida; `PAUSE_END` marca o começo real do jogo.
- **Relógio de reprodução do replay** — segundos desde o início do *arquivo* de replay, o que inclui
  a tela de carregamento e o período antes dos minions.
- **Relógio do VOD em vídeo** — segundos desde o início da gravação, que é arbitrário.

Calibre uma vez por sessão amostrando os dois relógios do client:

```python
async def calibrate(client) -> float:
    """Retorna o offset tal que: replay_time_s = timeline_ms / 1000 + offset."""
    pb   = (await client.get(f"{BASE}/replay/playback")).json()
    live = (await client.get(f"{BASE}/liveclientdata/gamestats")).json()
    return pb["time"] - live["gameTime"]        # ambos lidos na mesma janela de ~50 ms
```

Reamostre depois de cada seek e verifique se o offset está estável dentro de 0,5 s; deriva significa
que o usuário navegou manualmente e o mapeamento precisa ser refeito.

Para **VODs em vídeo** não há client para consultar, então recupere o relógio opticamente: faça OCR
do timer do jogo na ROI fixa da HUD em ~20 frames espalhados pelo vídeo, ajuste uma reta
(`segundos_de_jogo = a * segundos_de_video + b`, e `a` precisa dar ≈ 1,0 ou o VOD foi editado em
velocidade diferente), e então **verifique** o ajuste contra um evento conhecido da telemetria — pule
para o timestamp previsto do first blood e confirme que o banner de abate está na tela. Se a
verificação falhar, degrade para Modo C sem correspondência em vez de desalinhar silenciosamente
todos os findings em 40 segundos.

---

## 3.5 Fluxo de dados

```
 ┌──────────────┐                                        ┌─────────────────────────┐
 │ Client LoL   │                                        │  Riot Web API           │
 │ (replay      │                                        │  match-v5 + timeline    │
 │  rodando)    │                                        └────────────┬────────────┘
 └──────┬───────┘                                                     │ (1) busca, cache eterno
        │  (4) POST /replay/playback {time, paused}                   │
        │      POST /replay/render  {camera}                          ▼
        │  ◄───────────────────────────────┐             ┌─────────────────────────┐
        │                                  │             │  Destilação  (§2)       │
        │  (3) GET /liveclientdata/*       │             │  -> MatchFacts IR       │
        │      GET /replay/playback        │             └────────────┬────────────┘
        │  ────────────────────────────────┤                          │ (2)
        ▼                                  │                          ▼
 ┌──────────────┐                  ┌───────┴──────────────────────────────────────┐
 │ arquivo rofl │──(0) metadados──►│        Backend RiftCoach  (FastAPI)          │
 │ statsJson    │   busca matchId  │                                              │
 └──────────────┘                  │  ReplayGuard ─ valida modo replay, sempre    │
                                   │  MomentBuilder ─ ranqueia momentos treináveis│
                                   │  VisionSampler ─ 3 frames/momento (Modo B/C) │
                                   │  ModelRouter (§1) ─ 4 analistas + merge      │
                                   │  FactValidator (§4)                          │
                                   └───────┬──────────────────────────────────────┘
                                           │ (5) WebSocket: progresso + findings
                                           ▼
                                   ┌──────────────────────────────────────────────┐
                                   │  SPA React (navegador do sistema ou Tauri)   │
                                   │  ┌────────────────────────────────────────┐  │
                                   │  │ linha do tempo de ouro c/ marcadores   │  │
                                   │  ├────────────────────────────────────────┤  │
                                   │  │ [!] 14:22  Morreu na jungle inimiga    │  │
                                   │  │     com dragão nascendo e sem visão    │  │
                                   │  │                             [▶ Pular]  │  │
                                   │  │     evidência: D2(T1), wave(T2) ...    │  │
                                   │  │     > pergunte algo ________  (Groq)   │  │
                                   │  └────────────────────────────────────────┘  │
                                   └──────────────────────────────────────────────┘
```

**Passo 0 — metadados do `.rofl`.** O arquivo começa com os bytes mágicos `RIOT`, seguidos por um
bloco de metadados JSON contendo `gameLength` e `statsJson` (a linha completa de estatísticas de fim
de jogo por jogador). Isso basta para recuperar o match ID e a plataforma e cruzar com o Match-v5. O
restante do arquivo é conteúdo criptografado de chunks/keyframes — **nunca tentamos decodificá-lo**
(ver ARCHITECTURE §7).

```python
# riftcoach/replay/rofl.py
def read_metadata(path: Path) -> RoflMeta:
    with path.open("rb") as f:
        assert f.read(4) == b"RIOT"
        f.seek(262)                                  # bloco de cabeçalho
        meta_off, meta_len = struct.unpack("<II", f.read(8))
        f.seek(meta_off)
        meta = json.loads(f.read(meta_len).decode("utf-8"))
    return RoflMeta(
        game_length_ms=meta["gameLength"],
        stats=json.loads(meta["statsJson"]),         # statsJson é uma *string* JSON
    )
```

Os offsets do cabeçalho mudaram entre versões maiores do client — faça o parsing defensivamente e,
em caso de falha, use o nome do arquivo (os clients gravam `<PLATAFORMA>-<GAMEID>.rofl`, que é tudo
de que realmente precisamos).

---

## 3.6 `MomentBuilder` — escolhendo o que vale a pena assistir

Os findings vêm do LLM, mas **os momentos são escolhidos deterministicamente em Python antes de
qualquer modelo rodar.** Isso mantém a amostragem cara de visão bem direcionada e mantém a lista de
momentos estável entre trocas de modelo.

```python
@dataclass
class CoachableMoment:
    t_ms: int
    kind: Literal["death","objective_fight","recall_error","wave_crash",
                  "power_spike_idle","vision_gap","roam_window"]
    priority: float          # magnitude da oscilação de ouro/tempo — define o orçamento de amostragem
    focus_entity: str        # campeão em quem centralizar a câmera
    window: tuple[int,int]   # (t-8s, t+4s) fatia de evidência
```

Regras de seleção, em ordem de prioridade:

1. **Toda morte** (prioridade máxima quando `gold_swing` é grande ou `objective_window` está
   preenchido).
2. **Lutas de objetivo** — qualquer `ELITE_MONSTER_KILL` com um abate de campeão em ±25 s.
3. **Erros de recall** — voltou com menos de 350 de ouro, ou voltou com `wave_proxy ==
   PUSHING_TO_ENEMY`, ou gastou mais de 20 s caminhando de volta.
4. **Ociosidade em power spike** — segurou mais de 1.600 de ouro não gasto por mais de 90 s enquanto
   vivo.
5. **Buracos de visão** — uma janela de 60 s antes de um objetivo sem nenhuma ward própria perto do
   pit.
6. **Janelas de roam** — o oponente de rota saiu da rota e o jogador em foco não reagiu em 20 s.

Limite em 20 momentos, ordenados por prioridade. Acima de 20 o usuário para de assistir; o relatório
ainda lista os findings de prioridade menor em texto.

---

## 3.7 Navegação no replay

```python
# riftcoach/replay/controller.py
class ReplayController:
    async def seek_to(self, moment: CoachableMoment, lead_in_s: float = 8.0) -> None:
        await self.guard.assert_replay_mode(self.client)          # SEMPRE (§6)
        target = moment.t_ms / 1000 - lead_in_s + self.offset
        await self.client.post(f"{BASE}/replay/playback",
                               json={"time": max(0.0, target), "paused": False, "speed": 1.0})
        await self.client.post(f"{BASE}/replay/render",
                               json={"cameraMode": "fps", "cameraLockMode": "on"})
```

Os 8 segundos de antecedência são deliberados: pular para o momento exato da morte mostra a
consequência, não a causa. O erro quase sempre aconteceu 5–10 segundos antes — a decisão de pathing,
a ward que faltou, a wave empurrada. Regra de UX de coaching: **sempre pule para a decisão, nunca
para o desfecho.**

---

## 3.8 Modo C — VODs em vídeo sem um client rodando

Mesma interface, transporte diferente: o elemento `<video>` substitui o client do jogo,
`currentTime` substitui a Replay API, e a calibração óptica da §3.4 fornece o offset. A lista de
findings, o comportamento de navegação e as perguntas de follow-up são idênticos — a interface
`ReplaySink` é implementada duas vezes:

```python
class ReplaySink(Protocol):
    async def seek(self, t_ms: int) -> None: ...
    async def pause(self) -> None: ...
    async def capture(self, t_ms: int) -> Image | None: ...

class ClientReplaySink(ReplaySink):  ...   # 127.0.0.1:2999
class VideoFileSink(ReplaySink):     ...   # PyAV, seek preciso
class BrowserVideoSink(ReplaySink):  ...   # WebSocket -> <video>.currentTime
```

A extração de frames no Modo C usa PyAV em vez de chamar `ffmpeg` por frame — um container aberto,
seek-e-decodifica por momento, ~40 ms cada contra ~600 ms só de inicialização de processo. Use
`container.seek(offset, backward=True)` para cair no keyframe anterior e então decodifique para
frente até o PTS exato; pular direto para um frame que não é keyframe devolve lixo.

O suporte a `yt-dlp` para VODs do YouTube é **opt-in, apenas com URL fornecida pelo usuário**, e
documentado como responsabilidade do usuário quanto aos termos da plataforma de origem. Não é um
caminho de código padrão.
