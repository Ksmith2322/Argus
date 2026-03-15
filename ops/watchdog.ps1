# ops/watchdog.ps1 -- Monitor runner_live.py and auto-restart on crash
#
# Usage (manual):
#   .\ops\watchdog.ps1
#
# Usage (Task Scheduler -- every 5 minutes):
#   schtasks /create /tn "ArgusWatchdog" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" /sc minute /mo 5 /f
#
# Multi-coin aware:
#   1. Checks if runner_live.py processes are running (python with runner_live in cmdline)
#   2. Checks per-coin state freshness (ETH, BTC, SOL) via saved_at timestamp
#   3. If no runners found, sends Discord alert and restarts via launch_multi.ps1
#   4. If running, optionally sends heartbeat to Discord (every 6 hours)
#   5. Warns about stale coins (state not updated within StaleMinutes)
#   6. Logs all actions to ops/logs/watchdog.log

param(
    [switch]$DryRun,
    [int]$HeartbeatEveryHours = 6,
    [int]$StaleMinutes = 5
)

$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logFile = Join-Path $repoRoot "ops\logs\watchdog.log"
$heartbeatFile = Join-Path $repoRoot "ops\logs\watchdog_last_heartbeat.txt"
$coins = @("ETH", "BTC", "SOL")

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

function Get-RunnerProcesses {
    $runners = @()
    $procs = Get-Process -Name "python*" -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            $cmdline = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmdline -and $cmdline -match "runner_live") {
                $uptime = (Get-Date) - $p.StartTime
                $runners += @{
                    Pid = $p.Id
                    Uptime = $uptime
                    UptimeStr = "$([math]::Round($uptime.TotalHours, 1))h"
                }
            }
        } catch {}
    }
    return $runners
}

function Get-CoinStatus {
    param([string]$coin)
    $stateFile = Join-Path $repoRoot "state\runtime_state_${coin}_USD.json"
    $result = @{
        Coin = $coin
        Found = $false
        BotState = "UNKNOWN"
        Cash = 0
        Pnl = 0
        Stale = $true
        AgeMinutes = -1
    }
    if (Test-Path $stateFile) {
        try {
            $state = Get-Content $stateFile -Raw | ConvertFrom-Json
            $result.Found = $true
            $result.BotState = $state.bot_state
            $result.Cash = [math]::Round([double]$state.cash, 2)
            $result.Pnl = [math]::Round([double]$state.realized_pnl, 4)
            $savedAt = [double]$state.saved_at
            if ($savedAt -gt 0) {
                $savedTime = [DateTimeOffset]::FromUnixTimeSeconds([long]$savedAt).UtcDateTime
                $age = (Get-Date).ToUniversalTime() - $savedTime
                $result.AgeMinutes = [math]::Round($age.TotalMinutes, 1)
                $result.Stale = ($age.TotalMinutes -gt $StaleMinutes)
            }
        } catch {}
    }
    return $result
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

$runners = Get-RunnerProcesses
$runnerCount = @($runners).Count

# Collect per-coin status
$coinStatuses = @()
$staleCoinsList = @()
$healthyCoins = @()
foreach ($coin in $coins) {
    $cs = Get-CoinStatus -coin $coin
    $coinStatuses += $cs
    if ($cs.Found -and (-not $cs.Stale)) {
        $healthyCoins += $coin
    } elseif ($cs.Found -and $cs.Stale) {
        $staleCoinsList += $coin
    }
}

# Build summary string
$summaryParts = @()
foreach ($cs in $coinStatuses) {
    $ageTxt = "$($cs.AgeMinutes)m"
    if ($cs.Found) {
        $tag = if ($cs.Stale) { "STALE($ageTxt)" } else { "ok($ageTxt)" }
        $summaryParts += "$($cs.Coin):$($cs.BotState)/$tag/`$$($cs.Cash)"
    } else {
        $summaryParts += "$($cs.Coin):NO_STATE"
    }
}
$statusSummary = $summaryParts -join " | "

if ($runnerCount -gt 0) {
    $uptimeStr = (@($runners) | ForEach-Object { $_.UptimeStr }) -join ","

    if (Should-SendHeartbeat) {
        $hbMsg = "Argus Heartbeat OK: ${runnerCount} runner(s) uptime=$uptimeStr | $statusSummary"
        if (-not $DryRun) {
            Send-Discord $hbMsg
            (Get-Date).ToUniversalTime().ToString("o") | Set-Content $heartbeatFile
        }
        Write-WatchdogLog "HEARTBEAT: $hbMsg"
    } else {
        Write-WatchdogLog "OK: ${runnerCount} runner(s), heartbeat not due | $statusSummary"
    }

    # Warn about stale coins even if runners are alive
    if ($staleCoinsList.Count -gt 0) {
        $staleMsg = "WARNING: stale coins: $($staleCoinsList -join ', ') (state not updated in >$StaleMinutes m)"
        Write-WatchdogLog $staleMsg
    }
} else {
    Write-WatchdogLog "ALERT: NO runner_live.py processes found! | $statusSummary"

    if (-not $DryRun) {
        Send-Discord "ALERT: Argus runners DOWN (0 processes)! Attempting restart..."

        Write-WatchdogLog "Restarting via launch_multi.ps1..."
        try {
            & "$repoRoot\ops\launch_multi.ps1"
            Start-Sleep -Seconds 15

            $newRunners = Get-RunnerProcesses
            $newCount = @($newRunners).Count
            if ($newCount -gt 0) {
                Write-WatchdogLog "RESTART OK: $newCount runner(s) now running"
                Send-Discord "Argus restarted: $newCount runner(s) launched"
            } else {
                Write-WatchdogLog "RESTART FAILED: still no runners after 15s"
                Send-Discord "CRITICAL: Argus restart FAILED -- manual intervention needed"
            }
        } catch {
            Write-WatchdogLog "RESTART ERROR: $_"
            Send-Discord "CRITICAL: Argus restart threw error: $_"
        }
    } else {
        Write-WatchdogLog "DRY RUN: would restart via launch_multi.ps1"
    }
}