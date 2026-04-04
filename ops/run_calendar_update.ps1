# ops/run_calendar_update.ps1 -- Weekly economic calendar refresh
# Scheduled for Sunday evenings via Task Scheduler
# Usage: .\ops\run_calendar_update.ps1

$ErrorActionPreference = "Stop"

Set-Location "C:\Argus\repo"

Write-Host "=== Argus Economic Calendar Update ===" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

try {
    & C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.fetch_economic_calendar --weeks 3
    Write-Host "  [OK] Calendar updated" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Calendar update: $_" -ForegroundColor Red
    exit 1
}
