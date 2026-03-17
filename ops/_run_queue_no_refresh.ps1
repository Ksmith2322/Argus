# Run the mega queue WITHOUT the candle refresh step
# (Downloads are running separately)
$Host.UI.RawUI.WindowTitle = 'Argus-MegaQueue'
Set-Location 'C:\Argus\repo'

Write-Host "=== MEGA QUEUE: $((@(Get-Content ops\backtest_queue.jsonl | Where-Object { $_.Trim() -ne '' })).Count) jobs ===" -ForegroundColor Cyan
Write-Host "Skipping candle refresh (downloads running separately)" -ForegroundColor Yellow
Write-Host ""

# Temporarily rename refresh script so queue runner skips it
$refreshScript = "C:\Argus\repo\ops\refresh_candles.ps1"
$backupPath = "$refreshScript.bak"
if (Test-Path $refreshScript) {
    Rename-Item $refreshScript $backupPath
}

try {
    .\ops\run_queue.ps1
} finally {
    # Restore refresh script
    if (Test-Path $backupPath) {
        Rename-Item $backupPath $refreshScript
    }
}