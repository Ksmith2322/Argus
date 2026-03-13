param(
    [int]$Limit = 0,
    [switch]$SingleRun   # pass through to run_backtest.ps1 - skips Run 2 + determinism
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

# Build remote powershell command (avoids cmd backslash issues)
$limitClause = if ($Limit -gt 0) { "`$env:BACKTEST_LIMIT='$Limit'; " } else { "" }
$singleRunFlag = if ($SingleRun) { " -SingleRun" } else { "" }
$remotePs = "Set-Location C:/Argus/repo; git pull origin $BRANCH; ${limitClause}./ops/run_backtest.ps1${singleRunFlag}"

ssh $PC2 "powershell -NonInteractive -NoProfile -ExecutionPolicy Bypass -Command `"$remotePs`""

Write-Host "--- DISPATCH COMPLETE ---"