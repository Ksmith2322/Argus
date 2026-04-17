# Wick GBPUSD — Strategy Specification

**Origin:** Discovered via label-first research, 2026-04-16. See [`research/label_first/reports/FINDINGS_v2.md`](../../research/label_first/reports/FINDINGS_v2.md).

**Edge thesis:** On GBPUSD daily bars, "shooting star"-style candles (long upper wick, close near low) preceding upward mean reversion when the market is in a range/consolidation regime — NOT during strong trends. Validated PF 1.42 over 22 years, 5/5 walk-forward folds positive, random-baseline stress test passed.

## Entry rule (LONG only — Phase 2 SHORT side TBD)
At GBPUSD daily bar close, enter LONG **next bar's open** if ALL of:

1. **Wick pattern:**
   - `upper_wick_pct = (high - max(open, close)) / range > 0.6`
   - `close_pos_in_range = (close - low) / range < 0.3`

2. **Range-regime filter (BOTH required):**
   - `bb_width_pct < 33rd-percentile` (rolling 60-bar quantile of `4 × stdev(close, 20) / SMA(close, 20)`)
   - `choppiness_14 > 67th-percentile` (rolling 60-bar quantile of `100 * log10(sum(ATR, 14) / (max(high, 14) - min(low, 14))) / log10(14)`)

3. **Sanity:** ATR(14) finite and positive

## Exit rule
- **Target:** entry + 2.0 × ATR(14)
- **Stop:** entry - 1.0 × ATR(14)
- **Time exit:** close at end of 20th bar after entry if neither hit
- **Order of evaluation per bar:** stop first, then target (conservative)

## Position sizing
- Risk per trade: 1.0% of model equity ($10,000 default)
- Position size = (risk_usd) / (stop_distance × pip_value)
- Pip value GBPUSD ~$10 per pip per 100K lot

## Known constraints
- **Cadence:** ~13 trades/year average (sparse). Funding gate (60 trades) achievable in 4-5 years live (or sooner if combined with SHORT side).
- **Regime sensitivity:** Filters specifically reject trending markets. May produce zero signals during sustained directional moves.
- **Holding period:** Up to 20 bars = 20 trading days = ~4 weeks max.

## Promotion gate compatibility
- Stage: discovery → watcher → paper → real
- Cohort tagging: config_hash, git_sha, session_id on every trade
- pnl_pips field (FX uses pips, gate auto-detects via `_pnl_field`)
- log_dir: `forge/logs/wick_gbpusd/`

## Walk-forward results (Phase 2, 2026-04-16)
| Fold | Period | N | WR | PF | Exp (ATR) |
|---|---|---|---|---|---|
| 1 | 2003-12 → 2007-08 | 5 | — | — | — |
| 2 | 2007-08 → 2011-05 | 16 | 50.0% | 1.78 | +0.39 |
| 3 | 2011-05 → 2015-02 | 95 | 42.1% | 1.38 | +0.22 |
| 4 | 2015-02 → 2018-10 | 50 | 38.0% | 1.19 | +0.12 |
| 5 | 2018-10 → 2022-07 | 60 | 36.7% | 1.11 | +0.07 |
| 6 | 2022-07 → 2026-04 | 72 | 52.8% | 2.00 | +0.46 |
| **All** | **22 years** | **298** | **43.0%** | **1.42** | **+0.238** |
