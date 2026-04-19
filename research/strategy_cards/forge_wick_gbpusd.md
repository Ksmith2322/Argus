# Strategy Card: forge_wick_gbpusd

**Status last reviewed:** 2026-04-18

```yaml
strategy_id: forge_wick_gbpusd
owner: operator
mode: paper
asset_class: fx
instruments: [GBPUSD]
edge_hypothesis: |
  Daily GBPUSD bars with a long upper wick AND close near the bar's low AND
  compressed Bollinger Band width AND high choppiness often precede
  mean-reversion higher over subsequent days. Combines exhaustion signal
  (long upper wick, weak close) with compression filter (narrow BB, choppy).
why_this_edge_should_exist: |
  Label-first analysis on 22 years of GBPUSD daily history (5,818 bars, 1-day
  forward returns). Pattern meets multiple confluence criteria simultaneously
  and filters to a rare setup (~13/year). Walk-forward: PF 1.42 over 298
  trades, 5/5 valid folds positive. Compressed volatility + exhaustion is a
  classic reversal setup with documented academic support.
entry_rules:
  - Daily bar close evaluation (UTC close for GBPUSD)
  - upper_wick_pct > 0.6 (60%+ of bar range is upper wick)
  - close_pos_in_range < 0.3 (close in bottom 30% of bar)
  - bb_width_pct < 33rd percentile over 60-day window (compression)
  - choppiness_14 > 67th percentile over 60-day window (sideways action)
  - ATR finite and positive
  - No open position
exit_rules:
  - Target: entry + 2.0 × ATR
  - Stop: entry - 1.0 × ATR
  - Time stop: hold_bars = 20 days
stop_logic:
  - 2:1 reward:risk
  - Wide stops appropriate for daily FX mean-reversion trades
  - Position size inversely scales with ATR
position_sizing:
  - Units = (anchor × risk_pct) / (entry - stop_px in price terms × pip_value)
  - Risk_pct auto-set by fleet_sizing tier (currently "unproven" = 0.5%)
  - GBPUSD: each pip on 100K units ≈ $10; stop ~100 pips × 100K = ~$100 risk at 1 lot
expected_trade_frequency: ~13 trades/year (very sparse)
known_failure_modes:
  - Sparse: 13 trades/year means ~1 year to hit 30 trades
  - Daily timeframe: slow feedback loop
  - Signal requires multiple simultaneous filters — easy to over-fit
  - GBPUSD volatility regime shifts (BoE / Fed) can break mean-reversion assumption
  - Large stop distance can produce long-tailed bad trades
data_sources:
  - yfinance daily GBPUSD bars
  - IBKR paper/live for fills
feature_availability_times: After daily close
backtest_period: 22 years (5,818 bars), ~298 qualifying trades
walk_forward_periods: 5/5 valid folds positive (PF 1.42 walk-forward realistic)
transaction_cost_model: |
  IBKR retail FX: ~0.2 pip spread + small commission ≈ $2-3 per round-trip on a
  $10K-notional trade.
slippage_model: 1 pip at daily close (model 2 pips to be safe)
live_forward_start: pending — strategy has 0 live trades yet
promotion_gate: |
  Standard promotion gate problem: at ~13 trades/year, 60 valid trades takes
  ~5 years. Proposed special-case gate for this strategy:
  - 20+ valid live trades (more practical given frequency)
  - PF ≥ 1.30
  - Hit rate ≥ 40% (with 2:1 R/R, 34% hit rate is breakeven)
  - Positive expectancy after friction
  - Parameter stability across walk-forward years
kill_rules:
  - 10+ live trades with PF < 1.0
  - Live trades in 2 consecutive years at < 50% of expected frequency
  - Live expectancy significantly worse than walk-forward lower CI bound
dashboard_fields:
  - Days since last signal
  - Days since last trade
  - Bars evaluated (to confirm daily cadence runs)
  - Current bb_width_q33 and chop_q67 thresholds
  - Cumulative PnL if any live trades exist
runbook: |
  1. Heartbeat must be fresh after UTC daily close
  2. Signal criteria are strict — "NO_TRIGGER" most days is expected
  3. When trade fires: manual review of the 4 filters before paper-fill
  4. Given sparse cadence: promotion decision takes 18-24 months minimum
```

## Why this strategy is #3 in the applied shortlist

Per §18.5:
- Strongest long-history statistical edge (22 years, 5/5 folds)
- Sparse cadence is both the edge (selectivity) and the disqualifier (slow to validate)
- PDT-safe (daily bars, overnight holds)

## Honest caveat (per §18.7)

This is a **very slow** strategy. Promotion via the standard 60-trade gate at ~13 trades/year takes ~5 years. The strategy is included on the shortlist because:
1. Walk-forward evidence is stronger than most
2. It costs almost nothing to keep running on paper
3. If the 5-fold walk-forward held up in live data, it's a high-PF complement to faster strategies

But: do NOT expect this to contribute material live USD in the next 12 months.

## What to ship for this strategy next

1. Accept the sparse cadence — no special engineering needed
2. When/if the strategy fires its first live trade, flag it for manual review
3. Revisit the promotion gate in 18 months with whatever live sample exists
