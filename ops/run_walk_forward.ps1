# ops/run_walk_forward.ps1 -- Walk-forward validation with stability gate
#
# Runs Phase 12 walk-forward on candle data, then evaluates stability gate.
# Gate criteria (for production readiness): >= 6/8 windows profitable (PnL>0)
#
# Usage:
#   .\ops\run_walk_forward.ps1                         # ETH, 8 rolling windows
#   .\ops\run_walk_forward.ps1 -Coin BTC               # BTC
#   .\ops\run_walk_forward.ps1 -Windows 4 -Coin ETH    # 4 windows
#   .\ops\run_walk_forward.ps1 -Coin ETH -AllCoins      # run all screened coins
#
# Stability gate (PF>=1.20 mean, >=6/8 windows with pnl>0):

param(
    [string]$Coin = "ETH",
    [int]$Windows = 8,
    [string]$Mode = "rolling",
    [int]$MinWindowsPositive = 6,
    [switch]$AllCoins
)

$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logDir = Join-Path $repoRoot "ops\logs"

Set-Location $repoRoot

function Run-WalkForward {
    param([string]$CoinName)

    $csvFile = Join-Path $repoRoot "data\$($CoinName.ToLower())_usd_1m_90d.csv"
    if (-not (Test-Path $csvFile)) {
        $csvFile = Join-Path $repoRoot "data\$($CoinName.ToLower())_usd_1m.csv"
    }
    if (-not (Test-Path $csvFile)) {
        Write-Host "  [SKIP] No candle file for $CoinName" -ForegroundColor Yellow
        return $null
    }

    Write-Host ""
    Write-Host "=== Walk-Forward: $CoinName ($Windows $Mode windows) ===" -ForegroundColor Cyan
    Write-Host "  Candle file: $csvFile" -ForegroundColor Gray

    $runId = "wf_$(Get-Date -Format 'yyyyMMddTHHmmss')_$($CoinName.ToLower())"

    # Set product ID for the backtest
    $env:PRODUCT_ID = "$CoinName-USD"

    $output = & $pyExe -m backtest.walk_forward `
        --candles-csv $csvFile `
        --windows $Windows `
        --mode $Mode `
        --out-dir $logDir `
        --run-id $runId 2>&1

    $output | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }

    # Read the JSON summary
    $summaryFile = Join-Path $logDir "wf_summary_$runId.json"
    if (-not (Test-Path $summaryFile)) {
        Write-Host "  [ERROR] Summary file not found: $summaryFile" -ForegroundColor Red
        return $null
    }

    $summary = Get-Content $summaryFile -Raw | ConvertFrom-Json
    return $summary
}

function Print-StabilityGate {
    param($Summary, [string]$CoinName, [int]$MinPositive)

    if (-not $Summary) {
        Write-Host "  [NO DATA]" -ForegroundColor Red
        return $false
    }

    $agg = $Summary.aggregate
    $windows = $Summary.windows
    $nOk = $Summary.windows_ok
    $nTotal = $Summary.n_windows

    $pnlAgg = $agg.pnl
    $pnlPositive = if ($pnlAgg) { $pnlAgg.windows_positive } else { 0 }
    $pnlMean = if ($pnlAgg) { [math]::Round($pnlAgg.mean, 2) } else { 0 }
    $pnlMin = if ($pnlAgg) { [math]::Round($pnlAgg.min, 2) } else { 0 }

    $pfVals = @($windows | Where-Object { $_.status -eq "ok" -and $_.PSObject.Properties.Name -contains "profit_factor" } | ForEach-Object { [double]$_.profit_factor })
    $pfMean = if ($pfVals.Count -gt 0) { [math]::Round(($pfVals | Measure-Object -Average).Average, 3) } else { 0 }

    $wrAgg = $agg.win_rate
    $wrMean = if ($wrAgg) { [math]::Round($wrAgg.mean * 100, 1) } else { 0 }

    $passed = ($pnlPositive -ge $MinPositive) -and ($pfMean -ge 1.0)

    $gateColor = if ($passed) { "Green" } else { "Red" }
    $gateLabel = if ($passed) { "PASS" } else { "FAIL" }

    Write-Host ""
    Write-Host "  --- Stability Gate: $CoinName ---" -ForegroundColor Cyan
    Write-Host "  Windows ok:     $nOk/$nTotal"
    Write-Host "  PnL mean:       `$$pnlMean  (min: `$$pnlMin)"
    Write-Host "  PnL positive:   $pnlPositive/$nOk  (need >= $MinPositive)"
    Write-Host "  PF mean:        $pfMean  (need >= 1.0)"
    Write-Host "  Win rate mean:  $wrMean%"
    Write-Host ""
    Write-Host "  GATE: $gateLabel" -ForegroundColor $gateColor
    Write-Host ""

    # Per-window breakdown
    Write-Host "  Per-window breakdown:"
    $wi = 1
    foreach ($w in $windows) {
        if ($w.status -ne "ok") {
            Write-Host "  [$wi] ERROR: $($w.error)" -ForegroundColor Red
        } else {
            $wPnl = [math]::Round([double](if ($w.pnl_usd -ne $null) { $w.pnl_usd } else { 0 }), 2)
            $wTrades = if ($w.trades_closed) { $w.trades_closed } else { 0 }
            $wWr = if ($w.PSObject.Properties.Name -contains "win_rate") { [math]::Round([double]$w.win_rate * 100, 1) } else { "?" }
            $wPf = if ($w.PSObject.Properties.Name -contains "profit_factor") { [math]::Round([double]$w.profit_factor, 3) } else { "?" }
            $pnlColor = if ($wPnl -gt 0) { "Green" } elseif ($wPnl -lt 0) { "Red" } else { "Gray" }
            Write-Host "  [$wi] pnl=`$$wPnl  trades=$wTrades  wr=$wWr%  pf=$wPf" -ForegroundColor $pnlColor
        }
        $wi++
    }

    return $passed
}

# ---- Main ----

$coinsToRun = if ($AllCoins) {
    @("ETH", "BTC", "AVAX", "DOGE", "SUI", "LINK", "ADA")
} else {
    @($Coin)
}

$results = @{}
foreach ($c in $coinsToRun) {
    $summary = Run-WalkForward -CoinName $c
    $passed = Print-StabilityGate -Summary $summary -CoinName $c -MinPositive $MinWindowsPositive
    $results[$c] = @{ Summary = $summary; Passed = $passed }
}

# Final summary
Write-Host ""
Write-Host "=== Walk-Forward Results ===" -ForegroundColor Cyan
foreach ($c in $coinsToRun) {
    $r = $results[$c]
    $label = if ($r.Passed) { "PASS" } else { "FAIL" }
    $color = if ($r.Passed) { "Green" } else { "Red" }
    Write-Host "  $c : $label" -ForegroundColor $color
}
Write-Host ""
Write-Host "Gate criteria: PF >= 1.0 mean AND >= $MinWindowsPositive/$Windows windows profitable"
Write-Host "Logs: $logDir\wf_summary_*.json"