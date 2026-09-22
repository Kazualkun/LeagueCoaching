# §4 — Knowledge Base: Fine-Tuning vs. RAG / Dynamic Prompts

[🇧🇷 Português](../04-knowledge-base.md) · 🇺🇸 English

## 4.1 Verdict

**RAG + dynamic prompt assembly. No knowledge fine-tune. Not now, not later.**

A small LoRA for *output formatting* is a defensible v2 optimisation and is discussed in §4.6, but
it is a latency/token optimisation, not a knowledge mechanism, and the project ships without it.

---

## 4.2 Why fine-tuning loses — five independent reasons

Any one of these is sufficient. Together they close the question.

### R1 — The refresh cadence is mathematically incompatible

Patches land roughly every two weeks. A QLoRA cycle that actually improves anything is: curate the
delta dataset → train → eval against a regression suite → quantise → convert to GGUF → publish →
every user re-downloads several GB. That is days of maintainer work and gigabytes of bandwidth, on
a two-week clock, forever, for a volunteer project. RAG's equivalent refresh is one HTTP GET against
DataDragon's `versions.json` and it completes in under a second.

### R2 — Fine-tuning teaches behaviour, not facts

This is the fundamental one. Fine-tuning reliably changes *how* a model responds — tone, format,
structure, task framing. It is an unreliable and lossy way to install *facts*, and the facts it does
install are unfixable without retraining. Our volatile knowledge is entirely factual: item stats,
costs, build paths, champion ratios, death-timer coefficients, objective respawn timers. Facts
belong in context, where they are auditable, patch-pinned, and correctable by editing a row.

### R3 — The dataset does not exist, and cannot be cleanly built

"Fine-tune on high-elo decision data" assumes a labelled corpus of `(game state → correct decision)`
pairs. There is no such public dataset. Constructing one means either:

- **Scraping coaching VODs / commentary** — a rights problem the project cannot absorb, and the
  labels are prose, not decisions; or
- **Mining Challenger replays** — you get *what* strong players did, with no counterfactual and no
  causal label. Outcome-based labelling is hopelessly confounded: a Challenger player dying at 14:22
  is not evidence that dying at 14:22 is correct; or
- **Distilling from a larger model** — which caps quality at the teacher, adds no knowledge the
  teacher did not have, and is strictly worse than just *calling the teacher*, which our free cloud
  tier already does.

This reason alone ends the debate. The bottleneck was never the training; it was always the labels.

### R4 — A LoRA cannot deploy to the cloud path

C2 requires the app to work on a potato laptop via Gemini or Groq. You cannot ship a LoRA to those
endpoints. So a fine-tune produces **two divergent quality profiles** requiring two prompt sets, two
eval suites and two sets of bug reports — for the *minority* of users who have the GPU. The
maintenance cost lands exactly where the benefit does not.

### R5 — Prompt-side knowledge is debuggable; weight-side knowledge is not

When the coach says something wrong, RAG lets you print the exact retrieved context and see the bad
row. With a fine-tune, "why did it say Eclipse gives omnivamp" has no answer short of a training
audit. For an OSS project relying on community bug reports, debuggability *is* the feature.

### What we lose by not fine-tuning, honestly

Fine-tuning would genuinely buy: shorter prompts (the schema and the coaching voice could be baked
in, saving ~900 tokens/call), more consistent tone, and better JSON adherence on small models. Those
are real. They are worth roughly 15% of latency and some polish — against the five reasons above.
Structured-output constraints (§1.5) already solve the JSON-adherence part for free.

---

## 4.3 The knowledge architecture — three layers

The word "RAG" implies one thing. We actually need three, with different volatility and different
retrieval mechanics:

```
L1  PRINCIPLES         patch-independent   human-curated   semantic retrieval   ~120 chunks
L2  PATCH FACTS        every ~2 weeks      auto-synced     deterministic lookup ~400 items
L3  BENCHMARKS         every patch         CI-generated    deterministic lookup ~2 MB parquet
```

### L1 — Principles corpus (`knowledge/principles/*.md`)

Patch-independent fundamentals. These change on a multi-year timescale, not a biweekly one:

```
knowledge/principles/
├── waves/slow-push.md          # what creates it, cannon timing, when it is correct
├── waves/freezing.md           # the ~3-caster threshold, when to break
├── waves/crashing-and-recall.md
├── waves/bounce-mechanics.md
├── trading/stance-and-spacing.md
├── trading/minion-aggro.md
├── trading/level-spike-windows.md   # 2, 3, 6, 11, 16 and why
├── macro/tempo-and-prio.md
├── macro/objective-setup.md         # the 60-90s vision window before spawn
├── macro/crossmap-and-tradeoffs.md
├── economy/recall-thresholds.md     # component breakpoints, not item breakpoints
├── economy/death-timers.md          # the actual BRW formula
├── vision/deep-vs-defensive.md
└── fights/target-selection.md
```

Each file is front-mattered with retrieval keys:

```markdown
---
id: waves-slow-push
applies_to: {roles: [TOP, MIDDLE, BOTTOM], phases: [early, mid]}
triggers: [wave_proxy=PUSHING_TO_ENEMY, recall_error, roam_window]
tier: T1_PRINCIPLE
---
A slow push is created by killing the enemy wave slightly slower than it arrives...
```

**This is the project's primary contribution surface.** A Master-tier player who cannot write Python
can write `waves/bounce-mechanics.md`, and that is a materially more valuable contribution than most
code PRs. Design the repo so that is the easiest possible PR to make (§5).

### L2 — Patch facts (auto-synced, zero human maintenance)

Sources, in preference order:

```python
DDRAGON = "https://ddragon.leagueoflegends.com"
#   /api/versions.json                     -> ["15.18.1", "15.17.1", ...]
#   /cdn/{v}/data/en_US/item.json          -> stats, gold, from/into, tags
#   /cdn/{v}/data/en_US/champion/{c}.json  -> base stats, per-level growth, ability ratios
#   /cdn/{v}/data/en_US/runesReforged.json
#   /cdn/{v}/data/en_US/summoner.json      -> summoner spell cooldowns

CDRAGON = "https://raw.communitydragon.org/latest"
#   /plugins/rcp-be-lol-game-data/global/default/v1/items.json   -> richer than DDragon
#   /plugins/rcp-be-lol-game-data/global/default/v1/perks.json
```

DataDragon is the authoritative, versioned baseline. CommunityDragon fills gaps (it carries fields
DDragon omits and is updated faster) but is community-run and only reliably exposes `latest` — so:
**DDragon for anything patch-pinned, CDragon for enrichment only.**

Normalised into SQLite:

```sql
CREATE TABLE item  (patch TEXT, item_id INT, name TEXT, gold_total INT, gold_base INT,
                    stats_json TEXT, builds_from TEXT, builds_into TEXT, tags TEXT,
                    PRIMARY KEY (patch, item_id));
CREATE TABLE champion (patch TEXT, key INT, name TEXT, stats_json TEXT, spells_json TEXT,
                    PRIMARY KEY (patch, key));
CREATE TABLE rune  (patch TEXT, perk_id INT, name TEXT, tree TEXT, desc TEXT,
                    PRIMARY KEY (patch, perk_id));
CREATE TABLE patch_diff (from_patch TEXT, to_patch TEXT, entity_type TEXT, entity_id INT,
                    field TEXT, old_value TEXT, new_value TEXT);
```

### The patch-notes trick — diff DataDragon instead of scraping notes

Everyone's instinct is to scrape Riot's patch-notes page. It is HTML, it is restructured regularly,
it is prose that still needs an LLM to interpret (hallucination risk), and scraping it is ToS-grey.

**Instead: diff two DataDragon versions and generate the changelog yourself.**

```python
# riftcoach/knowledge/patchdiff.py
def diff_patches(old: str, new: str) -> list[PatchChange]:
    """Structural diff of item.json / champion.json between two DDragon versions.
    Produces machine-generated, literally-true change records."""
    # -> PatchChange(entity="Luden's Companion", field="stats.AP",
    #                old=100, new=95, patch="15.18.1")
```

Properties this gives you, none of which scraping gives you:

- **Literally true.** It is a diff of the authoritative data files, not an interpretation of prose.
- **Complete.** Catches silent changes Riot did not write up.
- **Machine-readable.** Feeds directly into prompts and into the validator.
- **Zero maintenance.** No selectors to fix when the notes page is redesigned.
- **Retroactive.** All historical versions are on the CDN, so you can generate any diff on demand.

Rendered into a prompt slice:

```
PATCH 15.18.1 CHANGES RELEVANT TO THIS MATCH:
- Luden's Companion: AP 100 -> 95, total cost 3200 -> 3100
- Orianna: base armour 20 -> 22
- Infernal Drake: bonus AD/AP 4% -> 3%
```

Only changes touching entities *actually present in the match* are injected. Usually 0–4 lines.

### L3 — Benchmarks

Percentiles for `cs@10`, `gd@10`, `vision/min`, `dpm`, `deaths`, `time_dead_pct`, by
`(role, rank_tier, patch_major)`.

**Do not scrape op.gg/u.gg.** ToS-violating, fragile, and unnecessary. Generate them:

- A maintainer-run GitHub Action samples random matches per rank tier through the Riot API
  (`league-v4` → `match-v5`), aggregates to percentiles, and commits
  `knowledge/benchmarks/{patch_major}.parquet` (~2 MB) as a release asset.
- Only aggregates are published — no PUUIDs, no per-player rows. Compliant and privacy-clean.
- Fallback: percentiles computed from the user's own last 50 games, so the feature works on day one
  and for off-meta roles the bundled table misses.

Benchmarks are what turn "your CS was low" into "61 CS@10 puts you at the 28th percentile for
Emerald mid, where p50 is 68" — the difference between an observation and a coachable target.

---

## 4.4 Retrieval mechanics — mostly *not* embeddings

The common mistake is to embed everything and vector-search it. **Most of our retrieval is a lookup
with a known key.** Given `MatchFacts`, we know exactly which items were bought, which champions
played, which role, which rank. Nothing needs to be *searched*.

| Layer | Mechanism | Why |
|---|---|---|
| L2 patch facts | `SELECT ... WHERE patch=? AND item_id IN (...)` | Exact keys are known. Embeddings would be strictly worse and could retrieve the wrong item. |
| L3 benchmarks | Parquet lookup by `(role, tier)` | Same. |
| L1 principles | **Hybrid: trigger-tag filter → embedding rank** | This is genuinely fuzzy. |

L1 retrieval, concretely:

```python
# riftcoach/knowledge/retrieve.py
def retrieve_principles(facts: MatchFacts, analyst: str, k: int = 4) -> list[Chunk]:
    # 1. Hard filter on structured triggers emitted by the parser:
    #    {"wave_proxy=PUSHING_TO_ENEMY", "recall_error", "role=MIDDLE", "phase=early"}
    #    Typically cuts 120 chunks -> ~15.
    # 2. Embed the analyst's question + the top findings-so-far, cosine-rank the survivors.
    # 3. Return top-k, hard-capped at 700 tokens total.
```

Stack: **`fastembed` (ONNX, BGE-small-en-v1.5, ~130 MB, CPU) + `sqlite-vec`.**

Rationale: `fastembed` needs no PyTorch. That matters enormously — requiring a torch install turns a
30-second `uv sync` into a multi-gigabyte CUDA-matching ordeal on Windows, which is exactly the
user this project is for. `sqlite-vec` is a single extension on the SQLite file we already have. No
Chroma (heavy transitive deps), no external vector service (violates C2/C3), no `sentence-
transformers` (drags in torch).

At 120 chunks the index is trivially small; the whole thing could be brute-forced in numpy. Use
`sqlite-vec` anyway so the corpus can grow to thousands of community-contributed chunks without a
rewrite.

---

## 4.5 Anti-staleness enforcement — three mechanisms

RAG supplies the right facts. These three make sure the model *uses* them.

### M1 — Closed-world system prompt

```
You may state numeric values ONLY if they appear verbatim in the PATCH FACTS block.
If a number you need is absent, say "(exact value not in context)" and reason
qualitatively. You have no reliable memory of current item stats; your training data
predates this patch. Never name an item, rune or champion ability that does not
appear in the provided context.
```

### M2 — `FactValidator` (post-generation, deterministic)

```python
# riftcoach/knowledge/validator.py
def validate(report: CoachingReport, patch_db: PatchDB) -> list[Violation]:
    """Runs on EVERY generated report before it reaches the UI."""
    # 1. NER-lite: match all item/champion/rune names via an Aho-Corasick automaton
    #    built from the patch DB (fast, exact, no model).
    # 2. Any entity not in patch_db[report.patch]  -> HALLUCINATED_ENTITY
    # 3. Any entity valid in an OLDER patch but removed -> STALE_ENTITY  (the
    #    high-value catch: "build Ludens Echo" / "rush Divine Sunderer")
    # 4. Numerics adjacent to an entity, compared against the DB -> STALE_STAT
    # 5. Any Finding whose timestamp_ms is outside [0, duration] -> BAD_ANCHOR
```

Violations trigger one regeneration with the violations fed back. Still failing → the finding is
dropped and the model is recorded in `compat.jsonl` (§1.5). **Never show an unvalidated finding.**

Mechanism 3 is the one that earns its keep. A model trained before the patch will confidently
recommend removed items. That is the single most credibility-destroying failure a LoL coach can
have, and an Aho-Corasick scan against a 400-row table catches ~all of it for microseconds.

### M3 — Patch pinning and expiry

`CoachingReport.patch` is stored. The UI shows *"generated for patch 15.18.1"* and, if the live
patch has moved on, a banner: *"this analysis predates patch 15.19 — itemisation advice may be
outdated."* Cheap, honest, and it prevents the archive from quietly rotting.

---

## 4.6 The one fine-tune that might be worth it (v2, optional)

A **style/format LoRA**, explicitly *not* a knowledge LoRA:

- **Training data:** ~2,000 `(EvidencePacket → validated CoachingReport)` pairs harvested from
  Gemini Flash runs that passed `FactValidator` cleanly, plus human-rated quality scores.
- **What it teaches:** the output schema, the coaching voice, the severity calibration, the habit of
  citing evidence tiers. All patch-independent.
- **What it must never teach:** any item stat, any champion number, any "current meta" claim.
- **Method:** Unsloth + QLoRA, rank 16, on Qwen3-8B. Roughly 2 GPU-hours.
- **Payoff:** lets the 8 GB tier drop the ~900-token schema preamble and behave like the 14B tier.
- **Guard:** the fine-tuned model runs through the *same* `FactValidator`. If a style LoRA starts
  hallucinating items, the validator catches it and we delete the LoRA.

Ship this only after the eval harness (§5) can prove it is better. Without that proof it is a
liability that reintroduces every problem in §4.2.
