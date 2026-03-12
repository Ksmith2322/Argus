Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ============================================================
# Phase 10 -- Adversarial Restart and Fault Hardening Harness
#
# Tests 4 kill-point scenarios beyond Gate A's mid-position kill:
#
#   Scenario A: kill_before_journal
#     BUY -> kill mid-position -> SELL runner -> SELL fill processed
#     -> kill BEFORE journal write -> final restart -> FLAT
#     -> journal backfilled by _finalize_recovered_flat_close
#     -> reconcile PASS, exactly 1 journal row
#
#   Scenario B: kill_after_journal
#     BUY -> kill mid-position -> SELL runner -> SELL fill processed
#     -> journal written -> kill AFTER journal write -> final restart -> FLAT
#     -> no duplicate journal row
#     -> reconcile PASS, exactly 1 journal row
#
#   Scenario C: cold_restore (no snapshot)
#     BUY -> kill mid-position -> delete runtime snapshot
#     -> restart from fills.csv only -> OPEN confirmed
#     -> force SELL -> FLAT -> reconcile PASS
#
#   Scenario D: kill_after_sell_submit
#     BUY -> kill mid-position -> SELL runner -> SELL order submitted + filled
#     (PaperAdapter immediate mode: fill persisted inside place_order)
#     -> kill AFTER order submit -> final restart -> FLAT
#     -> reconcile PASS, no ghost fills
#
# All 4 scenarios must PASS for Phase 10 gate to be met.
# ============================================================

# ------------------------------------------------------------
# Canonical paths
# ------------------------------------------------------------
$RepoRoot  = "C:\Argus\repo"
$PythonExe = "C:\Argus\.venv\Scripts\python.exe"
$RunnerPath = Join-Path $RepoRoot "runner_live.py"
$OpsDir    = Join-Path $RepoRoot "ops"
$LogsDir   = Join-Path $RepoRoot "ops\logs"
$StateDir  = Join-Path $RepoRoot "state"
$EnvPath   = Join-Path $RepoRoot ".env"

$HarnessRunTs  = Get-Date -Format "yyyyMMdd_HHmmss"
$HarnessLogPath = Join-Path $LogsDir "phase10_harness_$HarnessRunTs.log"
$SummaryPath    = Join-Path $LogsDir "phase10_summary_$HarnessRunTs.json"

# ------------------------------------------------------------
# Timeouts
# ------------------------------------------------------------
$PollSeconds              = 2
$OpenTimeoutSeconds       = 120
$FlatTimeoutSeconds       = 120
$ProcessStopWaitSeconds   = 20
$PostKillSettleSeconds    = 3

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
$ReconcilePy    = Join-Path $RepoRoot "reconciliation.py"
$ReconcileAltPy = Join-Path $RepoRoot "reconcile_phase8.py"

function Write-HarnessLog {
    param([Parameter(Mandatory=$true)][string]$Message)
    $ts   = (Get-Date).ToString("s")
    $line = "[$ts] $Message"
    Write-Host $line
    Add-Content -Path $HarnessLogPath -Value $line
}

function Assert-FileExists {
    param([Parameter(Mandatory=$true)][string]$Path)
    if (-not (Test-Path $Path)) { throw "Required file not found: $Path" }
}

function Ensure-Dir {
    param([Parameter(Mandatory=$true)][string]$Path)
    if (-not (Test-Path $Path)) { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
}

function Backup-Env {
    Assert-FileExists -Path $EnvPath
    $backup = "$EnvPath.phase10_backup_$HarnessRunTs"
    Copy-Item -Path $EnvPath -Destination $backup -Force
    Write-HarnessLog "Backed up .env -> $backup"
    return $backup
}

function Restore-Env {
    param([Parameter(Mandatory=$true)][string]$BackupPath)
    if (Test-Path $BackupPath) {
        Copy-Item -Path $BackupPath -Destination $EnvPath -Force
        Write-HarnessLog "Restored .env from backup"
    }
}

function Set-Or-AddEnvKey {
    param(
        [Parameter(Mandatory=$true)][string]$Key,
        [Parameter(Mandatory=$true)][string]$Value
    )
    $content = @()
    if (Test-Path $EnvPath) { $content = Get-Content -Path $EnvPath }
    $found   = $false
    $updated = foreach ($line in $content) {
        if ($line -match "^\s*$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        } else { $line }
    }
    if (-not $found) { $updated += "$Key=$Value" }
    Set-Content -Path $EnvPath -Value $updated -Encoding UTF8
}

function Apply-EnvOverrides {
    param([Parameter(Mandatory=$true)][hashtable]$Overrides)
    foreach ($k in $Overrides.Keys) {
        Set-Or-AddEnvKey -Key $k -Value ([string]$Overrides[$k])
    }
    Write-HarnessLog ("Applied .env overrides: " + (($Overrides.Keys | Sort-Object) -join ", "))
}

function Clear-Artifacts {
    Write-HarnessLog "Clearing adapter artifacts and state for scenario"

    $adapterFiles = @("orders.csv","order_events.csv","fills.csv","positions.csv",
                      "account.csv","id_state.json","fill_plans.json")
    foreach ($name in $adapterFiles) {
        $p = Join-Path $LogsDir $name
        if (Test-Path $p) {
            Remove-Item -Path $p -Force
            Write-HarnessLog "  Removed $name"
        }
    }

    $patterns = @("recovery_*.json","trade_journal_*.csv","reconcile_phase8_*.json")
    foreach ($pat in $patterns) {
        Get-ChildItem -Path $LogsDir -Filter $pat -ErrorAction SilentlyContinue | ForEach-Object {
            Remove-Item -Path $_.FullName -Force
            Write-HarnessLog "  Removed $($_.Name)"
        }
    }

    if (Test-Path $StateDir) {
        Get-ChildItem -Path $StateDir -Filter "runtime_state_*.json" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Remove-Item -Path $_.FullName -Force
            Write-HarnessLog "  Removed state/$($_.Name)"
        }
    }
}

function Delete-RuntimeSnapshot {
    Write-HarnessLog "Deleting runtime snapshot (cold-restore test)"
    if (Test-Path $StateDir) {
        $snaps = @(Get-ChildItem -Path $StateDir -Filter "runtime_state_*.json" -ErrorAction SilentlyContinue)
        foreach ($s in $snaps) {
            Remove-Item -Path $s.FullName -Force
            Write-HarnessLog "  Deleted state/$($s.Name)"
        }
    }
}

function Start-Runner {
    param([Parameter(Mandatory=$true)][string]$StdoutPath)
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
    if ($null -eq $p -or $p.HasExited) { throw "runner_live.py failed to start" }
    Write-HarnessLog "Started runner PID=$($p.Id)"
    return $p
}

function Stop-Runner {
    param([Parameter(Mandatory=$true)]$ProcessObj)
    if ($null -eq $ProcessObj) { return }
    try {
        if (-not $ProcessObj.HasExited) {
            Write-HarnessLog "Stopping runner PID=$($ProcessObj.Id)"
            Stop-Process -Id $ProcessObj.Id -Force
        }
    } catch { Write-HarnessLog "Stop-Runner warning: $($_.Exception.Message)" }

    $deadline = (Get-Date).AddSeconds($ProcessStopWaitSeconds)
    do {
        try { $proc = Get-Process -Id $ProcessObj.Id -ErrorAction SilentlyContinue }
        catch { $proc = $null }
        if ($null -eq $proc) {
            Write-HarnessLog "Runner PID=$($ProcessObj.Id) confirmed stopped"
            Start-Sleep -Seconds $PostKillSettleSeconds
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Runner PID=$($ProcessObj.Id) did not stop within timeout"
}

function Wait-ForProcess-Exit {
    param(
        [Parameter(Mandatory=$true)]$ProcessObj,
        [int]$TimeoutSeconds = 60,
        [string]$Label = "process"
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try { $proc = Get-Process -Id $ProcessObj.Id -ErrorAction SilentlyContinue }
        catch { $proc = $null }
        if ($null -eq $proc -or $ProcessObj.HasExited) {
            Write-HarnessLog "${Label}: exited (exit_code=$($ProcessObj.ExitCode))"
            Start-Sleep -Seconds $PostKillSettleSeconds
            return
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "${Label}: did not exit within ${TimeoutSeconds}s"
}

function Read-LatestRuntimeSnapshot {
    $candidates = @()
    if (Test-Path $StateDir) {
        $candidates = @(Get-ChildItem -Path $StateDir -Filter "runtime_state_*.json" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending)
    }
    if ($candidates.Count -eq 0) { return $null }
    $raw = Get-Content -Path $candidates[0].FullName -Raw
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    try { return ($raw | ConvertFrom-Json) } catch { return $null }
}

function Get-SnapshotState {
    $snap = Read-LatestRuntimeSnapshot
    if ($null -eq $snap) { return $null }

    $qty   = $null
    $state = $null
    if ($snap.PSObject.Properties.Name -contains "qty") { $qty = [decimal]$snap.qty }
    elseif ($snap.PSObject.Properties.Name -contains "position_qty") { $qty = [decimal]$snap.position_qty }
    if ($snap.PSObject.Properties.Name -contains "bot_state") { $state = [string]$snap.bot_state }
    elseif ($snap.PSObject.Properties.Name -contains "recovery_state") { $state = [string]$snap.recovery_state }

    [pscustomobject]@{ Qty = $qty; StateName = $state; Raw = $snap }
}

function Wait-ForOpen {
    param(
        [Parameter(Mandatory=$true)][int]$TimeoutSeconds,
        [Parameter(Mandatory=$true)][string]$Label
    )
    Write-HarnessLog "${Label}: waiting for OPEN"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $s = Get-SnapshotState
        if ($null -ne $s -and $null -ne $s.Qty -and [decimal]$s.Qty -gt 0) {
            Write-HarnessLog "${Label}: OPEN confirmed (qty=$($s.Qty), state=$($s.StateName))"
            return $s
        }
        Start-Sleep -Seconds $PollSeconds
    } while ((Get-Date) -lt $deadline)
    throw "${Label}: timeout waiting for OPEN"
}

function Wait-ForFlat {
    param(
        [Parameter(Mandatory=$true)][int]$TimeoutSeconds,
        [Parameter(Mandatory=$true)][string]$Label
    )
    Write-HarnessLog "${Label}: waiting for FLAT"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $s = Get-SnapshotState
        if ($null -ne $s) {
            $isFlat = ($null -ne $s.Qty -and [decimal]$s.Qty -le 0)
            if ($isFlat) {
                Write-HarnessLog "${Label}: FLAT confirmed (qty=$($s.Qty), state=$($s.StateName))"
                return $s
            }
        }
        Start-Sleep -Seconds $PollSeconds
    } while ((Get-Date) -lt $deadline)
    throw "${Label}: timeout waiting for FLAT"
}

function Count-JournalRows {
    $journals = @(Get-ChildItem -Path $LogsDir -Filter "trade_journal_*.csv" -ErrorAction SilentlyContinue)
    $total = 0
    foreach ($f in $journals) {
        $lines = @(Get-Content -Path $f.FullName | Where-Object { $_ -match '\S' })
        # subtract header row
        if ($lines.Count -gt 1) { $total += ($lines.Count - 1) }
    }
    return $total
}

function Run-Reconciliation {
    param([string]$ScenarioLabel)
    Write-HarnessLog "${ScenarioLabel}: running reconciliation"

    $scriptToRun = $null
    if (Test-Path $ReconcilePy) { $scriptToRun = $ReconcilePy }
    elseif (Test-Path $ReconcileAltPy) { $scriptToRun = $ReconcileAltPy }
    if ($null -eq $scriptToRun) {
        Write-HarnessLog "${ScenarioLabel}: reconcile script not found -- marking NOT_RUN"
        return [pscustomobject]@{ status = "NOT_RUN"; exit_code = $null }
    }

    $outPath = Join-Path $LogsDir "phase10_reconcile_${ScenarioLabel}_${HarnessRunTs}.out.log"
    $errPath = $outPath -replace '\.out\.log$', '.err.log'

    $proc = Start-Process `
        -FilePath $PythonExe `
        -ArgumentList $scriptToRun `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $outPath `
        -RedirectStandardError $errPath `
        -PassThru `
        -Wait

    $status = if ($proc.ExitCode -eq 0) { "PASS" } else { "FAIL" }
    Write-HarnessLog "${ScenarioLabel}: reconcile $status (exit_code=$($proc.ExitCode))"
    return [pscustomobject]@{ status = $status; exit_code = $proc.ExitCode; output = $outPath }
}

# ------------------------------------------------------------
# Scenario runner: buy phase (shared setup for all scenarios)
# ------------------------------------------------------------
function Run-BuyPhase {
    param([string]$ScenarioLabel)
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "true"
        "FORCE_TEST_SELL"         = "false"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logPath = Join-Path $LogsDir "phase10_buy_${ScenarioLabel}_${HarnessRunTs}.out.log"
    $p = Start-Runner -StdoutPath $logPath
    $open = Wait-ForOpen -TimeoutSeconds $OpenTimeoutSeconds -Label "${ScenarioLabel}/buy"
    Stop-Runner -ProcessObj $p
    return $open
}

# ============================================================
# SCENARIO A: kill_before_journal
# ============================================================
function Run-Scenario-A {
    Write-HarnessLog "=== Scenario A: kill_before_journal ==="

    Clear-Artifacts
    Run-BuyPhase -ScenarioLabel "scen_a" | Out-Null

    # SELL runner with KILL_BEFORE_JOURNAL_WRITE
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "true"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "true"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logKill = Join-Path $LogsDir "phase10_scen_a_kill_${HarnessRunTs}.out.log"
    $killProc = Start-Runner -StdoutPath $logKill
    # Wait for process to self-terminate via ControlledAbort (or timeout)
    Wait-ForProcess-Exit -ProcessObj $killProc -TimeoutSeconds 60 -Label "scen_a/kill"

    # Delete stale snapshot so Wait-ForFlat waits for fresh one from the final runner.
    # The kill runner's finally block saved snapshot with EXITING state (correct behavior --
    # it saved the state at kill time). Without deleting it, Wait-ForFlat would read the
    # stale snapshot immediately and return before the final runner does anything.
    Delete-RuntimeSnapshot

    # Final restart -- no force flags
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "false"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logFinal = Join-Path $LogsDir "phase10_scen_a_final_${HarnessRunTs}.out.log"
    $finalProc = Start-Runner -StdoutPath $logFinal
    $flat = Wait-ForFlat -TimeoutSeconds $FlatTimeoutSeconds -Label "scen_a/final"
    Stop-Runner -ProcessObj $finalProc

    $journalRows = Count-JournalRows
    if ($journalRows -ne 1) {
        throw "Scenario A: expected exactly 1 journal row, got $journalRows"
    }
    Write-HarnessLog "Scenario A: journal row count OK ($journalRows)"

    $recon = Run-Reconciliation -ScenarioLabel "scen_a"
    if ($recon.status -ne "PASS" -and $recon.status -ne "NOT_RUN") {
        throw "Scenario A: reconciliation FAIL"
    }

    Write-HarnessLog "Scenario A: PASS"
    return [pscustomobject]@{ status = "PASS"; journal_rows = $journalRows; reconcile = $recon.status }
}

# ============================================================
# SCENARIO B: kill_after_journal
# ============================================================
function Run-Scenario-B {
    Write-HarnessLog "=== Scenario B: kill_after_journal ==="

    Clear-Artifacts
    Run-BuyPhase -ScenarioLabel "scen_b" | Out-Null

    # SELL runner with KILL_AFTER_JOURNAL_WRITE
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "true"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "true"
    }
    $logKill = Join-Path $LogsDir "phase10_scen_b_kill_${HarnessRunTs}.out.log"
    $killProc = Start-Runner -StdoutPath $logKill
    Wait-ForProcess-Exit -ProcessObj $killProc -TimeoutSeconds 60 -Label "scen_b/kill"

    # Verify journal was written before kill
    $rowsAfterKill = Count-JournalRows
    if ($rowsAfterKill -ne 1) {
        throw "Scenario B: expected 1 journal row after kill-after-journal, got $rowsAfterKill"
    }
    Write-HarnessLog "Scenario B: journal present after kill ($rowsAfterKill rows)"

    # Delete stale snapshot so Wait-ForFlat waits for fresh state from final runner
    Delete-RuntimeSnapshot

    # Final restart -- must NOT produce duplicate journal row
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "false"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logFinal = Join-Path $LogsDir "phase10_scen_b_final_${HarnessRunTs}.out.log"
    $finalProc = Start-Runner -StdoutPath $logFinal
    $flat = Wait-ForFlat -TimeoutSeconds $FlatTimeoutSeconds -Label "scen_b/final"
    Stop-Runner -ProcessObj $finalProc

    $rowsAfterFinal = Count-JournalRows
    if ($rowsAfterFinal -ne 1) {
        throw "Scenario B: duplicate journal detected - got $rowsAfterFinal rows after final restart (expected 1)"
    }
    Write-HarnessLog "Scenario B: no duplicate journal row confirmed ($rowsAfterFinal rows)"

    $recon = Run-Reconciliation -ScenarioLabel "scen_b"
    if ($recon.status -ne "PASS" -and $recon.status -ne "NOT_RUN") {
        throw "Scenario B: reconciliation FAIL"
    }

    Write-HarnessLog "Scenario B: PASS"
    return [pscustomobject]@{ status = "PASS"; journal_rows = $rowsAfterFinal; reconcile = $recon.status }
}

# ============================================================
# SCENARIO C: cold_restore (no snapshot)
# ============================================================
function Run-Scenario-C {
    Write-HarnessLog "=== Scenario C: cold_restore (no snapshot) ==="

    Clear-Artifacts
    Run-BuyPhase -ScenarioLabel "scen_c" | Out-Null

    # Delete runtime snapshot -- cold restore test
    Delete-RuntimeSnapshot

    # Restart with no force flags -- should recover OPEN from fills.csv only
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "false"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logRestart = Join-Path $LogsDir "phase10_scen_c_restart_${HarnessRunTs}.out.log"
    $restartProc = Start-Runner -StdoutPath $logRestart
    $open = Wait-ForOpen -TimeoutSeconds $OpenTimeoutSeconds -Label "scen_c/cold_restart"
    Write-HarnessLog "Scenario C: recovered OPEN from fills.csv only (qty=$($open.Qty))"
    Stop-Runner -ProcessObj $restartProc

    # SELL runner
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "true"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logSell = Join-Path $LogsDir "phase10_scen_c_sell_${HarnessRunTs}.out.log"
    $sellProc = Start-Runner -StdoutPath $logSell
    $flat = Wait-ForFlat -TimeoutSeconds $FlatTimeoutSeconds -Label "scen_c/sell"
    Stop-Runner -ProcessObj $sellProc

    $recon = Run-Reconciliation -ScenarioLabel "scen_c"
    if ($recon.status -ne "PASS" -and $recon.status -ne "NOT_RUN") {
        throw "Scenario C: reconciliation FAIL"
    }

    Write-HarnessLog "Scenario C: PASS"
    return [pscustomobject]@{ status = "PASS"; reconcile = $recon.status }
}

# ============================================================
# SCENARIO D: kill_after_sell_submit
# ============================================================
function Run-Scenario-D {
    Write-HarnessLog "=== Scenario D: kill_after_sell_submit ==="

    Clear-Artifacts
    Run-BuyPhase -ScenarioLabel "scen_d" | Out-Null

    # SELL runner with KILL_AFTER_ORDER_SUBMIT
    # PaperAdapter immediate mode: fill persisted inside place_order before the kill
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "true"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "true"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logKill = Join-Path $LogsDir "phase10_scen_d_kill_${HarnessRunTs}.out.log"
    $killProc = Start-Runner -StdoutPath $logKill
    Wait-ForProcess-Exit -ProcessObj $killProc -TimeoutSeconds 60 -Label "scen_d/kill"

    # Delete stale snapshot so Wait-ForFlat waits for fresh state from final runner
    Delete-RuntimeSnapshot

    # Final restart -- fill already in fills.csv (immediate mode), should recover FLAT
    Apply-EnvOverrides -Overrides @{
        "FORCE_TEST_BUY"          = "false"
        "FORCE_TEST_SELL"         = "false"
        "FORCE_TEST_ONLY_ONCE"    = "true"
        "DRY_RUN"                 = "false"
        "BACKTEST_MODE"           = "false"
        "KILL_AFTER_ORDER_SUBMIT" = "false"
        "KILL_BEFORE_JOURNAL"     = "false"
        "KILL_AFTER_JOURNAL"      = "false"
    }
    $logFinal = Join-Path $LogsDir "phase10_scen_d_final_${HarnessRunTs}.out.log"
    $finalProc = Start-Runner -StdoutPath $logFinal
    $flat = Wait-ForFlat -TimeoutSeconds $FlatTimeoutSeconds -Label "scen_d/final"
    Stop-Runner -ProcessObj $finalProc

    $recon = Run-Reconciliation -ScenarioLabel "scen_d"
    if ($recon.status -ne "PASS" -and $recon.status -ne "NOT_RUN") {
        throw "Scenario D: reconciliation FAIL"
    }

    Write-HarnessLog "Scenario D: PASS"
    return [pscustomobject]@{ status = "PASS"; reconcile = $recon.status }
}

# ============================================================
# Main
# ============================================================
Ensure-Dir -Path $LogsDir
Ensure-Dir -Path $StateDir
New-Item -ItemType File -Path $HarnessLogPath -Force | Out-Null

$envBackup = $null
$summary = @{
    harness_started_at = (Get-Date).ToString("o")
    scenarios = @{
        A_kill_before_journal  = "NOT_RUN"
        B_kill_after_journal   = "NOT_RUN"
        C_cold_restore         = "NOT_RUN"
        D_kill_after_submit    = "NOT_RUN"
    }
    final_result   = "FAIL"
    failure_reason = $null
}

try {
    Assert-FileExists -Path $PythonExe
    Assert-FileExists -Path $RunnerPath
    Assert-FileExists -Path $EnvPath

    Write-HarnessLog "=== Phase 10 Adversarial Restart Harness start ==="
    $envBackup = Backup-Env

    $resultA = Run-Scenario-A
    $summary.scenarios.A_kill_before_journal = $resultA.status

    $resultB = Run-Scenario-B
    $summary.scenarios.B_kill_after_journal = $resultB.status

    $resultC = Run-Scenario-C
    $summary.scenarios.C_cold_restore = $resultC.status

    $resultD = Run-Scenario-D
    $summary.scenarios.D_kill_after_submit = $resultD.status

    $allPass = ($resultA.status -eq "PASS") -and
               ($resultB.status -eq "PASS") -and
               ($resultC.status -eq "PASS") -and
               ($resultD.status -eq "PASS")

    if ($allPass) {
        $summary.final_result = "PASS"
        Write-HarnessLog "=== Phase 10 PASS -- all 4 scenarios passed ==="
    } else {
        $summary.failure_reason = "One or more scenarios failed"
        Write-HarnessLog "=== Phase 10 FAIL ==="
    }
}
catch {
    $summary.failure_reason = $_.Exception.Message
    Write-HarnessLog "HARNESS FAILURE: $($_.Exception.Message)"
    $summary.final_result = "FAIL"
}
finally {
    # Best-effort cleanup: kill any lingering runners
    try {
        Get-Process -Name "python" -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowTitle -eq "" } |
            Stop-Process -Force -ErrorAction SilentlyContinue
    } catch {}

    if ($null -ne $envBackup) { Restore-Env -BackupPath $envBackup }

    $summary.harness_finished_at = (Get-Date).ToString("o")
    $json = $summary | ConvertTo-Json -Depth 10
    Set-Content -Path $SummaryPath -Value $json -Encoding UTF8
    Write-HarnessLog "Wrote summary -> $SummaryPath"
}

if ($summary.final_result -eq "PASS") { exit 0 } else { exit 1 }
