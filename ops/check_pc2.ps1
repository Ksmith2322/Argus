# ops/check_pc2.ps1 -- Quick PC2 backtest status check
# Usage: .\ops\check_pc2.ps1
param()

$PC2 = "ksmith2322@yahoo.com@192.168.1.98"

Write-Host "--- PC2 BACKTEST STATUS ---" -ForegroundColor Cyan

# Check if Python is running (backtest in progress)
$procs = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "Get-Process python* -ErrorAction SilentlyContinue | Select-Object Id,CPU,StartTime | ConvertTo-Json"' 2>$null
$procsObj = $null
if ($procs) {
    try { $procsObj = $procs | ConvertFrom-Json } catch {}
}

if ($procsObj) {
    Write-Host "STATUS: RUNNING" -ForegroundColor Yellow
    $procsArr = @($procsObj)
    foreach ($p in $procsArr) {
        $start = $p.StartTime
        Write-Host "  PID=$($p.Id)  CPU=$([math]::Round($p.CPU, 1))s  Started=$start"
    }

    # Estimate progress by comparing signals file sizes
    $progress = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "
        $logs = \"C:/Argus/repo/ops/logs\"
        $sigs = Get-ChildItem $logs -Filter \"bt_signals_bt_*.csv\" | Sort-Object LastWriteTime -Descending
        if ($sigs.Count -ge 2) {
            $current = $sigs[0].Length
            $reference = $sigs[1].Length
            if ($reference -gt 0) {
                [math]::Round($current / $reference * 100, 1)
            }
        }
    "' 2>$null

    if ($progress) {
        Write-Host "  Progress: ~${progress}% (vs previous run)" -ForegroundColor Yellow
    }
} else {
    Write-Host "STATUS: IDLE (no Python running)" -ForegroundColor Green

    # Show latest summary
    $summary = ssh $PC2 'powershell -NonInteractive -NoProfile -Command "
        $latest = Get-ChildItem C:/Argus/repo/ops/logs -Filter \"bt_summary_bt_*.json\" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) { Get-Content $latest.FullName -Raw }
    "' 2>$null

    if ($summary) {
        try {
            $j = $summary | ConvertFrom-Json
            Write-Host ""
            Write-Host "LATEST RUN: $($j.run_id)" -ForegroundColor Green
            Write-Host "  Trades:      $($j.trades_closed)"
            Write-Host "  Win Rate:    $($j.win_rate_pct)%"
            Write-Host "  PnL:         `$$($j.pnl_usd)"
            Write-Host "  PF:          $($j.profit_factor)"
            Write-Host "  Expectancy:  `$$($j.expectancy_usd)/trade"
            Write-Host "  Max DD:      $($j.max_drawdown_pct)% (`$$($j.max_drawdown_usd))"
            Write-Host "  Entries:     $($j.entry_filled) filled / $($j.entry_attempts) attempts"
        } catch {
            Write-Host "  (could not parse summary)"
        }
    }
}

Write-Host "----------------------------" -ForegroundColor Cyan