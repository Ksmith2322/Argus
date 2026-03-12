# line above: Set-StrictMode -Version Latest
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============================================================
# Gate A Phase 8 Acceptance Harness
#
# Purpose:
# - clear relevant runtime state/artifacts
# - launch runner_live.py with forced BUY
# - wait for position OPEN proof
# - kill process
# - restart runner_live.py
# - validate recovered OPEN
# - force SELL
# - validate FLAT
# - run reconciliation
# - emit PASS / FAIL
#
# Assumptions:
# - repo root is C:\Argus\repo
# - venv python is C:\Argus\.venv\Scripts\python.exe
# - runner entrypoint is C:\Argus\repo\runner_live.py
# - runtime snapshot is in ops\logs\runtime_snapshot.json
# - recovery/reconcile reports land in ops\logs\
# - .env exists in repo root and runner reads from it
# ============================================================

# ------------------------------------------------------------
# Canonical paths
# ------------------------------------------------------------
$RepoRoot = "C:\Argus\repo"
$PythonExe = "C:\Argus\.venv\Scripts\python.exe"
$RunnerPath = Join-Path $RepoRoot "runner_live.py"
$OpsDir = Join-Path $RepoRoot "ops"
$LogsDir = Join-Path $RepoRoot "ops\logs"
$StateDir = Join-Path $RepoRoot "state"
$EnvPath = Join-Path $RepoRoot ".env"

$HarnessRunTs = Get-Date -Format "yyyyMMdd_HHmmss"
$HarnessLogPath = Join-Path $LogsDir "gate_a_harness_$HarnessRunTs.log"
$StdoutBuyPath = Join-Path $LogsDir "gate_a_buy_$HarnessRunTs.out.log"
$StdoutRestartPath = Join-Path $LogsDir "gate_a_restart_$HarnessRunTs.out.log"
$StdoutSellPath = Join-Path $LogsDir "gate_a_sell_$HarnessRunTs.out.log"
$SummaryPath = Join-Path $LogsDir "gate_a_summary_$HarnessRunTs.json"

# ------------------------------------------------------------
# Timeouts / polling
# ------------------------------------------------------------
$PollSeconds = 2
$OpenTimeoutSeconds = 180
$RecoveredOpenTimeoutSeconds = 180
$FlatTimeoutSeconds = 180
$ProcessStopWaitSeconds = 20
$PostKillSettleSeconds = 3

# ------------------------------------------------------------
# Optional reconciliation entrypoint
# ------------------------------------------------------------
$ReconcilePy = Join-Path $RepoRoot "reconciliation.py"
$ReconcileAltPy = Join-Path $RepoRoot "reconcile_phase8.py"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
function Write-HarnessLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )

    $ts = (Get-Date).ToString("s")
    $line = "[$ts] $Message"
    Write-Host $line
    Add-Content -Path $HarnessLogPath -Value $line
}

function Assert-FileExists {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path $Path)) {
        throw "Required file not found: $Path"
    }
}

function Ensure-Dir {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
}

function Backup-Env {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    Assert-FileExists -Path $Path
    $backup = "$Path.gate_a_backup_$HarnessRunTs"
    Copy-Item -Path $Path -Destination $backup -Force
    Write-HarnessLog "Backed up .env -> $backup"
    return $backup
}

function Restore-Env {
    param(
        [Parameter(Mandatory = $true)][string]$BackupPath,
        [Parameter(Mandatory = $true)][string]$OriginalPath
    )

    if (Test-Path $BackupPath) {
        Copy-Item -Path $BackupPath -Destination $OriginalPath -Force
        Write-HarnessLog "Restored .env from backup"
    }
}

function Set-Or-AddEnvKey {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $content = @()
    if (Test-Path $Path) {
        $content = Get-Content -Path $Path
    }

    $found = $false
    $updated = foreach ($line in $content) {
        if ($line -match "^\s*$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        }
        else {
            $line
        }
    }

    if (-not $found) {
        $updated += "$Key=$Value"
    }

    Set-Content -Path $Path -Value $updated -Encoding UTF8
}

function Apply-EnvOverrides {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Overrides
    )

    foreach ($k in $Overrides.Keys) {
        Set-Or-AddEnvKey -Path $Path -Key $k -Value ([string]$Overrides[$k])
    }

    Write-HarnessLog ("Applied .env overrides: " + (($Overrides.Keys | Sort-Object) -join ", "))
}

function Start-Runner {
    param(
        [Parameter(Mandatory = $true)][string]$StdoutPath
    )

    Assert-FileExists -Path $PythonExe
    Assert-FileExists -Path $RunnerPath

    Write-HarnessLog "Starting runner_live.py -> $StdoutPath"

    $StderrPath = $StdoutPath -replace '\.out\.log$', '.err.log'
    if ($StderrPath -eq $StdoutPath) { $StderrPath = $StdoutPath + ".err.log" }

    $p = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $RunnerPath `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $StdoutPath `
        -RedirectStandardError $StderrPath `
        -PassThru

    Start-Sleep -Seconds 2

    if ($null -eq $p -or $p.HasExited) {
        throw "runner_live.py failed to start"
    }

    Write-HarnessLog "Started runner PID=$($p.Id)"
    return $p
}

function Stop-Runner {
    param(
        [Parameter(Mandatory = $true)]$ProcessObj
    )

    if ($null -eq $ProcessObj) {
        return
    }

    try {
        if (-not $ProcessObj.HasExited) {
            Write-HarnessLog "Stopping runner PID=$($ProcessObj.Id)"
            Stop-Process -Id $ProcessObj.Id -Force
        }
    }
    catch {
        Write-HarnessLog "Stop-Runner warning: $($_.Exception.Message)"
    }

    $deadline = (Get-Date).AddSeconds($ProcessStopWaitSeconds)
    do {
        try {
            $proc = Get-Process -Id $ProcessObj.Id -ErrorAction SilentlyContinue
        }
        catch {
            $proc = $null
        }

        if ($null -eq $proc) {
            Write-HarnessLog "Runner PID=$($ProcessObj.Id) confirmed stopped"
            Start-Sleep -Seconds $PostKillSettleSeconds
            return
        }

        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)

    throw "Runner PID=$($ProcessObj.Id) did not stop within timeout"
}

function Remove-RelevantArtifacts {
    Write-HarnessLog "Clearing relevant Phase 8 state/artifacts"

    $patterns = @(
        "runtime_snapshot*.json",
        "reconcile*.json",
        "reconcile*.txt",
        "recovery*.json",
        "recovery*.txt",
        "contradiction*.json",
        "contradiction*.txt",
        "trade_journal_*.csv",
        "orders_*.csv",
        "daily_summary_*.json",
        "gate_a_*.json",
        "gate_a_*.log",
        "gate_a_*.out.log"
    )

    foreach ($pattern in $patterns) {
        Get-ChildItem -Path $LogsDir -Filter $pattern -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                Remove-Item -Path $_.FullName -Force
                Write-HarnessLog "Removed $($_.Name)"
            }
            catch {
                Write-HarnessLog "Could not remove $($_.Name): $($_.Exception.Message)"
            }
        }
    }

    $adapterFiles = @(
        "orders.csv",
        "order_events.csv",
        "fills.csv",
        "positions.csv",
        "account.csv",
        "id_state.json",
        "fill_plans.json"
    )

    foreach ($name in $adapterFiles) {
        $p = Join-Path $LogsDir $name
        if (Test-Path $p) {
            Remove-Item -Path $p -Force
            Write-HarnessLog "Removed adapter artifact $name"
        }
    }

    # Clear runtime snapshot from canonical state dir
    if (Test-Path $StateDir) {
        Get-ChildItem -Path $StateDir -Filter "runtime_state_*.json" -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                Remove-Item -Path $_.FullName -Force
                Write-HarnessLog "Removed state snapshot $($_.Name)"
            }
            catch {
                Write-HarnessLog "Could not remove state snapshot $($_.Name): $($_.Exception.Message)"
            }
        }
    }
}

function Read-LatestRuntimeSnapshot {
    # runner_live.py writes to state\runtime_state_<SYMBOL>.json (e.g. runtime_state_ETH_USD.json)
    $candidates = @()

    if (Test-Path $StateDir) {
        $candidates = @(Get-ChildItem -Path $StateDir -Filter "runtime_state_*.json" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
    }

    # Fallback: legacy runtime_snapshot*.json in LogsDir (older layout)
    if ($candidates.Count -eq 0) {
        $candidates = @(Get-ChildItem -Path $LogsDir -Filter "runtime_snapshot*.json" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
    }

    if ($candidates.Count -eq 0) {
        return $null
    }

    $raw = Get-Content -Path $candidates[0].FullName -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $null
    }

    try {
        return ($raw | ConvertFrom-Json)
    }
    catch {
        Write-HarnessLog "Snapshot parse warning on $($candidates[0].Name): $($_.Exception.Message)"
        return $null
    }
}

function Get-FlatOrOpenState {
    $snap = Read-LatestRuntimeSnapshot
    if ($null -eq $snap) {
        return $null
    }

    $inPos = $null
    $qty = $null
    $avgEntry = $null
    $symbol = $null
    $stateName = $null

    if ($snap.PSObject.Properties.Name -contains "in_position") {
        $inPos = [bool]$snap.in_position
    }
    elseif ($snap.PSObject.Properties.Name -contains "position_open") {
        $inPos = [bool]$snap.position_open
    }

    if ($snap.PSObject.Properties.Name -contains "qty") {
        $qty = [decimal]$snap.qty
    }
    elseif ($snap.PSObject.Properties.Name -contains "position_qty") {
        $qty = [decimal]$snap.position_qty
    }

    if ($snap.PSObject.Properties.Name -contains "avg_entry") {
        $avgEntry = $snap.avg_entry
    }
    elseif ($snap.PSObject.Properties.Name -contains "entry_px") {
        $avgEntry = $snap.entry_px
    }

    if ($snap.PSObject.Properties.Name -contains "symbol") {
        $symbol = [string]$snap.symbol
    }
    elseif ($snap.PSObject.Properties.Name -contains "product_id") {
        $symbol = [string]$snap.product_id
    }

    if ($snap.PSObject.Properties.Name -contains "bot_state") {
        $stateName = [string]$snap.bot_state
    }
    elseif ($snap.PSObject.Properties.Name -contains "state") {
        $stateName = [string]$snap.state
    }
    elseif ($snap.PSObject.Properties.Name -contains "position_state") {
        $stateName = [string]$snap.position_state
    }

    [pscustomobject]@{
        InPosition = $inPos
        Qty        = $qty
        AvgEntry   = $avgEntry
        Symbol     = $symbol
        StateName  = $stateName
        Raw        = $snap
    }
}

function Wait-ForOpen {
    param(
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$PhaseLabel
    )

    Write-HarnessLog "${PhaseLabel}: waiting for recovered/open position proof"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $s = Get-FlatOrOpenState
        if ($null -ne $s) {
            $qtyPositive = $false
            if ($null -ne $s.Qty) {
                $qtyPositive = ([decimal]$s.Qty -gt 0)
            }

            if (($s.InPosition -eq $true) -or $qtyPositive) {
                Write-HarnessLog "${PhaseLabel}: OPEN confirmed (qty=$($s.Qty), avg_entry=$($s.AvgEntry), symbol=$($s.Symbol), state=$($s.StateName))"
                return $s
            }
        }

        Start-Sleep -Seconds $PollSeconds
    } while ((Get-Date) -lt $deadline)

    throw "${PhaseLabel}: timeout waiting for OPEN state"
}

function Wait-ForFlat {
    param(
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$PhaseLabel
    )

    Write-HarnessLog "${PhaseLabel}: waiting for FLAT proof"

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $s = Get-FlatOrOpenState
        if ($null -ne $s) {
            $qtyZero = $false
            if ($null -ne $s.Qty) {
                $qtyZero = ([decimal]$s.Qty -le 0)
            }

            if (($s.InPosition -eq $false -and $null -ne $s.InPosition) -or $qtyZero) {
                Write-HarnessLog "${PhaseLabel}: FLAT confirmed (qty=$($s.Qty), state=$($s.StateName))"
                return $s
            }
        }

        Start-Sleep -Seconds $PollSeconds
    } while ((Get-Date) -lt $deadline)

    throw "${PhaseLabel}: timeout waiting for FLAT state"
}

function Run-Reconciliation {
    Write-HarnessLog "Running reconciliation"

    $scriptToRun = $null
    if (Test-Path $ReconcilePy) {
        $scriptToRun = $ReconcilePy
    }
    elseif (Test-Path $ReconcileAltPy) {
        $scriptToRun = $ReconcileAltPy
    }

    if ($null -eq $scriptToRun) {
        Write-HarnessLog "Reconciliation script not found; marking reconcile as NOT_RUN"
        return [pscustomobject]@{
            status = "NOT_RUN"
            exit_code = $null
            script = $null
        }
    }

    $outPath = Join-Path $LogsDir "gate_a_reconcile_$HarnessRunTs.out.log"

    $outErrPath = $outPath -replace '\.out\.log$', '.err.log'
    if ($outErrPath -eq $outPath) { $outErrPath = $outPath + ".err.log" }

    $proc = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $scriptToRun `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $outPath `
        -RedirectStandardError $outErrPath `
        -PassThru `
        -Wait

    $status = if ($proc.ExitCode -eq 0) { "PASS" } else { "FAIL" }
    Write-HarnessLog "Reconciliation status=$status exit_code=$($proc.ExitCode) output=$outPath"

    return [pscustomobject]@{
        status = $status
        exit_code = $proc.ExitCode
        script = $scriptToRun
        output = $outPath
    }
}

function Save-Summary {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Summary
    )

    $json = $Summary | ConvertTo-Json -Depth 10
    Set-Content -Path $SummaryPath -Value $json -Encoding UTF8
    Write-HarnessLog "Wrote summary -> $SummaryPath"
}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
Ensure-Dir -Path $LogsDir
New-Item -ItemType File -Path $HarnessLogPath -Force | Out-Null

$envBackup = $null
$buyProc = $null
$restartProc = $null
$sellProc = $null

$summary = @{
    harness_started_at = (Get-Date).ToString("o")
    repo_root = $RepoRoot
    logs_dir = $LogsDir
    phases = @{
        cleanup = "NOT_RUN"
        buy_open = "NOT_RUN"
        kill_mid_position = "NOT_RUN"
        restart_recover_open = "NOT_RUN"
        sell_flat = "NOT_RUN"
        reconcile = "NOT_RUN"
    }
    reconcile = $null
    final_result = "FAIL"
    failure_reason = $null
}

try {
    Assert-FileExists -Path $PythonExe
    Assert-FileExists -Path $RunnerPath
    Assert-FileExists -Path $EnvPath

    Write-HarnessLog "=== Gate A Phase 8 harness start ==="

    $envBackup = Backup-Env -Path $EnvPath

    Remove-RelevantArtifacts
    $summary.phases.cleanup = "PASS"

    # --------------------------------------------------------
    # Phase 1: force BUY and validate OPEN
    # --------------------------------------------------------
    Apply-EnvOverrides -Path $EnvPath -Overrides @{
        "FORCE_TEST_BUY"       = "true"
        "FORCE_TEST_SELL"      = "false"
        "FORCE_TEST_ONLY_ONCE" = "true"
        "DRY_RUN"              = "false"
        "BACKTEST_MODE"        = "false"
    }

    $buyProc = Start-Runner -StdoutPath $StdoutBuyPath
    $open1 = Wait-ForOpen -TimeoutSeconds $OpenTimeoutSeconds -PhaseLabel "BUY phase"
    $summary.phases.buy_open = "PASS"

    # --------------------------------------------------------
    # Phase 2: kill mid-position
    # --------------------------------------------------------
    Stop-Runner -ProcessObj $buyProc
    $summary.phases.kill_mid_position = "PASS"

    # --------------------------------------------------------
    # Phase 3: restart and validate recovered OPEN
    # --------------------------------------------------------
    Apply-EnvOverrides -Path $EnvPath -Overrides @{
        "FORCE_TEST_BUY"       = "false"
        "FORCE_TEST_SELL"      = "false"
        "FORCE_TEST_ONLY_ONCE" = "true"
        "DRY_RUN"              = "false"
        "BACKTEST_MODE"        = "false"
    }

    $restartProc = Start-Runner -StdoutPath $StdoutRestartPath
    $open2 = Wait-ForOpen -TimeoutSeconds $RecoveredOpenTimeoutSeconds -PhaseLabel "Restart recovery phase"
    $summary.phases.restart_recover_open = "PASS"

    Stop-Runner -ProcessObj $restartProc

    # --------------------------------------------------------
    # Phase 4: restart again and force SELL to validate FLAT
    # --------------------------------------------------------
    Apply-EnvOverrides -Path $EnvPath -Overrides @{
        "FORCE_TEST_BUY"       = "false"
        "FORCE_TEST_SELL"      = "true"
        "FORCE_TEST_ONLY_ONCE" = "true"
        "DRY_RUN"              = "false"
        "BACKTEST_MODE"        = "false"
    }

    $sellProc = Start-Runner -StdoutPath $StdoutSellPath
    $flat = Wait-ForFlat -TimeoutSeconds $FlatTimeoutSeconds -PhaseLabel "SELL phase"
    $summary.phases.sell_flat = "PASS"

    Stop-Runner -ProcessObj $sellProc

    # --------------------------------------------------------
    # Phase 5: reconciliation
    # --------------------------------------------------------
    $recon = Run-Reconciliation
    $summary.reconcile = $recon

    if ($recon.status -eq "PASS" -or $recon.status -eq "NOT_RUN") {
        $summary.phases.reconcile = $recon.status
    }
    else {
        throw "Reconciliation failed"
    }

    $summary.final_result = "PASS"
    Write-HarnessLog "=== Gate A Phase 8 PASS ==="
}
catch {
    $summary.failure_reason = $_.Exception.Message
    Write-HarnessLog "HARNESS FAILURE: $($_.Exception.Message)"
    $summary.final_result = "FAIL"
}
finally {
    foreach ($p in @($buyProc, $restartProc, $sellProc)) {
        if ($null -ne $p) {
            try {
                if (-not $p.HasExited) {
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                }
            }
            catch {}
        }
    }

    if ($null -ne $envBackup) {
        Restore-Env -BackupPath $envBackup -OriginalPath $EnvPath
    }

    $summary.harness_finished_at = (Get-Date).ToString("o")
    Save-Summary -Summary $summary
}

if ($summary.final_result -eq "PASS") {
    exit 0
}
else {
    exit 1
}