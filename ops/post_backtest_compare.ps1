# ops/post_backtest_compare.ps1 — Run after a backtest completes to auto-compare against baseline
# Usage: .\ops\post_backtest_compare.ps1 [-Baseline <run_id>]
#
# If no -Baseline given, compares the two most recent runs.
param(
    [string]$Baseline = ""
)

$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$repoRoot = "C:\Argus\repo"

Write-Host "=== POST-BACKTEST ANALYSIS ===" -ForegroundColor Cyan
Write-Host ""

if ($Baseline) {
    # Find the latest run that isn't the baseline
    $summaries = Get-ChildItem "$repoRoot\ops\logs" -Filter "bt_summary_bt_*.json" |
        Where-Object { $_.Name -notmatch "latest" } |
        Sort-Object LastWriteTime -Descending
    $latest = ($summaries | Select-Object -First 1)
    $latestId = $latest.BaseName -replace "bt_summary_", ""

    Write-Host "--- COMPARISON ---" -ForegroundColor Yellow
    & $pyExe "$repoRoot\ops\compare_runs.py" $Baseline $latestId
    Write-Host ""
    Write-Host "--- SCORE ANALYSIS (new run) ---" -ForegroundColor Yellow
    & $pyExe "$repoRoot\ops\analyze_scores.py" $latestId
} else {
    Write-Host "--- COMPARISON (latest 2 runs) ---" -ForegroundColor Yellow
    & $pyExe "$repoRoot\ops\compare_runs.py" --latest 2
    Write-Host ""
    Write-Host "--- SCORE ANALYSIS (latest run) ---" -ForegroundColor Yellow
    & $pyExe "$repoRoot\ops\analyze_scores.py" --latest
}

Write-Host ""
Write-Host "=== DONE ===" -ForegroundColor Cyan