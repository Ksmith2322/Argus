# Relaunch BTC runner with updated config
$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"

$innerCmd = @"
`$Host.UI.RawUI.WindowTitle = 'Argus-BTC'
Set-Location '$repoRoot'
`$env:PRODUCT_ID = 'BTC-USD'
`$env:ARGUS_LOG_DIR = '$repoRoot\ops\logs\btc'
`$env:ARGUS_COIN_ENV = '$repoRoot\.env.btc'
Write-Host 'Per-coin overlay: .env.btc (BE trigger 0.008)' -ForegroundColor Yellow
Write-Host '=== Argus Runner: BTC-USD ===' -ForegroundColor Cyan
& '$pyExe' .\runner_live.py
Write-Host 'Runner exited. Press any key...' -ForegroundColor Red
`$null = `$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')
"@

Start-Process powershell.exe -ArgumentList @("-NoExit", "-NoProfile", "-Command", $innerCmd)
Write-Host "BTC runner relaunched with breakeven trigger" -ForegroundColor Green