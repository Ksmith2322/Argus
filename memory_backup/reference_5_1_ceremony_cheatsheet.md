---
name: 5/1 ceremony cheat sheet — quick reference for ceremony day
description: Single-page criteria + action reference. Print or open in second window during 5/1, 5/15, 5/31 ceremonies.
type: reference
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
## Run order on ceremony day (≈30 min total)

```
cd c:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m ops.run_5_1_prep            # 1. snapshot
C:/Argus/.venv/Scripts/python.exe -m ops.operational_vetting     # 2. silent strategies vetting
C:/Argus/.venv/Scripts/python.exe -m ops.generate_verdict_skeleton  # 3. skeleton with auto-fills
```

Then open the editor: **http://localhost:8080/verdict_editor**

The editor auto-loads the skeleton, sorts manual-judgment strategies first (the actual decisions), and saves to `argus_flow/logs/verdict_<DATE>.json` on Save.

## Verdict tier vocabulary (8 types)

| Verdict | When |
|---|---|
| **BLOCKED** | Operational vetting failed. Cannot judge edge yet. |
| **OBSERVE** | Insufficient sample (n<5) OR by-design quiet. |
| **KEEP-PAPER** | Valid behavior, modestly positive (5≤n<15, PF 0.95-1.20). Not promotion-ready. |
| **WINNER-CANDIDATE** | Promising paper evidence (n≥15, PF≥1.20, not DEGRADED 14d). |
| **REAL-CANDIDATE** | Eligible for real-money gate review (n≥30, PF≥1.30, HEALTHY 21d). |
| **REWORK** | One specific named fix required. Can apply alongside REDUCE on the live runner. |
| **QUARANTINE** | Concept may be valid, implementation/data untrusted. Stop runner OR research_only. |
| **KILL** | Valid experiment failed OR backtest disproves edge. Stop, archive logs. |

## Quick-decision matrix

```
n < 5                                    →  OBSERVE (auto)
n < 5 + research_only mode               →  OBSERVE (auto)
n < 5 + operational fail                 →  BLOCKED (auto)
event-driven, expected silence           →  OBSERVE (auto)

5 ≤ n < 15 + 0.95 ≤ PF ≤ 1.20            →  KEEP-PAPER
n ≥ 15 + PF ≥ 1.20 + not DEGRADED 14d    →  WINNER-CANDIDATE
n ≥ 30 + PF ≥ 1.30 + HEALTHY 21d         →  REAL-CANDIDATE

n ≥ 30 + PF < 1.0                        →  candidate KILL — but check drilldown first
n ≥ 30 + PF < 1.0 + named fix exists     →  REWORK (with named_fix field filled)
n ≥ 30 + PF < 1.0 + drilldown shows
   salvageable subset (PF≥1.5, n≥10)     →  SCOPE_DOWN action via REDUCE+REWORK
                                            (verdict stays REWORK; action = "scope to <subset>")

dirty trade rate > 5%                    →  QUARANTINE (data hygiene before edge eval)
chronic execution failures               →  QUARANTINE (e.g. gdx_gld TWS socket bug)
```

## Target distribution (sanity check at end)

```
REAL-CANDIDATE:    0-1   (rare; needs n≥30 pre-conversion)
WINNER-CANDIDATE:  1-3   (vix_intraday is most likely)
KEEP-PAPER:        4-8   (the bulk of viable strategies)
REWORK:            ≤5    (more = cull isn't decisive enough)
QUARANTINE:        0-3   (gdx_gld is the canonical case)
OBSERVE:           4-8   (event-driven + thin-sample)
KILL:              2-6   (the deserved cull)
```

If counts fall outside these ranges, the fleet is either too immature (extend monitoring) or too cluttered (more aggressive cull). Document the pattern and adjust the 5-week roadmap.

## Anti-patterns (don't do these)

- **"It might fire next week"** — that's INSUFFICIENT_DATA, not WINNER. Don't be optimistic with verdicts.
- **"It almost works"** — that's REWORK only if there's a specific named fix. Otherwise KILL.
- **"We've spent so much time on it"** — sunk cost. If the data says KILL, kill it.
- **"It's research_only so we can keep it"** — research_only without a research outcome is just a runner consuming attention.
- **"PnL looks great"** — check benchmark alpha first. nq_overnight had +$163 but -7.87pp vs QQQ (illusory).

## Cross-references for each manual-judgment strategy

The verdict editor surfaces these as `↗` links per card:
- **Subset drilldown:** `/api/strategy_drilldown?strategy=<name>&window_days=60`
- **Benchmark alpha:** `/api/benchmark_alpha` (find strategy in `strategies` array)
- **Recommended actions:** `/api/recommended_actions` (find strategy in `actions`)
- **Main dashboard:** `/` (panels show live data)

## Output

Editor saves to `argus_flow/logs/verdict_<DATE>.json`. Previous file backed up as `.json.bak` automatically. Commit the file after the ceremony:

```
git add argus_flow/logs/verdict_<DATE>.json
git commit -m "review: <DATE> verdict record — N winners, M rework, K kill"
```

This file is the contract for Week 2 cull execution — execute exactly what was decided, not what feels right after the fact.

## Source documents

- [project_5_1_review_ceremony_20260501.md](project_5_1_review_ceremony_20260501.md) — full ceremony spec
- [project_real_money_readiness_gate_20260531.md](project_real_money_readiness_gate_20260531.md) — 5/31 readiness gate (separate from 5/1)
- [project_evidence_promotion_ladder.md](project_evidence_promotion_ladder.md) — 9-stage evidence ladder
