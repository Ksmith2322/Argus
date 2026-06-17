---
name: 5/31 reset cutover runbook — step-by-step operator procedure
description: Explicit procedural checklist for the 2026-05-31 evening reset. Stop archived runners, run epoch_reset.py, advance evidence_epoch to post_reset_20260601, swap startup scripts, verify the 5-strategy post-reset roster. Designed so the operator can execute the cutover in 60-90 minutes without ambiguity.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# 2026-05-31 Reset Cutover — Operator Runbook

**When:** Friday 2026-05-31 evening, after US market close (≥21:00 UTC).
**Duration:** 60-90 minutes if everything goes smoothly. Allow 2-3 hours.
**Why:** Archives contaminated pre-freeze evidence; activates clean
post-reset epoch + the surviving 5-strategy roster.

## Pre-flight (the day before, 5/30)

- [ ] Read this runbook end-to-end.
- [ ] Verify no open positions in `position_monitor.json`. If any open
      positions exist, decide before 5/30 close: close them OR carry
      them over (carrying them means they straddle the reset).
- [ ] Pull latest from `phase6-hardening` branch. Confirm working tree clean.
- [ ] `python -m pytest argus_flow/tests/test_sunset_roster.py -v` — must be green.
- [ ] `python -m pytest argus_flow/tests/test_killed_strategy_invariant.py` — must be green.
- [ ] Glance at TWS — is it healthy? Restart if it's been up >7 days.

## Phase 1: Stop all runners (15 min)

```powershell
cd C:\Argus\repo

# 1.1 List every running python process for visibility
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | `
    Select-Object ProcessId, CommandLine | Format-List

# 1.2 Stop all argus + forge + greek runners (NOT TWS, NOT dashboard)
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match '-m\s+(argus_flow\.runner_unified|forge\.|apollo\.|hermes\.|titan\.|ares\.)' -or
    $_.CommandLine -match 'forge\.gdx_gld_runner|forge\.vix_revert_runner|forge\.rebalance_runner'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction Continue
    Write-Host "stopped PID=$($_.ProcessId) — $($_.CommandLine.Substring(0,[Math]::Min($_.CommandLine.Length,80)))"
}

# 1.3 Sanity: confirm no runners remain
Start-Sleep -Seconds 3
$remaining = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match '-m\s+(argus_flow|forge|apollo|hermes|titan|ares)'
}
if ($remaining) {
    Write-Warning "Still running: $($remaining.Count) processes — investigate before continuing"
    $remaining | Select-Object ProcessId, CommandLine
} else {
    Write-Host "OK: all runners stopped"
}
```

- [ ] Step 1.2 ran without errors.
- [ ] Step 1.3 confirmed no remaining runner processes.

## Phase 2: Final pre-reset snapshot (10 min)

```powershell
# 2.1 Dry-run capacity_stress for the record
C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_capacity_stress --anchor 30000

# 2.2 Final ROI proof on contaminated epoch (so we have the "before" picture)
C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_strategy_roi_proof --window post_reset

# 2.3 Final robustness snapshot
C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_strategy_robustness

# 2.4 Copy the canonical_fills + position_monitor + heartbeat snapshots
$snapDir = "argus_flow\logs\_archive\pre_reset_snapshots\$(Get-Date -Format yyyyMMdd_HHmm)"
New-Item -ItemType Directory -Force -Path $snapDir | Out-Null
Copy-Item argus_flow\logs\canonical_fills.jsonl $snapDir\
Copy-Item argus_flow\logs\position_monitor.json $snapDir\
Copy-Item -Recurse argus_flow\logs\_broker $snapDir\
Get-ChildItem forge\logs\*\heartbeat.json | Copy-Item -Destination $snapDir\ -Force
Get-ChildItem forge\logs\*\trades.csv | Copy-Item -Destination $snapDir\ -Force
Write-Host "snapshot at $snapDir"
```

- [ ] All three audit scripts produced JSON output without errors.
- [ ] Snapshot directory contains canonical_fills.jsonl + position_monitor.json + heartbeats + trades.

## Phase 3: Run epoch_reset (20 min)

```powershell
# 3.1 Dry-run FIRST — verify what would move
C:\Argus\.venv\Scripts\python.exe -m ops.maintenance.epoch_reset --target 20260531

# Scan the dry-run output:
#  - Confirm: canonical_fills.jsonl is in the move list
#  - Confirm: forge/logs/*/trades.csv are in the move list
#  - Confirm: allocation_factors.json is NOT in the move list (preserved)
#  - Confirm: real_money_allowlist.json is NOT in the move list (preserved)
#  - Confirm: any state.json with open_trade=null is in the move list
```

- [ ] Dry-run output reviewed; preserves match expectations.
- [ ] No file in the move list should be one the operator wants to keep.

```powershell
# 3.2 If dry-run looks right, execute
C:\Argus\.venv\Scripts\python.exe -m ops.maintenance.epoch_reset --target 20260531 --execute

# 3.3 Verify: canonical_fills.jsonl is empty (or absent — script touches a fresh one)
Get-Item argus_flow\logs\canonical_fills.jsonl -ErrorAction SilentlyContinue | `
    Select-Object Length, LastWriteTime

# 3.4 Verify: manifest written
Get-Content argus_flow\logs\_archive\pre_reset_20260531\_manifest.jsonl | `
    Measure-Object -Line | Select-Object Lines
```

- [ ] Execute completed without error.
- [ ] canonical_fills.jsonl is empty (size = 0 bytes) or absent.
- [ ] Manifest has the expected number of moved files.

## Phase 4: Advance evidence_epoch (5 min)

```powershell
# 4.1 Edit argus_flow/configs/evidence_epoch.json
#  Set current_epoch_id from "pre_freeze_20260418" to "post_reset_20260601"
#  Keep both epochs in the epochs[] array (history preserved).
```

```powershell
# 4.2 Verify the change loaded correctly
C:\Argus\.venv\Scripts\python.exe -c "from helio.evidence_epoch import current_epoch; ep = current_epoch(); print(f'id={ep.id} is_clean={ep.is_clean}')"

# Expected output:
#   id=post_reset_20260601 is_clean=True
```

- [ ] current_epoch_id advanced to post_reset_20260601.
- [ ] is_clean=True confirmed.

## Phase 5: Activate the post-reset roster (15 min)

```powershell
# 5.1 Edit argus_flow/configs/allocation_factors.json
#   Update forge_pead from 0.0 to 0.5
#   Update forge_xs_momentum from 0.0 to 0.5
#   Leave the 19 archived strategies at 0.0 (don't touch)
#   Increment version + last_updated
```

```powershell
# 5.2 Verify allocation factors loaded
C:\Argus\.venv\Scripts\python.exe -c @"
import json
data = json.load(open('argus_flow/configs/allocation_factors.json'))
print('version:', data.get('version'))
for s in ['forge_gld_pm_long','forge_nq_overnight','forge_pead','forge_xs_momentum','forge_spy_trend_follower']:
    print(f'  {s}: {data[\"factors\"].get(s, \"<missing>\")}')"@
```

- [ ] forge_pead = 0.5
- [ ] forge_xs_momentum = 0.5
- [ ] forge_gld_pm_long, forge_nq_overnight, forge_spy_trend_follower allocation unchanged

## Phase 6: Launch the post-reset cohort (10 min)

```powershell
# 6.1 Use the post-reset launcher (only the 5 survivors)
.\ops\start_post_reset_runners.ps1
```

- [ ] start_post_reset_runners.ps1 reported launching all 5 expected modules.
- [ ] No "already running" warnings (Phase 1 cleared them).

```powershell
# 6.2 Verify all 5 are alive 30 seconds later
Start-Sleep -Seconds 30
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'argus_flow.runner_unified|forge\.gld_pm_long|forge\.nq_overnight|forge\.pead|forge\.xs_momentum|forge\.spy_trend_follower'
} | Select-Object ProcessId, @{N='Module';E={if($_.CommandLine -match '-m\s+([\w\.]+)'){$matches[1]}}} | Format-Table
```

- [ ] 5 modules in the table (one per surviving strategy).
- [ ] No "already running" warnings; no missing modules.

## Phase 7: Smoke test the new system (15 min)

```powershell
# 7.1 Restart the dashboard so it picks up the new factors + epoch
Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | `
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
Start-Sleep -Seconds 2
Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' `
    -ArgumentList 'ops/dashboard.py','--port','8080' `
    -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden

# 7.2 Wait and check dashboard
Start-Sleep -Seconds 5
Invoke-RestMethod -Uri http://localhost:8080/api/gateway_status

# 7.3 Confirm the 5 survivors show alive in fleet_health
Invoke-RestMethod -Uri http://localhost:8080/api/fleet_health | ConvertTo-Json -Depth 4
```

- [ ] gateway_status returns 200.
- [ ] fleet_health shows the 5 survivors as alive/healthy.
- [ ] No archived strategy appears as "running" (should appear as DOWN or absent).

## Phase 8: Final verification + log (10 min)

```powershell
# 8.1 Run the post-reset validation tests one more time
C:\Argus\.venv\Scripts\python.exe -m pytest argus_flow/tests/test_sunset_roster.py argus_flow/tests/test_killed_strategy_invariant.py argus_flow/tests/test_evidence_epoch.py -v

# 8.2 First ROI proof against the (empty) post-reset epoch — sanity check
C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_strategy_roi_proof --window post_reset
# Expected: "No strategies had post-reset trades to evaluate" — that's correct
# at minute zero of the new epoch.

# 8.3 Confirm position_monitor sees no orphans
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.position_monitor
```

- [ ] All sunset + killed-strategy + evidence_epoch tests still green.
- [ ] ROI proof reports the expected "no trades" against the new epoch.
- [ ] position_monitor exit code = 0 (no CRITICAL alerts).

```powershell
# 8.4 Log the cutover for the record
$logEntry = @{
    cutover_ts = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    previous_epoch = "pre_freeze_20260418"
    new_epoch = "post_reset_20260601"
    surviving_strategies = @("forge_gld_pm_long","forge_nq_overnight","forge_pead","forge_xs_momentum","forge_spy_trend_follower")
    archived_count = 19
    snapshot_dir = "$snapDir"
    operator = "ksmith2322"
} | ConvertTo-Json
Add-Content -Path "argus_flow\logs\reset_history.jsonl" -Value $logEntry
```

- [ ] reset_history.jsonl entry added.

## Done. The bot is now running the post-reset 5-strategy roster on a clean epoch.

## Post-cutover monitoring (first week of June)

- **Daily:** check `/api/fleet_health` — all 5 alive, last 24h healthy.
- **Day 3:** first canonical_fills row should exist (assuming any of the
  5 fired). If forge_gld_pm_long + forge_nq_overnight haven't traded
  in 72h, investigate (TWS connect issue, not strategy issue).
- **Day 7:** first `run_strategy_roi_proof --window post_reset` snapshot
  with actual trades. Read it.
- **Day 14:** first capacity_stress refresh — verify the 4-layer caps
  are sized correctly for the new allocations.
- **Day 30 (6/30):** mid-cycle review. If forge_pead has ≥10 fills,
  run bootstrap CI. If forge_xs_momentum has rebalanced once, verify
  the picks match the backtest universe ranking.
- **Day 90 (9/1):** real-money go/no-go check. Bootstrap CIs must clear
  the 1.20 floor with margin OR defer another 90 days.

## Rollback procedure

If anything goes wrong after Phase 3 (epoch_reset --execute):
1. Stop all runners via Phase 1 commands.
2. Restore from manifest: `python -m ops.maintenance.epoch_reset --rollback --target 20260531`
   (NOTE: this command needs to exist; verify before relying on it.)
3. Revert allocation_factors.json + evidence_epoch.json from git.
4. Relaunch via the OLD `start_all_runners.ps1` (still in repo).

## Common failure modes

- **"TWS not detected"**: open TWS first, log in, accept any prompts,
  then re-run Phase 6.
- **A runner immediately crashes**: check `argus_flow/logs/start_all_runners_*.log`
  for the launch line. Most common: a state.json that survived the
  reset is malformed — delete the specific runner's state.json and relaunch.
- **canonical_fills.jsonl already has rows immediately after reset**:
  one of the runners is writing pre-restart-cached data. Stop the runner,
  truncate the file, restart.
- **Dashboard shows wrong allocation factors**: dashboard caches at boot;
  restart it (Phase 7.1).

## What this runbook does NOT cover

- Real-money flag flip (REAL_MONEY_ENABLED + allowlist) — that's the
  10/1+ decision, gated on the 90-day post-reset evidence accumulation.
- Strategy parameter tuning — frozen until 5/31; thereafter discussed
  case-by-case in weekly review.
- Adding new strategies — frozen entirely; not allowed in the post-reset
  window until at least one survivor produces 60+ clean trades.
