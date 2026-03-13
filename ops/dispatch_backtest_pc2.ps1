param(
    [int]$Limit = 0,
    [switch]$SingleRun,          # pass through to run_backtest.ps1 - skips Run 2 + determinism
    [hashtable]$EnvOverrides = @{}  # e.g. @{USE_TRENDLINES="true"; TL_PENALTY_RESIST_SLOPE_NEG="5"}
)

$PC2 = "ksmith2322@yahoo.com@desktop-17cjmup"
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

# Build launcher script lines (avoids quoting hell over SSH)
$lines = @("Set-Location C:/Argus/repo")
if ($Limit -gt 0) {
    $lines += "`$env:BACKTEST_LIMIT='$Limit'"
}
foreach ($k in $EnvOverrides.Keys) {
    $v = $EnvOverrides[$k]
    $lines += "`$env:${k}='${v}'"
}
$lines += "./ops/run_backtest.ps1$singleRunFlag"

# Write launcher script locally, then SCP to PC2
$localTmp = "$env:TEMP\_pc2_run.ps1"
$lines | Set-Content -Path $localTmp -Encoding utf8
Write-Host "Launcher script:"
$lines | ForEach-Object { Write-Host "  $_" }

Write-Host "Uploading launcher to PC2..."
scp $localTmp "${PC2}:C:/Argus/repo/ops/_pc2_run.ps1"

# Launch detached process on PC2 via Start-Process (survives SSH disconnect)
Write-Host "Starting backtest on PC2..."
ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Start-Process -FilePath powershell.exe -ArgumentList @('-NonInteractive','-NoProfile','-ExecutionPolicy','Bypass','-File','C:/Argus/repo/ops/_pc2_run.ps1') -WindowStyle Hidden`""

# Verify it started
Start-Sleep -Seconds 15
$pyCount = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "(Get-Process python* -ErrorAction SilentlyContinue | Measure-Object).Count"' 2>$null
if ([int]$pyCount -gt 0) {
    Write-Host "CONFIRMED: $pyCount Python process(es) running on PC2" -ForegroundColor Green
} else {
    Write-Host "WARNING: No Python processes detected — check PC2 manually" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "--- DISPATCHED ---"
Write-Host "Check status: .\ops\check_pc2.ps1"
Write-Host "Pull results:  .\ops\pull_pc2_results.ps1"