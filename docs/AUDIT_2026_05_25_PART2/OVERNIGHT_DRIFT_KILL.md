# Overnight Drift — Disciplined Gate Kill (2026-05-25)

The Strategy agent's #1 ranked candidate (`forge_overnight_drift_qqq`,
~750 trades/year, "biggest trade-volume multiplier on the table") **failed
the disciplined gate at every plausible slippage assumption.** Not
deployed.

## Backtest setup

- Engine: `helio/overnight_drift.py` (new tonight)
- CLI: `ops/audit/run_overnight_drift_backtest.py`
- Period: 20 years of yfinance daily bars (2005–2026)
- Trade rule: BUY at MOC of day t (proxy: today's Close), SELL at MOO
  of day t+1 (proxy: tomorrow's Open). One trade per leg per day.
- Bootstrap: moving-block (block=5), 5000 resamples, 95% CI
- Walk-forward: chronological 50/50 H1/H2 split

## Disciplined-gate floor

| Layer | Threshold |
|---|---|
| Bootstrap PF CI lower bound | ≥ 1.20 |
| Walk-forward H1/H2 both pass the floor | required |

## Results across slippage assumptions

**At 10 bps round-trip (disciplined-gate default):**

| Leg | n | WR | PF | CI lower | Sharpe | Total compound | Verdict |
|---|---|---|---|---|---|---|---|
| SPY | 5031 | 45.2% | 0.72 | 0.66 | -1.62 | -98% | FAIL |
| QQQ | 5031 | 47.6% | 0.80 | 0.75 | -1.13 | -95% | FAIL |
| IWM | 5031 | 46.5% | 0.81 | 0.75 | -1.05 | -96% | FAIL |

**At 5 bps round-trip (the Strategy agent's stated pre-backtest assumption):**

| Leg | n | WR | PF | CI lower | Sharpe | Total compound | Verdict |
|---|---|---|---|---|---|---|---|
| SPY | 5031 | 50.0% | 0.90 | 0.83 | -0.54 | -76% | FAIL |
| QQQ | 5031 | 51.9% | 0.97 | 0.90 | -0.14 | -41% | FAIL |
| IWM | 5031 | 51.0% | 0.97 | 0.89 | -0.15 | -46% | FAIL |

**At 2 bps round-trip (institutional-tight, sub-retail-realistic):**

| Leg | n | WR | PF | CI lower | Sharpe | Total compound | Verdict |
|---|---|---|---|---|---|---|---|
| SPY | 5031 | 52.8% | 1.02 | 0.94 | +0.10 | +11% | FAIL (CI < 1.20) |
| QQQ | 5031 | 54.3% | 1.09 | 1.01 | +0.45 | +165% | FAIL (CI < 1.20) |
| IWM | 5031 | 53.4% | 1.08 | 0.99 | +0.39 | +145% | FAIL (CI < 1.20) |

**With the proposed 60-day Sharpe regime gate, at 10 bps:** marginally
worse (the gate strips ~30% of trades but the kept ones still lose on
average; regime gate isn't the problem — slippage is). Same FAIL on all
3 legs.

## Why it fails

The overnight equity-risk premium IS real — about **5-7% per year on
SPY/QQQ/IWM** before costs (academic literature: Kelly-Clark 2011,
Lou-Polk-Skouras 2019). But it's a **continuous** premium harvested by
buy-and-hold; the daily-rotation implementation incurs round-trip
slippage 250 times per year:

- 10 bps × 250 trades = **25%/yr slippage drag**
- 5 bps × 250 trades = **12.5%/yr slippage drag**
- 2 bps × 250 trades = **5%/yr slippage drag**

Against a 5-7% gross premium, only the 2 bps assumption leaves a positive
residual — and even at 2 bps, the bootstrap CI lower bound (1.00-1.01)
sits below the disciplined-gate floor (1.20). The premium is statistically
indistinguishable from noise at retail trading frequencies.

The strategy works in practice as a *structural* choice (mutual funds,
buy-and-hold ETF investors, retirement accounts) — those participants
capture the premium for free because they're not rotating in and out
daily. A daily-rebalancing bot pays the premium back in slippage.

## What this saves

Without this backtest, the most likely failure mode was:
- Build the runner (estimated 1 day)
- Deploy at 0.5× allocation
- Watch it bleed 0.5-1% per month for 60 days
- Operator pulls allocation; embedded learning lost

The kill costs us 2 hours of work and saves us 60 days of paper-account
drag plus the operational overhead of running a known-losing strategy.

## What this DOES NOT kill

The `helio/overnight_drift.py` engine is reusable for:

1. **Weekly cadence** (Friday close → Monday open). Only ~52 trades/year;
   slippage drag drops to 0.5-2.5%; might leave 3-5% of the premium
   intact. Worth a future test if a weekly-cadence backtest CLI flag
   is added.

2. **Conditional entry** (only trade when ex-ante predicted overnight
   move > slippage cost). E.g. enter only when VIX spike or pre-FOMC.
   Captures the gross premium when it's largest, skips the cost-eaten
   majority of days.

3. **Spread/pair structure** (long SPY-overnight, short SPY-intraday).
   Long-only is the slippage-heavy leg; if intraday short captures the
   complement, the spread cost halves while the signal doubles. Requires
   intraday data we don't have. Tier-2 roadmap item.

The code stays in `helio/` as a research artifact + template for the
weekly variant. No runner shipped.

## Strategy agent's miss

The agent's pre-backtest expectation was "PF 1.4-1.7 at 5 bps slippage."
At 5 bps, actual PFs are 0.90-0.97 — about half the prediction. The agent
under-estimated the slippage compounding cost of daily-frequency trading.
This is a generalized lesson: **for any daily-frequency strategy, drag
gross-edge by N×slippage_per_trade before assuming it survives.**

## Action items

- [x] `forge_overnight_drift_qqq` added to allocation_factors at 0.0
      with v22 kill-log entry citing this document
- [x] `forge_overnight_drift_qqq` added to
      `helio.roi_filter.KILLED_STRATEGY_CUTOFFS` so any future
      attempt to add a runner is refused at the submit_bracket
      invariant
- [ ] Sprint 1 redirected to the next-best move per SYNTHESIS.md:
      **Variant Consensus Sleeve** (Creative #2) — wires a virtual
      ledger taking positions only when ≥3 of 5 xs_momentum variants
      agree. Zero new capital, ~12 trades/month from existing engines,
      provides correlated-DD protection.
