---
name: Performance discovery tests + priority order (2026-04-19)
description: 10 discovery tests for earning permission to size, strategy-specific moves, Mamba/Tori sub-strategy framework, priority order
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
**Frame:** Confidence tests (yesterday's framework) tell us what we know. **Discovery tests tell us what to change.** Every test below earns *permission to size*, not proof of future profit. Backtests can diverge sharply from live — CFTC 4.41 / Investor.gov.

## 10 Discovery Tests

1. **Hardcoded→Computed Bridge.** For every red HARDCODED strategy, create `strategy_confidence/<strategy>.json` with `bt_trades, bt_pf, bt_wr, expectancy, p_expectancy_positive, sample_warning, source`. Dashboard prefers computed when file exists; falls back to hardcoded otherwise.
2. **Walk-Forward Stability.** Rolling train/test. Keep rules if PF survives most windows; demote if one window carries the edge; regime-route if edge flips by regime.
3. **Cost/Slippage Stress.** 1x/2x/3x costs + ugly fills. Non-negotiable for scalpers (Mamba, Cue Banks, Tori). Die at 2x → too fragile for live sizing.
4. **Trade-Frequency Capacity.** Signal count by month and filter strictness. Stricter + good PF but too few trades → clone to more symbols/timeframes; don't loosen.
5. **Exit Optimization.** Freeze entries, test fixed target/stop, trailing, time stop, partial at 1R runner to 2R, close by session/event.
6. **Regime Filter.** Trending/ranging/high-vol/low-vol/risk-on/risk-off/VIX bucket/above-below-MA. One-regime strategies → conditional. All-regime → allocation priority.
7. **Time-Of-Day / Day-Of-Week.** Kill dead sessions. Confidence lifts only in profitable windows. FX → London/NY overlap if that's where the edge lives.
8. **Signal Funnel.** `raw → filtered → approved → filled → valid outcome`. Filters blocking winners → loosen the right blocker. Raw signals bad → don't tune filters, rebuild setup. Fills ≠ signals → execution problem.
9. **Monte Carlo Trade-Order (5000).** Drawdown tails + ruin + monthly dispersion. Ugly tails with good EV → reduce size, not logic.
10. **Portfolio Correlation.** Strategies that lose together cap combined exposure. True hedges (GDX/GLD, VIX Revert) can earn offsetting allocation.

## Strategy-specific tests

- **Argus MTF Trend** — live/replay parity + blocker funnel. Live << replay → fix filters/feed/timing, not strategy rules.
- **Titan** — long-only vs all signals + breakout quality (add breadth filter if breakouts fail in weak breadth).
- **Ares** — top 2 vs top 3/4/6 sector rotation; churn cost vs smoothness.
- **Hermes** — score threshold sweep 70/75/80/85/90; if only 80+ profitable, stay strict and expand universe.
- **Apollo** — earnings gap bucket + post-event drift window; restrict to Tier 1 + large-gap if edge concentrates there.
- **GDX/GLD** — live-paper parity + hedge ratio stability. Keep signal-only until live fills confirm spread behavior.
- **Mamba** — setup-family split (see below).
- **Cue Banks** — Fib confluence by trend direction and session; require HTF trend alignment if that's where the edge is.
- **Tori** — bounce vs break vs retest split (see below).
- **VIX Revert** — VIX percentile vs raw threshold if raw >30 is too rare; hold-period sweep.
- **Index Rebal** — no-lookahead event-timestamp test. High PF + few trades → brutal timestamp validation required.
- **Sector Rot** — top-k + rebalance cadence + risk-off overlay.
- **Themis** — disclosure-date backtest, not transaction-date. Edge disappears under disclosure timing → tracking only.

## Mamba & Tori subset-edge framework
Current numbers say the **whole strategy** is weak, but a **sub-strategy** may be strong. The shape of the test:

`setup_type × confluence_score × session × volatility × exit_model`

**Mamba buckets to test:** opening range break, VWAP reclaim, liquidity sweep reversal, trend pullback, failed-breakout fade, first pullback after impulse.

**Promotion bar per bucket:**
- PF > 1.25 after costs
- Positive expectancy
- ≥30 backtest trades
- No single month carries the edge
- Stable out-of-sample result

**Tori clue:** bounce PF 3.56 while total PF 0.61 → classic subset-edge. Disable weak `break`; keep `bounce` and maybe A+ retest; expand strong family to more instruments rather than keeping weak families alive.

## UI regression (not yet added)
Playwright/UI-level assertions: fleet confidence has `CALC` or `INSUFFICIENT SAMPLE`; expected annual has `HARDCODED`; no unlabeled confidence number renders anywhere. Prevents the dashboard from becoming accidentally overconfident again.

## Priority order (attack this first)
1. Hardcoded→computed artifact bridge
2. Tori setup split
3. Mamba setup-family split
4. Cost/slippage stress for all scalpers
5. Walk-forward stability for rows with 50+ backtest trades
6. Portfolio correlation + allocation test
7. Live/replay parity for Argus and GDX/GLD

**Why:** Set 2026-04-19 after the honest-dashboard layer landed. Path is "dashboard honest → discover which sub-strategies earn size".

**How to apply:** When the user asks "what next on strategies," default to the top of this list. Don't propose tuning a strategy's rules before its discovery tests have run. Mamba/Tori specifically: do not tune knobs globally; find the one subset family with positive expectancy, clone it; disable weak families.
