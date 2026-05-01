# apply_game_plan_changes.ps1 - restart sequence for the 2026-04-30 master game plan batch.
#
# This script does NOT auto-apply. It prints the exact sequence of commands the
# operator should review before running. Run with -DryRun (default) to see the
# plan; -Execute to actually run it.
#
# Pre-requisites:
#  - Code changes are already committed (cuebanks bug fix, multi_orb window 24->32,
#    vix_intraday 17 UTC blackout, argus pairs configs, start_all_runners.ps1 updates).
#  - This script restarts affected runners so they pick up the new configs/code.
#
# Affected runners + reason:
#   tier 1 KILL (stop, do not restart): apollo, hermes, titan, forge_rebalance
#   bug fix (restart to pick up): forge_cuebanks, forge_multi_orb, forge_vix_intraday
#   config change (restart to pick up): argus (cadjpy, gbpusd via runner_unified)
#   launch flag change (restart fleet): forge_tori, forge_mamba (now --live)
#   restart needed: forge_gdx_gld (process is dead 18 days), forge_fomc_drift (died 4/24)
#
# Usage:
#   .\ops\apply_game_plan_changes.ps1                # dry-run (shows plan)
#   .\ops\apply_game_plan_changes.ps1 -Execute       # actually apply
[CmdletBinding()]
param(
    [switch]$Execute
)

$ErrorActionPreference = "Continue"
$repo = "C:\Argus\repo"
$python = "C:\Argus\.venv\Scripts\python.exe"

function Write-Plan { param([string]$msg) Write-Host "[PLAN] $msg" -ForegroundColor Cyan }
function Write-Step { param([string]$msg) Write-Host "  -> $msg" -ForegroundColor Yellow }

Write-Host "================================================================"
Write-Host "  ARGUS GAME PLAN APPLY - 2026-04-30 batch"
Write-Host "  Mode: $(if ($Execute) { 'EXECUTE' } else { 'DRY-RUN' })"
Write-Host "================================================================"
Write-Host ""

# Step 1: Stop Tier 1 KILL processes
Write-Plan "Step 1 - Stop Tier 1 KILL processes (apollo, hermes, titan, forge.rebalance_runner)"
$killModules = @('apollo.runner', 'hermes.runner', 'titan.runner', 'forge.rebalance_runner')
foreach ($mod in $killModules) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$mod*" }
    if ($procs) {
        foreach ($p in $procs) {
            Write-Step "Stop $mod PID=$($p.ProcessId)"
            if ($Execute) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        }
    } else {
        Write-Step "$mod - not running (already stopped)"
    }
}
Write-Host ""

# Step 2: Stop runners that need to pick up config/code changes
Write-Plan "Step 2 - Stop runners with config/code changes for restart"
$restartModules = @(
    'forge.cuebanks.runner',     # bug fix: sd_zones param
    'forge.multi_orb.runner',    # window 24->32
    'forge.vix_intraday.runner', # 17 UTC blackout
    'forge.tori.runner',         # --loop -> --live
    'forge.mamba.runner',        # --loop -> --live
    'forge.gdx_gld_runner',      # dead 18 days, restart
    'forge.fomc_drift.runner',   # dead 6 days, restart
    'argus_flow.runner_unified'  # cadjpy stage, gbpusd hour_filter
)
foreach ($mod in $restartModules) {
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$mod*" }
    if ($procs) {
        foreach ($p in $procs) {
            Write-Step "Stop $mod PID=$($p.ProcessId) (will restart)"
            if ($Execute) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        }
    } else {
        Write-Step "$mod - not running (will start fresh)"
    }
}
Write-Host ""

# Step 3: Wait for processes to clean up
Write-Plan "Step 3 - Wait 5s for clean shutdown"
if ($Execute) { Start-Sleep -Seconds 5 }

# Step 4: Re-launch via start_all_runners.ps1 (reads the updated launch table)
Write-Plan "Step 4 - Launch fleet via updated start_all_runners.ps1"
Write-Step "Command: powershell.exe -ExecutionPolicy Bypass -File $repo\ops\start_all_runners.ps1"
if ($Execute) {
    & powershell.exe -ExecutionPolicy Bypass -File "$repo\ops\start_all_runners.ps1"
}
Write-Host ""

# Step 5: Refresh dashboard so it picks up the new verdict/factor/heartbeats
Write-Plan "Step 5 - Restart dashboard to refresh state"
$dashProc = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue
if ($dashProc) {
    Write-Step "Dashboard listening on 8080 (PID=$($dashProc.OwningProcess))"
    if ($Execute) {
        Stop-Process -Id $dashProc.OwningProcess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
}
Write-Step "Start: Start-Process $python ops/dashboard.py --port 8080"
if ($Execute) {
    Start-Process -FilePath $python -ArgumentList 'ops/dashboard.py','--port','8080' -WorkingDirectory $repo -WindowStyle Hidden
}
Write-Host ""

# Step 6: Verify
Write-Plan "Step 6 - Verify (post-execute checks)"
Write-Step "Wait 30s for runners to settle, then:"
Write-Step "  curl -s http://localhost:8080/api/fleet_health"
Write-Step "  curl -s http://localhost:8080/api/gateway_status"
Write-Host ""

if (-not $Execute) {
    Write-Host "DRY-RUN complete. Review the plan above." -ForegroundColor Green
    Write-Host "Re-run with -Execute to apply." -ForegroundColor Green
} else {
    Write-Host "EXECUTION complete. Verify with:" -ForegroundColor Green
    Write-Host "  Start-Sleep 30; curl -s http://localhost:8080/api/fleet_health" -ForegroundColor Green
}
