# Opportunity Shadow Ledger

This is a read-only normalization of post-reset signal and blocked-opportunity artifacts.
Future outcome fields are intentionally blank until bar-aligned forward windows are wired.

- Rows: 29651

## By Ledger Class
- SIGNAL_GENERATED: 13893
- OBSERVED: 10519
- NO_TRIGGER: 4983
- BLOCKED: 256

## Largest Strategy Sources
- forge_mamba: 13828
- forge_gdx_gld: 5291
- forge_nq_london_close: 2291
- forge_multi_orb: 1855
- argus_gbpusd: 1652
- argus_usdjpy: 1514
- argus_cadjpy: 1405
- forge_vix_intraday: 696
- forge_jpy_pm_short: 394
- forge_wick_gbpusd: 197
- forge_aud_asian_breakout: 192
- forge_nq_overnight: 148
- forge_gld_pm_long: 123
- forge_spy_trend_follower: 59
- forge_tom_international: 6

## Required Next Step
Join this ledger to bar data and closed-trade rules to calculate false negatives and false positives.
Until that join exists, blocked trade counts are throughput evidence, not expectancy evidence.
