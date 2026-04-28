# ops/meta_watchdog.ps1 - supervisor for the supervisors.
#
# Purpose: the primary watchdog.ps1 and fleet_monitor.py can themselves die
# (2026-03-31 incident: watchdog exited with Last Result 1 and stayed dead
# for weeks, with nobody restarting IT). This script runs as a scheduled
# task every 15 minutes and restarts either of them if not running.
#
# Runs as SYSTEM account (survive-logoff, no password needed for built-in).
# Per-run execution window <5 seconds in the steady state.

$ErrorActionPreference = "Continue"
$logFile = "C:\Argus\repo\argus_flow\logs\meta_watchdog.log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    try { Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue } catch {}
}

function IsProcessRunning($cmdPattern) {
    try {
        $procs = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and ($_.CommandLine -match $cmdPattern)
        }
        return @($procs).Count -gt 0
    } catch {
        return $false
    }
}

Log "=== meta_watchdog check ==="

# 1) Is watchdog.ps1 running?
if (-not (IsProcessRunning 'watchdog\.ps1')) {
    Log "watchdog.ps1 is NOT running - restarting"
    Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NonInteractive", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", "C:\Argus\repo\ops\watchdog.ps1"
    ) -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    if (IsProcessRunning 'watchdog\.ps1') {
        Log "watchdog.ps1 restart OK"
    } else {
        Log "ERROR: watchdog.ps1 restart FAILED"
    }
} else {
    Log "watchdog.ps1 OK"
}

# 2) Is fleet_monitor running?
if (-not (IsProcessRunning 'helio\.fleet_monitor')) {
    Log "helio.fleet_monitor is NOT running - restarting"
    Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" -ArgumentList @(
        "-m", "helio.fleet_monitor", "--interval-s", "60", "--no-restart"
    ) -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if (IsProcessRunning 'helio\.fleet_monitor') {
        Log "fleet_monitor restart OK"
    } else {
        Log "ERROR: fleet_monitor restart FAILED"
    }
} else {
    Log "fleet_monitor OK"
}

# 3) Is the runner itself running? (last-resort defense if watchdog is wedged)
if (-not (IsProcessRunning 'argus_flow\.runner_unified')) {
    Log "runner_unified is NOT running - flagging only (watchdog should restart it)"
    # Don't restart here; let watchdog handle it. Just surface the fact.
}

Log "=== meta_watchdog check complete ==="
