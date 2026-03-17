# Quick check: is queue running? How many jobs left?
$qf = "C:\Argus\repo\ops\backtest_queue.jsonl"
$lines = @(Get-Content $qf -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" })
Write-Host "Queue jobs remaining: $($lines.Count)"

# Check queue_results.log for latest entry
$log = "C:\Argus\repo\ops\logs\queue_results.log"
if (Test-Path $log) {
    $last5 = Get-Content $log -Tail 5
    Write-Host "`nLast 5 log entries:"
    $last5 | ForEach-Object { Write-Host "  $_" }
}

# Check if any python backtest is running right now
$btProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*backtest*' }
if ($btProcs) {
    Write-Host "`nBacktest processes running:"
    $btProcs | ForEach-Object { Write-Host "  PID=$($_.ProcessId) CMD=$($_.CommandLine.Substring(0, [Math]::Min(100, $_.CommandLine.Length)))" }
} else {
    Write-Host "`nNo backtest processes detected"
}