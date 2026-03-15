# ops/dispatch_queue_pc2.ps1 -- Push queue + code to PC2 and start queue runner
#
# Usage:
#   .\ops\dispatch_queue_pc2.ps1                              # push default queue
#   .\ops\dispatch_queue_pc2.ps1 -QueueFile ops\backtest_queue_pc2.jsonl  # custom queue
#   .\ops\dispatch_queue_pc2.ps1 -MaxJobs 2                  # limit jobs on PC2
#
# Prerequisites: code pushed to git, PC2 reachable via SSH
param(
    [int]$MaxJobs = 0,
    [string]$QueueFile = "ops\backtest_queue.jsonl"
)

$PC2 = "ksmith2322@yahoo.com@192.168.1.98"
$BRANCH = "phase6-hardening"

Write-Host "--- DISPATCH QUEUE TO PC2 ---" -ForegroundColor Cyan

# Push latest from PC1
Set-Location C:\Argus\repo
git add -A
$status = git status --porcelain
if ($status) {
    $ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    git commit -m "pre-dispatch sync $ts"
}
git push origin $BRANCH

# Pull on PC2
Write-Host "Pulling latest code on PC2..."
ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Set-Location C:/Argus/repo; git pull origin $BRANCH`""

# Also SCP the queue file (in case it was modified after last commit)
$localQueue = Join-Path "C:\Argus\repo" $QueueFile
Write-Host "Uploading queue file ($QueueFile)..."
scp $localQueue "${PC2}:C:/Argus/repo/ops/backtest_queue.jsonl"

# Build launcher script
$ts = (Get-Date -Format "yyyyMMddTHHmmss")
$logFile = "C:/Argus/repo/ops/logs/pc2_queue_${ts}.log"
$maxJobsFlag = if ($MaxJobs -gt 0) { " -MaxJobs $MaxJobs" } else { "" }

$lines = @(
    "`$ErrorActionPreference = 'Stop'"
    "Set-Location C:/Argus/repo"
    ". C:/Argus/.venv/Scripts/Activate.ps1"
    "try {"
    "    ./ops/run_queue.ps1$maxJobsFlag *>&1 | Tee-Object -FilePath '$logFile'"
    "    Add-Content -Path '$logFile' -Value 'EXIT_CODE=0'"
    "} catch {"
    "    Add-Content -Path '$logFile' -Value `"FATAL: `$(`$_.Exception.Message)`""
    "    Add-Content -Path '$logFile' -Value 'EXIT_CODE=1'"
    "}"
)

$localTmp = "$env:TEMP\_pc2_queue_run.ps1"
$lines | Set-Content -Path $localTmp -Encoding utf8
Write-Host "Launcher script:"
$lines | ForEach-Object { Write-Host "  $_" }
Write-Host "Log file: $logFile"

Write-Host "Uploading launcher to PC2..."
scp $localTmp "${PC2}:C:/Argus/repo/ops/_pc2_queue_run.ps1"

# Launch via schtasks (detached from SSH)
$launchLines = @(
    '$taskName = "ArgusQueueBT"'
    '$psExe = "powershell.exe"'
    '$psArgs = "-NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/_pc2_queue_run.ps1"'
    'try {'
    '    schtasks /Create /TN $taskName /TR "$psExe $psArgs" /SC ONCE /ST 00:00 /F | Out-Null'
    '    schtasks /Run /TN $taskName | Out-Null'
    '} catch {'
    '    Start-Process -FilePath $psExe -ArgumentList $psArgs.Split(" ") -WindowStyle Hidden'
    '}'
)
$launchLocal = "$env:TEMP\_pc2_queue_launch.ps1"
$launchLines | Set-Content -Path $launchLocal -Encoding utf8
scp $launchLocal "${PC2}:C:/Argus/repo/ops/_pc2_queue_launch.ps1"

Write-Host "Starting queue runner on PC2..."
ssh $PC2 "powershell -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/_pc2_queue_launch.ps1"

# Verify
Start-Sleep -Seconds 10
$pyCount = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "(Get-Process python* -ErrorAction SilentlyContinue | Measure-Object).Count"' 2>$null
if ([int]$pyCount -gt 0) {
    Write-Host "CONFIRMED: $pyCount Python process(es) running on PC2" -ForegroundColor Green
} else {
    Write-Host "Queue runner starting (may take a moment for first job)..." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "--- DISPATCHED ---" -ForegroundColor Green
Write-Host "Check status:  .\ops\check_pc2.ps1"
Write-Host "Pull results:  .\ops\pull_pc2_results.ps1"
Write-Host "Queue log:     $logFile (on PC2)"