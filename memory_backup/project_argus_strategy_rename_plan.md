---
name: argus_* strategy rename — scope plan (deferred post-5/1)
description: Plan to rename argus_usdjpy/gbpusd/cadjpy → mtf_* (or similar) so "Argus" stays the bot name. Scoped 4/28; deferred until after 5/1 to avoid ceremony naming churn.
type: project
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
## The naming problem

"Argus" is the bot/fleet name overall. But three FX strategies use it as a prefix:
- `argus_usdjpy`
- `argus_gbpusd`
- `argus_cadjpy`

This creates ambiguity — when the user says "Argus is doing X" it's unclear whether they mean the whole bot or just the FX pairs. The package `argus_flow` STAYS (that's where the bot runs); only the per-strategy `argus_<pair>` keys need renaming.

## Why this was deferred from 2026-04-28

- Tonight's session built 7 panels that join on the strategy key (decision engine, drilldown, recommended actions, benchmark alpha, verdict skeleton, operational vetting registry, alpha attribution). One missed rename = strategy disappears from one panel.
- 5/1 ceremony is in 2.5 days. Verdicts depend on consistent strategy naming.
- Doing the rename in a fresh post-5/1 session = no time pressure, full attention on every reference.

## Proposed new prefix (pick one before executing)

| Option | Pros | Cons |
|---|---|---|
| `mtf_*` | Matches existing dashboard label "MTF Trend (4H/1H/5m)"; descriptive of strategy logic | Slightly cryptic |
| `trend_*` | Most readable | Other strategies are also trend-following; not unique |
| `fx_*` | Asset class match | Other FX strategies exist (jpy_pm_short etc) |
| `<greek_god>_*` | Matches Greek family pattern (apollo/hermes/titan) | Forces a name choice; cute factor risks |

**Recommended: `mtf_*`** — already present in the UI, descriptive, unique within the fleet.

## Concrete scope (verified 2026-04-28)

| Surface | File count | Hits | Notes |
|---|---|---|---|
| Code references (string literals "argus_usdjpy" etc) | ~12 files | ~60 hits | Mostly dashboard.py + ops/* prep scripts |
| Memory files | 8 files | varies | Including this one (which can keep "argus_*" as the rename target) |
| canonical_fills.jsonl | 1 file | 4 historical records | argus_usdjpy=4, argus_gbpusd=0, argus_cadjpy=0 |
| Per-strategy log dirs | 3 dirs | already correct | argus_flow/logs/{usdjpy,gbpusd,cadjpy} — not prefixed, no rename needed |
| IBKR client_id assignments | CLAUDE.md | comment only | argus_usdjpy=12, argus_gbpusd=51, argus_cadjpy=53 — text update only |
| `argus_flow.` package imports | 144 | NO CHANGE | Package stays; user wants Argus as the bot name |

## Order of operations (when this runs)

1. **Add a name-mapping shim** in dashboard endpoints — readers normalize old name → new name on read. So historical canonical_fills records keep `argus_usdjpy` on disk but the dashboard treats them as `mtf_usdjpy`. Done first so subsequent steps are non-breaking.

2. **Update writers** — anywhere that emits `strategy="argus_*"` to canonical_fills, per-strategy trades.csv, heartbeat.json, signals.csv, or strategy_actions records, change to emit `mtf_*`. Going forward all NEW data uses the new name.

3. **Update consumers** — every place that joins on the strategy key (operational_vetting.py STRATEGY_REGISTRY, dashboard hardcoded entries in api_strategy_performance, decision engine reasoning text, verdict skeleton _BENCHMARK_MAP if FX gets benchmarks added). The shim from step 1 keeps things working during the transition.

4. **Remove the shim** — once all writers + consumers are updated and the 4 historical records are migrated (a one-shot `sed`-equivalent script over the JSONL). The shim was scaffolding; should not stay long-term.

5. **Update memory files** — search-replace `argus_usdjpy` → `mtf_usdjpy` etc across the 8 memory files that reference these strategies.

6. **Restart fleet** — runners cache PARAMS at boot; the runner_unified will now write `mtf_*` to its outputs only after restart.

## Why this matters

Currently the dashboard's Strategy Performance row labeled "Argus" actually represents the 3-pair MTF system, not the bot. A reader who sees "Argus down 5%" might think the bot has an issue when it just means one strategy family is underperforming. Sharper naming = sharper communication.

## What NOT to do

- Don't rename the `argus_flow` package. The user explicitly said Argus stays as the bot name; the package contains the bot's core machinery.
- Don't rename the IBKR account label or scheduled task names that contain "Argus" — those refer to the whole bot.
- Don't do this rename WITHOUT a name-mapping shim first. Skipping the shim = broken dashboard panels for the duration of the transition.
- Don't do this in the same session as a strategy promotion or kill — naming churn during decisions = bugs.

## Estimated time

60-90 minutes of focused work in a fresh session. NOT a vibe-coding-late-at-night task.

## How to apply this memory

**Why:** the rename is a real cleanup but timing matters. Doing it pre-5/1 risks confusing the verdict ceremony; doing it tired risks missing references. Fresh post-5/1 attention is the right window.

**How to apply:**
- When user asks "should we do the argus rename now?" — check date. Pre-5/1 = defer. Post-5/1 with 60+ min in tank = green light.
- Always pick the new prefix BEFORE starting (don't decide mid-rename).
- Always write the shim first.
- Always restart the fleet after the rename so runners pick up the new name.
