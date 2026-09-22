# Contributing to RiftCoach AI

[🇧🇷 Português](CONTRIBUTING.md) · 🇺🇸 English

Thanks for considering a contribution. This document exists so that your first PR is easy to open and
easy to review.

**The most valuable contribution to this project is not code.** If you play at a high rank, skip
straight to [Writing principles](#writing-principles) — one well-written Markdown file on wave
mechanics improves every report the tool will ever generate.

---

## Contents

- [Non-negotiable rules](#non-negotiable-rules)
- [Writing principles](#writing-principles) ← start here if you are not a developer
- [Reporting a bad finding](#reporting-a-bad-finding)
- [Development environment](#development-environment)
- [Contributing code](#contributing-code)
- [The evidence-tier discipline](#the-evidence-tier-discipline)
- [Commits and PRs](#commits-and-prs)
- [Licensing your contribution](#licensing-your-contribution)

---

## Non-negotiable rules

These four are not up for discussion. PRs that violate them are closed, however good the code is.

1. **Nothing that runs during a live game.** No exceptions, no "but what if it's just a passive
   overlay". The interlock is architectural (`replay/guard.py`) and fails closed. See
   [COMPLIANCE.en.md](COMPLIANCE.en.md).
2. **No feature that requires payment.** Every capability needs a local path and a free cloud path.
   If your feature only works with a paid key, it does not go in.
3. **No scraping third-party sites** (op.gg, u.gg, porofessor, lolalytics). Official Riot API and
   DataDragon only. This is not fussiness: scraping violates the terms, breaks on its own, and is
   unnecessary — section 4.3 of the docs shows how to generate the same data from authorized sources.
4. **Every AI claim must be grounded and cited.** If the coach cannot cite the evidence behind a
   sentence, that is a bug, not an acceptable limitation.

---

## Writing principles

`knowledge/principles/` is the project's primary contribution surface. These are plain Markdown
files. **You do not need Python, and you do not need to clone the repo** — you can create the file
straight from the GitHub web interface.

### The format

Each file has frontmatter carrying the retrieval keys, followed by the prose:

```markdown
---
id: waves-bounce-mechanics
applies_to: {roles: [TOP, MIDDLE], phases: [early, mid]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error]
tier: T1_PRINCIPLE
---

A wave bounces when the enemy wave accumulates enough mass to push back on its own...
```

| Field | What it is | Values |
|---|---|---|
| `id` | Unique kebab-case identifier. Convention: `{folder}-{subject}`. | `waves-slow-push` |
| `applies_to.roles` | Roles the principle applies to. Omit for "all". | `TOP` `JUNGLE` `MIDDLE` `BOTTOM` `UTILITY` |
| `applies_to.phases` | Game phases. | `early` `mid` `late` |
| `triggers` | Structured triggers emitted by the parser. This is the hard filter that runs **before** semantic search. | see list below |
| `tier` | Always `T1_PRINCIPLE` for this corpus. | — |

Triggers the rules engine emits today (`riftcoach/analysis/rules.py`, the `TRIGGERS` constant):

```
wave_proxy=PUSHING_TO_ENEMY   wave_proxy=HOLDING_MID   wave_proxy=HELD_IN_OWN_HALF
recall_error   objective_window   death_cluster   vision_gap
gold_hoarding   itemization_gap   tempo_loss   lane_deficit
role=TOP role=JUNGLE role=MIDDLE role=BOTTOM role=UTILITY
phase=early phase=mid phase=late
```

Getting a trigger wrong breaks nothing — it just means the file is not retrieved in that case.
Getting the `id` wrong (reusing an existing one) does break. When in doubt, open the PR anyway and
review will sort it out.

### What makes a good principle

Write for someone who **already knows how to play** and wants to understand why. The audience is a
Gold player reading about a mistake they just made.

- **One concept per file.** `waves/freezing.md` is about freezing. Do not fold bouncing into it.
- **Give the numbers.** "Roughly three caster minions ahead" is useful. "A slightly bigger wave" is
  not.
- **Say when the principle is wrong.** Every LoL principle has an exception, and the exception is
  half the value. A file that says "always freeze when behind" is worse than no file.
- **Patch-independent.** If it becomes false next patch, it is not a principle — it is a patch fact,
  and those come from DataDragon automatically.
- **No undefined jargon.** The first time you use "prio", explain it.

Good length: 200–600 words. Larger files get chunked during retrieval and lose context.

### Where to put it

```
knowledge/principles/
├── waves/        slow-push, freezing, crashing-and-recall, bounce-mechanics
├── trading/      stance-and-spacing, minion-aggro, level-spike-windows
├── macro/        tempo-and-prio, objective-setup, crossmap-and-tradeoffs
├── economy/      recall-thresholds, death-timers
├── vision/       deep-vs-defensive
└── fights/       target-selection
```

Cannot find the right folder? Create one and say why in the PR.

---

## Reporting a bad finding

**This is the most useful bug report this project can receive.** If the coach gave you wrong advice,
open an issue with the *"Bad finding"* template.

Because every claim carries its evidence and its tier, these reports are genuinely diagnosable — you
can tell whether the error was measurement (parser bug), assumption (wrong T2 heuristic), or judgment
(bad prompt). Include:

- The full report, copy-pasted (it contains no PUUID and nothing identifying).
- Which finding is wrong and **why** — your read of the situation.
- The match ID, if you do not mind sharing it.

---

## Development environment

The project uses [uv](https://docs.astral.sh/uv/). It installs the right Python and resolves
everything from `uv.lock`, so your machine matches CI.

```bash
git clone https://github.com/Kazualkun/LeagueCoaching.git
cd LeagueCoaching

uv sync --all-extras --dev     # installs Python 3.11+ and all dependencies
uv run pytest                  # tests run without network, in seconds
uv run ruff check .
uv run mypy
```

**Tests never touch the network.** The fixtures in `tests/fixtures/*.json.gz` are anonymized real
matches. If your test needs network, it is in the wrong place — use `respx` to simulate the Riot API,
as `tests/test_cache_and_client.py` does.

To run against your own matches you need a Riot key (free, at
[developer.riotgames.com](https://developer.riotgames.com)):

```bash
uv run riftcoach auth                      # stored in the OS keychain, never in a file
uv run riftcoach doctor                    # checks the whole configuration
uv run riftcoach fetch "Name#TAG" -n 5
uv run riftcoach analyze "Name#TAG"        # report for the latest match, no AI
```

---

## Contributing code

### Good first issues

Roughly in dependency order:

- **`parse/waves.py`** — improve the wave-state proxy. Today it is four coarse states. Whoever makes
  this meaningfully more accurate improves the single most valuable coaching category in the product.
- **`parse/deaths.py`** — more tactical context per death.
- **`llm/providers/`** — add a free provider. The interface is a single class.
- **`vision/rois.py`** — HUD crop regions for resolutions other than 1080p and ultrawide.
- **`evals/golden/`** — annotate one match. No code needed, and it is the highest-leverage
  contribution available today: the corpus has **two** annotations, both machine-generated.
  While that holds, the measured precision is a floor of the corpus, not of the tool.
  See [`evals/golden/README.md`](evals/golden/README.md).
- **`knowledge/benchmarks/`** — the embedded table is small today. Running the sampling job and
  opening a PR with a fresh patch parquet improves every percentile the report shows.

### What CI requires

Three commands, all mandatory:

```bash
uv run ruff check .    # lint + import sorting
uv run mypy            # strict over core/, parse/ and riot/
uv run pytest
```

`mypy --strict` deliberately covers only `core/`, `parse/` and `riot/`: that is where silent
correctness bugs live. The rest of the code is typed, but not under `strict`.

### Layer boundaries that review enforces

- **`parse/` imports nothing from LLM. `llm/` imports nothing from Riot.** The distillation layer has
  to be testable with zero network and zero models. If that boundary holds, the parser stays correct
  while everything above it changes.
- **Metrics are computed in Python, never by the model.** If you catch yourself asking the LLM to
  add, divide or compare numbers, that computation belongs in `parse/` or `analysis/rules.py`.
- **Prompts are files** (`analysis/prompts/*.md`), not string literals buried in code.

### Style

- Code and comments are written in Portuguese, following what is already there. **No accents in
  docstrings and code comments** — `.md` files use normal accentuation, code does not. If you are
  not comfortable writing Portuguese, write the code in English and say so in the PR; review will
  translate rather than reject.
- Comments explain **why**, not what. The code already says what. Look at `analysis/advantage.py` for
  the tone.
- Lines up to 100 columns (`ruff` will tell you).
- Tests named as sentences: `test_same_lead_matters_less_late`, not `test_wp_2`. A test is an
  argument about correct behavior.

---

## The evidence-tier discipline

This is the rule that generates the most change requests in review, so it is worth reading before
writing code.

Every claim carries a tier:

| Tier | Means | Example |
|---|---|---|
| `T1_MEASURED` | Read directly from Riot telemetry. | "You died at 14:22" |
| `T2_DERIVED` | Computed from telemetry **plus a stated assumption**. | "The wave was pushing — derived from CS pace and position" |
| `T3_INFERRED` | Estimated from vision or heuristics. | "Your Flash was on cooldown — read from the HUD" |

The schema **enforces** this: an `Evidence` with tier T2 or T3 and no `assumption` raises
`ValidationError` at construction. This is not bureaucracy. The Riot API has no wave-state field, and
wave management is the highest-value coaching there is — a system that silently mixes measured CS
with guessed wave state produces advice the user cannot verify.

In practice, when adding a metric, ask: *is this literally a field in Riot's response?* If yes, T1.
If you computed it from Riot fields while assuming something, T2 — **and write down the assumption
you made**, in prose, in `assumption`. Do not write "derived from the data".

---

## Commits and PRs

- **One subject per PR.** A PR that improves `waves.py` and reformats `render.py` along the way gets
  a split request.
- Commit messages in the imperative, explaining why when it is not obvious. Portuguese or English,
  either is fine.
- **Prompt PRs are reviewed by the eval delta, not by opinion.** Run `uv run python evals/run.py` and
  paste the before/after. If your change improved the number, it goes in; if it made it worse, it
  does not, however much better the new text reads.
- **Principle PRs are reviewed by players**, not by developers. Expect discussion about the LoL
  content itself. That is the process working.
- Add a test for every new behavior in `core/`, `parse/` and `riot/`. In the other layers, use
  judgment.

---

## Licensing your contribution

By opening a PR you agree to license your contribution under the license of the part of the
repository it touches:

- **Code** → [AGPL-3.0](LICENSE). It makes the "100% free" promise enforceable: anyone running a
  modified RiftCoach as a hosted service must publish their changes.
- **`knowledge/principles/`** → [CC-BY-SA-4.0](knowledge/principles/LICENSE), so the coaching corpus
  can circulate independently of the code.

We do not ask for a CLA and we will not start.

---

RiftCoach AI is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games
or anyone officially involved in producing or managing Riot Games properties.
