# ops/launch_all_systems.ps1 -- Launch the entire Helio fleet
# Argus + Titan + Ares + Hermes + Apollo all running together
#
# Usage:
#   .\ops\launch_all_systems.ps1            # Paper mode
#   .\ops\launch_all_systems.ps1 -Live       # Live IBKR execution

param(
    [switch]$Live,
    [switch]$SkipDashboard
)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"
$liveFlag = if ($Live) { "--live" } else { "" }

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path "$logDir\fleet_launch_all.log" -Value $line -ErrorAction SilentlyContinue
}

function Start-System([string]$name, [string[]]$args) {
    Log "Starting $name..."
    Start-Process -FilePath $python `
        -ArgumentList $args `
        -WorkingDirectory "C:\Argus\repo" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

Log "=== Helio Fleet Launch ($(if ($Live) {'LIVE'} else {'PAPER'})) ==="

# 1. Argus (FX intraday) — already running, just check
$argusRunning = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}
if (-not $argusRunning) {
    Log "Argus not running, starting..."
    Start-System "Argus" @(
        "-m", "argus_flow.runner_unified",
        "--configs",
        "argus_flow/configs/audjpy_mtf_paper_v1.json",
        "argus_flow/configs/usdjpy_mtf_paper_v1.json",
        "argus_flow/configs/gbpusd_range_paper_v1.json",
        "argus_flow/configs/cadjpy_mtf_paper_v1.json"
    )
} else {
    Log "Argus already running, skipping"
}

# 2. Titan (stock swing, 60-min cycles)
$titanArgs = @("-m", "titan.runner", "--loop", "--interval-min", "60")
if ($Live) { $titanArgs += "--live" }
Start-System "Titan" $titanArgs

# 3. Hermes (gap fill, 120-min cycles)
$hermesArgs = @("-m", "hermes.runner", "--loop", "--interval-min", "120", "--min-score", "80")
if ($Live) { $hermesArgs += "--live" }
Start-System "Hermes" $hermesArgs

# 4. Apollo (earnings, 240-min cycles)
$apolloArgs = @("-m", "apollo.runner", "--loop", "--interval-min", "240", "--days", "14")
if ($Live) { $apolloArgs += "--live" }
Start-System "Apollo" $apolloArgs

# 5. Ares is monthly — runs via nightly cohort report, not as a daemon
Log "Ares: runs via nightly cohort report (monthly cadence, no daemon needed)"

# 6. Dashboard (if not running)
if (-not $SkipDashboard) {
    $dashRunning = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match "dashboard.py"
        } catch { $false }
    }
    if (-not $dashRunning) {
        Start-System "Dashboard" @("ops/dashboard.py", "--port", "8080")
    } else {
        Log "Dashboard already running, skipping"
    }
}

Start-Sleep -Seconds 5
Log "=== Fleet launch complete ==="
Log "Check status: Get-Process python | Where { (Get-CimInstance Win32_Process -Filter `"ProcessId = `$_.Id`").CommandLine -match 'runner|dashboard' }"
