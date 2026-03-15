# ops/watchdog.ps1 -- Monitor runner_live.py and auto-restart on crash
#
# Usage (manual):
#   .\ops\watchdog.ps1
#
# Usage (Task Scheduler -- every 5 minutes):
#   schtasks /create /tn "ArgusWatchdog" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" /sc minute /mo 5 /f
#
# What it does:
#   1. Checks if runner_live.py is running (python process with runner_live in cmdline)
#   2. If not running, sends Discord alert and restarts it
#   3. If running, optionally sends heartbeat to Discord (every 6 hours)
#   4. Logs all actions to ops/logs/watchdog.log

param(
    [switch]$DryRun,
    [int]$HeartbeatEveryHours = 6
)

$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logFile = Join-Path $repoRoot "ops\logs\watchdog.log"
$heartbeatFile = Join-Path $repoRoot "ops\logs\watchdog_last_heartbeat.txt"

function Write-WatchdogLog($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $line = "$ts  $msg"
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
    Write-Host $line
}

function Send-Discord($msg) {
    try {
        & $pyExe "$repoRoot\ops\notify.py" --test $msg 2>&1 | Out-Null
    } catch {
        Write-WatchdogLog "WARNING: Discord send failed: $_"
    }
}

function Is-RunnerAlive {
    # Look for python processes running runner_live.py
    $procs = Get-Process -Name "python*" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmdline -and $cmdline -match "runner_live") {
                return $true
            }
        } catch {}
    }
    return $false
}

function Should-SendHeartbeat {
    if (-not (Test-Path $heartbeatFile)) { return $true }
    try {
        $lastBeat = Get-Content $heartbeatFile -ErrorAction SilentlyContinue | Select-Object -First 1
        $lastTime = [DateTime]::Parse($lastBeat)
        $elapsed = (Get-Date).ToUniversalTime() - $lastTime
        return ($elapsed.TotalHours -ge $HeartbeatEveryHours)
    } catch {
        return $true
    }
}

# --- Main ---
Set-Location $repoRoot

if (Is-RunnerAlive) {
    # Runner is alive
    if (Should-SendHeartbeat) {
        # Read state for heartbeat info
        $stateFile = Join-Path $repoRoot "state\runtime_state_ETH_USD.json"
        $info = "running"
        if (Test-Path $stateFile) {
            try {
                $state = Get-Content $stateFile | ConvertFrom-Json
                $botState = $state.bot_state
                $cash = [math]::Round([double]$state.cash, 2)
                $pnl = [math]::Round([double]$state.realized_pnl, 4)
                $info = "state=$botState cash=`$$cash pnl=`$$pnl"
            } catch {}
        }

        $uptimeInfo = ""
        try {
            $procs = Get-Process -Name "python*" -ErrorAction SilentlyContinue
            foreach ($p in $procs) {
                $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
                if ($cmdline -and $cmdline -match "runner_live") {
                    $uptime = (Get-Date) - $p.StartTime
                    $uptimeInfo = " uptime=$([math]::Round($uptime.TotalHours, 1))h"
                    break
                }
            }
        } catch {}

        if (-not $DryRun) {
            Send-Discord "Argus Heartbeat OK: $info$uptimeInfo"
            (Get-Date).ToUniversalTime().ToString("o") | Set-Content $heartbeatFile
        }
        Write-WatchdogLog "HEARTBEAT: $info$uptimeInfo"
    } else {
        Write-WatchdogLog "OK: runner alive, heartbeat not due yet"
    }
} else {
    # Runner is DOWN
    Write-WatchdogLog "ALERT: runner_live.py NOT running!"

    if (-not $DryRun) {
        Send-Discord "ALERT: Argus runner_live.py is DOWN! Attempting auto-restart..."

        # Restart runner
        Write-WatchdogLog "Restarting runner_live.py..."
        try {
            Start-Process -FilePath $pyExe -ArgumentList "runner_live.py" -WorkingDirectory $repoRoot -WindowStyle Hidden
            Start-Sleep -Seconds 10

            if (Is-RunnerAlive) {
                Write-WatchdogLog "RESTART OK: runner_live.py is back up"
                Send-Discord "Argus runner_live.py restarted successfully"
            } else {
                Write-WatchdogLog "RESTART FAILED: runner still not detected after 10s"
                Send-Discord "CRITICAL: Argus restart FAILED -- manual intervention needed"
            }
        } catch {
            Write-WatchdogLog "RESTART ERROR: $_"
            Send-Discord "CRITICAL: Argus restart threw error: $_"
        }
    } else {
        Write-WatchdogLog "DRY RUN: would restart runner_live.py"
    }
}