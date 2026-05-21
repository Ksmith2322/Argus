---
name: Argus Cascade — CLOSED (Structural Kill 2026-03-22)
description: Cascade breakout+flow strategy fully validated and killed. Directional signal real but payoff distribution non-existent on spot. No further iteration needed.
type: project
---

## Status: CLOSED — Structural Kill (2026-03-22)

Cascade hypothesis fully explored and definitively killed. Not a parameter, filter, or venue problem — a fundamental payoff distribution failure.

## What Was Tested

**Hypothesis:** Breakout + aggressive order flow → tradable continuation in BTC.

**Data:** 843K Kraken BTC spot trades (14 days), 284K 1-second bars, 338 breakout events detected.

## What Was Proven

### Signal is real (directionally):
- 58% win rate on raw breakouts
- MFE > MAE on average (0.096% vs 0.060%)
- False breakout rate 41% (under 65% kill threshold)
- Asymmetry exists across all 9 sweep configs

### But payoff is non-existent:
- Average MFE only ~10bps — too small to cover any fee structure
- **Only 4.4% of trades reach even a 30bps target**
- 72.8% timeout (price doesn't move enough)
- 22.8% stopped out (losses arrive faster than gains)
- Every stop/target combination deeply negative
- No filter (delta_z, volume_z, compression, session) rescues it
- Causal "slow confirmation" filter = lookahead bias; edge is gone by confirmation time

### Structural conclusion:
> **Directional bias without monetizable displacement. No tradable payoff distribution exists.**

## Full Kill Chain Executed

1. Baseline replay → negative after fees (all 9 configs)
2. Fat tail analysis → top 20 MFE events still too small
3. Monotonic filter search → no filter combination produces positive expectancy
4. Synthetic perps economics → viable only at 5x maker or 10x taker (thin)
5. Causal slow filter → lookahead bias; edge gone after confirmation
6. Tight stop simulation → 4.4% target reach, 72.8% timeout → definitive structural kill

## Key Learnings (reusable)

### The "Payoff Test" — apply to ALL future strategies:
1. **Target reach frequency** — meaningful % must hit realistic target
2. **Timeout rate** — if most trades go nowhere → dead
3. **Stop vs target symmetry** — losses must not dominate in frequency/speed
4. **Distribution, not average** — tails must exist, not just small drift

### Other insights:
- Edge ≠ tradability. Directional bias ≠ monetizable edge.
- MFE/MAE without exit simulation is misleading
- Confirmation-based entry destroys impulse edges
- The instrument determines magnitude, not the signal

## What NOT To Do
- Do not revisit with "one more tweak"
- Do not assume perps/leverage rescues a 10bps move distribution
- Do not port this exact logic to another asset without magnitude proof first

## Infrastructure Preserved
All code in `argus_flow/` is reusable for future strategy research:
- Kraken API client (verified, $50 balance)
- Trade ingest pipeline (REST + WebSocket)
- Bar builder, validator
- Replay engine, sweep runner, edge classifier, delay simulator
- Full pipeline orchestrator

## Archived Results
- `argus_flow/replay_out/` — all replay outputs, sweep results, classification
- `argus_flow/replay_out/edge_classification.json` → DEAD
