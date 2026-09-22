# §1 — Model & Inference Engine Selection

[🇧🇷 Português](../01-model-routing.md) · 🇺🇸 English

## 1.1 The framing everyone gets wrong

The instinct is to pick "the best model." The correct move is to **decompose the workload into four
jobs with wildly different requirements** and route each one separately, because they have almost
nothing in common:

| Job | What it actually needs | Context | Tolerance for a weak model |
|---|---|---|---|
| **J1 — Perception** (read the HUD, locate icons on the minimap) | OCR of 11px text, spatial grounding, bbox output | tiny (1 image + 200 tok) | **Low** — a misread gold value poisons everything downstream |
| **J2 — Arithmetic** (CS@10, gold diff, death timers) | *nothing* — this is Python | — | n/a, never give this to a model |
| **J3 — Fact recall** (does Eclipse still give omnivamp?) | *nothing* — this is a DB lookup | — | n/a, never give this to a model |
| **J4 — Judgement** (was recalling at 12:40 correct?) | instruction-following, structured output, causal reasoning over ~3k tok | 8–32k | **Medium** — an 8B model is genuinely serviceable *if* J1–J3 are already solved |

Moving J2 and J3 out of the model is what collapses the hardware requirement. A 14B model asked to
"analyse this match" fails. The same 14B model asked *"here are 12 measured facts and 4 patch
constants; rank the three biggest mistakes and cite which facts support each"* succeeds. **The model
quality bar is set by how much work you refuse to give the model.**

---

## 1.2 Local models — the recommendation

### Text reasoning (J4) — primary workload

| Tier | VRAM | Model | Ollama tag | Why |
|---|---|---|---|---|
| T0 | none / <6 GB | *(cloud only)* | — | Do not attempt. A 3B model produces advice that is worse than no advice. |
| T1 | 8 GB | **Qwen3-8B** (Q4_K_M) | `qwen3:8b` | Best sub-10B instruction-follower; strong JSON adherence. Fallback: `qwen2.5:7b-instruct`. |
| T2 | 12–16 GB | **Qwen3-14B** (Q4_K_M) | `qwen3:14b` | The sweet spot. ~9 GB weights + ~3 GB KV at 32k. Alt: `phi4:14b`, `gemma3:12b`. |
| T3 | 24 GB+ | **Qwen3-30B-A3B** (Q4_K_M) | `qwen3:30b-a3b` | MoE: ~30B quality at ~3B active-param speed. Runs at 40+ tok/s on a 3090/4090 and degrades gracefully when partially offloaded to RAM — uniquely good for a consumer-hardware OSS project. |
| Mac | 16 GB unified | `qwen3:14b` | | Metal via Ollama; 32 GB+ → `qwen3:30b-a3b`. |

**Why Qwen over Llama 3.x for this task.** Three concrete reasons, not vibes:
1. **Structured output adherence.** Our entire pipeline is `list[Finding]` JSON. Qwen2.5/Qwen3 hold
   a schema across long outputs materially better than Llama 3.1/3.2 at equal size, which is the
   difference between a working app and a retry loop.
2. **Thinking mode (Qwen3).** `/think` gives a budgeted reasoning trace we can *discard before
   parsing*. Ranking mistakes by severity benefits from it, and we pay only local compute.
3. **Long-context stability at 32k.** The merge pass concatenates four analyst outputs. Llama 3.1 8B
   degrades noticeably in the second half of a 16k context; Qwen3 holds.

**The Ollama footgun you must handle on day one.** Ollama's default `num_ctx` is 4096 and it
**silently truncates** rather than erroring. Your carefully-distilled 4k evidence packet plus a 1.5k
system prompt will be quietly beheaded and the model will coach on half a game. Always set it
explicitly, and assert the model's reported context window at startup:

```python
options = {"num_ctx": 32768, "temperature": 0.3, "top_p": 0.9, "repeat_penalty": 1.05}
```

Low temperature is deliberate: we want the model to *select and rank* supplied evidence, not
to invent. Creativity is a defect here.

### Vision (J1)

**`qwen2.5vl:7b` (Ollama) is the local default.** Llama 3.2 Vision 11B is the obvious alternative
and it is the wrong pick for this domain: it is comparatively weak at dense small-text OCR, which is
*exactly* our workload (the gold counter, the CS counter, the 00:00 game clock, item tooltips at
1080p). Qwen2.5-VL was trained with explicit document-OCR and spatial-grounding objectives and can
return bounding boxes, which we need for minimap icon localisation. Qwen2-VL (the version named in
the brief) is its predecessor — usable, but 2.5 is a straight upgrade at the same size. On 8 GB,
drop to `qwen2.5vl:3b` for clock/HUD OCR only and route minimap work to cloud.

**Critical constraint on J1: never let the VLM do J4.** The VLM's only permitted output is a
`FrameObservation`:

```python
class FrameObservation(BaseModel):
    game_clock_s: int | None            # OCR'd from the HUD timer
    camera_zone: MapZone | None         # where the player was looking
    minimap_allies: list[MapZone]       # icon detection
    minimap_enemies_visible: list[MapZone]
    hud: HudState | None                # gold, cs, level, hp%, ult_ready
    wave_state: Literal["freeze","slow_push","fast_push","crashing","bounced","unknown"]
    confidence: float
```

Everything it emits is stamped `EvidenceTier.T3_INFERRED`. It is a sensor, not a coach.

---

## 1.3 Free cloud tiers — the recommendation

| Provider | Use it for | Free-tier shape | Verdict |
|---|---|---|---|
| **Google AI Studio — Gemini 2.5 Flash / 2.0 Flash** | **Default cloud. Text + the *only* good VOD path.** | Generous RPM/RPD, huge context | **Primary.** Uniquely: ingests a *video file directly* with native ~1 fps sampling and returns answers with real timestamps. That single capability collapses Mode C from a frame-extraction pipeline into one API call. |
| **Groq** | Latency-critical interactive follow-ups | High RPM, low RPD, OpenAI-compat | **Secondary.** Inference is dramatically faster than everything else; small daily quota makes it wrong for batch, right for "ask a question about this moment." |
| **OpenRouter (`:free` models)** | Overflow / user choice | Per-model, volatile | **Tertiary.** Model availability churns constantly — treat as user-configured, never as a default. |
| **Mistral** | When Google/Groq are unavailable in your country | Free tier, EU-based | **Fourth option.** In the catalog for geographic AVAILABILITY, not because it is better. |
| ~~Cerebras~~ | — | **no longer free** | Became a US$5 card-required trial (checked Sep 2026). Dropped from recommendations. |

All four speak the OpenAI Chat Completions schema (Gemini via its OpenAI-compat base URL), so a
single `AsyncOpenAI` instance with a swapped `base_url` + `api_key` covers the entire cloud surface.
Use the **native** `google-genai` SDK only for the video-upload path, because the Files API and
video part types have no OpenAI-compat equivalent.

**Free-tier quotas change constantly.** Do not hardcode them. Ship `providers.yaml` with
declared limits, let users edit it, and enforce client-side with a token bucket so a quota change
degrades into a graceful failover rather than a crash.

---

## 1.4 The two pipelines

### Pipeline P1 — Text-only post-game telemetry (Mode A)

```
MatchFacts IR (§2)
      │
      ├─► KnowledgeAssembler: inject patch facts for items/runes in this build (§4)
      ├─► BenchmarkAssembler: inject role/rank percentiles for this player (§4)
      │
      ▼
 EvidencePacket  (~3.5–4.5k tokens, fully typed, every number pre-computed)
      │
      ├──────────────┬──────────────┬──────────────┐   ← run in PARALLEL
      ▼              ▼              ▼              ▼
 laning_analyst  macro_analyst  economy_analyst  fight_analyst
 (~1.4k tok in)  (~1.2k)        (~1.0k)          (~1.3k)
      │              │              │              │
      └──────────────┴──────┬───────┴──────────────┘
                            ▼
                   head_coach (merge, dedupe, rank)   ~2k tok in
                            ▼
                   CoachingReport → FactValidator (§4.5) → UI
```

**Why four small passes instead of one big one — this is the load-bearing decision of §1.**

1. **It is what makes 8B models viable.** Each analyst sees ~1.2k tokens and answers one question.
   Multi-objective long-context prompts are precisely where small models collapse; single-objective
   short prompts are where they are nearly indistinguishable from large ones.
2. **It parallelises.** Four concurrent calls against Groq finish in about the time of one.
3. **Free-tier quotas are counted in requests, but *capability* is capped by context.** Five small
   requests fit every free tier comfortably; one 30k-token request does not fit some of them at all.
4. **Failures are isolated.** If `fight_analyst` returns malformed JSON, you retry 1.2k tokens, not
   the entire analysis.
5. **It makes the eval harness tractable.** You can score laning findings independently of macro
   findings and know which prompt regressed.

The cost is a merge pass and the risk of duplicate findings — handled by dedupe on
`(category, timestamp_ms ± 30s)` in `head_coach`, with the merge *ranking* rather than rewriting so
that evidence citations survive intact.

### Pipeline P2 — Multimodal VOD review (Modes B/C)

The key realisation: **when a matching `matchId` exists, you barely need vision at all.** The
timeline already gives you kills, items, wards, objectives, gold and position-per-minute as ground
truth. Vision is needed only for the five things telemetry cannot see:

1. Where the camera was pointed (map awareness proxy)
2. Wave state / minion positioning
3. Ability casts and cooldowns between the 60 s frame boundaries
4. Whether an enemy was *visibly* on the minimap before a death
5. Everything, when there is no `matchId` at all (Mode C unmatched)

So vision runs on **sampled frames at pre-identified coachable moments**, never on a stream:

```
CoachableMoment[] (from §2, e.g. 9 deaths + 4 objective fights + 3 recall errors)
      │
      ▼  for each moment, extract 3 frames at t-5s, t-1s, t+2s  (PyAV accurate seek)
      │
      ▼  crop to fixed ROIs: minimap / HUD-gold / HUD-cs / clock   ← resolution-normalised
      │
      ├─► RapidOCR (ONNX, CPU, ~15 ms/crop) for clock + numerics   ← cheap, deterministic
      └─► VLM only for minimap icons + wave state                  ← expensive, sampled
      │
      ▼
 FrameObservation[]  →  merged into MatchFacts as T3 evidence
      │
      ▼
 P1 runs unchanged, now with richer evidence
```

**Budget:** 16 moments × 3 frames = 48 frames, but only ~16 reach the VLM (one per moment, the
`t-1s` frame). At ~800 image tokens each that is ~13k image tokens for a whole game — one Gemini
Flash call, or ~40 s on a local Qwen2.5-VL-7B. Compare with naive 1 fps capture of a 30-minute game:
1,800 frames, ~1.4M image tokens. **~100× reduction, and better output**, because each surviving
frame is one the timeline already told us was decisive.

**Mode C shortcut.** If the cloud provider is Gemini and the user supplied a video, skip frame
extraction entirely: upload the file, ask for observations at the moment timestamps in one call.
Falls back to the frame pipeline on any other provider.

---

## 1.5 The failover / hybrid system

### Capability declaration, not model names

The router never reasons about model names. Every provider declares capabilities; every task
declares requirements; the router solves the constraint.

```python
# riftcoach/llm/base.py
class Capability(str, Enum):
    TEXT = "text"; VISION = "vision"; VIDEO_NATIVE = "video"
    JSON_SCHEMA = "json_schema"; LONG_CTX_32K = "ctx32k"

@dataclass(frozen=True)
class ProviderProfile:
    name: str                       # "ollama:qwen3:14b"
    caps: frozenset[Capability]
    ctx_tokens: int
    est_tok_per_s: float            # measured at first run, cached
    cost_class: Literal["local", "free_cloud", "paid"]
    rate_limit: RateLimit | None    # token bucket, enforced client-side
    privacy: Literal["local_only", "leaves_machine"]

@dataclass(frozen=True)
class TaskSpec:
    name: str                       # "laning_analyst"
    requires: frozenset[Capability]
    est_input_tokens: int
    latency_class: Literal["batch", "interactive"]
```

### Hardware probe (first run only, cached to `~/.riftcoach/hardware.json`)

```python
def probe() -> HardwareTier:
    # 1. NVIDIA: pynvml.nvmlDeviceGetMemoryInfo -> total VRAM
    # 2. AMD/Intel: torch not required — parse `wmic path win32_VideoController`
    #    on Windows, /sys/class/drm on Linux
    # 3. Apple: platform.machine() == "arm64" -> unified memory via sysctl hw.memsize
    # 4. Ollama reachable? GET http://127.0.0.1:11434/api/tags
    # 5. Which of our recommended tags are already pulled?
```

Map VRAM → tier using the §1.2 table, **subtracting KV-cache headroom for 32k context** (roughly
2–4 GB for a 14B at Q4). Tiering on weights alone is the classic mistake: the model loads, then OOMs
or spills to RAM at 6k tokens in, and the user blames the app.

### Selection algorithm

```python
async def select(self, task: TaskSpec) -> ProviderProfile:
    pool = [p for p in self.providers
            if task.requires <= p.caps
            and p.ctx_tokens >= task.est_input_tokens * 1.4      # headroom for output
            and self.breaker.is_closed(p.name)                    # §1.6
            and self.limiter.has_budget(p.name)]
    if not pool:
        raise NoViableProvider(task, self._diagnose())            # actionable message, never a 500
    return min(pool, key=lambda p: self.policy.rank(p, task))
```

Default `policy.rank` ordering, in priority order:

1. **Honour the user's privacy setting.** `privacy_mode=strict` filters out everything
   `leaves_machine`, full stop, before any other consideration.
2. **`cost_class`:** `local` < `free_cloud` < `paid`. Local first is not only about cost — it has no
   quota, so batch work never burns the daily allowance the interactive path needs.
3. **Latency class:** for `interactive` tasks, flip the ordering when local `est_tok_per_s < 15`.
   A 90-second wait for a follow-up question is a broken feature; a 90-second wait for a batch
   report is fine.
4. **Measured throughput** as the tiebreak.

### Degradation ladder (explicit, user-visible)

```
L0  local T3 model, all 5 passes local, vision local        "Full local"
L1  local text + cloud vision                                "Hybrid — vision in cloud"
L2  cloud text + cloud vision                                "Cloud"
L3  cloud text, vision DISABLED                              "Reduced — no VOD enrichment"
L4  telemetry-only, single merged pass, 8k ctx               "Minimal"
L5  deterministic report: metrics + benchmarks + rule hits   "No AI available"
```

**L5 is mandatory and it is not a consolation prize.** If every provider is down and every quota is
burnt, the app still renders CS@10 vs. rank percentile, gold-diff curves, death-location heatmaps,
recall-efficiency, and rule-engine hits ("you bought a Control Ward in only 2 of 9 recalls").
Roughly 40% of the product's value is computed in §2 and needs no model whatsoever. Shipping L5
first also means the rest of the app is testable before any model is wired up.

### Circuit breaker + rate limiting

```python
class ProviderBreaker:
    """Per-provider, three-state, failure-type-aware."""
    # 429 / quota          -> OPEN until the quota window resets (parse Retry-After; else
    #                         back off to the provider's declared reset from providers.yaml)
    # 5xx / timeout        -> OPEN 30s, exponential to 5min, HALF_OPEN probes with the
    #                         cheapest task in the queue
    # schema-violation x3  -> OPEN permanently for THIS SESSION and emit a warning:
    #                         this model cannot hold our output schema; stop wasting quota on it
```

The third case matters more than it looks. A free-tier model that cannot reliably emit our JSON will
burn a user's entire daily quota on retries and produce nothing. Detect it in three strikes,
eject it, tell the user which model failed and why, and fail over. Log it to
`~/.riftcoach/compat.jsonl` so the project can publish a real compatibility matrix (§5) built from
community data.

### Structured output, per provider

Never parse free text. Ranked by reliability:

1. **Ollama** — `format: <json-schema>` (native JSON-schema constrained decoding). Hard guarantee.
2. **Gemini** — `response_schema` + `response_mime_type="application/json"`. Hard guarantee.
3. **Groq / OpenRouter** — `response_format={"type":"json_object"}` + schema in the prompt.
   Soft guarantee → wrap in a Pydantic validate-and-repair loop, max 2 retries, then breaker.

```python
async def generate_validated[T: BaseModel](self, spec, prompt, model: type[T]) -> T:
    for attempt in range(3):
        raw = await self._call(spec, prompt, schema=model.model_json_schema())
        try:
            return model.model_validate_json(raw)
        except ValidationError as e:
            prompt = repair_prompt(prompt, raw, e)      # feed the errors back verbatim
            self.breaker.record_schema_violation(spec.provider)
    raise SchemaExhausted(spec.provider)
```

---

## 1.6 Concrete default configuration

```yaml
# config/providers.yaml — shipped defaults, user-editable
routing:
  privacy_mode: relaxed          # strict = never leave the machine
  prefer: cost                   # cost | speed | quality

providers:
  - name: ollama
    base_url: http://127.0.0.1:11434/v1
    cost_class: local
    privacy: local_only
    models:
      text:   {tier1: qwen3:8b, tier2: qwen3:14b, tier3: qwen3:30b-a3b}
      vision: {tier1: qwen2.5vl:3b, tier2: qwen2.5vl:7b, tier3: qwen2.5vl:7b}
    options: {num_ctx: 32768, temperature: 0.3}

  - name: gemini
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_ref: keyring:riftcoach/gemini      # never inline
    cost_class: free_cloud
    privacy: leaves_machine
    caps: [text, vision, video, json_schema, ctx32k]
    models: {text: gemini-2.5-flash, vision: gemini-2.5-flash, video: gemini-2.5-flash}

  - name: groq
    base_url: https://api.groq.com/openai/v1
    api_key_ref: keyring:riftcoach/groq
    cost_class: free_cloud
    caps: [text, json_schema, ctx32k]
    latency_preference: interactive
```

Model IDs live in config precisely because they rot. The router's contract is with
`Capability`, never with a string — swapping to next year's model is a YAML edit, not a code change.
