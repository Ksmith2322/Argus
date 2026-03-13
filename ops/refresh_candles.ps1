# ops/refresh_candles.ps1
# Downloads fresh 1m candles for all tracked pairs and optionally syncs to PC2.
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

# All tracked pairs — add new ones here
$pairs = @(
    @{ product="ETH-USD"; file="eth_usd_1m.csv" },
    @{ product="BTC-USD"; file="btc_usd_1m.csv" },
    @{ product="SOL-USD"; file="sol_usd_1m.csv" }
)

Write-Host "=== Refreshing candle data ($($pairs.Count) pairs, ${DaysBack}d) ===" -ForegroundColor Cyan

foreach ($p in $pairs) {
    Write-Host ""
    Write-Host "--- $($p.product) ---" -ForegroundColor Yellow
    $env:PRODUCT_ID = $p.product
    $env:GRANULARITY = "60"
    $env:DAYS_BACK = "$DaysBack"
    $env:OUT_CSV = "C:\Argus\repo\data\$($p.file)"

    C:\Argus\.venv\Scripts\python.exe -m backtest.download_candles

    Remove-Item Env:DAYS_BACK -ErrorAction SilentlyContinue
    Remove-Item Env:PRODUCT_ID -ErrorAction SilentlyContinue
    Remove-Item Env:GRANULARITY -ErrorAction SilentlyContinue
    Remove-Item Env:OUT_CSV -ErrorAction SilentlyContinue
}

# Validate downloaded candles
Write-Host ""
Write-Host "Validating candle data..." -ForegroundColor Cyan
C:\Argus\.venv\Scripts\python.exe C:\Argus\repo\ops\validate_candles.py
if ($LASTEXITCODE -eq 1) {
    Write-Host "CANDLE VALIDATION FAILED — aborting commit" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Committing updated candle data to git..." -ForegroundColor Cyan
$dataFiles = $pairs | ForEach-Object { "data/$($_.file)" }
git add $dataFiles
$status = git diff --cached --stat
if ($status) {
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssZ")
    $names = ($pairs | ForEach-Object { $_.product }) -join "+"
    git commit -m "refresh candles: ${DaysBack}d $names $ts"
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