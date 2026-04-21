# Register / update Argus scheduled tasks.
#
# MUST BE RUN FROM AN ELEVATED POWERSHELL.
# Run as Administrator, then:
#   cd C:\Argus\repo
#   .\ops\register_remaining_tasks.ps1
#
# Safe to re-run — each uses /F to overwrite if the task already exists.
# After running, if any task needs to survive RDP disconnect, also run:
#   schtasks /change /TN "<TaskName>" /RU "$env:USERDOMAIN\$env:USERNAME" /RP *

$ErrorActionPreference = "Stop"

# NOTE (2026-04-21 bug): schtasks /create has no "Working Directory" flag.
# When the bare python.exe is invoked as the task action, CWD defaults to
# system32 or user home, and `python -m forge.xxx.runner` fails to import
# because sys.path doesn't include C:\Argus\repo → the process exits with
# code 1 before even reaching log setup. Fix: wrap every task command in
# `cmd.exe /c "cd /d C:\Argus\repo && python -m ..."` so CWD is correct.

$python = "C:\Argus\.venv\Scripts\python.exe"
$repoDir = "C:\Argus\repo"

function Register-LoopTask($taskName, $moduleName) {
    $tr = "cmd.exe /c cd /d $repoDir && $python -m $moduleName --loop"
    schtasks.exe /create /TN $taskName /TR $tr /SC ONLOGON /RL LIMITED /F
}

# 1) GLD PM Long — runs 18/19/20 UTC signal hours via --loop.
Write-Host "Registering ArgusGldPmLoop..."
Register-LoopTask "ArgusGldPmLoop" "forge.gld_pm_long.runner"

# 2) Watchdog — auto-restart runner_unified on crash.
# watchdog.ps1 sets its own CWD on line 5, no cmd wrapper needed.
Write-Host "Registering ArgusWatchdog..."
schtasks.exe /create /TN "ArgusWatchdog" `
  /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" `
  /SC ONLOGON /RL HIGHEST /F

# 3) ArgusCohortReport — flip to survive RDP disconnect (needs password prompt).
Write-Host "Re-registering ArgusCohortReport (will prompt for password)..."
$user = "$env:USERDOMAIN\$env:USERNAME"
schtasks.exe /change /TN "ArgusCohortReport" /RU $user /RP *

# 4) NQ London Close Loop — 5m cadence during 16:00 UTC hour. PF 0.90 in
#    backtest (research-only), still paper-safe for data collection.
Write-Host "Registering ArgusNqLondonCloseLoop..."
Register-LoopTask "ArgusNqLondonCloseLoop" "forge.nq_london_close.runner"

# 5) AUD Asian Breakout Loop — hourly cadence during 01-07 UTC. PF 1.71 in
#    180d backtest, 26 trades.
Write-Host "Registering ArgusAudOrbLoop..."
Register-LoopTask "ArgusAudOrbLoop" "forge.aud_asian_breakout.runner"

# 5b) SPY Mean-Reversion Loop — 5m cadence during NY session (14-20 UTC).
#     Backtest: 34.6 trades/wk (cadence target MET), PF 1.03 (marginal).
#     RESEARCH_ONLY until params tuned; ships paper trades for OOS data.
Write-Host "Registering ArgusSpyMeanRevLoop..."
Register-LoopTask "ArgusSpyMeanRevLoop" "forge.spy_mean_rev.runner"

# 5c) VIX Intraday Mean-Rev Loop — 15m cadence during NY session.
#     Backtest: 15.2 trades/wk, PF 0.99. Adds volatility asset class.
#     RESEARCH_ONLY; collecting data for param tuning.
Write-Host "Registering ArgusVixIntradayLoop..."
Register-LoopTask "ArgusVixIntradayLoop" "forge.vix_intraday.runner"

# 5d) Multi-Instrument ORB Loop — 5m cadence, SPY/QQQ/IWM/GLD opening-range
#     breakout. Backtest: 19.3 trades/wk, PF 1.17 (positive edge, best of
#     the 3 high-cadence strategies shipped 2026-04-21).
Write-Host "Registering ArgusMultiOrbLoop..."
Register-LoopTask "ArgusMultiOrbLoop" "forge.multi_orb.runner"

# 6) Meta-watchdog — periodically restarts watchdog + fleet_monitor if dead.
#    Fires every 15 min regardless of logon state (uses SYSTEM account).
Write-Host "Registering ArgusMetaWatchdog (every 15 min)..."
schtasks.exe /create /TN "ArgusMetaWatchdog" `
  /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\meta_watchdog.ps1" `
  /SC MINUTE /MO 15 /RU SYSTEM /RL HIGHEST /F

Write-Host ""
Write-Host "Done. All tasks registered or updated."
Write-Host ""
Write-Host "To have the two new --loop tasks survive RDP disconnect (recommended):"
Write-Host "  schtasks /change /TN ArgusNqLondonCloseLoop /RU `"`$env:USERDOMAIN\`$env:USERNAME`" /RP *"
Write-Host "  schtasks /change /TN ArgusAudOrbLoop         /RU `"`$env:USERDOMAIN\`$env:USERNAME`" /RP *"
Write-Host "  schtasks /change /TN ArgusGldPmLoop          /RU `"`$env:USERDOMAIN\`$env:USERNAME`" /RP *"
Write-Host "  schtasks /change /TN ArgusWatchdog           /RU `"`$env:USERDOMAIN\`$env:USERNAME`" /RP *"
Write-Host ""
Write-Host "Verify with:"
Write-Host "  schtasks /query /TN ArgusNqLondonCloseLoop /FO LIST /V | Select-String 'Status|Logon Mode|Last Result'"
