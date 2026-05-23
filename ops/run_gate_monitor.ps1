# ops/run_gate_monitor.ps1 -- daily wrapper for the live gate monitor
#
# Runs helio.live_gate_monitor to snapshot cohort gate status, then
# helio.auto_pause to compute persistent-FAIL alerts. Both outputs land
# in argus_flow/logs/.
#
# Scheduled by ops/register_tasks.ps1 to run daily at 23:30 local
# (post-market-close, before USB backup at 03:00).
#
# Exit code preserved from auto_pause (0/1/2 for OK/WARNING/PAUSE).

$ErrorActionPreference = "Continue"   # don't crash on one step failure
$LogDir = "C:\Argus\repo\argus_flow\logs"
$LogFile = Join-Path $LogDir "gate_monitor_cron.log"

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts INFO  cron: gate_monitor start" | Out-File -Append $LogFile -Encoding utf8

Set-Location "C:\Argus\repo"

# Step 1: snapshot
$python = "C:\Argus\.venv\Scripts\python.exe"
& $python -m helio.live_gate_monitor 2>&1 | Out-File -Append $LogFile -Encoding utf8
$monitorRc = $LASTEXITCODE
"$ts INFO  live_gate_monitor exit=$monitorRc" | Out-File -Append $LogFile -Encoding utf8

# Step 2: detect persistent-FAIL alerts (snapshot=False because monitor just snapshotted)
& $python -m helio.auto_pause --history-only 2>&1 | Out-File -Append $LogFile -Encoding utf8
$pauseRc = $LASTEXITCODE
"$ts INFO  auto_pause exit=$pauseRc" | Out-File -Append $LogFile -Encoding utf8

# Propagate the worst exit code (auto_pause's: 0/1/2)
$ts2 = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts2 INFO  cron: gate_monitor done (exit=$pauseRc)" | Out-File -Append $LogFile -Encoding utf8
exit $pauseRc
