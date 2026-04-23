# Fleet snapshot — pre paper-reset (2026-04-22)

Captured at: 2026-04-22T17:34:19.403121+00:00

Canonical fills rows: 163 | Closed EXIT records: 163

## Per-strategy totals (raw dollars — 100x inflated pre-clamp)

| Strategy | N | Wins | Win% | GrossW | GrossL | PF | PnL_raw | PnL_honest_$10K |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| argus_gbpusd | 2 | 1 | 100.0% | $3.90 | $0.00 | inf | $3.90 | $0.04 |
| argus_usdjpy | 9 | 3 | 42.9% | $4,096.77 | $10.94 | 374.48 | $4,085.83 | $40.86 |
| forge_gdx_gld | 87 | 65 | 74.7% | $19,454.36 | $10,379.49 | 1.87 | $9,074.87 | $90.75 |
| forge_gld_pm_long | 6 | 2 | 33.3% | $10,227.28 | $10,222.50 | 1.00 | $4.78 | $0.05 |
| forge_jpy_pm_short | 6 | 4 | 66.7% | $28,058.40 | $200.00 | 140.29 | $27,858.40 | $278.58 |
| forge_multi_orb | 23 | 5 | 21.7% | $108.62 | $212.12 | 0.51 | $-103.50 | $-1.03 |
| forge_nq_overnight | 2 | 1 | 50.0% | $3,400.00 | $85.59 | 39.72 | $3,314.41 | $33.14 |
| forge_spy_mean_rev | 24 | 16 | 66.7% | $16,811.08 | $8,037.11 | 2.09 | $8,773.97 | $87.74 |
| forge_vix_intraday | 4 | 3 | 75.0% | $6,096.24 | $16.70 | 365.04 | $6,079.54 | $60.80 |
| **FLEET TOTAL** | 163 | | | | | | **$59,092.20** | **$590.92** |

## Headline

- Reported fleet PnL: **$59,092.20**
- Honest-scaled to intended $10K anchor: **$590.92** (~5.91% of $10K)
- Anchor during bug window: ~$1,002,933 (IBKR paper default)
- Clamp landed: 2026-04-21 ~19:22 UTC (fleet_sizing.json v2026-04-21.v4)

## Open positions at snapshot

- **spy_mean_rev**: entry_ts=2026-04-22 17:30:00+00:00 entry_px=710.0399169921875 risk_usd=$848.150285993276
- **vix_intraday**: entry_ts=2026-04-22 17:15:00+00:00 entry_px=39.160099029541016 risk_usd=$49.86858940124414

## Notable trades (sorted by |PnL|, top 10)

| strategy | entry_ts | pnl_usd | honest_$10K |
|---|---|---:|---:|
| forge_gld_pm_long | 2026-04-21 19:30:00+00:00 | $10,028.13 | $100.28 |
| forge_jpy_pm_short | 2026-04-20 19:00:00+00:00 | $8,250.01 | $82.50 |
| forge_jpy_pm_short | 2026-04-20 19:00:00+00:00 | $8,250.01 | $82.50 |
| forge_vix_intraday | 2026-04-21 19:30:00+00:00 | $6,017.05 | $60.17 |
| forge_jpy_pm_short | 2026-04-20 19:00:00+00:00 | $5,779.19 | $57.79 |
| forge_jpy_pm_short | 2026-04-20 19:00:00+00:00 | $5,779.19 | $57.79 |
| forge_gld_pm_long | 2026-04-20 18:30:00+00:00 | $-5,011.92 | $-50.12 |
| forge_gld_pm_long | 2026-04-20 18:30:00+00:00 | $-5,011.92 | $-50.12 |
| forge_nq_overnight | 2026-04-19 22:00:00+00:00 | $3,400.00 | $34.00 |
| forge_gdx_gld | 2016-02-04 | $-2,636.24 | $-26.36 |

## What resets and what persists after paper reset

**Will reset** (IBKR paper reset wipes):
- Paper account cash balance → new target (e.g., $10K)
- All open broker positions (spy_mean_rev's current position)
- Broker-side trade history visible in TWS

**Will persist** (stored locally in repo):
- All trades.csv files per strategy
- canonical_fills.jsonl
- All signals, heartbeats, state files, dashboards

**Recommendation**: archive the pre-reset data so post-reset analysis isn't polluted.

## Active runners at snapshot