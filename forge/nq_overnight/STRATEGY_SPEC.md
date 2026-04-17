# NQ Overnight Long — Strategy Specification

**Origin:** Discovered via label-first Phase 2C, 2026-04-16. See [`research/label_first/reports/FINDINGS_v3.md`](../../research/label_first/reports/FINDINGS_v3.md).

**Edge thesis:** NQ futures show consistent long-bias edge in overnight hours (post-US-close to Asian session). Hypothesis: institutional dollar-cost-averaging bid + Asian open buying create a small but persistent overnight drift. Walk-forward 5-6 of 6 folds positive across multiple hours.

## Walk-forward results (Phase 2C)

| Hour UTC | NY/Asia time | n | exp ATR | PF | Folds positive |
|---|---|---|---|---|---|
| 20 | 4pm NY (close) | 588 | +0.057 | **1.25** | **6/6** |
| 21 | 5pm NY | 238 | +0.062 | **1.29** | 5/6 |
| 22 | 6pm NY | 358 | +0.036 | 1.17 | 5/6 |
| 0 | 8pm NY (Asian open) | 592 | +0.036 | 1.20 | 5/6 |

## Entry rule
At top of hours 20:00, 21:00, 22:00, 23:00 UTC OR 00:00 UTC enter LONG NQ if:
1. Hour matches signal hour
2. ATR(14) finite and positive
3. No existing open position

## Exit rule
- **Target:** entry + 1.0 × ATR(14)
- **Stop:** entry - 0.5 × ATR(14)
- **Time exit:** close at end of bar 4 (4 hours after entry)
- **Order of evaluation per bar:** stop first, then target

## Position sizing
- Risk per trade: 1.0% of model equity ($10,000)
- NQ contract: $20 per point. Use micro NQ (MNQ) at $2 per point for $10K equity.

## Cadence
- 5 candidate hours × ~252 weekdays = ~1,260 candidate signals/year
- After ATR/sanity gates: ~1,200/year
- One-position-at-a-time → ~250-400 actual trades/year
- Funding gate (60 trades) achievable in **~1-2 months** of live operation

## Risks
- Modest edge per trade (PF 1.20-1.29 vs GLD's 1.6-2.3)
- Sample period = 2.5 years only (2023-2026) — bull market regime
- Overnight equity bias may not persist in downtrends
- Higher correlation with US equity exposure than GLD or GBPUSD

## Promotion gate compatibility
- Stage: discovery → watcher → paper → real
- Cohort tagging on every trade
- pnl_pts (futures, points-based)
- log_dir: `forge/logs/nq_overnight/`
