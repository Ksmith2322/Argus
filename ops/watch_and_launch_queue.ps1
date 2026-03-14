# ops/watch_and_launch_queue.ps1 -- Watch for in-flight backtests to finish, then launch queue
#
# Usage:
#   .\ops\watch_and_launch_queue.ps1                    # watch + launch PC1 queue
#   .\ops\watch_and_launch_queue.ps1 -IncludePC2        # also dispatch PC2 queue after launch
#   .\ops\watch_and_launch_queue.ps1 -PollSeconds 30    # custom poll interval
#
# Polls for running backtest.runner processes. When none remain, launches run_queue.ps1
# and optionally dispatches PC2 queue.
param(
    [switch]$IncludePC2,
    [int]$PollSeconds = 60,
    [string]$PC2QueueFile = "ops\backtest_queue_pc2.jsonl"
)

$ErrorActionPreference = "Stop"
$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"

Set-Location $repoRoot

function Get-BacktestProcessCount {
    $procs = Get-Process python*, python3* -ErrorAction SilentlyContinue |
        Where-Object {
            try {
                $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
                $cmd -match "backtest\.runner" -or $cmd -match "backtest\\runner"
            } catch { $false }
        }
    return @($procs).Count
}

Write-Host "=== QUEUE WATCHER ===" -ForegroundColor Cyan
Write-Host "Polling every ${PollSeconds}s for in-flight backtests to finish..."
Write-Host "IncludePC2: $IncludePC2"
Write-Host ""

# Poll until no backtest processes remain
$waited = 0
while ($true) {
    $count = Get-BacktestProcessCount
    if ($count -eq 0) {
        Write-Host ""
        Write-Host "No backtest processes detected. Ready to launch queue." -ForegroundColor Green
        break
    }

    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] $count backtest process(es) still running. Waiting ${PollSeconds}s..." -ForegroundColor Yellow
    Start-Sleep -Seconds $PollSeconds
    $waited += $PollSeconds
}

if ($waited -gt 0) {
    $waitMin = [math]::Round($waited / 60, 1)
    Write-Host "Waited ${waitMin} minutes for in-flight backtests." -ForegroundColor Cyan
}

# Brief pause to let file handles close
Start-Sleep -Seconds 5

# Launch PC1 queue
Write-Host ""
Write-Host "=== LAUNCHING PC1 QUEUE ===" -ForegroundColor Green
try {
    & $pyExe "$repoRoot\ops\notify.py" --test "Watcher: in-flight backtests done. Launching PC1 queue." 2>$null
} catch {}

& "$repoRoot\ops\run_queue.ps1"

# Dispatch PC2 if requested
if ($IncludePC2) {
    $pc2Queue = Join-Path $repoRoot $PC2QueueFile
    if (Test-Path $pc2Queue) {
        Write-Host ""
        Write-Host "=== DISPATCHING PC2 QUEUE ===" -ForegroundColor Green
        try {
            & $pyExe "$repoRoot\ops\notify.py" --test "Watcher: dispatching PC2 queue ($PC2QueueFile)." 2>$null
        } catch {}
        & "$repoRoot\ops\dispatch_queue_pc2.ps1" -QueueFile $PC2QueueFile
    } else {
        Write-Host "PC2 queue file not found: $pc2Queue" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "=== WATCHER COMPLETE ===" -ForegroundColor Green