# §3 — Interactive VOD Review Architecture

[🇧🇷 Português](../03-vod-review.md) · 🇺🇸 English

## 3.1 The two candidate designs

### Option A — Screen capture + realtime speech-to-speech

Continuously capture the screen while the user watches, stream frames + audio to a realtime
multimodal API, and have the AI talk over the replay like a coach sitting next to you.

### Option B — Pre-processed VOD timestamp mapping + timeline event linking

Analyse the match offline, produce a list of timestamped coachable moments, and let the user click
through them with the replay seeking to each one.

---

## 3.2 Evaluation

| Criterion | Option A | Option B |
|---|---|---|
| **Compliance (C1)** | **Fails.** Screen capture cannot reliably distinguish a replay from a live game, and the whole architecture is "watch the screen and give advice" — the exact shape Riot prohibits. Even if gated, it is one bug away from being a live-game coach. | **Passes structurally.** Nothing is captured; the app drives a replay via a documented API that only exists in replay mode. |
| **Cost (C2)** | **Fails.** Realtime multimodal sessions are the most expensive inference product that exists and there is no meaningful free tier. Local realtime speech-to-speech on a consumer GPU alongside a running game client is not viable. | **Passes.** One batch analysis, ~6.5k tokens, done once. |
| **Token economics** | 1 fps over 30 min = ~1,800 frames ≈ 1.4 M image tokens, plus audio. | ~16 sampled frames ≈ 13 k image tokens. **~100× cheaper.** |
| **Latency** | Must respond in <1 s while the game runs. On local hardware, a VLM first-token is 2–5 s. Unusable. | Zero — commentary is already written before the user clicks. |
| **Advice quality** | **Worse.** The model sees pixels only: it must guess gold, guess cooldowns, guess what happened 4 minutes ago. It has no ground truth and no time to reason. | **Better.** Every claim is backed by exact telemetry, ranked by severity across the whole game, with the benefit of hindsight — the model knows the fight at 24:00 was lost because of the ward that was not placed at 22:30. |
| **Reviewability** | Ephemeral speech. Cannot be re-read, shared, diffed, or evaluated. | A persistent, citable `CoachingReport`. Scoreable by an eval harness. Shareable with a friend or a real coach. |
| **Hardware floor** | GPU + headroom while a game renders. | Runs on a laptop with no GPU. |

Option A loses on every axis that matters, and it loses on the two hard constraints outright.

There is a real thing Option A has that B lacks: **the feeling of a coach next to you.** That is
worth capturing — and it can be had without the architecture, as shown below.

---

## 3.3 Final choice — Option B+, "synchronised replay review"

**Option B as the core, with three additions that recover A's interactivity at zero cost:**

1. **The app drives the replay, not the user.** Clicking a finding does not just show a timestamp —
   it `POST`s to the client's Replay API and the game seeks there, paused, with the camera on the
   relevant champion. This is the feature that makes it feel live.
2. **Scoped follow-up Q&A.** At any moment the user can ask "why was that bad?" — a *single* small
   LLM call with the ±30 s evidence slice as context. ~600 tokens, ~2 s on Groq. Interactive,
   but request/response, not streaming.
3. **Optional local TTS.** Piper (ONNX, CPU, ~50 MB, MIT-licensed) reads the finding aloud when it
   is seeked to. Free, offline, instant. Delivers the "coach talking to you" experience with none
   of the realtime cost.

The result is strictly better than Option A: it feels like a coach, it is cheaper, it is more
accurate, and it cannot get anyone banned.

---

## 3.4 The Replay API — mechanics

When the LoL client plays a `.rofl`, it exposes a local REST API on `https://127.0.0.1:2999`:

| Endpoint | Method | Payload / returns |
|---|---|---|
| `/replay/playback` | GET/POST | `{length, paused, seeking, speed, time}` — `time` is **seconds, float** |
| `/replay/render` | GET/POST | camera mode/position, FOV, fog of war, outlines, depth of field, HUD visibility |
| `/replay/sequence` | GET/POST | scripted keyframe camera sequences |
| `/liveclientdata/allgamedata` | GET | full scoreboard, items, scores, event list **at the current replay time** |
| `/liveclientdata/playerlist` | GET | per-player items/scores/runes |

TLS: the endpoint uses Riot's self-signed certificate. **Do not use `verify=False`** — ship Riot's
published `riotgames.pem` and pin it. It costs nothing and it prevents a local MITM from feeding
your app arbitrary data.

### Clock calibration (the part that will bite you)

Three different clocks are in play and they do not agree:

- **Timeline clock** — ms since game start; `PAUSE_END` marks the true start of play.
- **Replay playback clock** — seconds since the *replay file* begins, which includes the loading
  screen and the pre-minion period.
- **Video VOD clock** — seconds since the recording started, which is arbitrary.

Calibrate once per session by sampling both clocks from the client:

```python
async def calibrate(client) -> float:
    """Returns offset such that: replay_time_s = timeline_ms / 1000 + offset."""
    pb   = (await client.get(f"{BASE}/replay/playback")).json()
    live = (await client.get(f"{BASE}/liveclientdata/gamestats")).json()
    return pb["time"] - live["gameTime"]        # both read within the same ~50 ms
```

Re-sample after any seek and assert the offset is stable to within 0.5 s; drift means the user
scrubbed manually and the mapping must be rebuilt.

For **video VODs** there is no client to ask, so recover the clock optically: OCR the in-game timer
from its fixed HUD ROI on ~20 frames spread across the video, fit a line
(`game_seconds = a * video_seconds + b`, and `a` must come out ≈ 1.0 or the VOD is speed-edited),
then **verify** the fit against a known telemetry event — seek to the predicted first-blood
timestamp and confirm the kill banner is on screen. If verification fails, degrade to Mode C
unmatched rather than silently misaligning every finding by 40 seconds.

---

## 3.5 Data flow

```
 ┌──────────────┐                                        ┌─────────────────────────┐
 │  LoL Client  │                                        │  Riot Web API           │
 │  (replay     │                                        │  match-v5 + timeline    │
 │   playing)   │                                        └────────────┬────────────┘
 └──────┬───────┘                                                     │ (1) fetch, cache forever
        │  (4) POST /replay/playback {time, paused}                   │
        │      POST /replay/render  {camera}                          ▼
        │  ◄───────────────────────────────┐             ┌─────────────────────────┐
        │                                  │             │  Distillation  (§2)     │
        │  (3) GET /liveclientdata/*       │             │  -> MatchFacts IR       │
        │      GET /replay/playback        │             └────────────┬────────────┘
        │  ────────────────────────────────┤                          │ (2)
        ▼                                  │                          ▼
 ┌──────────────┐                  ┌───────┴──────────────────────────────────────┐
 │ rofl file    │──(0) metadata───►│        RiftCoach Backend  (FastAPI)          │
 │ statsJson    │   matchId lookup │                                              │
 └──────────────┘                  │  ReplayGuard ─ asserts replay mode, always   │
                                   │  MomentBuilder ─ ranks coachable moments     │
                                   │  VisionSampler ─ 3 frames/moment (Mode B/C)  │
                                   │  ModelRouter (§1) ─ 4 analysts + merge       │
                                   │  FactValidator (§4)                          │
                                   └───────┬──────────────────────────────────────┘
                                           │ (5) WebSocket: progress + findings
                                           ▼
                                   ┌──────────────────────────────────────────────┐
                                   │  React SPA (system browser or Tauri webview) │
                                   │  ┌────────────────────────────────────────┐  │
                                   │  │ gold-diff timeline w/ finding markers  │  │
                                   │  ├────────────────────────────────────────┤  │
                                   │  │ [!] 14:22  Died in enemy jungle with   │  │
                                   │  │     drake up and no vision   [▶ Seek]  │  │
                                   │  │     evidence: D2(T1), wave(T2) ...     │  │
                                   │  │     > ask a follow-up ________  (Groq) │  │
                                   │  └────────────────────────────────────────┘  │
                                   └──────────────────────────────────────────────┘
```

**Step 0 — `.rofl` metadata.** The file begins with the magic bytes `RIOT`, followed by a JSON
metadata block containing `gameLength` and `statsJson` (the full end-of-game stat line per player).
That is enough to recover the match ID and platform and join to Match-v5. The remainder of the file
is encrypted chunk/keyframe payload — **we never attempt to decode it** (see ARCHITECTURE §7).

```python
# riftcoach/replay/rofl.py
def read_metadata(path: Path) -> RoflMeta:
    with path.open("rb") as f:
        assert f.read(4) == b"RIOT"
        f.seek(262)                                  # header block
        meta_off, meta_len = struct.unpack("<II", f.read(8))
        f.seek(meta_off)
        meta = json.loads(f.read(meta_len).decode("utf-8"))
    return RoflMeta(
        game_length_ms=meta["gameLength"],
        stats=json.loads(meta["statsJson"]),         # statsJson is a JSON *string*
    )
```

Header offsets have shifted across major client versions — parse defensively, and on failure fall
back to the filename (clients write `<PLATFORM>-<GAMEID>.rofl`, which is all we strictly need).

---

## 3.6 `MomentBuilder` — choosing what is worth watching

Findings come from the LLM, but **moments are chosen deterministically in Python before any model
runs.** This keeps the expensive vision sampling targeted and keeps the moment list stable across
model swaps.

```python
@dataclass
class CoachableMoment:
    t_ms: int
    kind: Literal["death","objective_fight","recall_error","wave_crash",
                  "power_spike_idle","vision_gap","roam_window"]
    priority: float          # gold/tempo swing magnitude — drives sampling budget
    focus_entity: str        # champion to centre the camera on
    window: tuple[int,int]   # (t-8s, t+4s) evidence slice
```

Selection rules, in priority order:

1. **Every death** (highest priority when `gold_swing` is large or `objective_window` is set).
2. **Objective fights** — any `ELITE_MONSTER_KILL` with a champion kill within ±25 s.
3. **Recall errors** — recalled with < 350 gold, or recalled while `wave_proxy == PUSHING_TO_ENEMY`,
   or spent > 20 s walking back.
4. **Power-spike idling** — held > 1,600 unspent gold for > 90 s while alive.
5. **Vision gaps** — a 60 s window before an objective with zero own wards near the pit.
6. **Roam windows** — lane opponent left lane and the focus player did not react within 20 s.

Cap at 20 moments, sorted by priority. More than 20 and the user stops watching; the report still
lists lower-priority findings in text.

---

## 3.7 Seeking

```python
# riftcoach/replay/controller.py
class ReplayController:
    async def seek_to(self, moment: CoachableMoment, lead_in_s: float = 8.0) -> None:
        await self.guard.assert_replay_mode(self.client)          # EVERY time (§6)
        target = moment.t_ms / 1000 - lead_in_s + self.offset
        await self.client.post(f"{BASE}/replay/playback",
                               json={"time": max(0.0, target), "paused": False, "speed": 1.0})
        await self.client.post(f"{BASE}/replay/render",
                               json={"cameraMode": "fps", "cameraLockMode": "on"})
```

The 8-second lead-in is deliberate: seeking to the exact moment of death shows the consequence, not
the cause. The mistake almost always happened 5–10 seconds earlier — the pathing decision, the
missing ward, the wave shove. Coaching UX rule: **always seek to the decision, never to the
outcome.**

---

## 3.8 Mode C — video VODs without a running client

Same UI, different transport: the `<video>` element replaces the game client, `currentTime`
replaces the Replay API, and the optical calibration from §3.4 supplies the offset. The finding
list, the seek behaviour and the follow-up Q&A are identical — the `ReplaySink` interface is
implemented twice:

```python
class ReplaySink(Protocol):
    async def seek(self, t_ms: int) -> None: ...
    async def pause(self) -> None: ...
    async def capture(self, t_ms: int) -> Image | None: ...

class ClientReplaySink(ReplaySink):  ...   # 127.0.0.1:2999
class VideoFileSink(ReplaySink):     ...   # PyAV, accurate seek
class BrowserVideoSink(ReplaySink):  ...   # WebSocket -> <video>.currentTime
```

Frame extraction for Mode C uses PyAV rather than shelling out to `ffmpeg` per frame — one open
container, seek-and-decode per moment, ~40 ms each versus ~600 ms of process startup. Use
`container.seek(offset, backward=True)` to land on the preceding keyframe, then decode forward to
the exact PTS; seeking to a non-keyframe directly returns garbage frames.

`yt-dlp` support for YouTube VODs is **opt-in, user-supplied URL only**, and documented as the
user's responsibility with respect to the source platform's terms. It is not a default code path.
