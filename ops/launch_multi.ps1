# ops/launch_multi.ps1 -- Launch parallel Argus runners for multiple coins
#
# Usage:
#   .\ops\launch_multi.ps1              # launch ETH, BTC, SOL in separate windows
#   .\ops\launch_multi.ps1 -Coins ETH   # launch just ETH
#   .\ops\launch_multi.ps1 -DryRun      # show what would run
#
# Each coin gets:
#   - Its own PowerShell window with title "Argus-<COIN>"
#   - Isolated log directory: ops/logs/<coin>/
#   - Isolated state file: state/runtime_state_<COIN>_USD.json (automatic)
#   - Per-coin config overlay: .env.btc / .env.sol (only overridden keys)
#
# The base .env PRODUCT_ID is overridden per-coin via environment variable.
# Per-coin .env overlay (if exists) layers coin-specific params on top.

param(
    [string[]]$Coins = @("ETH", "BTC", "SOL"),
    [switch]$DryRun
)

$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"

Write-Host "=== Argus Multi-Coin Launcher ===" -ForegroundColor Cyan
Write-Host "Coins: $($Coins -join ', ')" -ForegroundColor Yellow
Write-Host ""

foreach ($coin in $Coins) {
    $productId = "$coin-USD"
    $logDir = Join-Path $repoRoot "ops\logs\$($coin.ToLower())"

    # Ensure per-coin log directory exists
    if (!(Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        Write-Host "Created log dir: $logDir" -ForegroundColor Gray
    }

    $windowTitle = "Argus-$coin"

    # Build the command that runs inside the new window
    # Sets per-coin env vars, then launches runner_live.py
    $innerCmd = @"
`$Host.UI.RawUI.WindowTitle = '$windowTitle'
Set-Location '$repoRoot'
`$env:PRODUCT_ID = '$productId'
`$env:ARGUS_LOG_DIR = '$logDir'
`$coinEnvFile = Join-Path '$repoRoot' ".env.$($coin.ToLower())"
if (Test-Path `$coinEnvFile) {
    `$env:ARGUS_COIN_ENV = `$coinEnvFile
    Write-Host "Per-coin overlay: `$coinEnvFile" -ForegroundColor Yellow
} else {
    `$env:ARGUS_COIN_ENV = ''
}
Write-Host '=== Argus Runner: $productId ===' -ForegroundColor Cyan
Write-Host "Log dir: $logDir" -ForegroundColor Gray
Write-Host ''
& '$pyExe' .\runner_live.py
Write-Host ''
Write-Host 'Runner exited. Press any key to close...' -ForegroundColor Red
`$null = `$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
"@

    if ($DryRun) {
        Write-Host "[DRY RUN] Would launch $productId in window '$windowTitle'" -ForegroundColor Yellow
        Write-Host "  Log dir: $logDir"
        Write-Host "  PRODUCT_ID=$productId"
        Write-Host ""
    } else {
        Write-Host "Launching $productId ..." -ForegroundColor Green

        # Start a new PowerShell window for this coin
        Start-Process powershell.exe -ArgumentList @(
            "-NoExit",
            "-NoProfile",
            "-Command",
            $innerCmd
        )

        Write-Host "  Window '$windowTitle' started." -ForegroundColor Gray
        Start-Sleep -Milliseconds 500
    }
}

Write-Host ""
Write-Host "=== All runners launched ===" -ForegroundColor Green
Write-Host "Each runner has its own window. Close a window to stop that runner."
Write-Host "Watchdog will auto-restart if Task Scheduler is configured."