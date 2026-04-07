# Launch queue runner on PC2 via scheduled task (works over SSH)
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\_run_queue_no_refresh.ps1" -WorkingDirectory "C:\Argus\repo"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 24)

# Remove old task if exists
Unregister-ScheduledTask -TaskName "ArgusMegaQueue" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName "ArgusMegaQueue" -Action $action -Trigger $trigger -Settings $settings -Description "Mega backtest queue runner" -RunLevel Highest

Write-Host "Scheduled task ArgusMegaQueue created - will start in 5 seconds" -ForegroundColor Green