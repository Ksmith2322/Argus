# Argus Best-In-Class Gap Sweep - 2026-05-24

## What Was Fixed

- `forge_xs_momentum` now exposes market-data diagnostics in `--check`.
- `ops.daily_health_check` now includes a `data_feed` component for active external-feed strategies.
- `helio.yfinance_cache` no longer depends exclusively on parquet support. If `pyarrow` or `fastparquet` is unavailable, it writes and reads a pickle fallback.
- Tests now cover CSV fallback attribution, stale daily bars, daily-health data-feed reporting, and yfinance-cache fallback storage.

## Current Operating State

- Active roster is still exactly:
  - `forge_gld_pm_long` at 0.5x
  - `forge_xs_momentum` at 1.0x
- Roster audit is clean: 2 ACTIVE, 28 KILLED, 2 PENDING_OPT_IN, 0 LIMBO, 0 ABANDONED.
- Orphan phantom audit is clean: 0 killed-strategy phantom positions.
- `xs_momentum --check` currently ranks from CSV cache in this sandbox:
  - Picks: `GLD`, `EEM`
  - Data status: YELLOW because every ticker used `csv_cache`
  - Latest bars: 2026-05-21
  - Stale tickers: none under the 5-day max age policy
- Daily health remains RED because real-money preflight blocks both active strategies for evidence/allowlist reasons, not because of roster drift or orphan state.

## Missing To Be Best-In-Class

1. Primary/secondary data feed contract
   - Decide the production source of truth for ETF daily bars: IBKR historical, paid data, or yfinance.
   - Make the fallback hierarchy explicit: primary -> secondary -> last-known-good cache.
   - Alert when fallback is used for more than one scheduled decision cycle.

2. Data freshness runbook
   - Schedule a cache refresh task before monthly `xs_momentum` evaluation.
   - Fail closed if bars are stale beyond 5 calendar days.
   - Store data source, latest bar timestamp, and ranking timestamp in every rebalance record.

3. Portfolio overlap guard
   - `xs_momentum` currently selects `GLD` while `gld_pm_long` is also active.
   - Add a combined exposure cap by asset family so offense cannot accidentally double the defense sleeve.

4. Preflight-to-action mapping
   - Daily health says both active strategies are BLOCKED, but the report should name the exact unblock path:
     evidence count, allowlist state, deployment stage, or operator approval.

5. Evidence quality gate
   - No strategy should scale from post-reset state until canonical ENTRY and EXIT rows exist and match local trade logs.
   - Add a check that rejects EXIT-only ROI math and stale pre-reset evidence.

6. Legacy launcher quarantine
   - `ops/start_post_reset_runners.ps1` is now safe.
   - Any older broad launcher should either be deprecated in docs or patched to refuse killed/deallocated strategies.

7. Live-vs-cache drift report
   - When yfinance works, compare the latest live download against cache before overwriting.
   - Alert if close prices diverge beyond a small threshold.

8. Trade blocker attribution
   - Log every skipped trade with one normalized blocker code:
     `DATA_STALE`, `PREFLIGHT_BLOCKED`, `ALLOC_ZERO`, `NOT_DUE`, `BROKER_POSITION_EXISTS`, `CAP_EXCEEDED`, `BOUNDARY_BLOCKED`.
   - The dashboard should show blocked opportunities by blocker code.

9. Restart verification
   - After every code deploy, restart affected runners and verify heartbeat version/source fields changed.
   - A runner being "up" is not enough; it must be up on the expected commit/config version.

10. Offense/defense decision ledger
    - Each active strategy should declare whether it is offense, defense, hedge, or research.
    - Evaluation should use sleeve-specific gates: offense needs excess return; defense needs drawdown protection and low correlation.
