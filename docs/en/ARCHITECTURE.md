# RiftCoach AI — Architecture Blueprint

[🇧🇷 Português](../ARCHITECTURE.md) · 🇺🇸 English

> Post-game League of Legends coaching. 100% free, 100% Riot-compliant, self-hosted.
> Status: design blueprint v1.0 · Target patch baseline: 15.x/16.x · Last revised: 2026-09-21

---

## 0. The one-paragraph thesis

Almost every "AI LoL coach" fails for the same reason: it shoves a 2 MB `match-v5/timeline` JSON
(or a stream of raw screenshots) at a general-purpose LLM and asks it to "coach." The model is then
simultaneously doing **perception**, **arithmetic**, **fact recall**, and **judgement** — and it is
bad at the first three. RiftCoach inverts this. Python does perception and arithmetic
deterministically, a versioned patch database supplies facts, and the LLM is used **only for
judgement over a pre-digested, cited, ~4k-token evidence packet.** That single inversion is what
makes an 8B local model produce Diamond-tier advice instead of confident nonsense, and it is what
lets the whole thing run on a free tier.

---

## 1. Non-negotiable constraints

| # | Constraint | Architectural consequence |
|---|---|---|
| C1 | **Zero real-time assistance.** No overlay, alert, or inference during a live game. | Port 2999 is touched **only** when `GET /replay/playback` returns 200 (replay mode). A live game 404s that route → hard abort. See `ReplayGuard`, §6. |
| C2 | **Zero mandatory cost.** | Every path has a local (Ollama) and a free-cloud implementation. No feature may exist only behind a paid key. |
| C3 | **Bring-your-own-key.** | No RiftCoach-operated server, no proxy, no telemetry. Keys live in the OS keyring, never in the repo. |
| C4 | **Never state a stale fact.** | Numeric item/champion facts are injected from a patch-pinned DB; a post-generation validator rejects any entity the DB does not know. §4. |
| C5 | **Every claim is anchored.** | Every `Finding` carries `timestamp_ms` + `evidence[]` + `evidence_tier`. Unanchored prose is a bug. |

---

## 2. System decomposition

```
                          +-------------------------------------------+
                          |             INGEST LAYER                  |
                          |  riot/match-v5 · riot/timeline · .rofl    |
                          |  meta · video file · YouTube URL          |
                          +---------------------+---------------------+
                                                | raw, immutable, cached forever (SQLite+zstd)
                          +---------------------v---------------------+
                          |          DISTILLATION LAYER  (§2)         |
                          |  perspective filter -> zone mapping ->    |
                          |  phase rollup -> derived metrics ->       |
                          |  MatchFacts IR   (~500k tok -> ~4k tok)   |
                          +---------------------+---------------------+
                                                |
              +---------------------------------+---------------------------------+
              |                                 |                                 |
   +----------v----------+       +--------------v--------------+     +------------v-----------+
   |  KNOWLEDGE LAYER §4 |       |  PERCEPTION LAYER  §3       |     |   BENCHMARK LAYER §4   |
   | patch DB (DDragon   |       |  VLM/OCR on *sampled*       |     | role/rank percentiles  |
   | diff) + principles  |       |  frames, only where         |     | (bundled parquet +     |
   | RAG corpus          |       |  telemetry is blind         |     |  user's own history)   |
   +----------+----------+       +--------------+--------------+     +------------+-----------+
              +---------------------------------+---------------------------------+
                                                |  EvidencePacket (typed, cited)
                          +---------------------v---------------------+
                          |        REASONING LAYER  (§1)              |
                          |  Mixture-of-Analysts: laning · macro ·    |
                          |  economy · fights  ->  head_coach merge   |
                          |  via ModelRouter (Ollama <-> free cloud)  |
                          +---------------------+---------------------+
                                                | List[Finding] (JSON schema-validated)
                          +---------------------v---------------------+
                          |     PRESENTATION LAYER  (§3, §5)          |
                          |  React SPA · moment timeline ·            |
                          |  click -> Replay API seek / <video> seek  |
                          +-------------------------------------------+
```

---

## 3. Decision register (the short version)

| Question | Decision | One-line reason |
|---|---|---|
| Text reasoning model | **Qwen3-14B / 30B-A3B local · Gemini 2.5 Flash cloud** | Best instruction-following-per-VRAM; Flash's free tier is the only one with both huge RPD and native video. |
| Vision model | **Qwen2.5-VL-7B local · Gemini Flash cloud** | Qwen2.5-VL is the only open VLM with reliable small-text HUD OCR *and* spatial grounding. |
| Vision's job | **Perception only, never judgement** | Decouples the weakest link; reasoning stays on a swappable text model. |
| Primary evidence source | **Match-v5 timeline, not pixels** | Timeline is ground truth and free; vision only fills its blind spots. |
| Prompt shape | **4 small specialist passes + 1 merge** | 8B models collapse on long multi-objective prompts; small passes also fit free-tier RPM. |
| VOD review | **Option B+ — pre-processed moments driving the client's Replay API** | Zero latency, zero cost, zero compliance risk; strictly better UX than streaming screen capture. |
| Knowledge | **RAG + dynamic prompts. No knowledge fine-tune.** | Patch cadence is 2 weeks; there is no licensable high-elo decision dataset; and a LoRA cannot deploy to the cloud path. |
| Patch notes source | **Auto-diff DataDragon between versions** | Machine-generated ground truth; no HTML scraping, no hallucination. |
| Backend | **Python 3.11 + FastAPI + uv** | Owns the CV/data ecosystem; `uv` makes Windows install one command. |
| Frontend | **Vite + React SPA served by FastAPI (v1) → Tauri shell (v2)** | Contributor velocity now, 10 MB installer later. Streamlit rejected: cannot do a scrubbable moment timeline. |
| Riot client | **Hand-rolled `httpx` client** | Match data is immutable → permanent caching is the biggest single win, and wrappers make that awkward. |
| LLM client | **One `AsyncOpenAI` against 5 different `base_url`s** | Ollama, Groq, OpenRouter, Mistral and Gemini all speak OpenAI-compat. One code path. |

Full reasoning per section:

- [§1 — Model & Inference Engine](01-model-routing.md)
- [§2 — Data Pipeline & Token Optimisation](02-data-pipeline.md)
- [§3 — Interactive VOD Review](03-vod-review.md)
- [§4 — Knowledge Base](04-knowledge-base.md)
- [§5 — Tech Stack & Repository](05-stack-and-repo.md)

---

## 4. The core data contract

Everything in the system is a transform between four types. Freeze these first; everything else is
replaceable.

```python
# riftcoach/core/schema.py
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field

class EvidenceTier(str, Enum):
    T1_MEASURED = "T1"   # read directly from Riot telemetry, e.g. "CS@10 = 61"
    T2_DERIVED  = "T2"   # computed from telemetry + a stated assumption, e.g. "lost prio at 14:10"
    T3_INFERRED = "T3"   # vision-estimated or heuristic, e.g. "the wave was slow-pushing"

class Evidence(BaseModel):
    tier: EvidenceTier
    timestamp_ms: int
    statement: str                      # "Died at MID_RIVER 38s before Drake spawn"
    source: Literal["timeline", "match", "vision", "patchdb", "benchmark"]
    assumption: str | None = None       # REQUIRED when tier != T1

class Finding(BaseModel):
    category: Literal["wave", "trading", "recall", "itemization",
                      "vision", "objective", "positioning", "tempo", "macro"]
    phase: Literal["early", "mid", "late"]
    severity: int = Field(ge=1, le=5)   # 5 = cost the game
    timestamp_ms: int                   # anchor -> replay seek target
    claim: str                          # what went wrong, one sentence
    evidence: list[Evidence] = Field(min_length=1)   # unanchored claims are rejected
    fix: str                            # the concrete alternative action
    drill: str | None = None            # practice rep for next game
    confidence: float = Field(ge=0, le=1)

class CoachingReport(BaseModel):
    match_id: str
    patch: str                          # e.g. "15.18.1" — pins the report to a meta
    puuid: str
    model_trace: dict[str, str]         # {"laning": "ollama/qwen3:14b", "merge": "gemini-2.5-flash"}
    findings: list[Finding]
    top_three: list[int]                # indices into findings — the only thing shown first
```

**Why `EvidenceTier` exists.** The constraint is "high actionability *and* accuracy." Wave
management advice is the highest-value coaching there is, and it is also the thing the Riot API
cannot measure — there is no wave-state field. A system that silently blends measured CS counts with
guessed wave states produces advice the user cannot trust and cannot verify. Tiering forces the
model to say *"minion kill-rate and your position imply you were slow-pushing (T2)"* rather than
*"you were slow-pushing."* It is the cheapest accuracy mechanism in the design, and it is what makes
the eval harness (§5) scoreable at all.

**Why every `Finding` carries `timestamp_ms`.** It is the join key between the reasoning layer and
the replay. A finding without a timestamp cannot be clicked, cannot be verified by the user, and
cannot be scored by the eval harness. Enforce it at the schema level so no prompt change can
regress it.

---

## 5. Processing modes

| Mode | Inputs | Evidence available | Cost | Notes |
|---|---|---|---|---|
| **A. Telemetry** (default) | `matchId` | T1/T2 | free, ~15 s | 80% of the value. Works with no GPU, no replay, no video. |
| **B. Synced Replay** | `matchId` + `.rofl` + running LoL client | T1/T2 + camera/minimap (T3) | free, ~60 s | The flagship interactive mode. §3. |
| **C. Video VOD** | video file / URL (+ optional `matchId`) | T1/T2 if matched, else T3 only | free, 2–10 min | For coaching content, streams, or accounts you do not own. |

Mode A must be complete and excellent **on its own**. B and C are enrichment. Any design where the
core value requires a replay or a GPU violates C2.

---

## 6. `ReplayGuard` — the compliance interlock

This is the only component that talks to `127.0.0.1:2999`. It is deliberately small, deliberately
boring, and it fails closed.

```python
# riftcoach/replay/guard.py
import httpx
from riftcoach.core.errors import LiveGameRefused

LIVE_CLIENT_CERT = "certs/riotgames.pem"   # Riot publishes this; do NOT use verify=False

class ReplayGuard:
    """Permits local-client I/O only while a REPLAY is playing.

    `GET /replay/playback` exists exclusively in replay mode. During a live game that
    route 404s while `/liveclientdata/*` still answers — so a 404 here is a positive
    signal that a live game may be in progress. We refuse, unconditionally.
    """
    BASE = "https://127.0.0.1:2999"

    async def assert_replay_mode(self, client: httpx.AsyncClient) -> float:
        try:
            r = await client.get(f"{self.BASE}/replay/playback", timeout=2.0)
        except httpx.ConnectError:
            raise LiveGameRefused("League client is not running.")
        if r.status_code == 404:
            raise LiveGameRefused(
                "A live game appears to be in progress. RiftCoach never runs during "
                "live games — start a replay and try again."
            )
        r.raise_for_status()
        return r.json()["length"]
```

Every call site wraps its work in `async with guard.session() as s:` which re-asserts before
**each** request, not once per session — a user can alt-tab out of a replay into champ select
mid-analysis, and a 5-minute batch job must notice.

**Policy position.** Riot documents both the Live Client Data API and the Replay API and permits
tools built on them; what they prohibit is software that automates gameplay or surfaces information
the player could not otherwise obtain. RiftCoach touches neither surface during live play. Ship this
stance in `COMPLIANCE.md` and in the README, because *"is this bannable?"* is the first question
every prospective user asks, and a specific, verifiable answer is a growth feature.

---

## 7. What this design deliberately does *not* do

Listed so contributors stop re-proposing them:

- **No `.rofl` frame decoding.** The chunk/keyframe payload is encrypted and the key is not
  recoverable from a personal replay file after the fact. Community decoders exist, are incomplete,
  and break most patches. We read `.rofl` *metadata* (`statsJson`) for match identification and hand
  the file to the client for playback. Anyone promising positional telemetry out of a raw `.rofl`
  is mistaken.
- **No live overlay.** Ever. See C1.
- **No op.gg / u.gg / porofessor scraping.** ToS-violating and fragile. Benchmarks come from
  aggregated Riot API sampling we run ourselves (§4).
- **No knowledge fine-tune.** See [§4](04-knowledge-base.md) for the full argument.
- **No hosted service.** Self-hosting is what keeps C2 and C3 true forever.
