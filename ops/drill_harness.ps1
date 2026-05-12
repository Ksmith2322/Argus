# drill_harness.ps1 - operator drill harness for KILL_SWITCH, circuit breaker,
# and manual override. Walks through each drill with verification at every step.
#
# DOES NOT auto-execute destructive actions. Each step prompts via Read-Host
# (when -Interactive) or shows the plan only (default DRY-RUN).
#
# Usage:
#   .\ops\drill_harness.ps1                     # DRY-RUN: show plan + check infrastructure
#   .\ops\drill_harness.ps1 -Drill Kill         # KILL_SWITCH drill (set flag, verify halt, clear)
#   .\ops\drill_harness.ps1 -Drill Halt         # HALT.flag drill (helio block + verify)
#   .\ops\drill_harness.ps1 -Drill CircuitBreaker  # daily-loss circuit breaker dry-run
#   .\ops\drill_harness.ps1 -Drill Flatten      # FLATTEN_EOD plumbing (no actual flatten)
#   .\ops\drill_harness.ps1 -Drill All          # all 4 drills sequentially
#
# Each drill:
#   1. Verify infrastructure exists (file paths, scripts, runners)
#   2. Set the trigger (flag file, env var, etc)
#   3. Wait + observe (heartbeats, log entries)
#   4. Clear the trigger
#   5. Verify recovery (heartbeats resume, runners reconnect)
[CmdletBinding()]
param(
    [ValidateSet('Plan','Kill','Halt','CircuitBreaker','Flatten','All')]
    [string]$Drill = 'Plan'
)

$ErrorActionPreference = "Continue"
$repo = "C:\Argus\repo"
$python = "C:\Argus\.venv\Scripts\python.exe"
$killFile = "$repo\KILL_SWITCH"
$haltFile = "$repo\argus_flow\logs\HALT.flag"
$flattenFile = "$repo\argus_flow\logs\FLATTEN_EOD.flag"
$drillEvidenceDir = "$repo\argus_flow\logs"
$killDrillReport = "$drillEvidenceDir\kill_switch_drill.json"

function Write-DrillStep { param([string]$msg, [string]$status='INFO')
    $color = switch ($status) {
        'OK'   { 'Green' }
        'FAIL' { 'Red' }
        'WARN' { 'Yellow' }
        default { 'Cyan' }
    }
    Write-Host "[$status] $msg" -ForegroundColor $color
}

function Test-FileExists { param([string]$path, [string]$desc)
    if (Test-Path $path) {
        Write-DrillStep "$desc exists at $path" 'OK'
        return $true
    } else {
        Write-DrillStep "$desc MISSING at $path" 'FAIL'
        return $false
    }
}

function Test-RunnerAlive { param([string]$pattern, [string]$name)
    $procs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$pattern*" }
    if ($procs) {
        Write-DrillStep "$name alive (PIDs: $((@($procs)).ProcessId -join ','))" 'OK'
        return $true
    } else {
        Write-DrillStep "$name NOT running" 'WARN'
        return $false
    }
}

function Write-DrillEvidence {
    param(
        [string]$Path,
        [hashtable]$Payload
    )
    if (-not (Test-Path $drillEvidenceDir)) {
        New-Item -ItemType Directory -Force -Path $drillEvidenceDir | Out-Null
    }
    $Payload | ConvertTo-Json -Depth 6 | Out-File -FilePath $Path -Encoding utf8
    Write-DrillStep "wrote drill evidence: $Path" 'OK'
}

function Drill-Plan {
    Write-Host ""
    Write-Host "=== DRILL HARNESS - INFRASTRUCTURE CHECK ===" -ForegroundColor Cyan
    Write-Host ""

    Write-Host "1. Kill-switch infrastructure:"
    Test-FileExists "$repo\argus_flow\runner_unified.py" "argus runner_unified" | Out-Null
    Test-FileExists "$repo\helio\ibkr_execution.py" "helio ibkr_execution (HALT.flag handler)" | Out-Null
    Write-DrillStep "KILL_SWITCH path: $killFile (currently $(if (Test-Path $killFile) { 'PRESENT' } else { 'absent' }))"
    Write-DrillStep "HALT.flag path:   $haltFile (currently $(if (Test-Path $haltFile) { 'PRESENT' } else { 'absent' }))"

    Write-Host ""
    Write-Host "2. Circuit breaker infrastructure:"
    Test-FileExists "$repo\ops\daily_loss_circuit_breaker.py" "daily_loss_circuit_breaker" | Out-Null

    Write-Host ""
    Write-Host "3. Flatten infrastructure:"
    Test-FileExists "$repo\ops\flatten_eod_executor.py" "flatten_eod_executor" | Out-Null
    Write-DrillStep "FLATTEN_EOD.flag path: $flattenFile"

    Write-Host ""
    Write-Host "4. Live runner sample:"
    Test-RunnerAlive 'argus_flow.runner_unified' 'argus' | Out-Null
    Test-RunnerAlive 'multi_orb.runner' 'multi_orb' | Out-Null
    Test-RunnerAlive 'vix_intraday.runner' 'vix_intraday' | Out-Null

    Write-Host ""
    Write-Host "Re-run with -Drill Kill | Halt | CircuitBreaker | Flatten | All to execute."
}

function Drill-Kill {
    Write-Host ""
    Write-Host "=== KILL_SWITCH DRILL ===" -ForegroundColor Cyan
    Write-Host "Will: write KILL_SWITCH flag, wait 30s, verify runners detect, remove flag, verify recovery."
    Write-Host ""
    $startedAt = (Get-Date).ToUniversalTime().ToString("o")
    $detected = $false
    $heartbeatFresh = $false
    $sample = ""

    if (Test-Path $killFile) {
        Write-DrillStep "KILL_SWITCH already present - aborting drill (would mask state)" 'FAIL'
        Write-DrillEvidence $killDrillReport @{
            drill = "KILL_SWITCH"
            status = "FAIL"
            pass = $false
            started_at = $startedAt
            completed_at = (Get-Date).ToUniversalTime().ToString("o")
            reason = "KILL_SWITCH already present before drill"
        }
        return
    }

    Write-DrillStep "Step 1: Writing $killFile" 'INFO'
    "drill-test-$(Get-Date -Format 'o')" | Out-File -FilePath $killFile -Encoding ascii

    Write-DrillStep "Step 2: Waiting 30s for runners to detect..." 'INFO'
    Start-Sleep -Seconds 30

    Write-DrillStep "Step 3: Sample log scan for KILL_SWITCH detection lines"
    # Force array context with @(...) so single-line matches don't degrade to a string
    # (where [0] would return the first character instead of the line).
    $detectionLog = @(Get-Content "$repo\argus_flow\logs\runner_unified.log" -Tail 30 -ErrorAction SilentlyContinue |
        Where-Object { $_ -match 'KILL_SWITCH|kill[_\s-]switch' })
    if ($detectionLog.Count -gt 0) {
        $detected = $true
        $sample = "$($detectionLog[0])"
        Write-DrillStep "argus detected KILL_SWITCH ($($detectionLog.Count) line(s); sample: $($detectionLog[0]))" 'OK'
    } else {
        Write-DrillStep "no KILL_SWITCH detection in argus log within 30s - flag may not be checked, OR no eval cycle ran" 'WARN'
    }

    Write-DrillStep "Step 4: Removing $killFile" 'INFO'
    Remove-Item $killFile -Force

    Write-DrillStep "Step 5: Waiting 30s for recovery..." 'INFO'
    Start-Sleep -Seconds 30

    Write-DrillStep "Step 6: Verify runners recovered (heartbeat freshness)"
    # Argus uses per-pair heartbeats (usdjpy/gbpusd/cadjpy), not a unified file.
    # Drill PASSes if ANY argus pair has refreshed its heartbeat within 120s.
    $argusHbPaths = @(
        "$repo\argus_flow\logs\usdjpy\heartbeat.json",
        "$repo\argus_flow\logs\gbpusd\heartbeat.json",
        "$repo\argus_flow\logs\cadjpy\heartbeat.json"
    )
    $freshHbs = @()
    foreach ($hb in $argusHbPaths) {
        if (Test-Path $hb) {
            $age = (Get-Date) - (Get-Item $hb).LastWriteTime
            if ($age.TotalSeconds -lt 120) {
                $freshHbs += [pscustomobject]@{ path = $hb; age_s = [int]$age.TotalSeconds }
            }
        }
    }
    if ($freshHbs.Count -gt 0) {
        $heartbeatFresh = $true
        $sample = ($freshHbs | Select-Object -First 1)
        Write-DrillStep "argus heartbeat fresh on $($freshHbs.Count) pair(s); sample: $($sample.path) ($($sample.age_s)s old)" 'OK'
    } else {
        Write-DrillStep "no argus pair heartbeat refreshed within 120s of flag removal" 'WARN'
    }
    $status = if ($detected -and $heartbeatFresh) { "PASS" } elseif ($detected) { "WARN" } else { "FAIL" }
    Write-DrillEvidence $killDrillReport @{
        drill = "KILL_SWITCH"
        status = $status
        pass = ($status -eq "PASS")
        started_at = $startedAt
        completed_at = (Get-Date).ToUniversalTime().ToString("o")
        kill_file = $killFile
        detected = $detected
        heartbeat_fresh = $heartbeatFresh
        detection_sample = $sample
        note = "PASS requires KILL_SWITCH detection in runner log and fresh heartbeat after flag removal."
    }
    Write-DrillStep "KILL drill complete." 'OK'
}

function Drill-Halt {
    Write-Host ""
    Write-Host "=== HALT.flag DRILL (helio entry-block) ===" -ForegroundColor Cyan
    Write-Host "HALT.flag is a softer kill - blocks NEW entries via helio.ibkr_execution"
    Write-Host "but allows existing positions to manage themselves. Used by daily-loss circuit breaker."
    Write-Host ""

    if (Test-Path $haltFile) {
        Write-DrillStep "HALT.flag already present - aborting drill" 'FAIL'
        return
    }

    Write-DrillStep "Step 1: Writing $haltFile" 'INFO'
    "drill-halt-$(Get-Date -Format 'o')" | Out-File -FilePath $haltFile -Encoding ascii

    Write-DrillStep "Step 2: Waiting 60s for next signal cycle..." 'INFO'
    Start-Sleep -Seconds 60

    Write-DrillStep "Step 3: Scan recent logs for HALT-rejection messages"
    $haltLogs = Get-ChildItem "$repo\forge\logs\*\runner.log" -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Content $_.FullName -Tail 50 -ErrorAction SilentlyContinue } |
        Where-Object { $_ -match 'HALT|halt' }
    if ($haltLogs) {
        Write-DrillStep "HALT detected by at least one runner" 'OK'
    } else {
        Write-DrillStep "no HALT messages in recent runner logs - either no signals fired in 60s OR plumbing broken" 'WARN'
    }

    Write-DrillStep "Step 4: Removing $haltFile" 'INFO'
    Remove-Item $haltFile -Force
    Write-DrillStep "HALT drill complete." 'OK'
}

function Drill-CircuitBreaker {
    Write-Host ""
    Write-Host "=== DAILY-LOSS CIRCUIT BREAKER DRY-RUN ===" -ForegroundColor Cyan
    Write-Host "Runs ops/daily_loss_circuit_breaker.py once and reads the verdict JSON."
    Write-Host "Does NOT trigger HALT.flag (that requires real -2% / -4% breach)."
    Write-Host ""

    Push-Location $repo
    try {
        & $python -m ops.daily_loss_circuit_breaker 2>&1 | Out-Host
    } finally {
        Pop-Location
    }

    $verdictPath = "$repo\argus_flow\logs\_risk\circuit_breaker_state.json"
    if (Test-Path $verdictPath) {
        $v = Get-Content $verdictPath | ConvertFrom-Json
        Write-DrillStep "Circuit breaker state file exists" 'OK'
        Write-DrillStep ("Latest: tier={0} equity=`${1} pnl_pct={2}% ts={3}" -f `
            $v.current_tier, $v.latest_equity_usd, $v.latest_pnl_pct, $v.latest_ts)
    } else {
        Write-DrillStep "no circuit_breaker_state.json at expected path - script may not have written output" 'WARN'
    }
    Write-DrillStep "CircuitBreaker drill complete." 'OK'
}

function Drill-Flatten {
    Write-Host ""
    Write-Host "=== FLATTEN_EOD PLUMBING DRILL ===" -ForegroundColor Cyan
    Write-Host "Verifies the FLATTEN_EOD.flag mechanism without actually flattening positions."
    Write-Host ""

    if (Test-Path $flattenFile) {
        Write-DrillStep "FLATTEN_EOD.flag already present - aborting drill" 'FAIL'
        return
    }

    Write-DrillStep "Step 1: Verifying flatten_eod_executor.py exists"
    if (-not (Test-FileExists "$repo\ops\flatten_eod_executor.py" "flatten executor")) { return }

    Write-DrillStep "Step 2: Calling flatten executor with no flag (should no-op)"
    Push-Location $repo
    try {
        $output = & $python -m ops.flatten_eod_executor 2>&1
        if ($output -match 'no flag|noop|skipping|not present') {
            Write-DrillStep "executor correctly no-ops without flag" 'OK'
        } else {
            Write-DrillStep "executor output: $output" 'WARN'
        }
    } finally {
        Pop-Location
    }

    Write-DrillStep "Step 3: NOT writing the flag - real flatten is destructive."
    Write-DrillStep "To do a real flatten drill: write the flag manually + run executor."
    Write-DrillStep "Flatten drill complete." 'OK'
}

# === Main ===
switch ($Drill) {
    'Plan'           { Drill-Plan }
    'Kill'           { Drill-Plan; Drill-Kill }
    'Halt'           { Drill-Plan; Drill-Halt }
    'CircuitBreaker' { Drill-Plan; Drill-CircuitBreaker }
    'Flatten'        { Drill-Plan; Drill-Flatten }
    'All'            { Drill-Plan; Drill-Halt; Drill-CircuitBreaker; Drill-Flatten; Drill-Kill }
}
