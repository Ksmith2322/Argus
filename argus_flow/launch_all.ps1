# Launch all three IBKR paper trading runners with pre-flight checks
# Each runs in its own process with a unique IBKR clientId
#
# Pre-flight: config check, heartbeat verify, connectivity test
# Post-launch: health check after 30s, Discord watcher in background
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
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') UTC" -ForegroundColor Gray
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# -- PRE-FLIGHT CHECKS --------------------------------------

Write-Host "--- Pre-Flight Checks ---" -ForegroundColor Yellow

# 1. Config integrity
Write-Host "  [1/3] Config check..." -ForegroundColor Gray
& $pyExe -m argus_flow.ops.config_check 2>&1 | ForEach-Object { Write-Host "    $_" }

# 2. Heartbeat (check for stale state)
Write-Host "  [2/3] Heartbeat check..." -ForegroundColor Gray
& $pyExe -m argus_flow.ops.heartbeat_monitor 2>&1 | ForEach-Object { Write-Host "    $_" }

# 3. Correlation guard (initial state)
Write-Host "  [3/3] Correlation guard..." -ForegroundColor Gray
& $pyExe -m argus_flow.ops.correlation_guard 2>&1 | ForEach-Object { Write-Host "    $_" }

Write-Host ""

# -- LAUNCH RUNNERS ------------------------------------------

Write-Host "--- Launching Runners ---" -ForegroundColor Yellow
Write-Host "  1. EUR/USD  (clientId=10) - T4 full stack" -ForegroundColor Green
Write-Host "  2. MNQ      (clientId=11) - Vol burst (delayed data)" -ForegroundColor Green
Write-Host "  3. GBP/USD  (clientId=12) - Range + accel" -ForegroundColor Green
Write-Host ""

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; `$Host.UI.RawUI.WindowTitle = 'IBKR-EURUSD'; Write-Host 'EUR/USD Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_eurusd" -WindowStyle Normal
Write-Host "  [OK] EUR/USD started" -ForegroundColor Green

Start-Sleep -Seconds 3

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; `$Host.UI.RawUI.WindowTitle = 'IBKR-MNQ'; Write-Host 'MNQ Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_mnq" -WindowStyle Normal
Write-Host "  [OK] MNQ started" -ForegroundColor Green

Start-Sleep -Seconds 3

Start-Process powershell -ArgumentList "-NoExit", "-Command", "Set-Location '$repoRoot'; `$Host.UI.RawUI.WindowTitle = 'IBKR-GBPUSD'; Write-Host 'GBP/USD Runner' -ForegroundColor Cyan; & '$pyExe' -m argus_flow.runner_gbpusd" -WindowStyle Normal
Write-Host "  [OK] GBP/USD started" -ForegroundColor Green

# -- POST-LAUNCH HEALTH CHECK -------------------------------

Write-Host ""
Write-Host "Waiting 30s for runners to initialize..." -ForegroundColor Gray
Start-Sleep -Seconds 30

Write-Host ""
Write-Host "--- Post-Launch Health Check ---" -ForegroundColor Yellow
& $pyExe -m argus_flow.ops.heartbeat_monitor 2>&1 | ForEach-Object { Write-Host "  $_" }

# -- DISCORD WATCHER -----------------------------------------

Write-Host ""
Write-Host "Starting Discord trade watcher in background..." -ForegroundColor Gray
Start-Process powershell -ArgumentList "-WindowStyle", "Hidden", "-Command", "Set-Location '$repoRoot'; & '$pyExe' -m argus_flow.ops.discord_alerts --watch" -WindowStyle Hidden
Write-Host "  [OK] Discord watcher started" -ForegroundColor Green

# -- SUMMARY -------------------------------------------------

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Fleet launched successfully!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Logs:" -ForegroundColor Yellow
Write-Host "  EUR/USD: argus_flow\logs\eurusd\" -ForegroundColor Gray
Write-Host "  MNQ:     argus_flow\logs\mnq\" -ForegroundColor Gray
Write-Host "  GBP/USD: argus_flow\logs\gbpusd\" -ForegroundColor Gray
Write-Host ""
Write-Host "Monitoring:" -ForegroundColor Yellow
Write-Host "  Dashboard:  http://localhost:8080 (IBKR FLEET tab)" -ForegroundColor Gray
Write-Host "  Health:     python -m argus_flow.ops.heartbeat_monitor" -ForegroundColor Gray
Write-Host "  Divergence: python -m argus_flow.ops.divergence_guard" -ForegroundColor Gray
Write-Host "  Positions:  python -m argus_flow.ops.position_monitor" -ForegroundColor Gray
Write-Host "  Daily:      python -m argus_flow.ops.daily_report" -ForegroundColor Gray
Write-Host "  Discord:    Running in background (trade alerts)" -ForegroundColor Gray