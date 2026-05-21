---
name: Scheduled task health — Argus task fleet semantics
description: How to read the live Argus scheduled-task state, what each LastTaskResult value means, and which tasks need cleanup vs operator action.
type: reference
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
# Scheduled task health — Argus

`Get-ScheduledTask -TaskName 'Argus*'` is the source of truth, not stored audit JSON. As of 2026-04-30 there are **25 Argus tasks** registered across the machine; only ~12 are actively healthy.

## LastTaskResult values you'll see

| Value | Meaning |
|-------|---------|
| `0` | Last run succeeded |
| `1` | Last run returned exit code 1 (script error or "no work" code) |
| `267011` | "Task has not yet run" (no historical run) |
| `4294967295` | `0xFFFFFFFF` = -1 = "task did not start" — usually means the action target is missing OR the runner it tried to launch was already up. Treat as DEPRECATED, not active failure. |

## Trigger types matter for staleness

A weekly trigger that "last ran 8 days ago" is fine; a daily trigger 8d old is broken. `readiness_eval._classify_task()` is trigger-aware:

| Trigger | Stale threshold |
|---------|-----------------|
| TimeTrigger (e.g. ArgusManagedTruth every 3min) | 2 days |
| DailyTrigger | 3 days |
| WeeklyTrigger | 14 days |
| BootTrigger | None — age means nothing; check whether the supervised process is actually alive |
| LogonTrigger | None — same |

## Current categorization (2026-04-30)

**DEPRECATED — safe to delete (8 tasks)**
LogonTrigger per-strategy loops superseded by forge runners:
- ArgusAudOrbLoop, ArgusCueBanksPaperLoop, ArgusGldPmLoop, ArgusMultiOrbLoop, ArgusNqLondonCloseLoop, ArgusSpyMeanRevLoop, ArgusToriPaperLoop, ArgusVixIntradayLoop

All have last_result=4294967295. The forge runners (`forge/<strategy>/runner.py --loop`) own these strategies now.

**DISABLED_STALE (2 tasks)**
- ArgusRefreshCandles (Disabled, age 35d)
- Argus_Nightly_Backtest (Disabled, age 48d)

**NEEDS RESTART, NOT DELETE (1 task)**
- ArgusWatchdog (BootTrigger, last_result=1, last fired 3/31). The watchdog process crashed 4/20 (last log entry); since trigger is At-Boot and machine hasn't rebooted, the supervisor has been DOWN for 10+ days. **Auto-recovery added 4/30** via `ops/watchdog_health_check.py` — runs every cycle from managed_truth_loop, detects stale `watchdog_managed.log` (>10min), and respawns. Internal Global mutex `ArgusManagedWatchdog` prevents double-launch. 30-min respawn cooldown prevents thrash. So this category should self-clear on next managed_truth_loop cycle.

**OPERATOR ACTION (1 task)**
- ArgusUSBBackup (DailyTrigger, last_result=1). `ops/backup_to_usb.ps1` exits 1 when no removable drive is detected. Last successful backup was 3/14 — USB drive has been unplugged for ~47 days. Plug in a USB drive and the next 3am run will succeed.

## Cleanup helper

```bash
cd c:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m ops.scheduled_task_cleanup
```

Lists deletion candidates + prints exact PowerShell to remove them. Does NOT auto-delete.
