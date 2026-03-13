param([int]$Limit = 0)

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

# Build remote command
$pull = "cd /d C:\Argus\repo && git pull origin $BRANCH"
if ($Limit -gt 0) {
    $setLim = "&& set BACKTEST_LIMIT=$Limit"
} else {
    $setLim = ""
}
$bt = "&& powershell -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\run_backtest.ps1"

ssh $PC2 "cmd /c ""$pull $setLim $bt"""

Write-Host "--- DISPATCH COMPLETE ---"