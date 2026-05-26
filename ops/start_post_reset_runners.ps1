# ops/start_post_reset_runners.ps1 - Active post-v22-reset fleet launcher
#
# Launches the v26 active roster: 11 capital-allocated strategies + 1 shadow.
# All connect to IB Gateway on port 4002 (paper). See allocation_factors.json
# v26 for the authoritative allocation table; this file mirrors it.
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

# 2026-05-25: cut over from TWS (7497) to IB Gateway (4002) for unattended
# production. Override only if pointing at TWS deliberately.
if (-not $env:IBKR_PORT) { $env:IBKR_PORT = '4002' }
$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"
$logFile = "$logDir\start_post_reset_runners_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

# Active roster -- matches allocation_factors.json v26.
# (module, args, client_id, allocation, note)
$runners = @(
    @{Module='forge.gld_pm_long.runner';            Args=@('--loop');                                    ClientId=102; Alloc=0.5;  Note='defense / metals diversifier'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','baseline','--loop');            ClientId=121; Alloc=1.0;  Note='broad_8 baseline'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','sectors','--loop');             ClientId=122; Alloc=0.25; Note='11 SPDR sectors variant'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','style','--loop');               ClientId=123; Alloc=0.5;  Note='8 style-factor ETFs'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','legacy15','--loop');            ClientId=124; Alloc=0.25; Note='10 sectors + 5 country'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','style_top3','--loop');          ClientId=125; Alloc=0.5;  Note='style top-3 variant'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','legacy15_regime','--loop');     ClientId=126; Alloc=0.25; Note='legacy15 + SPY>200dma gate'},
    @{Module='forge.tail_hedge.runner';             Args=@('--loop');                                    ClientId=127; Alloc=0.1;  Note='GLD/TLT when SPY<200dma (HEDGE role)'},
    @{Module='forge.xs_momentum.runner';            Args=@('--variant','global47','--loop');            ClientId=128; Alloc=0.25; Note='43-ETF global universe'},
    @{Module='forge.tom_spy.runner';                Args=@('--loop');                                    ClientId=129; Alloc=0.3;  Note='turn-of-month SPY (calendar)'},
    @{Module='forge.nov_spy.runner';                Args=@('--loop');                                    ClientId=130; Alloc=0.2;  Note='November-only SPY (calendar)'},
    @{Module='forge.xs_momentum_consensus.runner';  Args=@('--loop');                                    ClientId=$null; Alloc=0.0; Note='SHADOW -- virtual trades, no broker'},
    @{Module='forge.uso_pm_long.runner';             Args=@('--loop');                                    ClientId=131; Alloc=0.3;  Note='hourly USO PM-long (v27)'},
    @{Module='forge.ewz_breakout.runner';            Args=@('--loop');                                    ClientId=132; Alloc=0.2;  Note='daily EWZ 21d breakout (v28)'},
    @{Module='forge.ief_jul_hold.runner';            Args=@('--loop');                                    ClientId=133; Alloc=0.1;  Note='IEF July seasonal (v29 orthogonal)'},
    @{Module='forge.gld_jan_hold.runner';            Args=@('--loop');                                    ClientId=134; Alloc=0.1;  Note='GLD January seasonal (v29 orthogonal)'}
)

# Modules that must NOT be running when this launcher fires. KILLED registry
# entries plus the unified runner. Pending-opt-in strategies are NOT in this
# list since v21 promoted tom_spy + nov_spy out of pending state.
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
    'forge.vix_carry.runner', 'forge.coint_pairs.runner',
    # 5/25: paper_bridge variants still firing as zombies after sunset
    'forge.cuebanks.paper_bridge', 'forge.tori.paper_bridge'
)

Log "=== Argus v26 active fleet startup (target: $($runners.Count) runners; IBKR_PORT=$($env:IBKR_PORT)) ==="

# Broker pre-flight -- warn but don't refuse. Gateway/TWS may come up
# after the launcher and runners will retry their initial connect.
$gw = Get-NetTCPConnection -LocalPort $env:IBKR_PORT -State Listen -ErrorAction SilentlyContinue
if (-not $gw) {
    Log "WARNING: nothing listening on port $($env:IBKR_PORT). Runners will retry connect."
} else {
    $p = Get-Process -Id $gw[0].OwningProcess -ErrorAction SilentlyContinue
    Log "Broker check: $($p.ProcessName) (PID $($p.Id)) listening on $($env:IBKR_PORT)."
}

function Get-RunningPythonModules {
    # Returns a hashtable keyed by (module, variant) -> $true. Variant defaults
    # to "" for runners that don't use the --variant flag.
    $map = @{}
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
        $cmd = $_.CommandLine
        if (-not $cmd) { return }
        if ($cmd -match '-m\s+([\w\.]+)') {
            $module = $matches[1]
            $variant = ""
            if ($cmd -match '--variant\s+(\S+)') { $variant = $matches[1] }
            $map["${module}|${variant}"] = $true
        }
    }
    return $map
}

function RunnerKey($runner) {
    $variant = ""
    for ($i = 0; $i -lt $runner.Args.Count - 1; $i++) {
        if ($runner.Args[$i] -eq '--variant') { $variant = $runner.Args[$i+1]; break }
    }
    return "$($runner.Module)|$variant"
}

$runningMap = Get-RunningPythonModules

$blockedRunning = @()
foreach ($mod in $blockedModules) {
    foreach ($key in $runningMap.Keys) {
        if ($key -like "${mod}|*") { $blockedRunning += $key; break }
    }
}
if ($blockedRunning.Count -gt 0) {
    Log "REFUSING TO LAUNCH: $($blockedRunning.Count) KILLED-registry runners still alive:"
    foreach ($k in $blockedRunning) { Log "  - $k" }
    Log "Stop them first; this launcher only permits the active v26 roster."
    Log "See OPERATOR_HANDOFF.md section '1.5 Power-cycle / morning recovery'."
    exit 2
}

$launched = 0
$skipped = 0
foreach ($runner in $runners) {
    $key = RunnerKey $runner
    $alreadyRunning = $runningMap.ContainsKey($key)
    $label = "$($runner.Module) $($runner.Args -join ' ')"

    if ($alreadyRunning -and -not $RestartAll) {
        Log "  SKIP   $label  (already running)"
        $skipped++
        continue
    }

    if ($alreadyRunning -and $RestartAll) {
        $variantToken = ""
        if ($key -match '\|(.+)$') { $variantToken = $matches[1] }
        $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
            $cmd = $_.CommandLine
            $cmd -match "-m\s+$([regex]::Escape($runner.Module))(\s|$)" -and
            (-not $variantToken -or $cmd -match "--variant\s+$([regex]::Escape($variantToken))(\s|$)")
        }
        foreach ($proc in $procs) {
            try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop }
            catch { Log "    warn: failed to kill PID=$($proc.ProcessId): $_" }
        }
        Start-Sleep -Milliseconds 800
        Log "  KILLED $label ($(@($procs).Count) processes)"
    }

    if ($DryRun) {
        Log "  WOULD-LAUNCH $label  (client_id=$($runner.ClientId), alloc=$($runner.Alloc), $($runner.Note))"
        continue
    }

    Log "  LAUNCH $label  (client_id=$($runner.ClientId), alloc=$($runner.Alloc))"
    $argList = @('-m', $runner.Module) + $runner.Args
    Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    Start-Sleep -Milliseconds 800
    $launched++
}

Log "=== Done: launched=$launched, already-running=$skipped ==="

if (-not $DryRun) {
    Start-Sleep -Seconds 4
    $afterMap = Get-RunningPythonModules
    $missing = @()
    foreach ($runner in $runners) {
        $key = RunnerKey $runner
        if (-not $afterMap.ContainsKey($key)) { $missing += $key }
    }
    if ($missing.Count -gt 0) {
        Log "WARNING: these active runners did NOT come up after launch:"
        foreach ($k in $missing) { Log "  - $k" }
        exit 1
    }
    Log "Verified: all $($runners.Count) active v26 runners are up."
}
