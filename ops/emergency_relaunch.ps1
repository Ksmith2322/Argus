# Emergency fleet relaunch after the 2026-04-28 kill incident.
#
# Context: VS Code Python extension was auto-spawning system-Python duplicates
# of every .venv Python process. We killed the duplicates surgically, which
# unexpectedly killed 22 of 26 .venv parents as collateral damage (subprocess
# tied state). This script relaunches the dead runners + dashboard + watchdogs.
#
# Each Start-Process detaches from the current shell so they survive after
# this script exits. -WindowStyle Hidden keeps the desktop clean. The venv
# Python is hardcoded so this works regardless of PATH order.
#
# Run from c:/Argus/repo:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File ops/emergency_relaunch.ps1

$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$repoRoot = "C:\Argus\repo"
# 2026-05-25: default to IB Gateway paper (4002). Override before running
# this script if you specifically need TWS instead.
if (-not $env:IBKR_PORT) { $env:IBKR_PORT = "4002" }

Set-Location $repoRoot

Write-Host "=== Emergency Fleet Relaunch ===" -ForegroundColor Cyan
Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Gray
Write-Host ""

# Module paths mirror STRATEGY_REGISTRY in ops/operational_vetting.py.
# Two-element pairs: [module_path, args]. argus_flow.runner_unified is
# already running so it's omitted here.
$runners = @(
    @{ Mod = "forge.gdx_gld_runner"; Args = "--loop" }
    @{ Mod = "forge.gld_pm_long.runner"; Args = "--loop" }
    @{ Mod = "forge.jpy_pm_short.runner"; Args = "--loop" }
    @{ Mod = "forge.nq_overnight.runner"; Args = "--loop" }
    @{ Mod = "forge.nq_london_close.runner"; Args = "--loop" }
    @{ Mod = "forge.aud_asian_breakout.runner"; Args = "--loop" }
    @{ Mod = "forge.spy_mean_rev.runner"; Args = "--loop" }
    @{ Mod = "forge.multi_orb.runner"; Args = "--loop" }
    @{ Mod = "forge.vix_intraday.runner"; Args = "--loop" }
    @{ Mod = "forge.mamba.runner"; Args = "--loop" }
    @{ Mod = "forge.tori.runner"; Args = "--loop" }
    @{ Mod = "forge.cuebanks.runner"; Args = "--loop" }
    @{ Mod = "forge.vix_revert_runner"; Args = "--loop" }
    @{ Mod = "forge.rebalance_runner"; Args = "--loop" }
    @{ Mod = "forge.wick_gbpusd.runner"; Args = "--loop" }
    @{ Mod = "forge.fomc_drift.runner"; Args = "--loop" }
    @{ Mod = "forge.tom_international.runner"; Args = "--loop" }
    @{ Mod = "forge.atlas.runner"; Args = "--loop" }
    @{ Mod = "forge.themis.runner"; Args = "--loop" }
    @{ Mod = "apollo.runner"; Args = "--loop" }
    @{ Mod = "hermes.runner"; Args = "--loop" }
    @{ Mod = "titan.runner"; Args = "--loop" }
)

Write-Host "--- Strategy runners ---" -ForegroundColor Yellow
$launched = 0
foreach ($r in $runners) {
    $argList = "-m $($r.Mod) $($r.Args)"
    try {
        Start-Process -FilePath $pyExe -ArgumentList $argList -WorkingDirectory $repoRoot -WindowStyle Hidden -ErrorAction Stop
        Write-Host "  [OK] $($r.Mod)" -ForegroundColor Green
        $launched += 1
        Start-Sleep -Milliseconds 500
    }
    catch {
        Write-Host "  [FAIL] $($r.Mod) -- $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "--- Infrastructure (dashboard + monitors) ---" -ForegroundColor Yellow

# Dashboard
try {
    Start-Process -FilePath $pyExe -ArgumentList "ops/dashboard.py","--port","8080" -WorkingDirectory $repoRoot -WindowStyle Hidden -ErrorAction Stop
    Write-Host "  [OK] ops/dashboard.py (port 8080)" -ForegroundColor Green
} catch { Write-Host "  [FAIL] dashboard -- $_" -ForegroundColor Red }

# fleet_monitor (--no-restart so it doesn't try to restart things while we're stabilizing)
try {
    Start-Process -FilePath $pyExe -ArgumentList "-m","helio.fleet_monitor","--interval-s","60","--no-restart" -WorkingDirectory $repoRoot -WindowStyle Hidden -ErrorAction Stop
    Write-Host "  [OK] helio.fleet_monitor (--no-restart)" -ForegroundColor Green
} catch { Write-Host "  [FAIL] fleet_monitor -- $_" -ForegroundColor Red }

# managed_truth_loop
try {
    Start-Process -FilePath $pyExe -ArgumentList "-m","argus_flow.ops.managed_truth_loop" -WorkingDirectory $repoRoot -WindowStyle Hidden -ErrorAction Stop
    Write-Host "  [OK] argus_flow.ops.managed_truth_loop" -ForegroundColor Green
} catch { Write-Host "  [FAIL] managed_truth_loop -- $_" -ForegroundColor Red }

Write-Host ""
Write-Host "Launched $launched strategy runners + 3 infrastructure processes." -ForegroundColor Cyan
Write-Host "Waiting 20s for processes to register..." -ForegroundColor Gray
Start-Sleep -Seconds 20

Write-Host ""
Write-Host "--- Process count check ---" -ForegroundColor Yellow
$venvCount = (Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -match '\.venv' }).Count
$systemCount = (Get-CimInstance Win32_Process -Filter "name='python.exe'" | Where-Object { $_.CommandLine -notmatch '\.venv' -and $_.CommandLine -match '-m' }).Count
Write-Host "  venv python processes:   $venvCount"
Write-Host "  system python processes: $systemCount  (should ideally be 0; non-zero = VS Code attach is still active)"
Write-Host ""
Write-Host "Dashboard: http://localhost:8080" -ForegroundColor Cyan
