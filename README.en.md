<div align="center">

# RiftCoach AI

**A free, open-source League of Legends coach that reviews your games and tells you what to fix.**

No subscription. No account. No data leaves your machine unless you tell it to.
Runs locally on your GPU, or on free cloud API tiers if you don't have one.

[🇧🇷 Português](README.md) · 🇺🇸 English

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Is this bannable?](#is-this-bannable-no) · [Contributing](#contributing) · [Architecture](docs/en/ARCHITECTURE.md)

</div>

---

## What it does

You finish a game. You paste the match ID (or click it from your history). Ninety seconds later you
get something like:

> **#1 · severity 5 · 14:22 · positioning**
> You died in enemy topside jungle 38 seconds before Drake spawned, with no ward within 1600 units
> and Flash down.
> **Evidence:** death D2 *(T1, measured)* · no own ward near the pit in the prior 60 s *(T1)* ·
> wave was pushing toward the enemy tower *(T2, derived from CS rate + position)*
> **Fix:** with a drake under a minute out, your job at 14:00 is bot-side river vision, not a
> topside invade. Cross the map on the recall *before* the objective, not after.
> **Drill:** next 3 games — when an objective timer hits 60 s, check where you are. If you're on the
> wrong side of the map, you've already made the mistake.
> `[▶ Watch this in the replay]`

Click the button and your League client seeks the replay to 14:14 — eight seconds *before* the
death, because the mistake is the decision, not the outcome.

### Three ways to use it

| Mode | What you need | What you get |
|---|---|---|
| **Telemetry** | A match ID | Full analysis. No GPU, no replay, no download needed. |
| **Synced Replay** | Match ID + `.rofl` + League client | Everything above, plus click-to-seek review and map/camera analysis. |
| **Video VOD** | A video file or URL | Review any recorded game, including ones you didn't play. |

---

## Quickstart

```bash
git clone https://github.com/Kazualkun/LeagueCoaching.git
cd LeagueCoaching
uv run riftcoach
```

That's it — `uv` installs Python and every dependency for you. A setup wizard opens in your browser
and walks you through:

1. **A Riot API key** — free, from [developer.riotgames.com](https://developer.riotgames.com).
   Stored in your OS keyring, never in a file.
2. **Where the AI runs** — it probes your hardware and recommends one:

| Your GPU | Recommendation |
|---|---|
| 24 GB+ (4090, 3090, 7900 XTX) | `ollama pull qwen3:30b-a3b` — fully local, nothing leaves your PC |
| 12–16 GB (4070, 3080, 4060 Ti 16GB) | `ollama pull qwen3:14b` — fully local |
| 8 GB | `ollama pull qwen3:8b` — local, or use a free cloud tier for better results |
| Apple Silicon 16 GB+ | `ollama pull qwen3:14b` |
| No GPU / laptop | Free Google AI Studio key → Gemini Flash. Works fine. |

3. **Nothing else.** If every AI option fails, you still get a full statistical report — percentile
   benchmarks, death heatmaps, recall efficiency, gold curves — computed entirely offline.

---

## Is this bannable? No.

This is the first question everyone asks, so here is the specific answer.

RiftCoach **never runs during a live game.** It is architecturally incapable of it:

- The only component that talks to the League client (`ReplayGuard`) checks
  `GET https://127.0.0.1:2999/replay/playback` before **every single request**. That endpoint exists
  only while a replay is playing. If it returns 404 — which is what happens during a live game —
  RiftCoach refuses to continue and tells you why.
- There is no overlay, no alert, no automation, no input simulation, no memory reading.
- Everything else uses the official Match-v5 API on games that are already over.

Riot documents and permits both the Live Client Data API and the Replay API. What they prohibit is
software that automates gameplay or reveals information you couldn't otherwise have. RiftCoach does
neither, at any point. Full reasoning in [COMPLIANCE.md](COMPLIANCE.en.md).

---

## How it works

Most "AI coach" projects dump a 2 MB match timeline into an LLM and hope. That fails, because the
model ends up doing perception, arithmetic, fact-recall and judgement all at once — and it's bad at
the first three.

RiftCoach does the first three in Python and gives the model only the fourth:

```
 2.5 MB timeline  (~600,000 tokens)
        │
        ▼  perspective filter · coordinates -> named map zones · phase rollups
        ▼  every metric computed in Python · deaths enriched with tactical context
        │
  MatchFacts  (~1,100 tokens)     +  patch facts from DataDragon  +  rank benchmarks
        │
        ▼  four small specialist passes, run in parallel:
        ▼  laning · macro · economy · fights   ->   head coach merges and ranks
        │
  CoachingReport — every claim timestamped, cited, and tagged with how it was known
        │
        ▼  fact-validated against the current patch, then rendered
```

Two ideas do most of the work:

**Evidence tiers.** Every claim is marked `T1` (measured directly from Riot data), `T2` (derived,
with the assumption stated) or `T3` (inferred from video). The Riot API has no wave-state field, so
a coach that confidently says "you should have frozen" is guessing. Ours says *"your CS rate and
position imply you were pushing (derived) — if that's right, the freeze was available."* You can
check it. That's the point.

**The model is never trusted with facts.** Item stats, costs and champion numbers are injected from
a patch-pinned database built by diffing DataDragon between versions. Then a validator scans the
output and rejects any item or champion that doesn't exist in your patch — so it will never tell you
to build something that was removed six patches ago.

Full technical detail: **[docs/ARCHITECTURE.md](docs/en/ARCHITECTURE.md)**

---

## Project status

> **Usable today, with or without AI.** The data pipeline works end to end. With a model available —
> local or on a free tier — four specialist analysts run in parallel over the already-measured facts
> and a head coach unifies them. With no model at all, the same command delivers the full
> deterministic report. The footer always says which of the two you got.
>
> `riftcoach web` opens the report in your browser with the advantage curve, and if a replay is
> playing, clicking a finding makes the League client seek to it.

| Stage | Status |
|---|---|
| 0 · Riot client + cache + fixtures | ✅ |
| 1 · Timeline distillation → `MatchFacts` + render | ✅ |
| **6 · Advantage engine** (chess-engine style evaluation) | ✅ |
| 2 · DataDragon sync | ✅ |
| **2 · Benchmarks + no-AI report** (`riftcoach analyze`) | ✅ |
| **3 · Model router + analyst passes** | ✅ |
| **4 · Eval harness + provider compatibility matrix** | ✅ |
| **5 · Web UI + replay seeking** | ✅ |
| 7 · Vision enrichment + Tauri packaging | ☐ |

**576 tests**, `ruff` and `mypy --strict` clean. Numbers measured over 16 real SR matches:

| | |
|---|---|
| Token reduction | **168x** (~214,000 → ~1,275 per match) |
| Cache compression | 19x (2.5 MB timeline → 180 KB) |
| Critical errors detected | 1.5 per player per match (calibrated on 1,013 samples) |

---

## Contributing

**You do not need to be a Python developer to make the most valuable contribution to this project.**

### If you're a high-elo player — write coaching knowledge

`knowledge/principles/` is plain Markdown. A well-written file on bounce mechanics or trading stance
improves every single report the tool generates, forever. This is worth more than most code PRs.

```markdown
---
id: waves-bounce-mechanics
applies_to: {roles: [TOP, MIDDLE], phases: [early]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error]
---
A wave bounces when...
```

Start with [`CONTRIBUTING.en.md`](CONTRIBUTING.en.md) → *"Writing principles"*.

### If you want to improve the AI's output — edit prompts

`analysis/prompts/*.md` are Markdown files, not buried string literals. Change one, run
`uv run python evals/run.py`, and the harness tells you whether you made it better or worse against
30 human-annotated matches. Prompt PRs are welcome and are reviewed on eval deltas, not opinion.

### If you're a Python developer

Good first issues, roughly in dependency order:

- **`parse/waves.py`** — improve the wave-state proxy. Currently four coarse states. Anyone who
  makes this meaningfully sharper improves the highest-value coaching category in the product.
- **`parse/deaths.py`** — more tactical context per death.
- **`llm/providers/`** — add a free provider. The interface is one class.
- **`vision/rois.py`** — HUD crop regions for non-1080p resolutions and ultrawide.
- **`evals/golden/`** — annotate a match. No code required, enormous value.

### If you just want to help right now

Run it on your own games and open an issue when the advice is wrong. Attach the report. Bad findings
are the most useful bug reports this project can receive — and because every claim carries its
evidence and its tier, they're actually diagnosable.

### Ground rules

1. **Nothing that runs during a live game.** Non-negotiable, no exceptions, no "but what if it's
   just an overlay." PRs touching this are closed.
2. **No feature that requires payment.** Every capability needs a local path and a free-cloud path.
3. **No scraping third-party sites** (op.gg, u.gg, porofessor). Riot API and DataDragon only.
4. **Every claim the AI makes must be anchored and cited.** If it can't cite evidence, it's a bug.

---

## FAQ

**Do I need a GPU?** No. Free cloud tiers work well, and the no-AI statistical report works with
nothing at all.

**Does my data leave my machine?** Only if you choose a cloud provider. Set `privacy_mode: strict`
and it is enforced at the router level — cloud providers are filtered out before anything else is
considered.

**Can it read `.rofl` replay files?** It reads their metadata. The gameplay payload is encrypted and
the key isn't recoverable afterwards — anyone claiming to extract positional data from a raw `.rofl`
is mistaken. We use the Match-v5 timeline for data and the League client for playback, which gets
you strictly more.

**Will it be outdated after the next patch?** No. Patch facts are synced from DataDragon
automatically and a validator rejects anything that doesn't exist in your patch. That's why there's
no fine-tuned model — [here's the full argument](docs/en/04-knowledge-base.md).

**Does it work for ARAM / Arena?** Summoner's Rift first. The parser handles other queues without
crashing, but the coaching principles are SR-specific.

---

## License

Code: **AGPL-3.0**. Coaching corpus (`knowledge/principles/`): **CC-BY-SA-4.0**.

RiftCoach AI isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or
anyone officially involved in producing or managing Riot Games properties. League of Legends and
Riot Games are trademarks or registered trademarks of Riot Games, Inc.
