param(
    [int]$Limit = 0,
    [switch]$SingleRun,          # pass through to run_backtest.ps1 - skips Run 2 + determinism
    [hashtable]$EnvOverrides = @{}  # e.g. @{USE_TRENDLINES="true"; TL_PENALTY_RESIST_SLOPE_NEG="5"}
)

$PC2 = "ksmith2322@yahoo.com@192.168.1.98"
$BRANCH = "phase6-hardening"

Write-Host "--- DISPATCH TO PC2 ---"

# Push latest from PC1
Set-Location C:\Argus\repo
git add -A
$status = git status --porcelain
if ($status) {
    $ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    git commit -m "pre-dispatch sync $ts"
}
git push origin $BRANCH

$singleRunFlag = if ($SingleRun) { " -SingleRun" } else { "" }

# Pull first (via direct SSH)
Write-Host "Pulling latest code on PC2..."
ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Set-Location C:/Argus/repo; git pull origin $BRANCH`""

# Verify PC2 is on same commit as PC1
$pc1Hash = git rev-parse HEAD
$pc2Hash = ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Set-Location C:/Argus/repo; git rev-parse HEAD`"" 2>$null
if ($pc2Hash -and $pc2Hash.Trim() -ne $pc1Hash.Trim()) {
    Write-Warning "PC2 commit ($($pc2Hash.Trim().Substring(0,7))) != PC1 ($($pc1Hash.Trim().Substring(0,7))) — pull may have failed"
} else {
    Write-Host "PC2 in sync: $($pc1Hash.Trim().Substring(0,7))" -ForegroundColor Green
}

# Build launcher script lines (avoids quoting hell over SSH)
# Wrapper redirects stdout/stderr to a log file so we can diagnose crashes
$ts = (Get-Date -Format "yyyyMMddTHHmmss")
$logFile = "C:/Argus/repo/ops/logs/pc2_bt_${ts}.log"
$lines = @(
    "`$ErrorActionPreference = 'Stop'"
    "Set-Location C:/Argus/repo"
    ". C:/Argus/.venv/Scripts/Activate.ps1"
)
if ($Limit -gt 0) {
    $lines += "`$env:BACKTEST_LIMIT='$Limit'"
}
foreach ($k in $EnvOverrides.Keys) {
    $v = $EnvOverrides[$k]
    $lines += "`$env:${k}='${v}'"
}
$lines += @(
    "try {"
    "    ./ops/run_backtest.ps1$singleRunFlag *>&1 | Tee-Object -FilePath '$logFile'"
    "    Add-Content -Path '$logFile' -Value 'EXIT_CODE=0'"
    "} catch {"
    "    Add-Content -Path '$logFile' -Value `"FATAL: `$(`$_.Exception.Message)`""
    "    Add-Content -Path '$logFile' -Value 'EXIT_CODE=1'"
    "}"
)

# Write launcher script locally, then SCP to PC2
$localTmp = "$env:TEMP\_pc2_run.ps1"
$lines | Set-Content -Path $localTmp -Encoding utf8
Write-Host "Launcher script:"
$lines | ForEach-Object { Write-Host "  $_" }
Write-Host "Log file: $logFile"

Write-Host "Uploading launcher to PC2..."
scp $localTmp "${PC2}:C:/Argus/repo/ops/_pc2_run.ps1"

# Launch via schtasks (truly detached from SSH session -- survives disconnect)
# Write a tiny launch-wrapper that schtasks will invoke, avoiding nested-quote hell
Write-Host "Starting backtest on PC2..."
$launchLines = @(
    '$taskName = "ArgusDispatchBT"'
    '$psExe = "powershell.exe"'
    '$psArgs = "-NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/_pc2_run.ps1"'
    'try {'
    '    schtasks /Create /TN $taskName /TR "$psExe $psArgs" /SC ONCE /ST 00:00 /F | Out-Null'
    '    schtasks /Run /TN $taskName | Out-Null'
    '} catch {'
    '    Start-Process -FilePath $psExe -ArgumentList $psArgs.Split(" ") -WindowStyle Hidden'
    '}'
)
$launchLocal = "$env:TEMP\_pc2_launch.ps1"
$launchLines | Set-Content -Path $launchLocal -Encoding utf8
scp $launchLocal "${PC2}:C:/Argus/repo/ops/_pc2_launch.ps1"
ssh $PC2 "powershell -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/_pc2_launch.ps1"

# Verify it started
Start-Sleep -Seconds 15
$pyCount = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "(Get-Process python* -ErrorAction SilentlyContinue | Measure-Object).Count"' 2>$null
if ([int]$pyCount -gt 0) {
    Write-Host "CONFIRMED: $pyCount Python process(es) running on PC2" -ForegroundColor Green
} else {
    Write-Host "WARNING: No Python processes detected -- check PC2 manually" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "--- DISPATCHED ---"
Write-Host "Check status: .\ops\check_pc2.ps1"
Write-Host "Pull results:  .\ops\pull_pc2_results.ps1"