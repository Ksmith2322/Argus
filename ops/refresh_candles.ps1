# ops/refresh_candles.ps1
# Downloads fresh ETH-USD 1m candles and optionally syncs to PC2.
# Usage: .\ops\refresh_candles.ps1 [-DaysBack 30] [-SyncPC2]
param(
    [int]$DaysBack = 30,
    [switch]$SyncPC2
)

$ErrorActionPreference = "Stop"
$PC2 = "ksmith2322@yahoo.com@desktop-17cjmup"
$BRANCH = "phase6-hardening"

Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1

Write-Host "=== Refreshing ETH-USD 1m candle data ===" -ForegroundColor Cyan
Write-Host "Days back: $DaysBack"

$env:PRODUCT_ID = "ETH-USD"
$env:GRANULARITY = "60"
$env:DAYS_BACK = "$DaysBack"
# OUT_CSV defaults to data/eth_usd_1m.csv

C:\Argus\.venv\Scripts\python.exe -m backtest.download_candles

Remove-Item Env:DAYS_BACK -ErrorAction SilentlyContinue
Remove-Item Env:PRODUCT_ID -ErrorAction SilentlyContinue
Remove-Item Env:GRANULARITY -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Committing updated candle data to git..." -ForegroundColor Cyan
git add data/eth_usd_1m.csv
$status = git diff --cached --stat
if ($status) {
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssZ")
    git commit -m "refresh candles: ${DaysBack}d ETH-USD $ts"
    git push origin $BRANCH 2>&1
    Write-Host "Pushed to remote." -ForegroundColor Green
} else {
    Write-Host "No candle changes to commit." -ForegroundColor Yellow
}

if ($SyncPC2) {
    Write-Host ""
    Write-Host "--- SYNCING TO PC2 ---" -ForegroundColor Cyan
    ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Set-Location C:/Argus/repo; git pull origin $BRANCH`""
    Write-Host "PC2 synced." -ForegroundColor Green
}

Write-Host ""
Write-Host "=== Done ===" -ForegroundColor Green