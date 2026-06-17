---
name: Argus Validation Charter — Full Proof System
description: Complete evidence gates for 100/200/300% targets, missing systems, build order, kill rules, and operating discipline from ChatGPT 2026-03-27
type: project
originSessionId: 140c8938-b0fb-468a-8fce-f4e474d0834e
---
## The Machine Must Prove Six Things
1. Edge exists
2. Edge survives live friction
3. Edge is allocatable at fleet level
4. Edge is not concentration fraud
5. Controls actually help
6. New edges discovered faster than old ones decay

## Current State (2026-03-27)
- 1 anchor (EUR/USD), 1 probationary (GBP/USD), 1 quarantined (USD/JPY)
- 13 valid live trades. Infrastructure over-built relative to evidence.
- Bottleneck: trade accumulation, not code.

## Target Tiers

### 100-125% requires:
- 3-5 allocatable modules, post-friction expectancy stable
- 200-300 clean fleet trades
- No single module > 30% of fleet PnL
- At least one monetization uplift proven by cohort
- Risk scaling only after live continuity proof

### 200% requires ALL of 100% PLUS:
- 3+ allocatable modules with live proof
- 1 additional late-stage candidate
- 300+ clean fleet trades
- No module > 25% of fleet PnL
- Allocator working in shadow then capped live
- Evidence of ~0.44R/day net sustained

### 300% requires ALL of 200% PLUS:
- 4+ allocatable modules or 3 strong + second strategy family
- Second strategy family in actual paper validation
- 500+ fleet trades
- Evidence of ~0.56R/day net
- NOT achievable through leverage as main return engine

## Missing Artifacts to Build (in order)

### Week 1 (DONE):
1. Version/policy/cohort tagging ✓
2. Live drift report ✓
3. Shadow accounting (opportunity logger) ✓

### Week 2:
4. Standard concentration report (auto remove-best-1/3/5 per module)
5. Dependence report (module correlation, overlap, drawdown co-movement)

### Week 3:
6. Dynamic sizing in shadow mode (allocator research, not just "what size")

### Week 4:
7. Armed profit floor as champion/challenger cohort
8. Research factory: triage battery on unlabeled inventory symbols

## Key Concepts

### Edge Modules > Pairs
- EUR/USD base engine
- GBP/USD short-session engine
- Maybe later: USD/JPY Asia-range long engine
- The PAIR is a container. The MODULE is the real unit of trust.

### Champion/Challenger
- Every new control is a challenger
- Baseline remains champion
- Promotion only if challenger wins on pre-declared criteria
- Never replace globally

### Pre-Registration
Before any test, write: hypothesis, success metric, failure metric, sample size, action for each outcome.

### Edge Half-Life
Every module has lifecycle: emerging → stable → weakening → degraded → dead. Assume decay.

## Kill Rules (written now, not later)
Kill or demote if:
- Live expectancy below threshold for rolling window
- Friction haircut beyond acceptable band
- Remove-best-3 reveals non-allocatable concentration
- One session/regime carries outcome unintentionally
- Repeated runtime integrity anomalies
- Dependence too high with other active modules
- "It might come back" does NOT override demotion

## What NOT to Build Yet
- Fancy dashboard cosmetics
- More strategy knobs
- Broad dynamic sizing live rollout
- Global profit-floor deployment
- Second project before Argus proves first capital loop

## The Right Question
Not: "Can Argus get to 200-300%?"
But: "What evidence would make it irrational NOT to trust that 200-300% is achievable with acceptable concentration, drift, and drawdown?"

## 2026-04-18 Amendments (from PROFIT_MAX_SYSTEM_BLUEPRINT §18)

**Updated ROI framing:** The 100/200/300% targets above were set in 2026-03-27. Per blueprint §18.5 correctness pass, current live evidence supports NONE of these bands as near-term expectations. Working framing now:
- Current unproven state: 0-3% annual, including real chance of negative return
- After one validated edge: 3-8% annual target
- After promoted edge + stable execution: 8-15% annual stretch
- 15-40% per strategy: not supported by current live evidence
- 100-300% aspirational targets are NOT suspended but are clearly downstream of the 8-15% milestone

**One-In / One-Out Rule (new hard rule, from blueprint §18.9):**
> No new strategy family enters `paper` or `micro_live` mode until:
> (a) one existing strategy reaches a formal review point AND is either promoted, demoted, or killed;
> (b) the new strategy has a completed strategy card in `research/strategy_cards/`;
> (c) `project_fleet_master_<date>.md` is updated with the decision;
> (d) the dashboard path can show the new strategy's live-window USD PnL OR it is explicitly marked research-only.

**Rationale:** memory across fleet_master_20260326/20260408/20260416 shows the fleet grew from 5 → 12 → 19 systems while promoted count stayed at 0. Constraint is follow-through, not ideas. Every new strategy must be paid for by resolving an existing one.

**Stop-iterating threshold (new, from blueprint §18.10):**
- One strategy reaches 30 clean valid trades with PF ≥ 1.20 and positive expectancy → stop adding new strategy families for **30 days**, focus on observation + execution + risk.
- One strategy reaches the canonical promotion gate (60 valid + PF ≥ 1.30 + stable execution) → suspend new research for **90 days**, compound/monitor the proven sleeve.
- These do NOT override promotion_gate_v2.py — they layer on top.

**Applied 2026-04-18 shortlist** (per blueprint §18.6):
1. `forge_gdx_gld` — strategy card: `research/strategy_cards/forge_gdx_gld.md`
2. `forge_gld_pm_long` — strategy card: `research/strategy_cards/forge_gld_pm_long.md`
3. Apollo earnings drift — research-only, no execution: `research/strategy_cards/apollo_earnings_drift.md`
4. `forge_wick_gbpusd` — slow-cadence watch: `research/strategy_cards/forge_wick_gbpusd.md`

Everything else in the 19-system fleet → signal-only or research-only unless someone can produce a strategy card with a clean promotion plan.
