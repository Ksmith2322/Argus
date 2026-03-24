# Register as a nightly scheduled task (run from elevated prompt):
#   schtasks /Create /TN "ArgusCohortReport" /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\run_cohort_report.ps1" /SC DAILY /ST 23:00 /F

Set-Location "C:\Argus\repo"

$logFile = "C:\Argus\repo\argus_flow\logs\cohort_report.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

try {
    $output = & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.daily_report 2>&1
    $entry = "[$timestamp] $output"
} catch {
    $entry = "[$timestamp] ERROR: $_"
}

Add-Content -Path $logFile -Value $entry