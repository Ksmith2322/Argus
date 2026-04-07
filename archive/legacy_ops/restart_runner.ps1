# ops/restart_runner.ps1 -- Safely restart runner_live.py
# Sends graceful stop to existing runner, waits for clean shutdown, then restarts.
# Usage: .\ops\restart_runner.ps1 [-SkipConfirm]
param(
    [switch]$SkipConfirm
)

$ErrorActionPreference = "Stop"
Set-Location C:\Argus\repo

Write-Host "=== RUNNER RESTART ===" -ForegroundColor Cyan

# Find existing runner process
$runners = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmdline -match "runner_live"
    } catch { $false }
}

if ($runners) {
    Write-Host "Found runner process(es):" -ForegroundColor Yellow
    foreach ($r in $runners) {
        Write-Host "  PID=$($r.Id)  CPU=$([math]::Round($r.CPU, 1))s  Started=$($r.StartTime)"
    }

    if (-not $SkipConfirm) {
        $answer = Read-Host "Stop existing runner and restart with current .env? (y/n)"
        if ($answer -ne "y") {
            Write-Host "Aborted." -ForegroundColor Red
            exit 0
        }
    }

    # Graceful stop (sends Ctrl+C equivalent)
    Write-Host "Sending stop signal..." -ForegroundColor Yellow
    foreach ($r in $runners) {
        Stop-Process -Id $r.Id -ErrorAction SilentlyContinue
    }

    # Wait for clean shutdown (up to 30s)
    $waited = 0
    while ($waited -lt 30) {
        Start-Sleep -Seconds 2
        $waited += 2
        $still = Get-Process -Id ($runners | ForEach-Object { $_.Id }) -ErrorAction SilentlyContinue
        if (-not $still) {
            Write-Host "Runner stopped cleanly after ${waited}s." -ForegroundColor Green
            break
        }
        Write-Host "  Waiting... (${waited}s)"
    }

    # Force kill if still alive
    $still = Get-Process -Id ($runners | ForEach-Object { $_.Id }) -ErrorAction SilentlyContinue
    if ($still) {
        Write-Host "Force-killing after 30s timeout..." -ForegroundColor Red
        $still | Stop-Process -Force
        Start-Sleep -Seconds 2
    }
} else {
    Write-Host "No existing runner found." -ForegroundColor Yellow
}

# Show key config values
Write-Host ""
Write-Host "Current config:" -ForegroundColor Cyan
$envContent = Get-Content .env -ErrorAction SilentlyContinue
$keys = @("CONFLUENCE_MIN_SCORE", "CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE", "REGIME_ENTRY_BLOCK_LIST", "USE_TRENDLINES", "LIQ_MODE", "EXECUTION_MODE")
foreach ($k in $keys) {
    $line = $envContent | Where-Object { $_ -match "^${k}=" } | Select-Object -Last 1
    if ($line) {
        Write-Host "  $line"
    }
}

# Start runner in new window
Write-Host ""
Write-Host "Starting runner_live.py..." -ForegroundColor Green
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" `
    -ArgumentList ".\runner_live.py" `
    -WorkingDirectory "C:\Argus\repo" `
    -WindowStyle Normal

Start-Sleep -Seconds 3

# Verify it started
$newRunner = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmdline -match "runner_live"
    } catch { $false }
}

if ($newRunner) {
    Write-Host "Runner started: PID=$($newRunner.Id)" -ForegroundColor Green
} else {
    Write-Host "WARNING: Runner may not have started. Check manually." -ForegroundColor Red
}

Write-Host "=== DONE ===" -ForegroundColor Cyan