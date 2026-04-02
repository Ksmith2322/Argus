# Register:
# schtasks /Create /TN "ArgusWeeklyPairOnboarding" /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\run_weekly_pair_onboarding.ps1" /SC WEEKLY /D FRI /ST 18:00 /F

Set-Location "C:\Argus\repo"
$logFile = "C:\Argus\repo\argus_flow\logs\weekly_pair_onboarding.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

try {
    $output = & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.weekly_pair_onboarding 2>&1
    Add-Content -Path $logFile -Value "[$timestamp] $output"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] ERROR: $_"
}
