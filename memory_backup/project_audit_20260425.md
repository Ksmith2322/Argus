---
name: 2026-04-25 weekly audit + fixes
description: First weekly audit since 4/24 IBKR conversion. Found 4 nights of cohort_report failures, gld_pm_long submitting dead orders Saturday, plus 3 audit false positives. All in-scope items fixed.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## What was wrong (in priority order)

1. **C1 — Cohort report failed 4 nights running (4/22, 4/23, 4/24, 4/25)** because `run_cohort_report.ps1` `throw`s when `refresh_managed_truth` returns exit code 2 (lock contention with the daemon). Apollo/hermes/titan/ares/gdx_gld/jpy_pm_short/gld_pm_long/wick_gbpusd weren't running nightly. The daemon itself was healthy; the issue was the redundant explicit invocation + a strict ps1.
2. **C2 — gld_pm_long spamming dead orders** every hour Saturday 16:05–22:05 UTC. No weekday/market-hours guard. IBKR returned PreSubmitted/Inactive (queued), runner logged them as failures, retried next hour. No orders made it to canonical_fills (verified — the saturday rejections never filled).
3. **C3 — cuebanks IBKR_CLIENT_ID=108 collided with aud_asian_breakout=108**. cuebanks is RESEARCH_ONLY=True so the collision was latent, but a flip would have caused TWS clash. (mamba/tori `IBKR_CLIENT_IDS` plural-dict was a false positive — those constants are unused; the live-path uses `IBKR_CLIENT_ID = 114/115`.)
4. **C5 — full_audit.py reads stale reconciliation_report.json** from 4 days ago, falsely flagging 4 strategies as DRIFT when the actual data reconciles cleanly. Fixed: when artifact is >24h, fall back to in-memory check.
5. **H6 — full_audit.py stale-lock detection used .lock file mtime alone**, missing that the .lock is set at first acquisition and doesn't refresh. Locks held by alive runners showed as 60–70h stale. Fixed: check the .json companion's PID for liveness via tasklist.
6. **H4 — themis QuiverQuant API 401**ing on every cycle. Free-tier no longer works without a key. Fixed: catch 401 → log + return empty list (themis is informational-only; missing data non-fatal).
7. **H3 — tori_paper crash loop** with `'list' object has no attribute 'get'` at paper_bridge.py:140. `run_backtest()` shape changed; defensive flatten added. tori_paper not running anyway — fix is preventive.
8. **H2 — ArgusMetaWatchdog** in audit's expected list but never registered. Removed from list (ArgusWatchdog already supervises).

## Fixed today (2026-04-25)

| File | Change |
|---|---|
| forge/gld_pm_long/runner.py | weekend guard before `_open()` (skip if `now_utc.weekday() >= 5`) |
| forge/cuebanks/runner.py | `IBKR_CLIENT_ID 108 → 116` (memory-aligned) |
| ops/run_cohort_report.ps1 | `throw` → log warning + continue when refresh_managed_truth lock-contended |
| ops/full_audit.py | reconciliation lens falls back to in-memory if artifact >24h |
| ops/full_audit.py | stale-lock detection now checks PID liveness via tasklist |
| ops/full_audit.py | dropped ArgusMetaWatchdog from expected tasks |
| forge/themis/fetcher.py | catch 401 → return [] gracefully |
| forge/tori/paper_bridge.py | defensive flatten for non-dict `all_trades` |

Restarts done: gdx_gld + gld_pm_long (with patch). Both running.

## Not fixed — needs user action

- **H1 — 3 scheduled tasks Interactive-only** (ArgusGldPmLoop, ArgusNqLondonCloseLoop, ArgusAudOrbLoop). schtasks /Change requires admin elevation OR password prompt. User must run elevated PowerShell:
  ```powershell
  $tasks = @('ArgusGldPmLoop','ArgusNqLondonCloseLoop','ArgusAudOrbLoop')
  foreach ($t in $tasks) {
      $tmp = "$env:TEMP\$t.xml"
      schtasks /Query /TN $t /XML | Out-File -Encoding unicode $tmp
      (Get-Content $tmp -Raw -Encoding Unicode) -replace '<LogonType>InteractiveToken</LogonType>','<LogonType>S4U</LogonType>' | Out-File -Encoding unicode $tmp
      schtasks /Create /F /TN $t /XML $tmp
      Remove-Item $tmp
  }
  ```
  S4U avoids password prompt (Service-for-User). Without admin = no fix.

- **ArgusCohortReport last_result=1, ArgusWatchdog last_result=1**: from yesterday's failure. Will clear when tonight's 23:00 EDT cohort runs successfully (with the patched ps1).

## Audit deltas

| metric | before fixes | after fixes |
|---|---|---|
| reconciliation_drift_strategies | 4 | 0 |
| stale_locks (>48h) | 5 | 0 |
| ArgusMetaWatchdog flag | "not registered" | (removed) |
| gld_pm_long REAL_ENTRY FAILED rate | 1/hour Saturday | 0 (weekend-guarded) |
| cohort_report success path | aborts at step 1 | continues past lock contention |

## How to apply this memory

**Why:** when the user returns Sunday or Monday and asks "did anything change?" or "audit results?", this is the audit-day record.

**How to apply:**
- If user asks about the cohort failures: cite C1 fix in run_cohort_report.ps1
- If user asks why GLD didn't fire Monday: check that tonight's market_hours guard didn't accidentally block weekday signals (the guard is `weekday() >= 5`; Mon=0 so it should NOT block)
- If H1 admin-required scheduled-task fix isn't applied by Monday and the user logs off: those 3 loop runners may not auto-restart. fleet_monitor.py auto-restart is `no_restart: True` for some — verify before assuming auto-recovery.
- The 7 uncommitted files in repo are this session's edits. User has not asked to commit.
