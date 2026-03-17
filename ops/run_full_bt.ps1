[Environment]::SetEnvironmentVariable("BACKTEST_LIMIT","","Process"); Remove-Item Env:BACKTEST_LIMIT -ErrorAction SilentlyContinue; Set-Location C:/Argus/repo; ./ops/run_backtest.ps1
