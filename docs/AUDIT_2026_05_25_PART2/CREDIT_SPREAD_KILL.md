# Credit-Spread Regime — Disciplined Gate Kill (2026-05-25)

Strategy agent's #3 candidate (`forge_credit_spread_regime`) failed the
disciplined gate at every parameter combination tested. Not deployed.

## Strategy spec (per Strategy agent)

- Thesis: HYG/LQD ratio widening leads SPY drawdowns by 2-6 weeks
  (Gilchrist-Zakrajsek excess bond premium literature, replicated
  through 2024)
- ENTRY: long SPY at next open when (HYG/LQD ratio > 50d SMA of ratio)
  AND VIX < 22
- EXIT: at next open when ratio crosses below 50d SMA
- Pre-backtest expectation: PF 1.5-2.0 at 10 bps, CAGR +7-9%,
  max DD ~12%, n=25-40 round-trips over 20y, 30-90 day holds

## Backtest setup

- Engine: `helio/credit_spread_regime.py` (new)
- CLI: `ops/audit/run_credit_spread_backtest.py`
- Data: yfinance daily bars. HYG inception 2007-04-11 (binding
  constraint); effective backtest window ~19 years.
- Bootstrap: moving-block (block=3, ~mean hold), 5000 resamples
- Walk-forward: chronological H1/H2 split of trade list

## Results across parameter sweeps

| Config | n | WR | PF | CI lower | avg/trade | DD | H1 pass | H2 pass | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| SMA50, VIX<22, no-conf | 189 | 47.6% | 1.16 | 0.66 | 0.11% | 30% | False | False | FAIL |
| SMA100, VIX<22, no-conf | 133 | 46.6% | 1.27 | 0.63 | 0.23% | 25% | False | False | FAIL |
| SMA100, VIX<22, conf=5 | 52 | 53.8% | 1.32 | 0.47 | 0.47% | 27% | False | False | FAIL |
| SMA100, no VIX, no-conf | 162 | 45.7% | 1.25 | 0.67 | 0.24% | 27% | False | False | FAIL |
| SMA200, VIX<22, no-conf | **88** | 54.5% | **1.53** | 0.66 | 0.52% | 31% | False | False | FAIL |

**Best variant (SMA200) hits the Strategy agent's predicted PF=1.53.**
But the walk-forward picture is the killer:

- H1 (2007-2016): PF=2.09, CI lower=0.69 ← old era looks great in point estimate
- H2 (2016-2026): PF=1.23, CI lower=0.33 ← modern era much weaker

Even the strongest configuration has an H2 that's a coin flip after
slippage. The pattern (**H1 strong, H2 weak**) is the opposite of
consensus_sleeve (where H1 was weak and H2 was strong) and tom_spy /
nov_spy (where modern eras strengthened). The signal is *eroding*
over time, not strengthening.

## Why it fails (post-hoc)

Strategy agent's spec warned about "whipsaw in credit-equity
decorrelation regimes (rare; ~2015-16)." The data say that regime
isn't rare — it's persistent and growing. Three plausible causes:

1. **Central-bank backstops since 2008** (QE, Fed BSL, ECB asset
   purchases) systematically remove credit-stress as a leading
   indicator — when credit cracks, the central bank steps in before
   equity reacts. This is exactly the Gilchrist-Zakrajsek effect's
   amputation.
2. **HY market structure change** — more ETF-driven flow, less
   fundamental price discovery. The HYG/LQD ratio reflects ETF
   liquidity flows more than underlying credit risk in the modern era.
3. **VIX regime shift** — VIX has been suppressed in the post-2020
   QE era, so the VIX<22 filter passes more aggressively than
   intended (it was meant as a tail-risk gate, but in regimes where
   VIX rarely hits 22 it's vacuous).

The signal *did* work in 2007-2015 (H1 PF 2.09). It stopped working
after 2016. There's no parameter combination that recovers H2.

## What this saves

Strategy agent estimated this as a 3-day build. The disciplined gate
backtest took ~30 minutes. Same trade as overnight_drift earlier
tonight: 30-minute gate test caught what would have been multi-day
deploy + 60-day paper drift + operator pullback.

## What this DOES NOT kill

The `helio/credit_spread_regime.py` engine generalizes to other
credit-driven signals:

1. **Credit spread MAGNITUDE** (not just SMA crossing). Z-score the
   ratio over rolling 252d, enter at extreme deviations.
2. **Different denominator pair.** LQD vs IEF (HY vs Treasuries)
   tests duration premium instead of credit premium. May escape
   central-bank flatten.
3. **VIX term-structure proxy.** When ^VIX9D > ^VIX (front-month
   stress > 30d), the credit signal often anticipates same-day equity
   drop — different timing window, possibly real edge.

None of these are urgent. The credit-spread thesis specifically does
not generalize; the engine is reusable for future credit-driven
research.

## Strategy agent's miss (pattern with overnight_drift)

| Candidate | Predicted PF | Actual best PF | Predicted n | Actual n |
|---|---|---|---|---|
| forge_overnight_drift_qqq | 1.4-1.7 at 5bps | 0.97 at 5bps | 750/yr | 750/yr |
| forge_credit_spread_regime | 1.5-2.0 at 10bps | 1.53 (SMA200) | 25-40 / 20y | 52-189 / 20y |

**Lesson reinforced (after 2 misses):**

- Agents over-estimate edge size by 1.5-2x on average
- Agents under-estimate trade count by 4-5x for whipsaw-prone
  threshold-crossing signals (overnight_drift was correctly estimated
  on n, but credit_spread was 4-5x off because the agent didn't model
  the ratio's volatility around the SMA)
- For any new candidate from this batch, expect 50-70% of the
  predicted point PF, and run the H1/H2 split before estimating
  deploy readiness

Strategy agent's remaining candidates from STRATEGY.md:
- #2 forge_turn_of_quarter (n=80 over 20y — quick gate test)
- #4 forge_xs_low_vol_factor (USMV/SPLV ranking — likely fails
  for same factor-harvesting reason as xs_momentum)
- #5 forge_yield_curve_macro_gate (2s10s overlay — interesting but
  same modern-era erosion risk as credit-spread)
- #6 forge_etf_pair_meanrev_xlk_xlf (sector pair MR at z>2.5)
- #7 forge_sell_in_may_modulated (Halloween-effect SPY/SHY rotation)

Highest-conviction remaining bets given the 2/2 miss pattern: **#7
sell-in-May** (calendar anomalies have been the strongest survivors
of the disciplined gate so far — tom_spy and nov_spy both passed)
and **#6 XLK/XLF pair MR** (similar structure to legacy_15 which
passed).

## Action items

- [x] `forge_credit_spread_regime` added to allocation_factors at 0.0
      with v23 kill-log entry citing this document
- [x] Added to `helio.roi_filter.KILLED_STRATEGY_CUTOFFS` and
      `helio.killed_strategy_invariant.KILLED_STRATEGY_SYMBOLS`
- [ ] Next Sprint 1 candidate: forge_sell_in_may_modulated (calendar
      strategy, ~2/year, distinct from nov_spy, expected to survive
      based on prior calendar-anomaly hit rate)
