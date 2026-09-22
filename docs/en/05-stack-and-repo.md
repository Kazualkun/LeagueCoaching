# §5 — Tech Stack & Repository Structure

[🇧🇷 Português](../05-stack-and-repo.md) · 🇺🇸 English

## 5.1 Language and runtime

**Python 3.11+ backend.** Not a real debate: PyAV, RapidOCR, pandas/polars, the Riot ecosystem, the
ML tooling and every contributor who can write a parser all live there. 3.11 is the floor for
`ExceptionGroup`/`TaskGroup` (the analyst fan-out in §1 is a `TaskGroup`) and modern generics.

**`uv` for dependency and Python management.** This is a distribution decision, not a taste
decision. Our users are League players on Windows, many with no Python at all. `uv` installs the
interpreter itself, resolves in seconds, and produces a committed `uv.lock`. It reduces
"install Python 3.11, create a venv, pip install, hope your CUDA matches" to:

```
uv run riftcoach
```

That difference is worth more adoption than any feature.

---

## 5.2 Backend framework — FastAPI

Chosen for three properties this specific app needs:

1. **Native async.** The workload is I/O fan-out — Riot API, Ollama, cloud LLMs, the local client on
   2999 — all concurrent. Four analyst passes run in a `TaskGroup`.
2. **WebSockets.** Analysis takes 15–90 s. Streaming progress (`fetching timeline → distilling →
   laning analyst done → ...`) is the difference between "working" and "frozen".
3. **Pydantic is the framework.** Our entire data contract (§ARCHITECTURE.4) is Pydantic models; the
   API schema, the LLM output schema and the internal IR are the same objects. No duplication.

**Rejected:** Flask (no async, no WS ergonomics), Django (ORM-centric, enormous for a local app),
Streamlit *as the main app* (see below).

---

## 5.3 Frontend — phased

### v1: Vite + React + TypeScript SPA, built to static files, served by FastAPI, opened in the system browser

### v2: Tauri shell wrapping the identical SPA, with the Python backend as a sidecar

**Why not Streamlit.** It is the obvious "fast path" and it is a trap for *this* app. The core UX is
a scrubbable timeline with clickable finding markers, driving an external replay, with a
`<video>` element and streaming progress. Streamlit's execution model re-runs the script on every
interaction; that fights every one of those requirements. It is excellent for the **debug/dev
console** — inspecting `MatchFacts`, diffing prompts, eyeballing the parser — so keep it there:
`tools/inspector/` is a Streamlit app, and it is a genuinely good use of it.

**Why not Electron.** ~150 MB baseline and a Node runtime shipped to every user, for a webview the
OS already has.

**Why browser-first before Tauri.** It works on day one with zero extra toolchain, it keeps
headless/server deployment viable (analyse on a home server, view on a phone), and it means Tauri is
a packaging change later rather than a rewrite. Tauri v2 gives a ~10 MB installer using the OS
webview and has first-class sidecar support for shipping the Python binary — but it adds a Rust CI
toolchain and platform-specific signing. That is a v2 problem, not a v1 blocker.

---

## 5.4 Library selection

| Concern | Choice | Reasoning |
|---|---|---|
| **Riot API client** | **Hand-rolled on `httpx.AsyncClient`** | Match data is immutable, so a permanent zstd/SQLite cache is the single biggest win available (§2.6) — and wrappers make caching layers awkward. The API surface we need is 5 endpoints. Also gives us exact control of the two-window limiter and the dev-key-expiry UX. `pulsefire` is the best third-party option if you disagree; `riotwatcher` is sync and therefore wrong here. |
| **HTTP** | `httpx` + `tenacity` | One async client for Riot, DDragon and `127.0.0.1:2999`. |
| **Cache / DB** | `sqlite3` + `aiosqlite` + `zstandard` + `sqlite-vec` | One file, no daemon, no Docker. A 2.5 MB timeline → ~180 KB. |
| **Validation / IR** | `pydantic` v2 | The data contract. |
| **LLM clients** | `openai` (async) for all OpenAI-compat providers; `google-genai` **only** for video upload; `ollama` optional for model management | One code path for five providers (§1.3). |
| **Embeddings** | `fastembed` (ONNX CPU) | **No PyTorch dependency.** Non-negotiable for Windows install friction (§4.4). |
| **Video** | `av` (PyAV) | In-process accurate seek; ~15× faster than per-frame ffmpeg subprocesses. |
| **OCR** | `rapidocr-onnxruntime` | ONNX, CPU, no Paddle/torch. Handles the HUD clock and numerics deterministically — never pay a VLM for a 4-digit gold counter. |
| **Images** | `Pillow` + `numpy` | ROI crops, resolution normalisation. |
| **TTS (optional)** | `piper-tts` | MIT, ONNX, CPU, offline, ~50 MB. |
| **Secrets** | `keyring` | Windows Credential Manager / macOS Keychain / Secret Service. **Never** a dotfile. |
| **Config** | `pydantic-settings` + `providers.yaml` | Typed config, env override, user-editable provider list. |
| **Jobs** | `asyncio` + a SQLite `jobs` table | No Celery, no Redis. A local single-user app does not need a broker. |
| **CLI** | `typer` + `rich` | `riftcoach analyze NA1_123456` must work headless for power users and CI. |
| **Testing** | `pytest`, `pytest-asyncio`, `respx`, `syrupy` | `respx` mocks Riot/LLM HTTP; `syrupy` snapshot-tests `MatchFacts` against golden fixtures. |
| **Quality** | `ruff`, `mypy --strict` on `core/` and `parse/` | The parser is where silent correctness bugs live; type it strictly. |
| **Packaging** | `uv` + `hatchling`; PyInstaller for the v1 one-click launcher | |

---

## 5.5 Repository structure

```
riftcoach-ai/
├── README.md
├── COMPLIANCE.md                  # the Riot ToS position — a headline document, not a footnote
├── CONTRIBUTING.md
├── LICENSE                        # AGPL-3.0 (see 5.7)
├── pyproject.toml
├── uv.lock
├── .env.example                   # NO real keys, ever
├── .gitignore                     # *.key, .env, *.rofl, data/, !.env.example
│
├── riftcoach/
│   ├── __main__.py                # `uv run riftcoach` -> serves API + opens browser
│   ├── cli.py                     # typer: analyze / sync-patch / bench / doctor
│   ├── config.py                  # pydantic-settings, keyring integration
│   │
│   ├── core/
│   │   ├── schema.py              # Finding, Evidence, CoachingReport  <- THE contract
│   │   ├── errors.py              # LiveGameRefused, NoViableProvider, SchemaExhausted
│   │   └── zones.py               # MapZone enum + polygon index + side_relative()
│   │
│   ├── riot/
│   │   ├── client.py              # httpx + two-window limiter + dev-key-expiry UX
│   │   ├── cache.py               # SQLite + zstd, permanent for immutable resources
│   │   └── models.py              # thin typed views over raw Riot JSON
│   │
│   ├── parse/                     # ===== §2 =====
│   │   ├── distill.py             # timeline -> MatchFacts  (the 150x reduction)
│   │   ├── facts.py               # MatchFacts IR
│   │   ├── deaths.py              # DeathContext enrichment
│   │   ├── economy.py             # recalls, build path, spike timings
│   │   ├── objectives.py          # ObjectiveEvent + prep-window analysis
│   │   ├── waves.py               # wave_proxy (T2) — honest about its limits
│   │   ├── moments.py             # MomentBuilder -> CoachableMoment[]
│   │   └── render.py              # MatchFacts -> the compact text format (NOT json)
│   │
│   ├── knowledge/                 # ===== §4 =====
│   │   ├── sync.py                # DDragon/CDragon -> SQLite
│   │   ├── patchdiff.py           # version-to-version structural diff -> changelog
│   │   ├── retrieve.py            # trigger filter -> fastembed -> sqlite-vec
│   │   ├── benchmarks.py          # parquet percentiles + own-history fallback
│   │   └── validator.py           # FactValidator: Aho-Corasick entity + stat check
│   │
│   ├── llm/                       # ===== §1 =====
│   │   ├── base.py                # Capability, ProviderProfile, TaskSpec
│   │   ├── router.py              # select(), degradation ladder, privacy policy
│   │   ├── breaker.py             # circuit breaker + token-bucket limiter
│   │   ├── hardware.py            # VRAM probe -> tier (incl. KV-cache headroom)
│   │   └── providers/
│   │       ├── openai_compat.py   # ollama | groq | openrouter | cerebras | gemini-compat
│   │       └── gemini_native.py   # video upload path ONLY
│   │
│   ├── analysis/
│   │   ├── packet.py              # EvidencePacket assembly
│   │   ├── prompts/               # ---- the highest-churn directory in the repo ----
│   │   │   ├── laning.md
│   │   │   ├── macro.md
│   │   │   ├── economy.md
│   │   │   ├── fights.md
│   │   │   ├── head_coach.md
│   │   │   └── followup.md
│   │   ├── analysts.py            # the 4-way TaskGroup fan-out
│   │   ├── merge.py               # dedupe on (category, t +/- 30s), rank, top_three
│   │   └── rules.py               # deterministic rule engine — powers L5 (no-AI mode)
│   │
│   ├── replay/                    # ===== §3 =====
│   │   ├── guard.py               # ReplayGuard — the compliance interlock
│   │   ├── controller.py          # seek_to(), camera, clock calibration
│   │   ├── rofl.py                # metadata only; never decodes chunks
│   │   └── sinks.py               # ClientReplaySink | VideoFileSink | BrowserVideoSink
│   │
│   ├── vision/
│   │   ├── sampler.py             # 3 frames per moment via PyAV accurate seek
│   │   ├── rois.py                # resolution-normalised HUD/minimap crop regions
│   │   ├── ocr.py                 # RapidOCR: clock, gold, cs
│   │   └── vlm.py                 # FrameObservation extraction — perception only
│   │
│   └── api/
│       ├── app.py                 # FastAPI + static SPA mount
│       ├── routes.py              # /analyze /report/{id} /seek /followup /doctor
│       └── ws.py                  # progress streaming
│
├── web/                           # Vite + React + TS
│   ├── src/
│   │   ├── components/Timeline.tsx        # gold-diff curve + finding markers
│   │   ├── components/FindingCard.tsx     # claim / evidence(tier) / fix / [Seek]
│   │   ├── components/ReplayBridge.ts     # POST /seek  or  <video>.currentTime
│   │   └── components/SetupWizard.tsx     # key entry, hardware probe, model pull
│   └── package.json
│
├── knowledge/                     # ---- CONTRIBUTE HERE, no Python required ----
│   ├── principles/                # markdown + frontmatter (see §4.3)
│   └── benchmarks/                # {patch}.parquet, generated by CI
│
├── evals/                         # ===== the credibility layer =====
│   ├── fixtures/                  # ~30 anonymised matches, PUUIDs scrubbed
│   ├── golden/                    # human-annotated expected findings per fixture
│   ├── rubric.md                  # what "a good finding" means
│   ├── run.py                     # score every provider -> compatibility matrix
│   └── results/                   # committed history; regressions are visible in PRs
│
├── tools/
│   ├── inspector/                 # Streamlit dev console: inspect IR, diff prompts
│   └── sample_matches.py          # maintainer job: generate benchmark parquet
│
├── tests/
│   ├── fixtures/                  # golden timelines incl. remake, ARAM, lane-swap, 60min
│   └── test_distill.py            # syrupy snapshots of MatchFacts
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── 01-model-routing.md
│   ├── 02-data-pipeline.md
│   ├── 03-vod-review.md
│   ├── 04-knowledge-base.md
│   └── 05-stack-and-repo.md
│
└── .github/workflows/
    ├── ci.yml                     # ruff + mypy + pytest
    ├── patch-sync.yml             # daily: detect new DDragon version -> PR the diff
    ├── benchmarks.yml             # per patch: sample matches -> parquet release asset
    └── evals.yml                  # nightly: run evals on free tiers, publish the matrix
```

### Structural notes

- **`parse/` has no LLM imports and `llm/` has no Riot imports.** The distillation layer must be
  testable with zero network and zero models. If that boundary holds, the parser stays correct.
- **`analysis/prompts/*.md` are files, not string literals.** Prompts change constantly; keeping
  them as reviewable Markdown means a prompt PR has a readable diff and non-Python contributors can
  file one.
- **`analysis/rules.py` is not an afterthought.** It is the L5 no-AI report (§1.5) and the source of
  deterministic triggers that drive RAG retrieval (§4.4). Build it first — it is the fastest way to
  a working product and it makes everything above it testable.
- **`evals/` is committed, including results.** An OSS AI project with no eval harness cannot make
  accuracy claims, and cannot tell whether a prompt PR helped. The published provider
  compatibility matrix ("which free models actually work") is also, in practice, the most useful
  artefact the project will produce for its users.

---

## 5.6 Secrets — with a live example

A Riot development key was found in plaintext at the project root during scaffolding
(`.txt`). That is exactly the failure this section prevents. Rules:

1. Keys go in the OS keyring via `keyring.set_password("riftcoach", "riot", key)`.
2. `.env` is gitignored; only `.env.example` with placeholder values is committed.
3. `*.txt` at the repo root is **not** safe by default — gitignore aggressively and add a
   `pre-commit` hook running `detect-secrets` or `gitleaks`.
4. The setup wizard writes the key to the keyring and offers to delete the source file.
5. Dev keys expire every 24 h. Detect the expiry `403`, show a deep link to
   `developer.riotgames.com`, and prompt users to apply for a Personal API Key.

Since that key is now in a plaintext file on a Desktop path, **regenerate it** before doing anything
else.

---

## 5.7 Licensing

**AGPL-3.0** for the application. It keeps the "100% free" promise enforceable: anyone who runs a
modified RiftCoach as a hosted service must publish their changes, which prevents the most likely
bad outcome — someone wrapping it in a paid SaaS and out-marketing the original.

**CC-BY-SA-4.0** for `knowledge/principles/` so the coaching corpus can circulate independently of
the code.

Standard Riot disclaimer in the README: *RiftCoach AI is not endorsed by Riot Games and does not
reflect the views or opinions of Riot Games or anyone officially involved in producing or managing
Riot Games properties.*

---

## 5.8 Build order (the actual plan)

The dependency order that gets to a usable product fastest, with each stage independently useful:

| Stage | Ships | Why this order |
|---|---|---|
| **0** | `riot/` client + cache + golden fixtures | Everything depends on it; fixtures make the rest offline-testable. |
| **1** | `parse/` → `MatchFacts` + `render.py` + snapshot tests | The hard, high-value, model-independent work. Done right, everything above it is easy. |
| **2** | `knowledge/sync.py` + `benchmarks` + `rules.py` → **L5 report** | **A genuinely useful product with no AI at all.** Ship this publicly. |
| **3** | `llm/` router + `analysis/` 4+1 passes | Now the LLM has clean input and a fallback that already works. |
| **4** | `evals/` + compatibility matrix | Before adding features, prove the ones you have. |
| **5** | `web/` SPA + `replay/` seeking | The flagship UX, on a foundation that is already correct. |
| **6** | `vision/` enrichment, Tauri packaging, optional style LoRA | Polish. |

Stage 2 is the one to resist skipping. A deterministic analytics report that works with no GPU, no
API key and no model is both a real product and the safety net that keeps the whole system honest.
