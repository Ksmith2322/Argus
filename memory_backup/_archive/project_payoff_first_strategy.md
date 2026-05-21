---
name: Payoff-First Strategy Development
description: Active roadmap for next strategy candidate. Start from large displacement events, reverse-engineer precursors, build minimal causal trigger. Replaces Cascade approach.
type: project
---

## Approach: Payoff → Precursor → Rule

Inverse of Cascade (which was Signal → Hope for Payoff → Dead).

## Phase 1 — Define the payoff (no signals)
- Pick one market (BTC data already available, FX when IBKR clears)
- Define "large displacement": |price[t+H] - price[t]| / price[t] >= D
  - BTC: >= 50-150bps over 10-60 minutes
  - FX: >= 20-50 pips
- Output: list of timestamps where big moves actually happened (ground truth)

## Phase 2 — Label the "before"
- For each big move, capture t-60 to t window
- Extract observable features: vol_z, range_pct, activity_z, imbalance, spread, session
- No thresholds. Just data.

## Phase 3 — Find precursors (contrast analysis)
- Event vs non-event: what distribution shifts precede big moves?
- Monotonic checks: higher X → higher probability of big move?
- Discard non-monotonic features

## Phase 4 — Build minimal causal trigger
- 2-3 features max
- Everything must be known at t0
- No confirmation delays

## Phase 5 — Payoff test (same kill framework as Cascade)
- Entry at t0, fixed stop, fixed target, timeout
- Report: target hit rate, stop hit rate, timeout %, expectancy
- Kill if: target reach low, timeout dominates, no tail

## Kill criteria (from Cascade learnings)
1. Target reach frequency must be meaningful (not 4%)
2. Timeout must not dominate (not 72%)
3. Stop vs target symmetry — losses must not arrive faster
4. Distribution must have real tails, not small drift
5. Include costs from day one

## Two candidate starting classes
1. **Volatility Expansion from Compression** — tight range + rising activity → expansion
2. **Event/Session Shock Windows** — displacement clusters around known high-liquidity windows

## BTC Spot Result: NEGATIVE (2026-03-22)
- 7/8 precursor features weak separation from background
- Large moves not forecastable from microstructure
- No further BTC spot iteration

## Next Target: EUR/USD via IBKR (pending account verification)

### Why FX is better than BTC spot:
- Stronger session structure (Asia range, London open, NY open)
- More repeatable liquidity windows
- Known macro/event catalysts
- Cleaner time-of-day effects
- Less random than crypto

### FX-specific feature set (v1):
- Session / overlap window
- Pre-window range compression
- Realized volatility change
- Spread state
- Time since session open
- Distance from prior session high/low
- Breakout of Asia range / London range

### Status: Waiting for IBKR account verification (~24hrs as of 2026-03-22)
