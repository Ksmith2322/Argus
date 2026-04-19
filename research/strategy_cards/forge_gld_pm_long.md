# Strategy Card: forge_gld_pm_long

**Status last reviewed:** 2026-04-18

```yaml
strategy_id: forge_gld_pm_long
owner: operator
mode: paper
asset_class: etf
instruments: [GLD]
edge_hypothesis: |
  GLD tends to continue trending upward in the US afternoon session (18-20 UTC,
  roughly 2-4pm ET) during quiet macro regimes. Entry at top-of-hour when ATR
  is healthy, target/stop scaled by ATR.
why_this_edge_should_exist: |
  Hour-of-day session analysis on 2.5 years of GLD 1h bars shows 6/6
  walk-forward folds positive at hours 18/19/20 UTC, with per-hour PFs of
  1.33/1.63/1.77/2.28. Mechanism is ambiguous but may involve late-session
  positioning, post-London liquidity patterns, or systematic US asset-allocator
  flows. Label-first research found this, not chart-eyeballing.
entry_rules:
  - Top-of-hour evaluation at 18, 19, 20 UTC (2pm, 3pm, 4pm ET) on weekdays
  - ATR(14) must be finite and > 0
  - No open position (one trade at a time)
  - Enter long at hour-close price
exit_rules:
  - Target: entry + 1.0 × ATR (reward)
  - Stop: entry - 0.5 × ATR (risk)
  - Time stop: exit at market close of next 4 market bars (hold_bars=4)
stop_logic:
  - ATR-scaled stop adapts to volatility regime
  - Stop below entry = half the target distance → 2:1 reward:risk
  - Time stop prevents indefinite holds during choppy action
position_sizing:
  - Shares = (anchor × risk_pct) / (entry - stop_px)
  - Risk_pct auto-set by fleet_sizing tier (currently "unproven" = 0.5%)
  - Example at $10K anchor, 0.5%, 0.5 ATR stop ≈ $1: 50 / 1 = 50 shares
expected_trade_frequency: ~250 trades/year (backtest 530 trades / 2 years)
known_failure_modes:
  - Regime shift: if GLD enters strong bearish trend, long-only strategy bleeds
  - Low ATR (compressed volatility): signal suppressed but may miss breakouts
  - Gap opens between 4pm close and next day can exceed stop distance
  - Fed/CPI days may distort afternoon session patterns
data_sources:
  - yfinance 1h GLD bars
  - IBKR paper/live for fills
feature_availability_times: Top of hour during US market session (pre-close on 4pm hour)
backtest_period: 2.5 years, 530 trades, runner-backtest PF 1.73
walk_forward_periods: 6/6 folds positive per hour (per research/label_first/reports/FINDINGS_v3.md)
transaction_cost_model: |
  IBKR retail stock commissions: ~$1 per trade.
  Spread on GLD: ~1 cent.
  Assumed cost: $2-3 per round-trip.
slippage_model: 1 cent entry + 1 cent exit at top-of-hour liquid window
live_forward_start: 2026-04-16 (first live trade hit target at +$199.15)
promotion_gate: |
  Per argus_flow/ops/promotion_gate_v2.py canonical rules:
  - 60+ valid live trades (NOT 30; standard is 60)
  - PF ≥ 1.30
  - Positive expectancy after friction
  - Live signal frequency ≥ 0.5 of replay expectation
  - At least 2 observed market regimes
kill_rules:
  - 30+ live trades with PF < 1.0 after costs
  - Live hourly signal frequency drops below 0.3 of replay for 2 consecutive weeks
  - Realized slippage exceeds model by 2x
  - Any single winning trade contributes > 40% of total PnL (fragility flag)
dashboard_fields:
  - Current tier + effective risk_pct
  - Trades last 7/30/90 days with PF each window
  - Open trade (bars_held + wall_minutes, entry/target/stop)
  - Hour-by-hour PF breakdown (which of the 3 hours is working?)
runbook: |
  1. Heartbeat must be fresh at 2/3/4pm ET weekdays
  2. Monitor fleet_perf_summary.json live_window for trade count growth
  3. If hour-18 PF diverges from hour-20 PF by > 30%, flag for review
  4. Weekly: check signal_frequency_report.json — replay expected ~1.0/day
```

## Why this strategy is #2 in the applied shortlist

Per §18.5:
- Best near-term research profile: PF 1.73 backtest over 530 trades
- First live trade (2026-04-16) hit target at target — early positive signal, not conclusive
- Overnight-safe (PDT exempt, max hold 4 market bars)
- 250 trades/year cadence gives enough sample to evaluate within months, not years

## Open questions

1. **Single live trade bias:** 1-trade +$199 win means very little. Need 10+ before any judgment.
2. **Regime dependence:** 2-year backtest may not include deep bearish GLD periods.
3. **Time-of-day stability:** per-hour PFs range 1.33-2.28. Is the "edge" really hour 20, with hour 18 riding along? Needs monitoring.

## What to ship for this strategy next

1. Extend live-vs-replay drift to monitor per-hour trade rate (§18.8 week 3)
2. After 10 live trades: per-hour PF breakdown to see if edge is 1 hour or all 3
3. Kill-rule watchdog per rules above
