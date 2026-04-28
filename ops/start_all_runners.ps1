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
    @{Module='forge.spy_mean_rev.runner';     Args=@('--loop')},
    @{Module='forge.vix_intraday.runner';     Args=@('--loop')},
    @{Module='forge.nq_london_close.runner';  Args=@('--loop')},
    @{Module='forge.aud_asian_breakout.runner'; Args=@('--loop')},
    @{Module='forge.multi_orb.runner';        Args=@('--loop')},
    @{Module='forge.fomc_drift.runner';       Args=@('--loop')},
    @{Module='forge.tom_international.runner'; Args=@('--loop')},
    @{Module='forge.wick_gbpusd.runner';      Args=@('--loop')},
    @{Module='forge.vix_revert_runner';       Args=@('--loop')},
    @{Module='forge.mamba.runner';            Args=@('--loop')},
    @{Module='forge.tori.runner';             Args=@('--loop')},
    @{Module='forge.cuebanks.runner';         Args=@('--loop')},
    @{Module='forge.rebalance_runner';        Args=@('--loop')},
    @{Module='forge.gdx_gld_runner';          Args=@('--live','--loop')},
    @{Module='forge.atlas.runner';            Args=@('--loop','--interval-sec','120')},
    @{Module='forge.themis.runner';           Args=@('--loop','--interval-min','360')},
    @{Module='apollo.runner';                 Args=@('--loop','--live')},
    @{Module='hermes.runner';                 Args=@('--execute','--loop','--interval-min','30')},
    @{Module='titan.runner';                  Args=@('--loop','--live')}
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
    Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
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
