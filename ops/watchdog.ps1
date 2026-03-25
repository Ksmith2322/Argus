# ops/watchdog.ps1 -- Monitor runner_unified.py and auto-restart on crash
# Checks heartbeat staleness every 60s. If all heartbeats stale > 10min, restarts.
# Usage: .\ops\watchdog.ps1
# Register as scheduled task:
#   schtasks /Create /TN "ArgusWatchdog" /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" /SC ONSTART /F

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logFile = "C:\Argus\repo\argus_flow\logs\watchdog.log"
$heartbeatDirs = @(
    "C:\Argus\repo\argus_flow\logs\gbpusd",
    "C:\Argus\repo\argus_flow\logs\eurusd",
    "C:\Argus\repo\argus_flow\logs\eurjpy"
)
$staleThresholdSeconds = 600  # 10 minutes
$checkIntervalSeconds = 60
$maxRestartsPerHour = 3

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

function Is-RunnerAlive {
    $procs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match "runner_unified"
        } catch { $false }
    }
    return ($null -ne $procs)
}

function Get-HeartbeatAge {
    $ages = @()
    foreach ($dir in $heartbeatDirs) {
        $hb = Join-Path $dir "heartbeat.json"
        if (Test-Path $hb) {
            $age = ((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds
            $ages += $age
        }
    }
    if ($ages.Count -eq 0) { return 9999 }
    return ($ages | Measure-Object -Minimum).Minimum
}

function Restart-Runner {
    Log "Restarting runner_unified.py..."

    # Kill any zombie python processes matching runner_unified
    Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match "runner_unified"
        } catch { $false }
    } | Stop-Process -Force -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 5

    Start-Process -FilePath $python `
        -ArgumentList "-m", "argus_flow.runner_unified", "--configs", `
            "argus_flow/configs/gbpusd_range_paper_v1.json", `
            "argus_flow/configs/eurusd_t4_paper_v1.json", `
            "argus_flow/configs/eurjpy_t4_paper_v1.json" `
        -WorkingDirectory "C:\Argus\repo" `
        -WindowStyle Hidden

    Start-Sleep -Seconds 10

    if (Is-RunnerAlive) {
        Log "Runner restarted successfully"
    } else {
        Log "ERROR: Runner failed to restart!"
    }
}

Log "=== Watchdog started ==="
Log "Monitoring heartbeats every ${checkIntervalSeconds}s (stale threshold: ${staleThresholdSeconds}s)"

$restartTimes = @()

while ($true) {
    $alive = Is-RunnerAlive
    $hbAge = Get-HeartbeatAge

    if (-not $alive) {
        Log "Runner process NOT FOUND! Heartbeat age: ${hbAge}s"

        # Rate limit restarts
        $now = Get-Date
        $restartTimes = $restartTimes | Where-Object { ($now - $_).TotalHours -lt 1 }

        if ($restartTimes.Count -ge $maxRestartsPerHour) {
            Log "ERROR: Max restarts ($maxRestartsPerHour/hr) exceeded. Manual intervention required."
        } else {
            Restart-Runner
            $restartTimes += $now
        }
    }
    elseif ($hbAge -gt $staleThresholdSeconds) {
        Log "WARNING: Runner alive but heartbeats stale (${hbAge}s > ${staleThresholdSeconds}s threshold)"
        # Don't auto-restart yet — runner may be reconnecting
        # If stale for 30+ min, then restart
        if ($hbAge -gt 1800) {
            Log "Heartbeats stale for 30+ min. Force restarting..."
            $now = Get-Date
            $restartTimes = $restartTimes | Where-Object { ($now - $_).TotalHours -lt 1 }
            if ($restartTimes.Count -lt $maxRestartsPerHour) {
                Restart-Runner
                $restartTimes += $now
            } else {
                Log "ERROR: Max restarts exceeded. Manual intervention required."
            }
        }
    }

    Start-Sleep -Seconds $checkIntervalSeconds
}
