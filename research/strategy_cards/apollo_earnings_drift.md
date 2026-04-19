# Strategy Card: apollo_earnings_drift

**Status last reviewed:** 2026-04-18

```yaml
strategy_id: apollo_earnings_drift
owner: operator
mode: research_only   # scanner alerts only; no execution path yet
asset_class: equity
instruments: [US liquid large-caps — dynamic from earnings calendar]
edge_hypothesis: |
  Stocks with a strong pre-earnings setup (score ≥ 75 on Apollo's composite
  of BB squeeze, volume buildup, insider/analyst features, historical
  surprise pattern) tend to exhibit positive drift in the T+1/T+3/T+5 window
  after the score is recorded. This is post-announcement drift (PEAD),
  documented in academic literature.
why_this_edge_should_exist: |
  Post-Earnings Announcement Drift is one of the most-documented market
  anomalies (Bernard & Thomas 1989, Fama 1998 survey). The edge persists in
  part because (a) analysts update slowly, (b) institutions unwind
  positions over days not instantly, (c) retail attention lags.
  Apollo's current 50-record forward-return sample shows:
    - T+1: n=50, 52% positive, avg +0.58%
    - T+3: n=50, 66% positive, avg +1.16%
    - T+5: n=17, 71% positive, avg +2.00%
  These are PROMISING but NOT PROVEN — particularly T+5 has n=17 only.
entry_rules:
  - score ≥ 75 at scan time (Apollo's post-dedup threshold)
  - Entry ON next trading day open at score-date close (T+0 → T+1 signal)
  - Direction: long for "long" scores, short for "short" scores
  - Must not already have a position in the symbol from another strategy
exit_rules:
  - T+1 strategy: exit at T+1 close (1 trading day hold)
  - T+3 strategy: exit at T+3 close (3 trading day hold)
  - T+5 strategy: exit at T+5 close (5 trading day hold)
  - No intraday stops: accept the T+N holding horizon
stop_logic:
  - Position-size stop: max loss = risk_pct × anchor
  - Catastrophic stop: if position moves >5% against at any time, manual review
position_sizing:
  - Shares = (anchor × risk_pct) / max_expected_adverse_move
  - max_expected_adverse_move ≈ 3-5% based on sample (use 5% conservative)
  - Risk_pct auto-set by fleet_sizing tier (currently "research_only" = 0%)
expected_trade_frequency: 30-50 trades/year (earnings season clusters)
known_failure_modes:
  - Earnings gap-down can blow through position size in seconds
  - Overnight gaps mean no intraday stop protection
  - Score criteria may be overfit on the 50-sample forward-return dataset
  - Multi-strategy correlation: Apollo + S&P momentum can double-expose during earnings season
  - Ticker concentration: score dedup may still send multiple similar names
data_sources:
  - Apollo scanner outputs (apollo/logs/scan_*.json)
  - yfinance for forward-return computation in apollo/ops/backfill_forward_returns.py
  - IBKR for fills (pending)
feature_availability_times: After market close on scan date
backtest_period: "backtest" is the 50 forward-return records; no formal historical run yet
walk_forward_periods: Not run — would require rolling-window training on the score model
transaction_cost_model: |
  IBKR retail stock: ~$1/trade.
  Liquid large-caps spread: ~1-2 cents.
  Assumed cost: $3 round-trip.
slippage_model: 2-5 cents for market-open entry (liquidity gap)
live_forward_start: not started — no execution path
promotion_gate: |
  Custom criteria given strategy structure (event-driven, not continuous):
  - T+1 horizon: 60 trades, PF ≥ 1.15 (tight because 1-day holds compound costs)
  - T+3 horizon: 40 trades, PF ≥ 1.30, avg > 0.5% after costs
  - T+5 horizon: 40 trades, PF ≥ 1.40, avg > 1.0% after costs
  - Hit rate ≥ 55% for T+3 (forward-return sample is 66% but expect degradation)
kill_rules:
  - 20+ live trades with PF < 1.0 at the chosen horizon
  - Any single catastrophic loss > 10% of position
  - Correlation with other strategies > 0.5 over rolling 30 trades
  - Apollo scan score distribution drifts (old high-scoring names stop outperforming)
dashboard_fields:
  - Forward-return panel: T+1/T+3/T+5 mean + hit rate + n
  - Recent Apollo signals awaiting T+N resolution
  - Per-horizon PF if executed
  - Score-band breakdown (does 75-85 vs 85-95 vs 95-100 behave differently?)
runbook: |
  1. apollo.ops.backfill_forward_returns runs nightly in run_cohort_report.ps1
  2. Dashboard shows forward_returns.jsonl rolling stats
  3. NO EXECUTION until strategy is promoted from research_only
  4. Decision point: after 100 forward-return records (from 50 today), review hit rate stability before building execution path
```

## Why this strategy is #3-4 in the applied shortlist

Per §18.5 + §18.2:
- Forward returns are finally being captured (apollo/logs/forward_returns.jsonl)
- Early sample looks promising: T+3 +1.16% avg, 66% hit rate on n=50
- BUT: no execution path exists. It's currently research-only.
- T+5 sample is too small (n=17) to trust

## Decision point

The blueprint calls this strategy a "convert to execution" candidate, but the honest read is:

**Before building the execution path, wait until forward_returns.jsonl has 100+ records** (vs. today's 50). That's ~3-4 more months of nightly scans given earnings-season cadence. If hit rate holds at 60-70% on T+3 at n=100, build execution. If it drifts to 55% or lower, the scan-to-trade edge was noise.

## What to ship for this strategy next

1. Keep the nightly backfill running (already wired into cohort report)
2. Add dashboard panel showing T+1/T+3/T+5 rolling stats and sample size
3. Set calendar check-in for 2026-07-15 (3 months) to review forward_returns.jsonl — if it supports edge, start execution-path design then
4. Do NOT build execution path in the next 4 weeks — sample too small
