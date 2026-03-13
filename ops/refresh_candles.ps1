# ops/refresh_candles.ps1
# Downloads the latest 30 days of ETH-USD 1m candles and overwrites data/eth_usd_1m.csv.
# Run manually or schedule weekly via Task Scheduler.
# After running, commit + push so PC2 gets fresh data via git pull.

$ErrorActionPreference = "Stop"
Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1

Write-Host "=== Refreshing ETH-USD 1m candle data ===" -ForegroundColor Cyan
Write-Host "Downloading last 30 days..."

$env:PRODUCT_ID = "ETH-USD"
$env:GRANULARITY = "60"
$env:DAYS_BACK = "30"
# OUT_CSV defaults to data/eth_usd_1m.csv

C:\Argus\.venv\Scripts\python.exe -m backtest.download_candles

Write-Host ""
Write-Host "Committing updated candle data to git..." -ForegroundColor Cyan
git add data/eth_usd_1m.csv
$ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssZ")
git commit -m "refresh candles $ts" 2>&1
git push origin phase6-hardening 2>&1

Write-Host ""
Write-Host "=== Done. PC2 can now git pull to get fresh candles. ===" -ForegroundColor Green
