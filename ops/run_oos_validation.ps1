# ops/run_oos_validation.ps1 -- Out-of-sample (OOS) validation
#
# Splits the 90-day candle file into:
#   - 60-day TRAIN window (days 0-59)
#   - 30-day TEST  window (days 60-89, the held-out OOS period)
#
# Runs the backtest on the TEST slice using the current .env config.
# Purpose: prove the strategy generalizes beyond the data it was tuned on.
#
# Usage:
#   .\ops\run_oos_validation.ps1                  # ETH (default)
#   .\ops\run_oos_validation.ps1 -Coin BTC
#   .\ops\run_oos_validation.ps1 -Coin ETH -BTC   # run both
#   .\ops\run_oos_validation.ps1 -AllCoins         # all screened coins with 90d data
#
# Output:
#   ops/logs/oos_<coin>_train.csv  (60d, for reference / re-training)
#   ops/logs/oos_<coin>_test.csv   (30d, OOS held-out slice)
#   ops/logs/oos_summary_<run_id>.json
#
# Pass/Fail gate: PF >= 1.0 AND win_rate >= 30% on the OOS test window
#
param(
    [string]$Coin = "ETH",
    [switch]$BTC,
    [switch]$AllCoins,
    [int]$TrainDays = 60,
    [int]$TestDays  = 30,
    [switch]$KeepSlices    # keep the split CSV files after run
)

$repoRoot = "C:\Argus\repo"
$pyExe    = "C:\Argus\.venv\Scripts\python.exe"
$logDir   = Join-Path $repoRoot "ops\logs"

Set-Location $repoRoot

# Gate thresholds
$OOS_MIN_PF = 1.0
$OOS_MIN_WR = 0.30

function Write-OosLog($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Write-Host "$ts  $msg"
}

function Split-CandleFile {
    param([string]$SourceCsv, [int]$TrainDays, [int]$TestDays, [string]$Coin)

    if (-not (Test-Path $SourceCsv)) {
        Write-OosLog "  [SKIP] No candle file: $SourceCsv"
        return $null
    }

    $trainOut = Join-Path $logDir "oos_${Coin}_train.csv"
    $testOut  = Join-Path $logDir "oos_${Coin}_test.csv"

    $splitScript = @"
import csv, sys
from pathlib import Path

src = r'$SourceCsv'
train_out = r'$trainOut'
test_out  = r'$testOut'
train_days = $TrainDays
test_days  = $TestDays

with open(src, 'r', newline='', encoding='utf-8') as f:
    rows = list(csv.DictReader(f))

if not rows:
    print('ERROR: no rows in', src)
    sys.exit(1)

# Detect timestamp field
ts_field = None
for candidate in ('timestamp', 'time', 'open_time', 'ts'):
    if candidate in rows[0]:
        ts_field = candidate
        break

if not ts_field:
    print('ERROR: cannot detect timestamp column. Fields:', list(rows[0].keys()))
    sys.exit(1)

# Sort by timestamp (they should already be sorted)
rows.sort(key=lambda r: float(r[ts_field]))

first_ts = float(rows[0][ts_field])
train_end_ts = first_ts + train_days * 86400
test_end_ts  = train_end_ts + test_days * 86400

train_rows = [r for r in rows if float(r[ts_field]) <  train_end_ts]
test_rows  = [r for r in rows if train_end_ts <= float(r[ts_field]) < test_end_ts]

fieldnames = list(rows[0].keys())

def write_csv(path, data):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(data)

write_csv(train_out, train_rows)
write_csv(test_out,  test_rows)

print(f'Split OK: total={len(rows)}  train={len(train_rows)} ({train_days}d)  test={len(test_rows)} ({test_days}d)')
print(f'Train: {train_out}')
print(f'Test:  {test_out}')
"@

    $result = & $pyExe -c $splitScript 2>&1
    $result | ForEach-Object { Write-OosLog "  $_" }

    if ($LASTEXITCODE -ne 0) {
        Write-OosLog "  [ERROR] Split failed for $Coin"
        return $null
    }

    return @{ TrainCsv = $trainOut; TestCsv = $testOut }
}

function Run-OosBacktest {
    param([string]$CoinName, [string]$TestCsv, [string]$RunId)

    Write-OosLog ""
    Write-OosLog "=== OOS Backtest: $CoinName ($TestDays-day test window) ==="
    Write-OosLog "  Test CSV: $TestCsv"

    # Runner is env-var driven
    $env:PRODUCT_ID           = "$CoinName-USD"
    $env:BACKTEST_CSV         = $TestCsv
    $env:BACKTEST_SYMBOL      = "$CoinName-USD"
    $env:ARGUS_RUN_ID         = $RunId
    $env:ARGUS_BT_ARTIFACT_DIR = $logDir
    $env:BT_LITE_MODE         = "true"     # fast; summary JSON is still written

    $output = & $pyExe -m backtest.runner 2>&1
    $output | ForEach-Object { Write-OosLog "  $_" }

    # Clean env
    Remove-Item Env:BACKTEST_CSV       -ErrorAction SilentlyContinue
    Remove-Item Env:BACKTEST_SYMBOL    -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_RUN_ID       -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_BT_ARTIFACT_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:BT_LITE_MODE       -ErrorAction SilentlyContinue

    $summaryFile = Join-Path $logDir "bt_summary_${RunId}.json"
    if (-not (Test-Path $summaryFile)) {
        Write-OosLog "  [ERROR] No summary file found: $summaryFile"
        return $null
    }

    return Get-Content $summaryFile -Raw | ConvertFrom-Json
}

function Print-OosResult {
    param($Summary, [string]$CoinName)

    if (-not $Summary) {
        Write-OosLog "  [NO DATA] Cannot evaluate OOS gate for $CoinName"
        return $false
    }

    $pnl    = [math]::Round([double]($Summary.pnl_usd ?? $Summary.total_pnl ?? 0), 4)
    $pf     = [math]::Round([double]($Summary.profit_factor ?? 0), 3)
    $wr     = [math]::Round([double]($Summary.win_rate_pct ?? ($Summary.win_rate * 100) ?? 0), 1)
    $trades = [int]($Summary.trades_closed ?? $Summary.total_trades ?? 0)
    $dd     = [math]::Round([double]($Summary.max_drawdown_pct ?? 0), 2)

    $passed = ($pf -ge $OOS_MIN_PF) -and ($wr / 100 -ge $OOS_MIN_WR)
    $gateColor = if ($passed) { "Green" } else { "Red" }
    $gateLabel = if ($passed) { "PASS" } else { "FAIL" }

    Write-OosLog ""
    Write-OosLog "  --- OOS Gate: $CoinName (${TestDays}d held-out test) ---"
    Write-OosLog "  PnL:       `$$pnl"
    Write-OosLog "  Trades:    $trades"
    Write-OosLog "  Win Rate:  $wr%  (need >= $([math]::Round($OOS_MIN_WR * 100))%)"
    Write-OosLog "  Prof Factor: $pf  (need >= $OOS_MIN_PF)"
    Write-OosLog "  Max DD:    $dd%"
    Write-OosLog ""
    Write-Host "  OOS GATE: $gateLabel" -ForegroundColor $gateColor

    return $passed
}

# ---- Main ----

$coinsToRun = if ($AllCoins) {
    @("ETH", "BTC", "AVAX", "DOGE", "SUI", "LINK", "ADA")
} elseif ($BTC) {
    @("ETH", "BTC")
} else {
    @($Coin)
}

Write-OosLog ""
Write-OosLog "=== Argus OOS Validation: ${TrainDays}d train / ${TestDays}d test ==="
Write-OosLog "Coins: $($coinsToRun -join ', ')"

$allResults = @{}
foreach ($c in $coinsToRun) {
    $src90d = Join-Path $repoRoot "data\$($c.ToLower())_usd_1m_90d.csv"
    $src30d = Join-Path $repoRoot "data\$($c.ToLower())_usd_1m.csv"

    # Prefer 90d file; fall back to 30d if 90d not available
    $srcFile = if (Test-Path $src90d) { $src90d } elseif (Test-Path $src30d) { $src30d } else { $null }

    if (-not $srcFile) {
        Write-OosLog "  [SKIP] No candle data for $c"
        $allResults[$c] = @{ Passed = $false; Summary = $null; Error = "no_data" }
        continue
    }

    $runId  = "oos_$(Get-Date -Format 'yyyyMMddTHHmmss')_$($c.ToLower())"
    $slices = Split-CandleFile -SourceCsv $srcFile -TrainDays $TrainDays -TestDays $TestDays -Coin $c

    if (-not $slices) {
        $allResults[$c] = @{ Passed = $false; Summary = $null; Error = "split_failed" }
        continue
    }

    $summary = Run-OosBacktest -CoinName $c -TestCsv $slices.TestCsv -RunId $runId
    $passed  = Print-OosResult -Summary $summary -CoinName $c
    $allResults[$c] = @{ Passed = $passed; Summary = $summary; RunId = $runId }

    if (-not $KeepSlices) {
        Remove-Item $slices.TrainCsv -ErrorAction SilentlyContinue
        Remove-Item $slices.TestCsv  -ErrorAction SilentlyContinue
    }
}

# ---- Summary ----
Write-OosLog ""
Write-OosLog "=== OOS Validation Results ==="
foreach ($c in $coinsToRun) {
    $r = $allResults[$c]
    if ($r.Error) {
        Write-Host "  $c : SKIP ($($r.Error))" -ForegroundColor Yellow
    } else {
        $label = if ($r.Passed) { "PASS" } else { "FAIL" }
        $color = if ($r.Passed) { "Green" } else { "Red" }
        Write-Host "  $c : $label" -ForegroundColor $color
    }
}
Write-OosLog ""
Write-OosLog "Gate criteria: PF >= $OOS_MIN_PF AND win_rate >= $([math]::Round($OOS_MIN_WR * 100))% on ${TestDays}-day held-out test"
Write-OosLog "Slices saved: $logDir\oos_*.csv (use -KeepSlices to retain)"