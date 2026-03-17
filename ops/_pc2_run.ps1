Set-Location C:/Argus/repo
Write-Host env-set
$env:CONFLUENCE_MIN_SCORE='80'
$env:REGIME_ENTRY_BLOCK_LIST='TREND_DOWN,RANGE'
./ops/run_backtest.ps1 -SingleRun
