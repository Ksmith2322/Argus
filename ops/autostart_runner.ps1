# ops/autostart_runner.ps1
# Launches runner_live.py if not already running.
# Intended for Task Scheduler "At startup" trigger.
# Waits 30s after boot for network/services to stabilize.

$ErrorActionPreference = "Continue"
$logFile = "C:\Argus\repo\ops\logs\autostart_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

Log "=== Runner auto-start check ==="

# Wait for system to stabilize after boot
Start-Sleep -Seconds 30

# Check if runner is already running
$existing = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_live"
    } catch { $false }
}

if ($existing) {
    Log "Runner already running (PID=$($existing.Id)). Skipping."
    exit 0
}

Log "No runner found. Starting runner_live.py..."

# Start in a new window so it persists independently
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" `
    -ArgumentList ".\runner_live.py" `
    -WorkingDirectory "C:\Argus\repo" `
    -WindowStyle Normal

Start-Sleep -Seconds 5

# Verify
$newRunner = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_live"
    } catch { $false }
}

if ($newRunner) {
    Log "Runner started: PID=$($newRunner.Id)"
} else {
    Log "WARNING: Runner may not have started. Check manually."
}

Log "=== Done ==="