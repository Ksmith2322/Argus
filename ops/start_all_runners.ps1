# ops/start_all_runners.ps1 - Canonical fleet launcher
#
# Launches any of the 23 fleet runners (argus_flow + 17 forge + 4 Greek + atlas/themis)
# that are not currently running. Idempotent - safe to run repeatedly.
#
# Usage:
#   .\ops\start_all_runners.ps1               # Launch missing runners
#   .\ops\start_all_runners.ps1 -DryRun       # Show what would launch
#   .\ops\start_all_runners.ps1 -RestartAll   # Kill all and relaunch (use after code changes)
#
# Recommended: register as Task Scheduler "ArgusFleetStartup" task with LogonTrigger.
# Pre-existing per-runner LogonTrigger tasks (ArgusGldPmLoop, etc.) are harmless because
# this script's idempotency check skips already-running modules.

param(
    [switch]$DryRun,
    [switch]$RestartAll
)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"
$env:IBKR_PORT = '7497'
$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"
$logFile = "$logDir\start_all_runners_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

# -- Canonical runner inventory --------------------------------------
# Adjust here if a runner needs different flags. This is the single source of truth.
$runners = @(
    @{Module='argus_flow.runner_unified'; Args=@('--configs',
        'argus_flow/configs/cadjpy_mtf_paper_v1.json',
        'argus_flow/configs/gbpusd_range_paper_v1.json',
        'argus_flow/configs/usdjpy_mtf_paper_v1.json')},
    @{Module='forge.gld_pm_long.runner';      Args=@('--loop')},
    @{Module='forge.jpy_pm_short.runner';     Args=@('--loop')},
    @{Module='forge.nq_overnight.runner';     Args=@('--loop')},
    # 2026-04-30: forge.spy_mean_rev.runner KILLED. Already at factor 0.0
    # via allocation_factors.json; no point spawning the process. Re-add
    # if a successor mean-reversion strategy is built.
    # @{Module='forge.spy_mean_rev.runner';     Args=@('--loop')},
    @{Module='forge.vix_intraday.runner';     Args=@('--loop')},
    @{Module='forge.nq_london_close.runner';  Args=@('--loop')},
    @{Module='forge.aud_asian_breakout.runner'; Args=@('--loop')},
    # 2026-05-07: forge.multi_orb.runner KILLED. Already at factor 0.0 via
    # allocation_factors.json. n=161 real trades, PF=0.83, ROI=-12% (plus 7
    # phantoms worth -$1,194). Tried v1 24-bar / v3 32-bar / v4 24-bar revert,
    # plus the safe_position_size sizing-floor fix. Thesis exhausted — equity
    # ETF opening-range breakout doesn't have edge as configured. Re-add only
    # if a successor breakout strategy is built with new evidence.
    # @{Module='forge.multi_orb.runner';        Args=@('--loop')},
    @{Module='forge.fomc_drift.runner';       Args=@('--loop')},
    @{Module='forge.tom_international.runner'; Args=@('--loop')},
    @{Module='forge.wick_gbpusd.runner';      Args=@('--loop')},
    @{Module='forge.vix_revert_runner';       Args=@('--loop')},
    # 2026-04-30: tori/mamba switched --loop -> --live so signals actually
    # submit orders. They were silent because --loop is signal-only.
    @{Module='forge.mamba.runner';            Args=@('--live')},
    @{Module='forge.tori.runner';             Args=@('--live')},
    # 2026-05-07 audit: cuebanks was still on --loop despite 4/30 sd_zones bug fix.
    # That's why 0 signals fired post-fix: runner was in signal-only mode all week.
    @{Module='forge.cuebanks.runner';         Args=@('--live')},
    # 2026-04-30: forge.rebalance_runner KILLED — silent calendar-event scanner,
    # no events pending in May, validated PF 1.31 backtest but execution friction
    # often eats it. Revisit post-5/31 if Q3 events appear.
    # @{Module='forge.rebalance_runner';        Args=@('--loop')},
    # 2026-05-07 audit: gdx_gld silent-deaths every ~22:00 UTC. Capture stderr
    # so the next death leaves a diagnostic trace (asyncio crashes, ib_insync
    # Connection_lost, segfaults — anything not emitted via Python logger).
    @{Module='forge.gdx_gld_runner';          Args=@('--live','--loop'); CaptureStderr=$true},
    @{Module='forge.atlas.runner';            Args=@('--loop','--interval-sec','120')},
    @{Module='forge.themis.runner';           Args=@('--loop','--interval-min','360')},
    # 2026-05-03: SPY trend-follower (50/200 SMA regime). Long-equity-beta
    # sleeve to close the 8.34pp SPY gap. 5y backtest PF 22.19 / 70% capture
    # of buy-and-hold. Deploy in signal-only first, promote to --live after
    # 2-3 cycles validate regime detection. 4hr loop is plenty for daily strategy.
    @{Module='forge.spy_trend_follower.runner'; Args=@('--signal-only','--loop','--interval-min','240')}
    # 2026-04-30: apollo/hermes/titan KILLED. Per master game plan:
    #   - apollo: earnings calendar scanner, data feed broken Q2 2026, no automated execution
    #   - hermes: backtest PF=0.88 (NEGATIVE expectancy on 20-trade history)
    #   - titan: no backtest, no clear edge thesis, stale state
    # Removed from auto-launch. Re-add manually if reactivated.
    # @{Module='apollo.runner';                 Args=@('--loop','--live')},
    # @{Module='hermes.runner';                 Args=@('--execute','--loop','--interval-min','30')},
    # @{Module='titan.runner';                  Args=@('--loop','--live')}
)

Log "=== Argus fleet startup (target: $($runners.Count) logical runners) ==="

# Optional TWS readiness - log warning, don't block. Runners will fail their first eval
# and retry - better to log than refuse to start the entire fleet because TWS is slow.
$twsRunning = Get-Process -Name 'tws' -ErrorAction SilentlyContinue
if (-not $twsRunning) {
    Log "WARNING: TWS process not detected. Runners will fail their initial IBKR connect; they'll retry on next eval cycle."
} else {
    Log "TWS process detected."
}

# Build set of currently-running modules
$runningModules = @{}
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd -and $cmd -match '-m\s+([\w\.]+)') {
        $runningModules[$matches[1]] = $true
    }
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
        # Kill all matching processes (launcher pair)
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
    if ($r.ContainsKey('CaptureStderr') -and $r.CaptureStderr) {
        $stderrTag = ($module -replace '[\.\\/]', '_')
        $dateTag = (Get-Date).ToString('yyyyMMdd')
        $stderrLog = Join-Path $logDir "${stderrTag}_stderr_${dateTag}.log"
        Log "    stderr -> $stderrLog"
        Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden -RedirectStandardError $stderrLog
    } else {
        Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    }
    Start-Sleep -Milliseconds 800
    $launched++
}

Log "=== Done: launched=$launched, already-running=$skipped ==="

if (-not $DryRun) {
    Start-Sleep -Seconds 4
    # Verify
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
        Log "Verified: all $($runners.Count) runners are up."
    }
}
