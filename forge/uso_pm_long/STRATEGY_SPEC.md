# GLD PM Long — Strategy Specification

**Origin:** Discovered via label-first Phase 2C, 2026-04-16. See [`research/label_first/reports/FINDINGS_v3.md`](../../research/label_first/reports/FINDINGS_v3.md).

**Edge thesis:** GLD (gold ETF) shows strong long-only intraday edge during US afternoon hours (2-4pm NY = 18:00-20:00 UTC). Hypothesis: institutional gold accumulation during PM London fix runoff + US afternoon trading creates a consistent upward drift. Validated 6/6 walk-forward folds positive across multiple hour cells, t-statistics 6-7 (extraordinarily strong).

## Walk-forward results (Phase 2C)

| Hour UTC | NY Time | n | exp ATR | PF | t-stat | Folds positive |
|---|---|---|---|---|---|---|
| 17 | 1pm | 717 | +0.098 | 1.33 | 3.6 | 6/6 |
| 18 | 2pm | 718 | **+0.171** | **1.63** | **6.2** | 6/6 |
| 19 | 3pm | 718 | **+0.202** | **1.77** | **7.3** | 6/6 |
| 20 | 4pm (close) | 248 | **+0.296** | **2.28** | 6.2 | 6/6 |

## Entry rule
At any of hours 18:00, 19:00, 20:00 UTC (top of hour), enter LONG GLD if:
1. Hour matches signal hour
2. ATR(14) finite and positive
3. No existing open position (one trade per hour-cycle)

## Exit rule
- **Target:** entry + 1.0 × ATR(14)
- **Stop:** entry - 0.5 × ATR(14)
- **Time exit:** close at end of bar 4 (4 hours after entry)
- **Order of evaluation per bar:** stop first, then target

## Position sizing
- Risk per trade: 1.0% of live broker equity (dynamic — pulled via `helio.fleet_sizing.get_sizing_anchor_usd()` at eval time; no hardcoded default)
- Position size = risk_usd / stop_distance_usd
- GLD ~$200 share price; ATR ~$1; stop ~$0.50 → ~200 shares per trade

## Cadence expectation
- 3 hours × ~252 trading days = ~756 candidate signals/year per hour
- After ATR/sanity gates: ~700/year
- BUT only one open position at a time → likely 200-300 actual trades/year
- Funding gate (60 trades) achievable within **~1 month** of live operation

## Risks
- Concentrated time-of-day = vulnerable to US afternoon regime change
- 2.5 years of test data only (2023-2026) — shorter history than GBPUSD
- Strong t-stats partly inflated by sample size; expect modest mean reversion in live
- Gold has macro-driven structural shifts (rate cycles); the PM bias may not persist forever

## Promotion gate compatibility
- Stage: discovery → watcher → paper → real
- Cohort tagging on every trade
- pnl_pts (futures) — but GLD is an ETF priced in USD/share. Use pnl_usd as primary metric.
- log_dir: `forge/logs/gld_pm_long/`
