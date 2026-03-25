# ops/autostart_runner.ps1
# Launches runner_unified.py (IBKR FX) if not already running.
# Intended for Task Scheduler "At startup" trigger or Startup folder.
# Waits 30s after boot for network/TWS to stabilize.

$ErrorActionPreference = "Continue"
$logFile = "C:\Argus\repo\argus_flow\logs\autostart_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

Log "=== Runner auto-start check ==="

# Wait for system + TWS to stabilize after boot
Start-Sleep -Seconds 30

# Check if unified runner is already running
$existing = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}

if ($existing) {
    Log "Unified runner already running (PID=$($existing.Id)). Skipping."
    exit 0
}

Log "No unified runner found. Starting runner_unified.py with Class A cohort..."

# Active cohort configs (Class A — frozen per COHORT_SPEC.md)
$configs = @(
    "argus_flow/configs/gbpusd_range_paper_v1.json",
    "argus_flow/configs/eurusd_t4_paper_v1.json",
    "argus_flow/configs/eurjpy_t4_paper_v1.json"
)
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "argus_flow.runner_unified", "--configs", $configs[0], $configs[1], $configs[2] `
    -WorkingDirectory "C:\Argus\repo" `
    -WindowStyle Hidden

Start-Sleep -Seconds 10

# Verify
$newRunner = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}

if ($newRunner) {
    Log "Unified runner started: PID=$($newRunner.Id)"
} else {
    Log "WARNING: Runner may not have started. Check manually."
}

Log "=== Done ==="