# JPY PM Short — Strategy Specification

**Origin:** Discovered via label-first Phase 2D, 2026-04-16. See [`research/label_first/reports/FINDINGS_v3.md`](../../research/label_first/reports/FINDINGS_v3.md).

**Edge thesis:** USDJPY and CADJPY both show consistent SHORT-side edge at 19:00 UTC (3pm NY time). The JPY (yen) tends to strengthen against USD and CAD in NY late afternoon, possibly reflecting end-of-day USD position unwinds or risk-off positioning. Same hour, two correlated pairs, both with 6/6 walk-forward folds positive.

## Walk-forward results (Phase 2D)

| Pair | Hour UTC | n | exp ATR | PF | t-stat | Folds positive | Median PF |
|---|---|---|---|---|---|---|---|
| USDJPY | 19 (3pm NY) | 727 | +0.087 | **1.37** | **3.7** | **6/6** | 1.42 |
| CADJPY | 19 (3pm NY) | 727 | +0.075 | **1.32** | 3.3 | **6/6** | 1.34 |

## Entry rule
At 19:00 UTC top-of-hour (≈ 3pm NY), enter SHORT both USDJPY and CADJPY if:
1. Hour matches (19 UTC)
2. ATR(14) finite and positive
3. No existing open position on that pair

## Exit rule
- **Target:** entry - 1.0 × ATR(14)
- **Stop:** entry + 0.5 × ATR(14)
- **Time exit:** close at end of bar 4
- Order: stop first, then target

## Position sizing
- Risk per trade: 1.0% of model equity ($10,000 per pair = $20K total notional)
- FX 100K lot, ~$10/pip per 100K
- Pos = risk_usd / (stop_pips * pip_value/100K)

## Cadence
- 2 pairs × ~250 weekdays = ~500 candidate signals/year
- After ATR/sanity gates: ~480/year
- One-trade-at-a-time per pair → ~250 actual trades/year per pair
- Funding gate (60 trades) achievable in **~3-4 months** per pair

## Risks
- Both pairs JPY-correlated — drawdowns will hit together
- 2.5-year backtest window (2023-2026) coincides with BOJ policy normalization era; JPY behavior may shift on policy reversals
- Single hour edge — vulnerable to time-of-day microstructure shifts (algorithmic flows changing)

## Promotion gate compatibility
- Stage: discovery → watcher → paper → real
- Cohort tagging on every trade
- pnl_pips (FX, gate auto-detects)
- log_dir: `forge/logs/jpy_pm_short/`
- Two trade rows per signal hour (USDJPY + CADJPY)
