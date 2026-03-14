# ops/status_all.ps1 -- Unified status check: PC1 + PC2, live runner, backtests
# Usage: .\ops\status_all.ps1
param()
$ErrorActionPreference = "Stop"
$PC2 = "ksmith2322@yahoo.com@desktop-17cjmup"
$repoRoot = "C:\Argus\repo"
$logsDir = "$repoRoot\ops\logs"

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  ARGUS STATUS REPORT  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "============================================" -ForegroundColor Cyan

# --- 1. Live Runner ---
Write-Host ""
Write-Host "--- LIVE RUNNER (PC1) ---" -ForegroundColor Yellow
$liveState = "$repoRoot\state\runtime_state_ETH_USD.json"
if (Test-Path $liveState) {
    $stateAge = ((Get-Date) - (Get-Item $liveState).LastWriteTime).TotalMinutes
    $state = Get-Content $liveState -Raw | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($state) {
        $pos = if ($state.bot_state) { $state.bot_state } else { "?" }
        $cash = if ($state.cash) { $state.cash } else { "?" }
        $pnl = if ($state.realized_pnl) { $state.realized_pnl } else { "0" }
        Write-Host "  State: $pos  |  Cash: `$$cash  |  PnL: `$$pnl  |  Updated: $([math]::Round($stateAge,1))m ago"
        if ($stateAge -gt 5) {
            Write-Host "  WARNING: state file >5min old -- runner may be down" -ForegroundColor Red
        } else {
            Write-Host "  STATUS: HEALTHY" -ForegroundColor Green
        }
    }
} else {
    Write-Host "  No state file found -- runner not active" -ForegroundColor Red
}

# --- 2. PC1 Backtests ---
Write-Host ""
Write-Host "--- BACKTESTS (PC1) ---" -ForegroundColor Yellow
$btProcs = Get-Process python* -ErrorAction SilentlyContinue
$runHeaders = Get-ChildItem $logsDir -Filter "run_header_bt_*.json" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 5

foreach ($rh in $runHeaders) {
    $hdr = Get-Content $rh.FullName -Raw | ConvertFrom-Json
    $rid = $hdr.run_id
    $sumPath = Join-Path $logsDir "bt_summary_$rid.json"
    $eqPath = Join-Path $logsDir "equity_$rid.csv"

    if (Test-Path $sumPath) {
        $sum = Get-Content $sumPath -Raw | ConvertFrom-Json
        Write-Host "  DONE  $rid  |  trades=$($sum.trades_closed)  WR=$($sum.win_rate_pct)%  PnL=`$$($sum.pnl_usd)  PF=$($sum.profit_factor)"
    } elseif (Test-Path $eqPath) {
        $lines = (Get-Content $eqPath | Measure-Object -Line).Lines - 1
        # Get total from candle file
        $candleCsv = $hdr.candles_csv
        $total = 0
        if ($candleCsv -and (Test-Path $candleCsv)) {
            $total = (Get-Content $candleCsv | Measure-Object -Line).Lines - 1
        }
        $pctStr = if ($total -gt 0) { "$([math]::Round($lines / $total * 100))% ($lines/$total)" } else { "$lines bars" }
        Write-Host "  RUN   $rid  |  progress=$pctStr" -ForegroundColor Yellow
    } else {
        Write-Host "  ???   $rid  |  no equity or summary found" -ForegroundColor Red
    }
}

# --- 3. Queue Status ---
Write-Host ""
Write-Host "--- JOB QUEUE ---" -ForegroundColor Yellow
$queueFile = "$repoRoot\ops\backtest_queue.jsonl"
if (Test-Path $queueFile) {
    $queueLines = @(Get-Content $queueFile | Where-Object { $_.Trim() -ne "" })
    Write-Host "  Pending jobs: $($queueLines.Count)"
    foreach ($ql in $queueLines) {
        try {
            $j = $ql | ConvertFrom-Json
            Write-Host "    - $($j.label)" -ForegroundColor Gray
        } catch {}
    }
} else {
    Write-Host "  No queue file"
}

# --- 4. PC2 Status ---
Write-Host ""
Write-Host "--- PC2 STATUS ---" -ForegroundColor Yellow
try {
    $pc2Procs = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "(Get-Process python* -ErrorAction SilentlyContinue | Measure-Object).Count"' 2>$null
    if ([int]$pc2Procs -gt 0) {
        Write-Host "  STATUS: RUNNING ($pc2Procs Python process(es))" -ForegroundColor Yellow

        # Get latest equity file progress
        $pc2Info = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "
            $eq = Get-ChildItem C:/Argus/repo/ops/logs -Filter \"equity_bt_*.csv\" -ErrorAction SilentlyContinue | Sort LastWriteTime -Desc | Select -First 1
            if ($eq) { Write-Host \"$($eq.Name)|$($eq.Length)\" }
        "' 2>$null
        if ($pc2Info) {
            $parts = $pc2Info.Split("|")
            Write-Host "  Latest: $($parts[0])  size=$($parts[1]) bytes"
        }
    } else {
        Write-Host "  STATUS: IDLE" -ForegroundColor Green

        # Show latest completed run
        $pc2Summary = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "
            $s = Get-ChildItem C:/Argus/repo/ops/logs -Filter \"bt_summary_bt_*.json\" -ErrorAction SilentlyContinue | Sort LastWriteTime -Desc | Select -First 1
            if ($s) { Get-Content $s.FullName -Raw }
        "' 2>$null
        if ($pc2Summary) {
            try {
                $sj = $pc2Summary | ConvertFrom-Json
                Write-Host "  Latest: $($sj.run_id)  trades=$($sj.trades_closed)  WR=$($sj.win_rate_pct)%  PnL=`$$($sj.pnl_usd)"
            } catch {}
        }
    }
} catch {
    Write-Host "  UNREACHABLE (SSH failed)" -ForegroundColor Red
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan