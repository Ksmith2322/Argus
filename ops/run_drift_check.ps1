# ops/run_drift_check.ps1 -- Daily feature drift detection
# Scheduled daily at 06:00 via Task Scheduler
# Usage: .\ops\run_drift_check.ps1

$ErrorActionPreference = "Stop"

Set-Location "C:\Argus\repo"

$logFile = "C:\Argus\repo\argus_flow\logs\drift_check.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$python = "C:\Argus\.venv\Scripts\python.exe"

Write-Host "=== Argus Drift Detector ===" -ForegroundColor Cyan
Write-Host "  $timestamp"

try {
    $output = & $python -m helio.drift_detector 2>&1
    foreach ($line in @($output)) {
        if ($line) {
            Write-Host "  $line"
            Add-Content -Path $logFile -Value "[$timestamp] $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "exit code $LASTEXITCODE"
    }
    Add-Content -Path $logFile -Value "[$timestamp] === Drift check complete ==="
    Write-Host "  [OK] Drift check complete" -ForegroundColor Green
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] ERROR: $_"
    Write-Host "  [FAIL] Drift check: $_" -ForegroundColor Red
    exit 1
}
