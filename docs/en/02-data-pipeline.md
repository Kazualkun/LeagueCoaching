# §2 — Data Pipeline & Token Optimisation

[🇧🇷 Português](../02-data-pipeline.md) · 🇺🇸 English

## 2.1 The problem, quantified

A 32-minute Summoner's Rift game:

| Artefact | Raw size | ~Tokens |
|---|---|---|
| `match-v5/matches/{id}` | ~120 KB | ~30,000 |
| `match-v5/matches/{id}/timeline` | 1.8–3.2 MB | **450,000–800,000** |

The timeline is 32 frames (one per 60 s) of 10 `participantFrames`, plus 800–2,000 events. The bulk
is not the interesting part — it is `victimDamageReceived` / `victimDamageDealt` arrays (every
`CHAMPION_KILL` carries a full per-source damage breakdown, often 20+ objects), `championStats`
(every frame carries 20 combat stats for all 10 players), and `ITEM_DESTROYED`/`ITEM_UNDO` noise.

**Target: ≤ 4,500 tokens for the complete evidence packet.** That is a ~150× reduction, and it must
*increase* usable signal, not merely shrink the payload.

---

## 2.2 What we actually extract (exact field list)

### From `match-v5/matches/{id}` — `info.participants[]`

Keep, for the focus player and the lane opponent:

```
puuid, championName, teamPosition, teamId, win, champLevel
kills, deaths, assists
totalMinionsKilled, neutralMinionsKilled          # sum -> cs
goldEarned, goldSpent
totalDamageDealtToChampions, damageDealtToTurrets, damageDealtToObjectives
totalDamageTaken, totalHealsOnTeammates, totalTimeSpentDead
visionScore, wardsPlaced, wardsKilled, visionWardsBoughtInGame, detectorWardsPlaced
item0..item6                                      # -> resolved names + final build order
summoner1Id, summoner2Id
perks.styles[].selections[].perk, perks.statPerks  # -> resolved rune names
challenges.*                                      # SELECTIVELY — see below
```

`challenges` is a goldmine that most parsers ignore. Riot already computes dozens of the metrics you
would otherwise derive badly. Take these and skip the derivation:

```
laneMinionsFirst10Minutes, maxCsAdvantageOnLaneOpponent, maxLevelLeadLaneOpponent
earlyLaningPhaseGoldExpAdvantage, laningPhaseGoldExpAdvantage
controlWardsPlaced, wardTakedowns, visionScorePerMinute, stealthWardsPlaced
killParticipation, teamDamagePercentage, damageTakenOnTeamPercentage
goldPerMinute, damagePerMinute
turretPlatesTaken, takedownsFirstXMinutes
soloKills, killsNearEnemyTurret, deathsByEnemyChamps
skillshotsDodged, skillshotsHit, enemyChampionImmobilizations
dodgeSkillShotsSmallWindow, saveAllyFromDeath
epicMonsterSteals, dragonTakedowns, baronTakedowns, riftHeraldTakedowns
immobilizeAndKillWithAlly, pickKillWithAlly
```

For the **other eight players**, keep only: `championName`, `teamPosition`, `teamId`, `kills`,
`deaths`, `assists`, `goldEarned`, `totalDamageDealtToChampions`, `item0..6`. Everything else is
discarded. This perspective filter alone is a ~4× cut.

### From `timeline` — `info.frames[].participantFrames`

Keep **only for the focus player, the lane opponent, and both junglers**, and only these fields:

```
totalGold, xp, level, minionsKilled, jungleMinionsKilled, position{x,y}
```

Explicitly **drop**: `currentGold` (noisy, derivable at recall points), `championStats` (20 fields ×
10 players × 32 frames = ~6,400 numbers of near-zero coaching value), `damageStats` (keep only the
end-of-game aggregate from `match`), `timeEnemySpentControlled`.

For the remaining six players keep `totalGold` only, and aggregate it immediately to a per-team
total per frame. Team gold *differential* over time is high-signal (it shows tempo swings); ten
individual gold curves are not.

### From `timeline` — `info.frames[].events[]`

| Event type | Action |
|---|---|
| `CHAMPION_KILL` | **Keep, enriched.** Drop `victimDamageReceived`/`victimDamageDealt` arrays entirely — but first reduce them to `top_damage_source: championName` and `damage_share: float`. One string + one float replaces ~2 KB. |
| `ITEM_PURCHASED` | Keep for focus + opponent only. Resolve `itemId` → name (§4). Collapse into a **build path with timestamps**. |
| `ITEM_SOLD` / `ITEM_DESTROYED` / `ITEM_UNDO` | **Drop**, except: an `ITEM_UNDO` within 3 s of a purchase means the purchase never happened — apply it, then drop both. `ITEM_DESTROYED` of a component is just a completion; already implied by the completed item. |
| `SKILL_LEVEL_UP` | **Collapse to one string**: `"Q>W>E>Q>Q>R>..."`. 18 events → 1 field. |
| `LEVEL_UP` | **Drop** — fully derivable from `participantFrames.level`. |
| `WARD_PLACED` / `WARD_KILL` | Aggregate to counts by type and by 5-minute bucket. Keep individual events **only** within ±60 s of an objective spawn (that is the coachable slice). |
| `BUILDING_KILL` | Keep all. ~11–22 events, each high-value. |
| `TURRET_PLATE_DESTROYED` | Keep all (≤ 25 events; plate gold is a real early-tempo signal). |
| `ELITE_MONSTER_KILL` | Keep all. Enrich with `monsterType`, `monsterSubType` (drake element). |
| `DRAGON_SOUL_GIVEN`, `FEAT_UPDATE` | Keep. |
| `OBJECTIVE_BOUNTY_PRESTART/START` | Keep — explains gold swings the model would otherwise misattribute. |
| `CHAMPION_SPECIAL_KILL` | Keep only `FIRST_BLOOD` and multikills. |
| `PAUSE_END` | Keep — it defines true `t=0` for clock calibration (§3). |
| `GAME_END` | Keep. |
| Everything else | Drop. |

---

## 2.3 The three transforms that do the real work

### Transform 1 — Coordinate → named map zone

Raw positions are `x, y` in roughly `[0, 15000]`. **An LLM cannot reason about `(7432, 8891)`.** It
can reason brilliantly about `"MID_RIVER"`. This transform simultaneously cuts tokens (~9 chars → a
short enum) *and* is the single largest accuracy improvement in the pipeline.

```python
# riftcoach/parse/zones.py
MAP_MAX = 14870      # SR world extent used by match-v5 positions

class MapZone(str, Enum):
    BLUE_BASE = "BLUE_BASE"; RED_BASE = "RED_BASE"
    TOP_LANE_BLUE = "TOP_BLUE"; TOP_LANE_MID = "TOP_MID"; TOP_LANE_RED = "TOP_RED"
    MID_LANE_BLUE = "MID_BLUE"; MID_LANE_MID = "MID_MID"; MID_LANE_RED = "MID_RED"
    BOT_LANE_BLUE = "BOT_BLUE"; BOT_LANE_MID = "BOT_MID"; BOT_LANE_RED = "BOT_RED"
    TOP_RIVER = "TOP_RIVER"; BOT_RIVER = "BOT_RIVER"
    BARON_PIT = "BARON_PIT"; DRAGON_PIT = "DRAGON_PIT"
    BLUE_TOP_JUNGLE = "BLUE_TOPJG"; BLUE_BOT_JUNGLE = "BLUE_BOTJG"
    RED_TOP_JUNGLE = "RED_TOPJG"; RED_BOT_JUNGLE = "RED_BOTJG"
    BLUE_BUFF_BLUE = "BLUE_BLUEBUFF"; BLUE_BUFF_RED = "BLUE_REDBUFF"
    RED_BUFF_BLUE = "RED_BLUEBUFF"; RED_BUFF_RED = "RED_REDBUFF"

# Polygon lookup, not a grid: lanes are diagonal bands and a square grid
# mislabels river/jungle constantly. Prebuilt shapely STRtree, ~3 us/query.
_TREE = build_zone_index(ZONE_POLYGONS)

def to_zone(x: int, y: int) -> MapZone: ...

def side_relative(zone: MapZone, team_id: int) -> str:
    """'ENEMY_JUNGLE_TOPSIDE' / 'OWN_JUNGLE_BOTSIDE' — ALWAYS present this to the
    model instead of BLUE/RED. The model should never have to remember which side
    the focus player is on; that is exactly the kind of bookkeeping it gets wrong."""
```

The `side_relative` step is not cosmetic. Every mistake an LLM makes about "you overextended" traces
back to it losing track of which half of the map is dangerous. Pre-resolving it removes the failure
mode entirely.

### Transform 2 — Death enrichment

A raw `CHAMPION_KILL` is ~2 KB and says almost nothing coachable. Each death becomes **one line of
~35 tokens** carrying the full tactical context, computed in Python from the surrounding frames:

```python
class DeathContext(BaseModel):
    n: int                          # death number
    t: str                          # "14:22"
    zone: str                       # "ENEMY_JUNGLE_TOPSIDE" (side-relative)
    killers: list[str]              # ["LeeSin", "Ahri"]
    top_damage_source: str
    gold_swing: int                 # shutdown given + bounty lost + enemy gold gained
    death_timer_s: float            # BRW formula, patch-pinned table (§4)
    allies_within_2000u: int        # from nearest participantFrames
    enemies_unaccounted: int        # enemies NOT visible on the last minimap read (T3, vision only)
    objective_window: str | None    # "DRAKE_SPAWN_IN_38s" | "BARON_UP" | None
    wave_proxy: str                 # "PUSHING_TO_ENEMY" (T2) — see Transform 3
    had_vision: bool                # own ward alive within 1600u at t (T2)
    gold_at_death: int              # currentGold: did they die holding 1400 unspent gold?
    summs_available: list[str]      # flash/tp tracked by last-cast + patch cooldown (T2)
```

Rendered: 

```
D3 14:22 ENEMY_JG_TOPSIDE killers=LeeSin,Ahri dmg=Ahri swing=-450 timer=23s
   allies=0 obj=DRAKE_IN_38s wave=PUSHING_TO_ENEMY vision=NO gold_held=1150 summs=[]
```

Nine deaths ≈ 315 tokens, and *every* one of them is directly coachable. This is the highest
signal-per-token structure in the entire system — a model given these lines produces specific,
correct advice ("three of your nine deaths were in enemy topside jungle with no ward and no flash,
all while a drake was under 60 s from spawning") without needing to do any inference we could have
done for it.

`summs_available` deserves a note: Riot's timeline has no "summoner spell cast" event. Track Flash
via `CHAMPION_KILL` positional jumps? Unreliable. **Honest answer: this field is T3 and is populated
only in Modes B/C from HUD vision.** In Mode A it is `None`, and the prompt must never claim
summoner state from telemetry alone. Mark it, do not fake it.

### Transform 3 — Wave-state proxy (and its honest limits)

There is no wave data in the Riot API. Full stop. What is derivable:

```python
def wave_proxy(frames, pid, t_ms) -> tuple[str, str]:
    """Returns (state, assumption). ALWAYS EvidenceTier.T2 or T3 — never T1."""
    # cs_rate: minionsKilled delta over the surrounding 60s window
    # position band: own-half / mid / enemy-half of the lane
    # A player in the enemy-half band with a high cs_rate is almost certainly
    # pushing; in the own-half band with a near-zero rate, either frozen or roaming.
```

Four states: `PUSHING_TO_ENEMY`, `HOLDING_MID`, `HELD_IN_OWN_HALF`, `NOT_IN_LANE`. That is the
honest resolution limit of telemetry. True freeze/slow-push/crash discrimination requires seeing
minion counts, which means vision (Mode B/C) — and there it is still T3.

**Do not paper over this.** A coach that confidently says "you should have frozen here" on evidence
it does not have is worse than one that says "your CS rate and position suggest you were pushing
into a warded lane (derived, not measured) — if that is right, the freeze was available." The second
is trustworthy; the first gets the project a reputation for hallucinating.

---

## 2.4 The `MatchFacts` intermediate representation

```python
# riftcoach/parse/facts.py
class LaneSnapshot(BaseModel):
    """Per-minute focus-vs-opponent deltas. Deltas, never absolutes: the model
    cares about the gap, and deltas compress far better (small ints, many zeros)."""
    minute: int
    gd: int          # gold diff, rounded to nearest 25
    xpd: int         # xp diff, rounded to nearest 50
    csd: int         # cs diff
    zone: str        # focus player's side-relative zone

class RecallEvent(BaseModel):
    t: str
    gold_on_back: int
    bought: list[str]              # resolved item names
    wave_proxy_before: str
    seconds_lost: float            # time from recall to lane re-arrival (T2, from positions)
    was_forced: bool               # preceded by a death or <25% hp proxy

class ObjectiveEvent(BaseModel):
    t: str; kind: str; subtype: str | None; taken_by_team: bool
    contested: bool                # any champion kill within ±25 s
    prep_wards_60s: int            # own wards placed near the pit in the preceding 60 s
    team_gold_diff_at: int
    focus_player_zone: str         # where WAS the player when this happened?

class MatchFacts(BaseModel):
    # identity
    match_id: str; patch: str; queue: str; duration_s: int; win: bool
    focus: PlayerSummary; opponent: PlayerSummary
    team: list[PlayerSummary]; enemy: list[PlayerSummary]   # reduced
    # derived headline metrics (ALL computed in Python — tier T1)
    cs_at_10: int; cs_at_14: int; csd_at_10: int; gd_at_10: int; gd_at_14: int; xpd_at_10: int
    dpm: float; damage_share: float; kill_participation: float; gold_per_min: float
    vision_score_per_min: float; control_wards: int
    time_dead_s: int; time_dead_pct: float
    plates_taken: int; solo_kills: int
    # series
    lane_series: list[LaneSnapshot]          # 0..18, then every 3 min
    team_gold_diff_series: list[int]         # per minute, whole game
    # events
    deaths: list[DeathContext]
    kills_by_focus: list[KillContext]        # same shape, inverted
    recalls: list[RecallEvent]
    objectives: list[ObjectiveEvent]
    build_path: list[tuple[str, str]]        # [("06:12", "Lost Chapter"), ...]
    skill_order: str                         # "Q>W>E>Q>Q>R>..."
    runes: RuneSet; summoners: tuple[str, str]
    # vision-derived, Modes B/C only
    observations: list[FrameObservation] = []
```

### Series thinning

`lane_series` is per-minute for 0–18 min, then every 3 minutes. Rationale: laning phase decisions
are minute-scale; late-game decisions are event-scale and are captured by `objectives`/`deaths`
anyway. A 40-minute game yields 18 + 8 = 26 snapshots, not 40.

Round aggressively. `gd: 1247` → `gd: 1250`. The model's judgement is identical and you save
characters across hundreds of numbers. Never round *timestamps* — they are join keys.

---

## 2.5 Rendering to tokens

**Do not send JSON to the model.** JSON spends ~40% of its tokens on braces, quotes and repeated
keys. Render the IR as a compact, tabular, header-once format:

```
=== MATCH 15.18.1 | RANKED_SOLO | 32:14 | LOSS ===
YOU: Orianna MIDDLE | 7/9/11 | 221cs | 612 DPM | 28.1% dmg | KP 64%
OPP: Syndra MIDDLE  | 11/4/8 | 248cs | 744 DPM | 33.0% dmg

LANE (vs Syndra) — gd/xpd/csd, your zone
min  1   2   3   4   5   6   7   8   9  10  11  12  13  14
gd   0 -25 -50   0 -75 -150 -125 -300 -275 -450 -400 -625 -900 -1100
xpd  0   0 -50 -50 -100 -150 -100 -250 -200 -350 -300 -500 -700 -850
csd  0  -2  -3  -1  -4  -6  -5  -9  -8 -13 -12 -16 -21 -24
zone MID MID MID MID MID MID MID MID MID MID MID OWN OWN OWN

BENCHMARKS (MIDDLE, EMERALD)      you    p50    p90
cs@10                              61     68     79
gd@10                            -450    +12   +680
vision/min                       0.71   0.88   1.24
time dead                       14.2%   9.8%   6.1%

DEATHS (9)
D1 08:41 OWN_MID_LANE killers=Syndra dmg=Syndra swing=-300 timer=14s allies=0
   obj=- wave=PUSHING_TO_ENEMY vision=NO gold_held=700
D2 12:03 ENEMY_JG_BOTSIDE killers=Syndra,Viego dmg=Viego swing=-450 timer=18s
   allies=0 obj=DRAKE_IN_51s wave=HOLDING_MID vision=NO gold_held=150
...

RECALLS (6)
R1 05:12 gold=780 bought=[Lost Chapter] wave=PUSHING_TO_ENEMY lost=19s forced=NO
R2 09:30 gold=1290 bought=[Boots, Control Ward] wave=HELD_IN_OWN_HALF lost=24s forced=YES(death)
...

OBJECTIVES (11)
14:22 DRAGON/INFERNAL enemy contested=YES prep_wards=0 teamgd=-1400 you_were=OWN_MID_LANE
...

BUILD  06:12 Lost Chapter | 09:31 Boots | 11:48 Luden's Companion | ...
SKILLS Q>W>E>Q>Q>R>Q>W>Q>R>...
RUNES  Electrocute / Sudden Impact / Eyeball / Treasure Hunter // Manaflow / Transcendence
```

This renders in ~1,100 tokens for a full game. The equivalent JSON is ~2,800. The equivalent raw
timeline is ~600,000.

### Measured budget

| Component | Tokens |
|---|---|
| System prompt + output schema | ~900 |
| Rendered `MatchFacts` | ~1,100 |
| Patch facts for items/runes in play (§4) | ~500 |
| Retrieved principles (top-4 RAG chunks) | ~700 |
| Benchmark table | ~150 |
| **Total per analyst pass** | **~1,100–1,600** (each analyst sees only its relevant slices) |
| **Full 5-pass analysis** | **~6,500 in / ~2,500 out** |

Comfortably inside every free tier and inside an 8 GB local model at 8k context.

---

## 2.6 Implementation notes

### Caching — the highest-leverage engineering decision

Completed match data is **immutable**. Cache it permanently:

```python
# riftcoach/riot/cache.py — SQLite, zstd level 10
# CREATE TABLE raw (key TEXT PRIMARY KEY, payload BLOB, fetched_at INT, schema_v INT)
```

A 2.5 MB timeline compresses to ~180 KB (JSON is extremely compressible). A 500-match history is
~90 MB. In exchange: re-analysis is instant, developing the parser costs zero API calls, the eval
harness runs offline, and the 20 req/s dev-key limit stops mattering after the first fetch.

Cache the **distilled `MatchFacts` separately**, keyed by `(match_id, puuid, parser_version)`, so a
parser change invalidates only the derived layer and never re-fetches.

### Rate limiting

Dev keys: 20 req/s, 100 req/2 min, **per routing value**, and they **expire every 24 hours** — a
first-class UX problem, not a footnote. Detect a `403` with the key-expiry body and surface a
specific "your dev key expired, regenerate at developer.riotgames.com" message with a deep link.
Prompt users to apply for a Personal API Key (no expiry) once they are past the trial.

Implement a two-window token bucket per `(region, key)` and honour `Retry-After` and the
`X-Rate-Limit-Count` / `X-App-Rate-Limit` response headers rather than guessing:

```python
class RiotLimiter:
    # windows parsed from X-App-Rate-Limit: "20:1,100:120"
    # a 429 sets a hard gate until Retry-After elapses; all coroutines await the same gate
```

### Parser correctness

`parser_version` is baked into the cache key **and** into `CoachingReport`, so a user can tell why
yesterday's report differs from today's. Golden fixtures: commit ~10 anonymised timelines
(`tests/fixtures/`, PUUIDs scrubbed) covering an ARAM game, a remake, a 60-minute game, a game with
a disconnect, and a game where the focus player swapped lanes. Snapshot-test `MatchFacts` against
them. Every one of those edge cases will otherwise crash the parser in production — especially lane
swaps, where `teamPosition` is unreliable and the "lane opponent" must be resolved by *positional
proximity during minutes 2–8*, not by role string.
