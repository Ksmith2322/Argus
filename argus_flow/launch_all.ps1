# Launch all three IBKR paper trading runners in parallel
# Each runs in its own process with a unique IBKR clientId
#
# Prerequisites:
#   - IBKR TWS Gateway running on port 4002 (paper trading)
#   - Read-Only API unchecked in gateway settings
#
# Usage:
#   .\argus_flow\launch_all.ps1
#
# To stop: Ctrl+C in each terminal, or close the terminals

$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$repoRoot = "C:\Argus\repo"

Set-Location $repoRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  IBKR Paper Trading Fleet Launcher" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Launching 3 runners:" -ForegroundColor Yellow
Write-Host "  1. EUR/USD  (clientId=10) - T4 full stack" -ForegroundColor Green
Write-Host "  2. MNQ      (clientId=11) - Vol burst" -ForegroundColor Green
Write-Host "  3. GBP/USD  (clientId=12) - Range + accel" -ForegroundColor Green
Write-Host ""

# Launch each in a new terminal window
Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; Write-Host 'EUR/USD Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_eurusd" -WindowStyle Normal
Write-Host "  [OK] EUR/USD started" -ForegroundColor Green

Start-Sleep -Seconds 3

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; Write-Host 'MNQ Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_mnq" -WindowStyle Normal
Write-Host "  [OK] MNQ started" -ForegroundColor Green

Start-Sleep -Seconds 3

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; Write-Host 'GBP/USD Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_gbpusd" -WindowStyle Normal
Write-Host "  [OK] GBP/USD started" -ForegroundColor Green

Write-Host ""
Write-Host "All runners launched. Check individual terminal windows." -ForegroundColor Cyan
Write-Host ""
Write-Host "Logs:" -ForegroundColor Yellow
Write-Host "  EUR/USD: argus_flow\logs\eurusd\" -ForegroundColor Gray
Write-Host "  MNQ:     argus_flow\logs\mnq\" -ForegroundColor Gray
Write-Host "  GBP/USD: argus_flow\logs\gbpusd\" -ForegroundColor Gray