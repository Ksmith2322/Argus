# ops/start_post_reset_runners.ps1 - Post-2026-05-31 reset fleet launcher
#
# Launches ONLY the 5 surviving strategies from the 5/31 sunset.
# Per project_2026_05_31_sunset_decisions.md + project_2026_05_31_reset_runbook.md:
#
#   1. forge_gld_pm_long      (live, proven, 0.4x cap)
#   2. forge_nq_overnight     (live, on probation, brake-halved 0.5x)
#   3. forge_pead             (NEW, paper-candidate, 0.5x cap post-reset)
#   4. forge_xs_momentum      (NEW, paper-candidate, 0.5x cap post-reset)
#   5. forge_spy_trend_follower (passive beta benchmark)
#
# Plus argus_flow.runner_unified for the 3 FX pairs — kept running BUT
# allocation_factor=0.0 so it cannot place orders. The runner is alive
# only so paper-state reconciliation works on the existing positions.
# Operator decision needed by 6/15 on whether to remove argus_flow
# entirely or keep it dormant.
#
# Usage:
#   .\ops\start_post_reset_runners.ps1            # Launch missing runners
#   .\ops\start_post_reset_runners.ps1 -DryRun    # Show what would launch
#   .\ops\start_post_reset_runners.ps1 -RestartAll # Kill all and relaunch
#
# This script REPLACES start_all_runners.ps1 after the 5/31 cutover.
# The old script is retained in-repo for rollback purposes.

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

# Post-reset runner inventory (5 surviving strategies + dormant argus_flow)
$runners = @(
    # Argus FX runner — kept alive for paper-state reconciliation only.
    # Allocation 0.0 in allocation_factors.json prevents new orders.
    @{Module='argus_flow.runner_unified'; Args=@('--configs',
        'argus_flow/configs/cadjpy_mtf_paper_v1.json',
        'argus_flow/configs/gbpusd_range_paper_v1.json',
        'argus_flow/configs/usdjpy_mtf_paper_v1.json')},

    # Forge gld_pm_long — proven live winner, real-fill evidence PF=2.73
    @{Module='forge.gld_pm_long.runner'; Args=@('--loop')},

    # Forge nq_overnight — on probation (74% drawdown from $710 peak)
    @{Module='forge.nq_overnight.runner'; Args=@('--loop')},

    # Forge pead — NEW. Backtest PF=2.04 (curated) / 1.61 (non-curated).
    # Activated at 0.5x post-reset (see allocation_factors v9 or later).
    # Daily evaluation at 13:35 UTC (just after US open) so the runner
    # picks up overnight earnings announcements.
    @{Module='forge.pead.runner'; Args=@('--loop')},

    # Forge xs_momentum — NEW. Backtest PF=2.05 over 9 years. Only candidate
    # with 95% CI lower bound above 1.20 promotion floor. Activated 0.5x
    # post-reset. Monthly rebalance; --evaluate fires at month-start.
    # Loop mode sleeps until next month boundary.
    @{Module='forge.xs_momentum.runner'; Args=@('--evaluate')},

    # Forge spy_trend_follower — passive beta benchmark. 4-hr loop.
    @{Module='forge.spy_trend_follower.runner'; Args=@('--signal-only','--loop','--interval-min','240')}
)

Log "=== Argus POST-RESET fleet startup (target: $($runners.Count) runners) ==="

$twsRunning = Get-Process -Name 'tws' -ErrorAction SilentlyContinue
if (-not $twsRunning) {
    Log "WARNING: TWS process not detected. Runners will fail their initial IBKR connect; they'll retry on next eval cycle."
} else {
    Log "TWS process detected."
}

$runningModules = @{}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd -and $cmd -match '-m\s+([\w\.]+)') {
        $runningModules[$matches[1]] = $true
    }
}

# Refuse to launch if an ARCHIVED runner is currently up. The sunset
# requires all archived runners to be stopped before the post-reset
# launch — otherwise we have a mixed fleet and the 5/31 reset is invalid.
$archivedModules = @(
    'forge.aud_asian_breakout.runner', 'forge.wick_gbpusd.runner',
    'forge.jpy_pm_short.runner', 'forge.mamba.runner',
    'forge.tori.runner', 'forge.cuebanks.runner',
    'forge.vix_revert_runner', 'forge.fomc_drift.runner',
    'forge.tom_international.runner', 'forge.rebalance_runner',
    'forge.gdx_gld_runner', 'forge.atlas.runner', 'forge.themis.runner',
    'apollo.runner', 'hermes.runner', 'titan.runner', 'ares.runner',
    'forge.multi_orb.runner', 'forge.spy_mean_rev.runner',
    'forge.vix_intraday.runner', 'forge.nq_london_close.runner'
)
$archivedRunning = @()
foreach ($am in $archivedModules) {
    if ($runningModules.ContainsKey($am)) { $archivedRunning += $am }
}
if ($archivedRunning.Count -gt 0) {
    Log "REFUSING TO LAUNCH: $($archivedRunning.Count) archived runners still alive:"
    foreach ($am in $archivedRunning) { Log "  - $am" }
    Log "Stop them first (Phase 1 of project_2026_05_31_reset_runbook.md)."
    exit 2
}

$launched = 0; $skipped = 0
foreach ($r in $runners) {
    $module = $r.Module
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
        foreach ($p in $procs) {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop }
            catch { Log "    warn: failed to kill PID=$($p.ProcessId): $_" }
        }
        Start-Sleep -Milliseconds 800
        Log "  KILLED $module ($(@($procs).Count) processes)"
    }

    if ($DryRun) {
        Log "  WOULD-LAUNCH $module $($r.Args -join ' ')"
        continue
    }

    Log "  LAUNCH $module $($r.Args -join ' ')"
    $argList = @('-m', $module) + $r.Args
    Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    Start-Sleep -Milliseconds 800
    $launched++
}

Log "=== Done: launched=$launched, already-running=$skipped ==="

if (-not $DryRun) {
    Start-Sleep -Seconds 4
    $afterRunning = @{}
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
        if ($_.CommandLine -and $_.CommandLine -match '-m\s+([\w\.]+)') {
            $afterRunning[$matches[1]] = $true
        }
    }
    $missing = @()
    foreach ($r in $runners) {
        if (-not $afterRunning.ContainsKey($r.Module)) { $missing += $r.Module }
    }
    if ($missing.Count -gt 0) {
        Log "WARNING: these runners did NOT come up after launch:"
        foreach ($m in $missing) { Log "  - $m" }
    } else {
        Log "Verified: all $($runners.Count) post-reset runners are up."
    }
}
