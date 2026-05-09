# Argus MTF Gate Diagnosis

Read-only diagnosis of post-reset CADJPY/GBPUSD/USDJPY signal artifacts.

- Total post-reset rows: 4472
- Rows with direction: 182
- MTF hard-blocked rows: 72
- MTF block share of all rows: 1.61%
- MTF block share of directional rows: 39.56%

## Top Block Reasons
- MTF_SHORT_NOT_AT_RESISTANCE: 45
- ENTRY_SUBMITTED: 32
- REAL_ENTRY_FAILED: 29
- RUNTIME_BLOCKED_RECON_DRIFT: 22
- MTF_LONG_NOT_AT_SUPPORT: 13
- MTF_SHORT_COUNTER_TREND_NO_RSI: 13
- SPREAD_BLOCKED: 12
- NEWS_BLOCKED: 7
- AI_OVERLAY_SKIP: 4
- STALE_TICKER_BLOCKED: 3

## Interpretation
Argus pairs are not dead from trade PnL evidence; they are blocked systems with no broker fills.
The next valid performance question is whether the MTF hard gate is preventing negative expectancy or blocking winners.
Do not loosen the gate until blocked rows are joined to forward outcome windows.

## Exact Next Test
Replay every MTF_BLOCKED row through the original stop, target, timeout, spread, and session model.
Pass gate: blocked losers outnumber blocked winners after friction, or the gate improves expectancy versus ungated entries.
Fail gate: blocked winners materially exceed blocked losers after friction across at least 60 post-reset candidates per pair.

## Largest Diagnosis Rows
- argus_usdjpy NO_TRIGGER: 1480 rows; Baseline market evaluations; not an entry gate failure.
- argus_gbpusd NO_TRIGGER: 1429 rows; Baseline market evaluations; not an entry gate failure.
- argus_cadjpy NO_TRIGGER: 1381 rows; Baseline market evaluations; not an entry gate failure.
- argus_gbpusd REAL_ENTRY_FAILED: 29 rows; Signal artifact; classify with opportunity ledger.
- argus_gbpusd MTF_SHORT_NOT_AT_RESISTANCE: 23 rows; High-value audit target: compare blocked opportunities to forward outcomes before changing thresholds.
- argus_gbpusd RUNTIME_BLOCKED_RECON_DRIFT: 22 rows; Blocked opportunity source; requires forward outcome scoring.
- argus_gbpusd ENTRY_SUBMITTED: 20 rows; Signal artifact; classify with opportunity ledger.
- argus_usdjpy MTF_SHORT_NOT_AT_RESISTANCE: 16 rows; High-value audit target: compare blocked opportunities to forward outcomes before changing thresholds.
- argus_gbpusd MTF_LONG_NOT_AT_SUPPORT: 13 rows; High-value audit target: compare blocked opportunities to forward outcomes before changing thresholds.
- argus_gbpusd MTF_SHORT_COUNTER_TREND_NO_RSI: 13 rows; High-value audit target: compare blocked opportunities to forward outcomes before changing thresholds.
- argus_gbpusd SPREAD_BLOCKED: 11 rows; Blocked opportunity source; requires forward outcome scoring.
- argus_cadjpy ENTRY_SUBMITTED: 9 rows; Signal artifact; classify with opportunity ledger.
