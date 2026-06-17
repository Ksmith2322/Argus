# Turn-of-Quarter — Disciplined Gate Kill (2026-05-25)

Strategy agent's #2 candidate (`forge_turn_of_quarter`) — fourth kill
of the night. The point estimate is the closest to passing of any of
tonight's failures, but the bootstrap CI lower bound + walk-forward
split + thin sample combine to keep it below the gate.

## Strategy spec

- ENTRY: at MOC of last trading day of each quarter (Mar/Jun/Sep/Dec)
- EXIT: at MOC of 3rd trading day of new quarter
- Universe: SPY
- Pre-backtest: PF 1.8-2.5 at 5bps, n=80 over 20y, CAGR contribution
  1.5-2.5%, max DD 8-12%

## Results across configs

| Config | n | WR | PF | CI lower | CAGR | DD | Verdict |
|---|---|---|---|---|---|---|---|
| 20y / 10bps / exit=3 | 80 | 60.0% | 1.53 | 0.90 | n/a | 8.1% | FAIL |
| 20y / 10bps / exit=5 | 80 | 63.7% | 1.37 | 0.71 | n/a | 16.8% | FAIL |
| 20y / 5bps / exit=3 | 80 | 61.3% | 1.64 | 0.96 | n/a | 7.6% | FAIL |
| 30y / 10bps / exit=3 | 120 | 62.5% | 1.61 | **1.02** | n/a | 9.5% | FAIL |
| 30y / 5bps / exit=3 | **120** | **63.3%** | **1.71** | **1.08** | ~1.7% | 9.2% | FAIL |

Best variant (30y / 5bps): CI lower 1.08 is **12 hundredths below the
1.20 floor**. Within statistical noise, but firmly on the FAIL side.

Walk-forward (best config):
- H1: n=60, PF=1.78, CI lower=0.97 (FAIL)
- H2: n=60, PF=1.62, CI lower=0.85 (FAIL)

Both halves miss the floor by 0.20-0.35 even at the best parameters.

## Why it's still a kill despite being "real edge"

1. **Marginal CAGR contribution.** At full 1.0× allocation, the strategy
   would add ~1.7%/yr to fleet CAGR. At a more realistic 0.2× (tom_spy
   tier), it's ~0.3-0.5%/yr. Below the noise threshold of paper-account
   PnL variation.

2. **Overlaps with tom_spy.** Strategy agent claimed TOQ was "distinct
   from the monthly TOM effect." Mechanically that's true — TOQ enters
   on the LAST trading day of the quarter, tom_spy enters on the
   4th-to-last. But both exit on the 3rd trading day of next month;
   exit windows overlap. Per quarter, the TOQ trade is ~50% in the
   same hold period as tom_spy. Net new evidence per year is ~2 trades,
   not 4.

3. **CI floor exists for a reason.** Past kills with similar "almost
   passed" status (forge_pead at CI 1.025 at 40bps; forge_nq_overnight
   at CI 0.526 at 7bps) both produced negative live PnL when re-tested
   later. The gate is calibrated to refuse such borderlines.

4. **Calendar capacity in the fleet.** tom_spy (0.3×) + nov_spy (0.2×)
   already cover the calendar-anomaly slot. A third calendar strategy
   adds correlated, not independent, evidence — the variance reduction
   of adding it is small.

## Strategy agent's miss (4-for-4 tonight)

| Candidate | Predicted PF/Sharpe | Actual best | Pattern |
|---|---|---|---|
| overnight_drift_qqq | PF 1.4-1.7 @ 5bps | PF 0.97 @ 5bps | slippage drag swamps premium |
| credit_spread_regime | PF 1.5-2.0 @ 10bps | PF 1.53 (SMA200) | modern-era erosion (H2 weak) |
| sell_in_may_modulated | Sharpe +0.2 vs SPY | Sharpe -0.11 | QE bull killed it |
| turn_of_quarter | PF 1.8-2.5 @ 5bps | PF 1.71 (30y/5bps) | thin n; overlap w/ tom_spy |

The pattern is now unambiguous: **4 of 4 Strategy-agent candidates
failed the disciplined gate.** The agent over-estimates by 25-50% on
point PF and doesn't model:
- Slippage compounding for daily-frequency strategies
- Modern-era regime erosion of older edges
- Statistical noise at small n
- Overlap with already-deployed strategies

Only the Creative agent's **Variant Consensus Sleeve** (synthesis of
existing engines, not a new strategy) survived the gate at MARGINAL_PASS.

## What stays in `helio/turn_of_quarter.py`

- Calendar logic for quarter-end + Nth-trading-day-of-quarter detection
- Bootstrap PF CI on a small-n calendar sample
- Walk-forward H1/H2

Reusable for variants: a "quarter-end PUT spread" hedge, a "first 5
days of quarter momentum continuation" filter, etc. The engine isn't
specific to TOQ.

## Action items

- [x] `forge_turn_of_quarter` added to allocation_factors at 0.0
      with v26 kill-log entry
- [x] Added to `helio.roi_filter.KILLED_STRATEGY_CUTOFFS`
- [x] Added to `helio.killed_strategy_invariant.KILLED_STRATEGY_SYMBOLS`
- [ ] Next: build Pre-Trade Replay Bridge (Creative #1) — infrastructure
      that protects what we DO have, since new-strategy hit rate is 1/5
      tonight
