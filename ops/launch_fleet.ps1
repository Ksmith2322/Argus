# ops/launch_fleet.ps1 -- Launch entire Argus IBKR FX fleet
# Starts: runner_unified.py, dashboard, discord trade watcher
# Usage: .\ops\launch_fleet.ps1
#        .\ops\launch_fleet.ps1 -SkipDashboard
#        .\ops\launch_fleet.ps1 -SkipDiscord

param(
    [switch]$SkipDashboard,
    [switch]$SkipDiscord
)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path "$logDir\fleet_launch.log" -Value $line -ErrorAction SilentlyContinue
}

function Is-Running($pattern) {
    $procs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match $pattern
        } catch { $false }
    }
    return $procs
}

Log "=== Fleet Launch ==="

# 1. Unified Runner
$runner = Is-Running "runner_unified"
if ($runner) {
    Log "Runner already running (PID=$($runner.Id)). Skipping."
} else {
    Log "Starting runner_unified.py..."
    Start-Process -FilePath $python `
        -ArgumentList "-m", "argus_flow.runner_unified", "--configs", `
            "argus_flow/configs/gbpusd_range_paper_v1.json", `
            "argus_flow/configs/eurusd_t4_paper_v1.json", `
            "argus_flow/configs/eurjpy_t4_paper_v1.json" `
        -WorkingDirectory "C:\Argus\repo" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 5
    $check = Is-Running "runner_unified"
    if ($check) { Log "Runner started: PID=$($check.Id)" }
    else { Log "WARNING: Runner may not have started!" }
}

# 2. Dashboard
if (-not $SkipDashboard) {
    $dash = Is-Running "dashboard"
    if ($dash) {
        Log "Dashboard already running (PID=$($dash.Id)). Skipping."
    } else {
        Log "Starting dashboard..."
        Start-Process -FilePath $python `
            -ArgumentList "ops/dashboard.py", "--port", "8080" `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 3
        Log "Dashboard started on http://localhost:8080"
    }
}

# 3. Discord trade watcher
if (-not $SkipDiscord) {
    $discord = Is-Running "discord_alerts.*--watch"
    if ($discord) {
        Log "Discord watcher already running (PID=$($discord.Id)). Skipping."
    } else {
        Log "Starting Discord trade watcher..."
        Start-Process -FilePath $python `
            -ArgumentList "-m", "argus_flow.ops.discord_alerts", "--watch" `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 2
        Log "Discord watcher started"
    }
}

# 4. Run smoke test
Log "Running smoke test..."
try {
    & $python -m argus_flow.ops.smoke_test 2>&1 | ForEach-Object { Log "  $_" }
} catch {
    Log "Smoke test error: $_"
}

Log "=== Fleet Launch Complete ==="
Write-Host ""
Write-Host "Fleet Status:" -ForegroundColor Cyan
Write-Host "  Runner:    $(if (Is-Running 'runner_unified') { 'RUNNING' } else { 'NOT RUNNING' })"
Write-Host "  Dashboard: $(if (Is-Running 'dashboard') { 'RUNNING (http://localhost:8080)' } else { 'NOT RUNNING' })"
Write-Host "  Discord:   $(if (Is-Running 'discord_alerts') { 'RUNNING' } else { 'NOT RUNNING' })"
