---
name: Weekly audit routine (end-of-week)
description: 10-minute end-of-week sweep. Run when markets are closed. Layered: mechanical full-audit + manual quick checks for things audit code can miss.
type: reference
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## When to run

End of each week, markets closed (Friday evening / Saturday). Or after any major change.

## Step 1 — Mechanical (30 sec)

```bash
cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m ops.full_audit --diff
```

Writes dated report to `docs/audits/<stamp>/report.md` with auto-diff vs prior.

## Step 2 — Quick reads (5 min)

The audit code can miss things. Always sanity-check these:

1. **Cohort canary** — `tail -30 c:/Argus/repo/argus_flow/logs/cohort_report.log`. Any "WITH FAILURES" in the last 7 nights? Investigate root cause; the 2026-04-25 fix made the throw non-fatal but the underlying lock contention still gets logged.
2. **DOWN runners** — `cat c:/Argus/repo/argus_flow/logs/fleet_status.json | grep -B1 '"DOWN"'`. Each DOWN needs investigation.
3. **REAL_ENTRY failures** — `for d in c:/Argus/repo/forge/logs/*/runner.log; do grep -c "REAL_ENTRY FAILED\|REAL_ENTRY EXCEPTION" "$d" 2>/dev/null; done`. Any non-zero spikes since last week.
4. **Scheduled-task last_result** — `schtasks /Query /FO TABLE | grep Argus`. Anything ≠ 0 needs investigation.
5. **gdx_gld socket disconnects** — endemic to that runner; if count >2/day, investigate TWS stability.
6. **drawdown_pause** state — `curl -s http://localhost:8080/api/risk_state | python -c 'import json,sys; print(json.load(sys.stdin)["portfolio_risk_state"])'`. If pause=true, that's a real event.

## Step 3 — Memory snapshot (2 min)

If anything novel was diagnosed, append to:
- `reference_failure_modes.md` — new failure modes with symptom→cause→detection→fix
- `project_audit_<YYYYMMDD>.md` — fresh memory entry per audit, summarizing the week

## Step 4 — Fixes (variable)

Apply documented fixes immediately. Park larger investigations in `project_deferred_cleanups_<date>.md`.

## What's allowed to be RED long-term

These flags can persist without alarm:

- `apollo_forward_returns: STALE` — refreshes only when cohort_report runs successfully
- `drawdown_dd%` near 100% — informational, not a fault unless `drawdown_pause=true`
- `signal_to_entry_pct = 0` on weekends — markets closed
- `forge_tori STALE` >4h — that's its sleep cadence (per failure mode #5)
- `1 DEGRADED` in operational_maturity — surfaces issues, doesn't itself indicate breakage

## What MUST be green

- `broker_equity_usd` fresh + sensible (~$11.8K + interest accrual)
- `reconciliation_drift_strategies: []` (post-2026-04-25 audit fix)
- `stale_locks: []` (post-2026-04-25 audit fix; only counts dead-owner locks now)
- All scheduled tasks last_result=0
- IBKR port 7497 listening
- `pause_entries: false`
- 3 argus pairs all `broker_connected: true`

## Audit deltas worth tracking week-over-week

- `canonical_fills_live` count (should grow weekly during market days)
- `errors_24h` (should decrease over time as fleet stabilizes)
- `divergent_strategies` (should stay 0)
- `signal_to_entry_pct` (track week-over-week trend; sustained drops = gating regression)
- `dispositions_count` (track promotions / kills)
