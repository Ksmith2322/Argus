# Strategy Card: forge_gdx_gld

**Status last reviewed:** 2026-04-18

```yaml
strategy_id: forge_gdx_gld
owner: operator
mode: paper
asset_class: pair (ETF × ETF)
instruments: [GDX, GLD]
edge_hypothesis: |
  The log-ratio spread log(GDX/GLD) mean-reverts around a slowly-moving equilibrium
  driven by miner operating leverage vs. spot gold. Entry is a z-score extreme,
  exit is z-reversion to zero or a stop at further extreme.
why_this_edge_should_exist: |
  Gold miners are a leveraged proxy for gold. Divergences between miner equity
  prices and gold spot arise from (a) idiosyncratic mining sector news, (b) risk
  appetite / beta shifts, (c) ETF flow imbalances. These revert over days to
  weeks because the underlying cointegration is real (mining economics are gold
  price-linked).
entry_rules:
  - Rolling 60-day z-score of log(GDX/GLD) spread
  - Long-spread entry when z ≤ -2.0 (short GLD, long GDX — miners oversold vs gold)
  - Short-spread entry when z ≥ +2.0 (long GLD, short GDX — miners overbought vs gold)
  - Evaluate at daily close (4pm ET)
exit_rules:
  - Mean-revert exit when z crosses through 0
  - Stop-out when z reaches ±3.5
  - No time-based exit (position held until z-exit or stop)
stop_logic:
  - Z-score stop at ±3.5 from entry direction
  - Dollar-neutral pair → PnL driven by spread divergence, not directional market move
position_sizing:
  - Dollar-neutral: each leg sized to equity / 2 in notional
  - Risk_pct auto-set by fleet_sizing tier system (currently "unproven" = 0.5%)
  - Position = (anchor × risk_pct) / worst-case-z-stop-distance-in-dollar-terms
expected_trade_frequency: 40-50 trades/year (backtest 2006-2026)
known_failure_modes:
  - Cointegration breakdown during regime change (e.g., 2016 mining crisis, 2020 COVID)
  - Short borrow unavailable on GDX during high-demand periods
  - One-leg fill without other-leg fill leaves directional exposure
  - Ex-dividend dates create small artificial spread moves
data_sources:
  - yfinance daily OHLCV for GDX + GLD
  - IBKR paper/live for fills
feature_availability_times: End-of-day close (4pm ET for US ETFs)
backtest_period: 2006-05-22 → 2026-03-13 (historical back-fill)
walk_forward_periods: Not yet run in formal walk-forward — deferred; 87 trades is sample
transaction_cost_model: |
  IBKR retail stock commissions: ~$1/leg = $2 round-trip per pair trade.
  Spread: ~1-3 cents on GDX, ~1-2 cents on GLD at market open.
  Assumed cost: $3-5 per round-trip pair trade.
slippage_model: 1 cent per leg at mid-liquid hours; 2-3 cents if trading at open/close
live_forward_start: pending (no executable IBKR order path yet)
promotion_gate: |
  Per argus_flow/ops/promotion_gate_v2.py canonical rules:
  - 60+ valid live trades
  - PF ≥ 1.30
  - Positive expectancy after friction
  - Stable live-vs-replay parity (no severe drift)
  - Signal_frequency_ratio ≥ 0.5 vs backtest
kill_rules:
  - 20+ live trades with PF < 1.0 after costs
  - Cointegration breakdown: spread mean-reversion half-life > 30 days for 60 consecutive days
  - Hedge-ratio instability (z-score distribution shifts regime)
  - GDX short borrow unavailable for 10+ days in a rolling 90-day window
dashboard_fields:
  - Current spread z-score
  - Open trade direction + entry z + current z
  - Trade count (last 30d, last 90d, all-time live)
  - PF vs replay expectation
  - Fill-truth PnL (post fill-truth pipeline land)
runbook: |
  1. Monitor forge/logs/gdx_gld/heartbeat.json — must be fresh daily during trading hours
  2. Review signal_frequency_report.json weekly for drift
  3. Any one-leg fill or stop-out triggers manual review before next entry
  4. Monthly: check cointegration stability in 60-day rolling window
```

## Why this strategy is #1 in the applied shortlist

Per §18.5 of the blueprint:
- 87 historical trades (sample large enough to be credible, though it's back-fill not live)
- Dollar-neutral = low directional market-beta exposure
- Overnight holds = PDT-safe at $10K retail funding
- Survives retail cost threshold at $10K (see §18.6 cost analysis)

## Open questions

1. **Back-fill vs live gap:** the 87-trade historical sample was retroactively sized at today's $10K anchor. Live evidence is 0 trades post-2026-04-17 cutoff. Real promotion requires building live trade count from zero.
2. **Short-borrow availability:** GDX short leg depends on broker making shares available. This hasn't been stress-tested.
3. **Execution path:** currently signal-only. Needs fill-truth order pipeline to become production candidate.

## What to ship for this strategy next

1. Fill-derived trade pipeline (blueprint §18.7 week 2)
2. Live-vs-replay drift panel showing first 10 live trades vs back-fill (§18.7 week 3)
3. Explicit signal-to-fill-to-pnl journal, one row per live trade
4. Kill-rule watchdog per rules above
