---
name: EUR/USD Deployment Doctrine (2026-03-26)
description: Final deployment doctrine from 12+ tests and ChatGPT synthesis — session rules, exit rules, fleet pruning, test roadmap
type: project
---

## Core Thesis
EUR/USD range_accel is a conditional London-session drift strategy. Not universal mean-reversion. Not a tight-target scalp.

## Rules (ACTIVE NOW)
- 75-minute timeout (deployed)
- No tight profit targets (15/20 pip targets proven harmful)
- No binary regime GATE
- Both directions enabled (longs conditional, shorts robust)

## Rules (TO IMPLEMENT)
- Hard-block NY entries (15-19 UTC) — dead at every friction level
- Soft long handicap: reduced size or stricter threshold for longs
- London cluster limit: max 1-2 of GBPUSD/EURJPY/GBPJPY/CADJPY (they trade same days)

## Key Evidence
- Ablation: filters improve edge 2x (range_pct is key)
- Walk-forward: positive 5/6 test periods
- Drift curve: peak at 60min, decay after. Classic real-edge shape
- Bootstrap: 89% P(expectancy > 0)
- Neighborhood: every config tested is profitable (broad plateau)
- Shorts: robust across all regimes, sessions, efficiency levels
- Longs: only work in CHOPPY + morning (8-11 UTC)
- NY: negative at ALL friction levels — must be excluded
- 40-pip stop + TO75: PF 1.86 — highest, worth testing next
- EURJPY/GBPJPY 0.97 correlated — same trade
- USDJPY provides real diversification

## Next Experiments (in order)
1. Stop 40 + TO75 cohort (best exit candidate)
2. Long handicap cohort (reduced size or stricter threshold)
3. Dynamic exit family (break-even after +10, trailing, partial TP)
4. Long-side rescue (find which narrow conditions save longs)

## Fleet Doctrine
- London cluster (GBPUSD, EURJPY, GBPJPY, CADJPY) = one macro trade
- Keep 1-2 max from cluster
- USDJPY = true diversifier (keep)
- AUD family = moderate diversification (keep monitoring)

## Test Phases Remaining
- Phase 1: Research narrowing (neighborhood, dynamic exits, wide-stop, long rescue, session boundary)
- Phase 2: Live translation proof (feature parity, shadow accounting, timeout salvage)
- Phase 3: Runtime integrity (kill/restart matrix, reconnect, reconciliation)
- Phase 4: Fleet proof (marginal contribution, overlap heat, concurrency stress)
