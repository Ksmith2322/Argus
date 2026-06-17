---
name: known fleet failure modes + diagnostic patterns
description: Operational failure modes seen in production with their symptoms, root causes, and fixes. First-look reference when something is "weirdly broken." Updated when new modes are discovered.
type: reference
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Diagnostic gotcha — `risk_oversight_report.json` field paths

When checking broker equity from the JSON, use the right paths or you'll see `$0.00` and chase a phantom bug:

- **Correct:** `d["broker_truth"]["account_equity_usd"]` ← real equity lives here
- **Top-level timestamp:** `d["timestamp"]` (NOT `generated_at`)
- **`broker_equity_usd`** is a top-level key ONLY in *downstream* consumers (fleet_state, dashboard) that re-export. It does NOT exist in the report file itself. Reading `d.get("broker_equity_usd")` at top level returns `None`, which a naive diagnostic prints as `$0.00`.

This bit me 2026-04-29 — chased a "the report writer is broken" trail before realizing my bash one-liner was reading wrong paths.

## #1 — Duplicate managed_truth daemon → silent cohort failure

**Symptoms (in order you'd notice them):**
- ares hasn't fired in N days (last `signal_*.json` in `ares/logs/` is stale)
- apollo/hermes/titan `orders.csv` files don't exist or haven't grown
- gdx_gld_runner hasn't been triggered
- `argus_flow/logs/cohort_report.log` shows lines like `=== Nightly cohort report complete (WITH FAILURES) ===` and `refresh_managed_truth ERROR: exit code 2` repeating nightly
- `schtasks /Query /TN 'ArgusCohortReport' /FO LIST /V` shows `Last Result: 1`

**Root cause:**
Two `managed_truth_loop` daemons running concurrently — usually because a session restart launched a second daemon without killing the first. Both try to acquire `argus_flow/logs/_locks/managed_truth_refresh.lock`; the loser writes `refresh lock already held and existing status is stale or failed` to its log and exits with code 2. `run_cohort_report.ps1` calls `refresh_managed_truth` first and `throws` on non-zero exit (lines 14-15), so the entire downstream chain (apollo, hermes, titan, ares, gdx_gld) never runs.

**Detection command:**
```powershell
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like '*managed_truth*' } | Select-Object ProcessId, CreationDate
```
If more than 2 PIDs (the venv launcher + actual interpreter pair = 2 PIDs per daemon → 4 total = 2 daemons), you have a duplicate.

**Fix:**
1. Kill the OLDER daemon's PIDs: `Stop-Process -Id <older_venv_pid>,<older_interpreter_pid> -Force`
2. Optionally clear the lock to let the survivor reclaim cleanly: `Remove-Item 'C:\Argus\repo\argus_flow\logs\_locks\managed_truth_refresh.lock' -Force` (it'll be re-acquired within seconds by the survivor)
3. Tonight's 23:00 cohort_report will run normally
4. Verify next morning: cohort_report.log shows `=== Nightly cohort report complete ===` (no "WITH FAILURES")

**Prevention (deferred — flag if recurs):**
Could add a "kill existing managed_truth_loop instances on launch" check to the daemon's startup. Or have the daemon write its PID to `_locks/managed_truth_daemon.pid` on start and refuse to start if one exists + alive.

**Detection alert wired (2026-04-25):** `ops/cohort_failure_check.py` runs hourly via managed_truth_loop. Scans `cohort_report.log` for "WITH FAILURES" within last 25h, alerts Discord with 24h cooldown. So if this happens again, you'll know within 1 hour, not 4 days.

**First seen:** 2026-04-22 (failures started). **Diagnosed + fixed:** 2026-04-25. **Alert wired:** 2026-04-25.

---

## #2 — Stale `open_trade` in heartbeat from pre-conversion era

**Symptoms:**
- Dashboard `/api/positions_open` shows positions
- But `/api/gateway_status broker_truth.fleet_open_risk_usd = $0` and `account_equity_usd` unchanged
- Heartbeat.json `open_trade` block lacks the `execution_venue` field
- The `git_sha` in the open_trade dict points to a commit BEFORE the IBKR-conversion commit (`fca278f` and later)

**Root cause:**
A forge runner had an open position in `state.json` from before the 2026-04-24 IBKR conversion. After conversion + restart, the runner reads the old state but the position was never on the broker — it was a signal-only simulation. The heartbeat continues to show the position (cosmetic) but no real broker exposure exists.

**Detection:**
```bash
cat c:/Argus/repo/forge/logs/<strategy>/state.json | grep -E "execution_venue|git_sha"
```
- No `execution_venue` field → stale signal-only state
- `git_sha` from before `fca278f` → pre-conversion state

**Fix:**
None needed — first new signal fires in `_open()` will overwrite state with `execution_venue=ibkr_paper`. If you want to clear it manually:
```bash
# Edit state.json and set "open_trade": null, save
```

**Self-resolves once markets reopen and a new signal fires.**

---

## #3 — TWS socket disconnect → runner exits with `ConnectionError: Socket disconnect`

**Symptoms:**
- A specific runner shows DOWN in `/api/fleet_health`, `process_alive=false`
- Log shows: `[ERROR] <runner>: Fatal error: Socket disconnect` followed by an `asyncio.exceptions.CancelledError` traceback
- TWS may have restarted, hit a connection cycle, or the network path glitched

**Detection:**
```bash
grep -i "socket disconnect\|ConnectionError" c:/Argus/repo/forge/logs/<strategy>/runner.log | tail -3
```

**Fix (one of):**
- Restart the dead runner: `Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList '-m','<module>','--loop' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden` (with `$env:IBKR_PORT='7497'`)
- If many runners died at once → TWS itself probably blipped. Often need to restart argus first to refresh broker_truth, then trigger `risk_oversight` manually before other runners can boot

**Why we don't auto-restart yet:**
`fleet_monitor.py` is configured with `no_restart: True` for some runners; auto-restart logic isn't fully wired. Manual fix for now. Per `project_deferred_cleanups_20260424.md`, hardening this is a planned tightening.

### Sub-pattern: overnight TWS API death (observed 4/29 + 4/30 mornings)

**Symptom:** TWS UI is up and running, but `tws_health_probe` reports `unreachable` every morning around 8am ET. Probably IBKR's nightly server reset at ~23:45 ET kills the API session even though the desktop app stays alive.

**Detection:** Discord alert fires automatically (added 2026-04-30) when `tws_health_probe` sees a transition from `healthy` → `unreachable` / `degraded` / `error`. Single alert per outage, not per probe cycle.

**Recovery (one command after re-auth):**
```bash
# 1. Re-authenticate in the TWS UI (manual, can't automate — needs credentials)
# 2. Then run:
cd c:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m ops.tws_recover
```

`ops/tws_recover.py` runs `tws_health_probe` + `risk_oversight` in sequence and reports back. ~1s end-to-end. Hard-refresh the browser to clear cached banners.

**Recovery alert:** when the probe transitions back to `healthy` it sends a second Discord message confirming recovery.

---

## #4 — Argus `FRIDAY_CLOSE` rule blocks entries Friday evening through weekend

**Symptoms:**
- argus shows `BLOCKED` status in fleet_health
- All 3 pairs heartbeats show `entries_blocked: True`, `entry_block_reason: "FRIDAY_CLOSE"`
- Position status is FLAT
- Process is alive, broker is connected

**This is INTENTIONAL not a bug.** Argus has a built-in safety rule to flatten FX positions and block new entries before the weekend (forex spot accumulates rollover risk + Sunday open gap risk).

**Auto-clears:** Sunday evening when London/Tokyo opens. Don't manually clear.

**Detection:**
```powershell
Get-Content 'C:\Argus\repo\argus_flow\logs\usdjpy\heartbeat.json' | ConvertFrom-Json | Select-Object entries_blocked, entry_block_reason
```

---

## #5 — Stale-status false positive (4hr-cycle runners)

**Symptoms:**
- A runner like `forge_tori` shows STALE in fleet_health with `max_age_s` ~14400-18000 seconds (~4-5 hours)
- Process is alive
- Latest log line shows "live cycle complete; sleeping 14400s"

**Root cause:**
Runner has a long sleep interval (4 hours for tori) but `fleet_monitor` thresholds default to ~5min staleness. STALE status is misleading — runner is healthy, just on a slow cadence.

**Fix:**
Per-runner stale threshold tuning in `helio/fleet_monitor.py`. For tori specifically, threshold should be ≥4hr+buffer (~16000s).

**Detection:**
Check runner's `--loop` interval in source. If `time.sleep(14400)` or similar long sleep, the STALE alert is noise.

---

## #6 — PDT (Pattern Day Trader) reject cascade

**Symptoms:**
- TWS popup: "Pattern Day Trade Reject — your order was rejected because you have $X in your securities segment, required minimum is $25,000"
- Strategy log shows repeating "TIME_STOP close failed: Inactive" or "Order rejected" every eval cycle
- After-hours order queue accumulates ("SELL X SPY ARCA" warnings about orders queued for next session open)
- Strategy state still shows position "open" but broker may not (state drift)

**Root cause:**
US securities segment equity is below $25,000 → IBKR enforces PDT rule, blocking >3 day-trades in 5 trading days. Each rejected exit attempt counts as a fresh order, escalating the popup count. Strategy never learns its exit failed → keeps retrying every cycle. After-hours queues compound the mess.

**Detection:**
```powershell
$tws_summary = & 'C:\Argus\.venv\Scripts\python.exe' -c "
from ib_insync import IB
ib = IB(); ib.connect('127.0.0.1', 7497, clientId=199, timeout=10)
for t in ib.accountSummary():
    if t.tag in ('NetLiquidation','TotalCashValue','StockMarketValue'):
        print(f'{t.tag}: {t.value} {t.currency}')
ib.disconnect()
"
```
If StockMarketValue/equity in securities segment < $25K → PDT will fire on >3 day trades.

**Fix:**
- Reset paper account to ≥ $30K via TWS Client Portal (gets above PDT threshold with margin).
- Verify state drift didn't leave phantom positions: check `forge/logs/*/state.json` for stale `open_trade` records and clear them.
- Sizing math should prevent recurrence: per fleet_sizing.json v7, stock/etf cap is 0.3× equity ($9K on $30K) — risk per position kept small enough that PDT can't trigger from small entries.

**First seen:** 2026-04-27 (first day of real fills after IBKR conversion). **Resolved same day** via account reset + helper hardening (see `project_helper_fixes_20260427.md`).

---

## #7 — OCO bracket double-fire (stop AND target both filled)

**Symptoms:**
- TWS Trade history shows BOT N → SLD N → SLD N (or SLD N → BOT N → BOT N for shorts) — three fills where there should be two
- Net broker position is OPPOSITE direction of strategy's stored direction
- Strategy log records ONE exit but TWS shows TWO closing fills
- Strategy state thinks it's still in the original position; broker shows the unintended counter-position

**Root cause (FIXED 2026-04-27):**
`helio/ibkr_execution.py:submit_bracket()` was placing stop and target as 3 independent orders (entry, stop, target) without `ocaGroup` linking. When one bracket leg fills, the other stays live and can fill on a subsequent price excursion → unintended opposite-direction position.

**Detection:**
- Compare broker position (via `query_position`) to strategy state's open_trade record. Mismatch direction = bracket double-fire.
- Look for "BOT N + SLD N + SLD N" pattern in TWS trade history within minutes.

**Fix (already applied):**
`submit_bracket` now sets `ocaGroup` and `ocaType=1` on both stop and target → IBKR cancels the other leg when one fills. If you encounter a double-fire predating the fix, manually flatten via a wide-LMT `outsideRth=True` BUY/SELL order (see ops/_flatten_spy_oneoff.py pattern from 2026-04-27).

**First seen:** 2026-04-27 SPY (BOT 16 → target SLD → stop SLD = unintended SHORT 16). **Fix landed same day.**

---

## #8 — `check_bracket_filled` doesn't survive process restart

**Symptoms:**
- Strategy enters position (broker shows fill), bracket orders placed
- Runner is restarted (e.g., overnight, after a deploy, after fleet_reboot)
- After restart: bracket fills happen at broker but strategy never knows
- Time-stop eventually fires; strategy queries broker, finds qty=0, records `reconcile_flat` exit at WRONG price (current bar's close, not the actual broker fill)
- trades.csv has wrong-priced exits with `exit_reason="reconcile_flat"` and `duration_min` close to the strategy's hold_bars limit

**Root cause (FIXED 2026-04-27):**
Old `check_bracket_filled` looked up fills by `orderId` via `ib.openTrades()` / `ib.trades()`. `orderId` is **session-scoped to the client_id connection**. After the runner disconnects (end of `evaluate_once()`) and reconnects (next cycle's `evaluate_once()`), the new IB session can't see fills from the previous session's orders.

**Detection:**
Look in `forge/logs/<strategy>/trades.csv` for rows where:
- `exit_reason = reconcile_flat`
- `duration_min` ≈ strategy's `hold_bars × bar_minutes` (i.e., max-hold ceiling)
- `exit_px` doesn't match TWS-recorded actual fill

**Fix (already applied):**
`check_bracket_filled` now uses **position-based detection** (compare broker position to expected) + **`reqExecutions` for fill price** (works across sessions). Callers updated to pass `entry_direction`, `entry_size`, `stop_px`, `target_px` for stop/target classification (otherwise reason defaults to "broker_exit").

**First seen:** 2026-04-27 multi_orb QQQ/IWM/GLD all hit reconcile_flat with wrong prices. **Fix landed same day.**

---

## #9 — Cohort report silent failure from PowerShell unicode parse error

**Symptoms:**
- `argus_flow/logs/cohort_report.log` stops being appended (last-modified date stuck for days)
- `argus_flow/logs/fleet_perf_summary.json` and `promotion_readiness_report.json` go stale (last-modified dates lag the current date by N days)
- `Get-ScheduledTaskInfo -TaskName 'ArgusCohortReport'` shows `LastTaskResult` = 1 (or other non-zero) but no log entries explain why
- Manual run: `powershell.exe -File ops/run_cohort_report.ps1` errors with `Missing closing '}'`, `Unexpected token`, `Missing expression after unary operator '--'`

**Root cause:**
PowerShell on Windows reads `.ps1` files using the system codepage (usually cp1252 on US English). When a file contains UTF-8 multibyte characters (like em-dash `—` U+2014, encoded as `0xE2 0x80 0x94`), PowerShell decodes them as garbage and the parser falls over. The script ON DISK appears valid in any UTF-8 editor but Windows PowerShell rejects the entire file before executing a single line. Result: the scheduled task "runs" (Task Scheduler launches powershell.exe), gets a parse error immediately, exits with non-zero, **writes nothing to the script's own log file**. Silent failure looks identical to "task ran but did nothing."

This happened 2026-04-27: a comment + log-line edit on 2026-04-25 used em-dash characters; cohort_report ran for ZERO of 4/25, 4/26, 4/27 nights. fleet_perf_summary.json was 6 days stale by detection time.

**Detection:**
```bash
# Check if any non-ASCII bytes lurk in PowerShell scripts
python -c "
import sys
for fp in ['ops/run_cohort_report.ps1', 'ops/start_all_runners.ps1']:
    src = open(fp, 'rb').read()
    bad = [(i,b) for i,b in enumerate(src) if b > 127]
    if bad:
        print(f'{fp}: {len(bad)} non-ASCII bytes (will break PowerShell parser)')
"
# Or just try running it manually:
powershell.exe -File C:/Argus/repo/ops/run_cohort_report.ps1
```

**Fix:**
Replace any non-ASCII characters with ASCII equivalents:
```python
text = open('ops/run_cohort_report.ps1', encoding='utf-8').read()
text = text.replace('—', '-').replace('–', '-').replace(''', "'").replace(''', "'").replace('"', '"').replace('"', '"').replace('…', '...')
open('ops/run_cohort_report.ps1', 'w', encoding='utf-8').write(text)
```

**Prevention:**
- When editing `.ps1` files via tools that may insert smart quotes / em-dashes (some markdown processors, AI-generated text), audit for non-ASCII bytes after save.
- Could also force PowerShell to read as UTF-8 via shebang-equivalent: `#Requires -PSEdition Core` then declare encoding. Pwsh 7+ defaults to UTF-8 but the system's `powershell.exe` (5.1) used by scheduled tasks does not.

**First seen + fixed:** 2026-04-27 evening (this session).

---

## #10 — TWS overnight server reset → data farms dead despite "connected"

**Symptoms:**
- Morning check after IBKR's nightly server reset (~23:45 ET / 04:45 UTC):
- Dashboard `/api/gateway_status broker_equity_usd = $0` despite `broker_connected=True` and pair `connected=True`
- TWS process is running (visible in Task Manager / `Get-Process tws`)
- Argus runner heartbeats stop refreshing (per-pair `age_s` grows past several thousand seconds)
- TWS popup appears: **"UNRECOGNIZED USERNAME OR PASSWORD"** or session-terminated dialog
- Direct API probe via ib_insync: `ib.connect()` succeeds but `ib.positions()` and `ib.accountSummary()` time out
- Argus log shows the trigger: `Warning 2110: Connectivity between Trader Workstation and server is broken` followed by *every* data farm marked broken (`hfarm`, `usfarm.nj`, `jfarm`, `usfuture`, `usopt.nj`, `cashfarm`, `eufarmnj`, `usfarm`, plus HMDS farms)

**Root cause:**
IBKR runs a daily server-side reset around 23:45 ET. TWS keeps its TCP socket open but loses authentication with the data backbone. The API layer accepts new client connections (so `ib.connect()` works) but no actual market data or account info flows. Auto-recovery USUALLY works but FAILS roughly weekly in our experience — the TWS popup is IBKR re-prompting for credentials and the pop-up sits there until manually dismissed.

The crucial diagnostic: `broker_connected=True AND broker_equity_usd=$0` is the canary. Healthy state has `broker_equity_usd > 0`.

**Detection (pre-RTH probe):**
```bash
cd c:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m ops.tws_health_probe
# Exit codes:
#   0 — healthy (data farms alive, NetLiq > 0)
#   1 — TWS API unreachable (TWS down or wrong port)
#   2 — degraded (connected but data farms dead) ← the overnight-reset state
#   3 — unexpected error
```

Also surfaced via dashboard `/api/tws_health` and a degraded-state banner at top of page.

**Fix:**
1. **User action**: re-login to TWS using paper account credentials (DUP472829). Watch TWS bottom-right indicator turn green for all data farms.
2. **System action** (run after re-login):
```bash
# Restart argus + gdx_gld which lost their sessions during the outage
powershell.exe -ExecutionPolicy Bypass -File c:/Argus/repo/ops/start_all_runners.ps1 -RestartAll
```
3. Other forge runners auto-reconnect on next eval cycle; no need to restart them.

**Prevention:**
- Schedule `ops/tws_health_probe.py` to run at 09:15 ET weekdays (15 min before RTH open). Sends Discord alert if FAIL — gives operator 15 min lead time to fix login.
- Wired into `managed_truth_loop` hourly checks too (so any TWS degrade during the day gets alerted within 1 hour).

**First seen + diagnosed:** 2026-04-28 morning (this incident).

---

## #11 — ArgusWatchdog supervisor crash → silent unmonitored fleet

**Symptoms:**
- `argus_flow/logs/watchdog_managed.log` last-modified is hours/days old (healthy = updated every ~60s)
- No supervisory Discord alerts when runners drift, dashboard goes down, or TWS dies
- `Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'watchdog_managed' }` returns nothing
- Yet `Get-ScheduledTask -TaskName 'ArgusWatchdog'` shows `State=Ready, LastTaskResult=1` (looks fine if you don't notice the LastRunTime is days-stale)

**Root cause:**
The ArgusWatchdog scheduled task uses a **BootTrigger** — only fires at machine startup. The script itself is `while ($true)` infinite-loop. If the supervisor process crashes mid-session (memory pressure, exception in a Discord call, nightly host event), nothing brings it back until the next OS reboot. The fleet runs unsupervised silently.

**First seen:** 2026-04-30 — discovered the watchdog had been DOWN since 2026-04-20 (10 days unsupervised). Supervisor responsibilities partially absorbed by `managed_truth_loop` hourly checks, but the master reconcile / Helio family supervision / IB Gateway watcher were all dark.

**Detection:**
```bash
# Compare log mtime to current time
python -c "
import os, time
from pathlib import Path
p = Path('C:/Argus/repo/argus_flow/logs/watchdog_managed.log')
age_min = (time.time() - p.stat().st_mtime) / 60
print(f'Watchdog log age: {age_min:.1f} min — {\"DEAD\" if age_min > 10 else \"alive\"}')
"
```

**Fix (manual, one-shot):**
```powershell
Start-ScheduledTask -TaskName 'ArgusWatchdog'
# OR directly:
Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','C:\Argus\repo\ops\watchdog_managed.ps1' -WindowStyle Hidden
```

The script's internal `Global\ArgusManagedWatchdog` mutex prevents double-launch — safe to run even if you're unsure whether one's already alive.

**Auto-recover (wired 2026-04-30):**
`ops/watchdog_health_check.py` runs every 3-min cycle from `managed_truth_loop`. Detects stale `watchdog_managed.log` (>10 min) and respawns. 30-min respawn cooldown prevents thrash if the watchdog is crash-looping. Discord alerts on every respawn so we know it happened.

So this failure mode now self-heals within 3 minutes of detection, with operator visibility via Discord.

---

## How to use this memory

When user says "X is broken" or "weird thing happening":
1. Match symptoms to one of the failure modes above
2. Run the detection command to confirm
3. Apply the fix (or note that it's intentional / self-resolves)
4. If a new failure mode is discovered, ADD IT HERE with same structure (symptom → root cause → detection → fix)
