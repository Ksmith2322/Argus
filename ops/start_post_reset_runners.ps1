# ops/start_post_reset_runners.ps1 - Active post-reset fleet launcher
#
# Launches ONLY the current active offense/defense roster:
#
#   1. forge_gld_pm_long      defense / metals diversifier, 0.5x
#   2. forge_xs_momentum      offense / tactical factor beta, 1.0x
#
# Killed, pending-opt-in, and dormant reconciliation runners are intentionally
# not launched here. If allocation_factor=0.0, the runner must stay stopped
# unless a separate operator runbook explicitly launches it in signal-only or
# recovery mode.
#
# Usage:
#   .\ops\start_post_reset_runners.ps1             # launch missing active runners
#   .\ops\start_post_reset_runners.ps1 -DryRun     # show what would launch
#   .\ops\start_post_reset_runners.ps1 -RestartAll # restart only active runners

param(
    [switch]$DryRun,
    [switch]$RestartAll
)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"
$env:IBKR_PORT = '7497'
$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"
$logFile = "$logDir\start_post_reset_runners_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

$runners = @(
    @{Module='forge.gld_pm_long.runner'; Args=@('--loop')},
    @{Module='forge.xs_momentum.runner'; Args=@('--loop')}
)

# Anything in this list is not part of the active roster and must not be
# running before this launcher starts the live paper fleet. Pending opt-in
# strategies are included because allocation_factor=0.0 means "do not run."
$blockedModules = @(
    'argus_flow.runner_unified',
    'forge.aud_asian_breakout.runner', 'forge.wick_gbpusd.runner',
    'forge.jpy_pm_short.runner', 'forge.mamba.runner',
    'forge.tori.runner', 'forge.cuebanks.runner',
    'forge.vix_revert_runner', 'forge.fomc_drift.runner',
    'forge.tom_international.runner', 'forge.rebalance_runner',
    'forge.gdx_gld_runner', 'forge.atlas.runner', 'forge.themis.runner',
    'apollo.runner', 'hermes.runner', 'titan.runner', 'ares.runner',
    'forge.multi_orb.runner', 'forge.spy_mean_rev.runner',
    'forge.vix_intraday.runner', 'forge.nq_london_close.runner',
    'forge.nq_overnight.runner', 'forge.pead.runner',
    'forge.spy_trend_follower.runner',
    'forge.tom_spy.runner', 'forge.nov_spy.runner'
)

Log "=== Argus active post-reset fleet startup (target: $($runners.Count) runners) ==="

$twsRunning = Get-Process -Name 'tws' -ErrorAction SilentlyContinue
if (-not $twsRunning) {
    Log "WARNING: TWS process not detected. Runners may fail their initial IBKR connect; they will retry on next eval cycle."
} else {
    Log "TWS process detected."
}

function Get-RunningPythonModules {
    $modules = @{}
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
        $cmd = $_.CommandLine
        if ($cmd -and $cmd -match '-m\s+([\w\.]+)') {
            $modules[$matches[1]] = $true
        }
    }
    return $modules
}

$runningModules = Get-RunningPythonModules

$blockedRunning = @()
foreach ($module in $blockedModules) {
    if ($runningModules.ContainsKey($module)) { $blockedRunning += $module }
}
if ($blockedRunning.Count -gt 0) {
    Log "REFUSING TO LAUNCH: $($blockedRunning.Count) blocked runners still alive:"
    foreach ($module in $blockedRunning) { Log "  - $module" }
    Log "Stop them first; this launcher only permits the active roster."
    exit 2
}

$launched = 0
$skipped = 0
foreach ($runner in $runners) {
    $module = $runner.Module
    $alreadyRunning = $runningModules.ContainsKey($module)

    if ($alreadyRunning -and -not $RestartAll) {
        Log "  SKIP   $module (already running)"
        $skipped++
        continue
    }

    if ($alreadyRunning -and $RestartAll) {
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
            $_.CommandLine -match "-m\s+$([regex]::Escape($module))(\s|$)"
        }
        foreach ($proc in $procs) {
            try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop }
            catch { Log "    warn: failed to kill PID=$($proc.ProcessId): $_" }
        }
        Start-Sleep -Milliseconds 800
        Log "  KILLED $module ($(@($procs).Count) processes)"
    }

    if ($DryRun) {
        Log "  WOULD-LAUNCH $module $($runner.Args -join ' ')"
        continue
    }

    Log "  LAUNCH $module $($runner.Args -join ' ')"
    $argList = @('-m', $module) + $runner.Args
    Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    Start-Sleep -Milliseconds 800
    $launched++
}

Log "=== Done: launched=$launched, already-running=$skipped ==="

if (-not $DryRun) {
    Start-Sleep -Seconds 4
    $afterRunning = Get-RunningPythonModules
    $missing = @()
    foreach ($runner in $runners) {
        if (-not $afterRunning.ContainsKey($runner.Module)) { $missing += $runner.Module }
    }
    if ($missing.Count -gt 0) {
        Log "WARNING: these active runners did NOT come up after launch:"
        foreach ($module in $missing) { Log "  - $module" }
        exit 1
    }
    Log "Verified: all $($runners.Count) active post-reset runners are up."
}
