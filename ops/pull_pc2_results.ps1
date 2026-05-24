# ops/pull_pc2_results.ps1 -- Pull latest backtest results from PC2 to PC1
# Usage: .\ops\pull_pc2_results.ps1 [-RunId <run_id>]
# If no RunId given, pulls the latest run.
param(
    [string]$RunId = ""
)

$PC2 = "ksmith2322@yahoo.com@192.168.1.101"
$LOCAL_DIR = "C:\Argus\repo\ops\logs\pc2"

if (!(Test-Path $LOCAL_DIR)) {
    New-Item -ItemType Directory -Force -Path $LOCAL_DIR | Out-Null
}

Write-Host "--- PULL PC2 RESULTS ---" -ForegroundColor Cyan

# Resolve run ID
if (-not $RunId) {
    $RunId = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "
        $latest = Get-ChildItem C:/Argus/repo/ops/logs -Filter \"bt_summary_bt_*.json\" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            $j = Get-Content $latest.FullName -Raw | ConvertFrom-Json
            $j.run_id
        }
    "' 2>$null
    if ($RunId) { $RunId = $RunId.Trim() }
}

if (-not $RunId) {
    Write-Host "ERROR: Could not determine run ID on PC2" -ForegroundColor Red
    exit 1
}

Write-Host "Run ID: $RunId"

# Files to pull
$files = @(
    "bt_summary_${RunId}.json",
    "trades_${RunId}.csv",
    "equity_${RunId}.csv",
    "entry_attempts_${RunId}.csv",
    "entry_attempt_detail_${RunId}.csv",
    "event_counts_${RunId}.csv",
    "run_header_${RunId}.json"
)

$pulled = 0
foreach ($f in $files) {
    $remotePath = "C:\Argus\repo\ops\logs\$f"
    $localPath = Join-Path $LOCAL_DIR $f

    Write-Host "  Pulling $f ..." -NoNewline
    $content = ssh $PC2 "type `"$remotePath`"" 2>$null
    if ($LASTEXITCODE -eq 0 -and $content) {
        $content | Out-File -FilePath $localPath -Encoding utf8
        Write-Host " OK" -ForegroundColor Green
        $pulled++
    } else {
        Write-Host " SKIP (not found)" -ForegroundColor Yellow
    }
}

# Also pull WOULD_BUY events for score analysis
Write-Host "  Pulling WOULD_BUY events ..." -NoNewline
$wbPath = Join-Path $LOCAL_DIR "would_buy_${RunId}.csv"
ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Select-String -Path C:/Argus/repo/ops/logs/bt_events_${RunId}.csv -Pattern WOULD_BUY | ForEach-Object { `$_.Line }`"" 2>$null | Out-File -FilePath $wbPath -Encoding utf8
if ((Test-Path $wbPath) -and (Get-Item $wbPath).Length -gt 0) {
    Write-Host " OK" -ForegroundColor Green
    $pulled++
} else {
    Write-Host " SKIP" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Pulled $pulled files to: $LOCAL_DIR" -ForegroundColor Green

# Print summary
$summaryPath = Join-Path $LOCAL_DIR "bt_summary_${RunId}.json"
if (Test-Path $summaryPath) {
    $j = Get-Content $summaryPath -Raw | ConvertFrom-Json
    Write-Host ""
    Write-Host "=== RESULTS ===" -ForegroundColor Cyan
    Write-Host "  Trades:      $($j.trades_closed)"
    Write-Host "  Win Rate:    $($j.win_rate_pct)%"
    Write-Host "  PnL:         `$$($j.pnl_usd)"
    Write-Host "  PF:          $($j.profit_factor)"
    Write-Host "  Expectancy:  `$$($j.expectancy_usd)/trade"
    Write-Host "  Max DD:      $($j.max_drawdown_pct)% (`$$($j.max_drawdown_usd))"
    Write-Host "  Entries:     $($j.entry_filled) filled / $($j.entry_attempts) attempts"
}

Write-Host "----------------------------" -ForegroundColor Cyan