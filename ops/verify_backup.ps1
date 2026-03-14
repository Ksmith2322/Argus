# ops/verify_backup.ps1 -- Weekly backup verification drill
# Verifies git backup integrity: remote reachable, push up-to-date, key files exist.
# Schedule via Task Scheduler (weekly) or run manually.
# Usage: .\ops\verify_backup.ps1
param()

$ErrorActionPreference = "Continue"
$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logFile = "$repoRoot\ops\logs\backup_verify_$((Get-Date).ToString('yyyyMMdd')).log"
$failures = @()

Set-Location $repoRoot

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

Log "=== Argus Backup Verification ==="

# 1. Check git status -- no uncommitted critical files
Log "--- Check 1: uncommitted changes ---"
$status = git status --porcelain 2>&1
$criticalUncommitted = $status | Where-Object {
    $_ -match '(config\.py|engine\.py|state\.py|ledger\.py|runner_live\.py|\.env)$'
}
if ($criticalUncommitted) {
    $msg = "WARN: critical files uncommitted: $($criticalUncommitted -join ', ')"
    Log $msg
    $failures += $msg
} else {
    Log "OK: no critical files uncommitted"
}

# 2. Check remote reachable
Log "--- Check 2: remote reachable ---"
$fetchOut = git fetch origin 2>&1
if ($LASTEXITCODE -ne 0) {
    $msg = "FAIL: git fetch failed -- remote unreachable"
    Log $msg
    $failures += $msg
} else {
    Log "OK: remote reachable"
}

# 3. Check local is not behind remote
Log "--- Check 3: local vs remote sync ---"
$behind = git rev-list --count HEAD..origin/phase6-hardening 2>&1
$ahead = git rev-list --count origin/phase6-hardening..HEAD 2>&1
if ([int]$behind -gt 0) {
    $msg = "WARN: local is $behind commit(s) behind remote"
    Log $msg
    $failures += $msg
}
if ([int]$ahead -gt 0) {
    Log "INFO: local is $ahead commit(s) ahead of remote (will sync at next backup)"
} else {
    Log "OK: local and remote in sync"
}

# 4. Check key files exist
Log "--- Check 4: key files exist ---"
$keyFiles = @(
    ".env",
    "config.py",
    "engine.py",
    "state.py",
    "ledger.py",
    "runner_live.py",
    "backtest\runner.py",
    "ops\auto_git_backup.ps1",
    "ops\run_backtest.ps1"
)
foreach ($f in $keyFiles) {
    $fp = Join-Path $repoRoot $f
    if (!(Test-Path $fp)) {
        $msg = "FAIL: missing key file: $f"
        Log $msg
        $failures += $msg
    }
}
if ($failures.Count -eq 0 -or ($failures | Where-Object { $_ -match "missing" }).Count -eq 0) {
    Log "OK: all key files present"
}

# 5. Check state files exist and are recent
Log "--- Check 5: runtime state freshness ---"
$stateFile = "$repoRoot\state\runtime_state_ETH_USD.json"
if (Test-Path $stateFile) {
    $stateAge = ((Get-Date) - (Get-Item $stateFile).LastWriteTime).TotalHours
    if ($stateAge -gt 24) {
        Log "WARN: runtime state is $([math]::Round($stateAge, 1))h old (runner may be down)"
    } else {
        Log "OK: runtime state updated $([math]::Round($stateAge, 1))h ago"
    }
} else {
    Log "INFO: no runtime state file (runner not active)"
}

# 6. Check candle data exists and is reasonable size
Log "--- Check 6: candle data ---"
$candleFiles = @("data\eth_usd_1m.csv", "data\btc_usd_1m.csv", "data\sol_usd_1m.csv")
foreach ($cf in $candleFiles) {
    $cfp = Join-Path $repoRoot $cf
    if (Test-Path $cfp) {
        $lines = (Get-Content $cfp | Select-Object -First 1 | Measure-Object).Count
        $size = (Get-Item $cfp).Length
        if ($size -lt 1000000) {
            Log "WARN: $cf is only $([math]::Round($size/1KB))KB (expected >1MB)"
        } else {
            Log "OK: $cf ($([math]::Round($size/1MB, 1))MB)"
        }
    } else {
        Log "INFO: $cf not present"
    }
}

# 7. Check last backup log
Log "--- Check 7: last backup log ---"
$backupLogs = Get-ChildItem "$repoRoot\ops\logs" -Filter "git_backup_*.log" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($backupLogs) {
    $logAge = ((Get-Date) - $backupLogs.LastWriteTime).TotalDays
    if ($logAge -gt 2) {
        $msg = "WARN: last backup log is $([math]::Round($logAge, 1)) days old"
        Log $msg
        $failures += $msg
    } else {
        Log "OK: last backup ran $([math]::Round($logAge, 1)) days ago ($($backupLogs.Name))"
    }
} else {
    $msg = "WARN: no backup logs found"
    Log $msg
    $failures += $msg
}

# Summary
Log ""
Log "=== VERIFICATION SUMMARY ==="
if ($failures.Count -eq 0) {
    Log "RESULT: ALL CHECKS PASSED"
} else {
    Log "RESULT: $($failures.Count) issue(s) found:"
    foreach ($f in $failures) { Log "  - $f" }
    # Discord alert on failure
    if (Test-Path "$repoRoot\ops\notify.py") {
        $alertMsg = "Backup verification: $($failures.Count) issue(s) found. Check $logFile"
        & $pyExe "$repoRoot\ops\notify.py" --error $alertMsg 2>$null
    }
}
Log "=== Done ==="