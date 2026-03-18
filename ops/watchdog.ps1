# ops/watchdog.ps1 -- Monitor runner_live.py and auto-restart on crash
#
# Usage (manual):
#   .\ops\watchdog.ps1
#
# Usage (Task Scheduler -- every 5 minutes):
#   schtasks /create /tn "ArgusWatchdog" /tr "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" /sc minute /mo 5 /f
#
# Multi-coin aware:
#   1. Checks if runner_live.py processes are running per coin (ETH, BTC)
#   2. Checks per-coin state freshness via saved_at timestamp
#   3. If ALL runners dead -> full restart via launch_multi.ps1
#   4. If individual coin stale (state not updated >StaleRestartMinutes) -> restart just that coin
#   5. If running, optionally sends heartbeat to Discord (every 6 hours)
#   6. Warns about stale coins (state not updated within StaleMinutes)
#   7. Logs all actions to ops/logs/watchdog.log

param(
    [switch]$DryRun,
    [int]$HeartbeatEveryHours = 6,
    [int]$StaleMinutes = 5,
    [int]$StaleRestartMinutes = 10
)

$repoRoot = "C:\Argus\repo"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logFile = Join-Path $repoRoot "ops\logs\watchdog.log"
$heartbeatFile = Join-Path $repoRoot "ops\logs\watchdog_last_heartbeat.txt"
$coinRestartFile = Join-Path $repoRoot "ops\logs\watchdog_coin_restart.json"
$dailyDigestFile = Join-Path $repoRoot "ops\logs\watchdog_last_daily_digest.txt"
$coins = @("ETH", "BTC")

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
                    CmdLine = $cmdline
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
        DailyPnl = 0
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
            if ($state.PSObject.Properties.Name -contains "daily_realized_pnl") {
                $result.DailyPnl = [math]::Round([double]$state.daily_realized_pnl, 4)
            }
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

function Should-SendDailyDigest {
    $todayKey = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
    if (-not (Test-Path $dailyDigestFile)) { return $true }
    try {
        $lastDay = (Get-Content $dailyDigestFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        return ($lastDay -ne $todayKey)
    } catch { return $true }
}

function Send-DailyDigest {
    param($CoinStatuses)
    $todayKey = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
    $statsLines = @()
    foreach ($cs in $CoinStatuses) {
        if ($cs.Found) {
            $statsLines += "{`"symbol`":`"$($cs.Coin)-USD`",`"total_pnl_usd`":$($cs.Pnl),`"cash`":$($cs.Cash),`"trades_today`":0,`"daily_pnl_usd`":0}"
        }
    }
    if ($statsLines.Count -eq 0) { return }
    $statsJson = "[" + ($statsLines -join ",") + "]"
    try {
        $pyScript = "import sys,json; sys.path.insert(0,r'$repoRoot'); from ops.notify import notify_daily_digest; notify_daily_digest(json.loads(r'$statsJson'))"
        & $pyExe -c $pyScript 2>&1 | Out-Null
        $todayKey | Set-Content $dailyDigestFile -ErrorAction SilentlyContinue
        Write-WatchdogLog "DAILY_DIGEST: sent for $($statsLines.Count) coin(s)"
    } catch {
        Write-WatchdogLog "WARNING: Daily digest send failed: $_"
    }
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

function Get-CoinRestartTimes {
    $times = @{}
    if (Test-Path $coinRestartFile) {
        try {
            $data = Get-Content $coinRestartFile -Raw | ConvertFrom-Json
            $data.PSObject.Properties | ForEach-Object { $times[$_.Name] = [DateTime]::Parse($_.Value) }
        } catch {}
    }
    return $times
}

function Set-CoinRestartTime {
    param([string]$coin, [hashtable]$existing)
    $existing[$coin] = (Get-Date).ToUniversalTime().ToString("o")
    $existing | ConvertTo-Json | Set-Content $coinRestartFile -ErrorAction SilentlyContinue
}

function Restart-Coin {
    param([string]$coin)
    Write-WatchdogLog "RESTART_COIN: Launching $coin runner..."
    try {
        & "$repoRoot\ops\launch_multi.ps1" -Coins @($coin)
        Start-Sleep -Seconds 10
        $cs = Get-CoinStatus -coin $coin
        if (-not $cs.Stale -or $cs.AgeMinutes -lt 2) {
            Write-WatchdogLog "RESTART_COIN OK: $coin state updated"
            Send-Discord "Argus $coin runner restarted (was stale)"
        } else {
            Write-WatchdogLog "RESTART_COIN UNCERTAIN: $coin state age=$($cs.AgeMinutes)m after restart"
            Send-Discord "Argus $coin restart attempted - verify manually"
        }
    } catch {
        Write-WatchdogLog "RESTART_COIN ERROR: $coin - $_"
        Send-Discord "CRITICAL: Argus $coin restart FAILED: $_"
    }
}

# --- Main ---
Set-Location $repoRoot

$runners = Get-RunnerProcesses
$runnerCount = @($runners).Count

# Collect per-coin status
$coinStatuses = @()
$staleCoinsList = @()
$staleRestartCandidates = @()
$healthyCoins = @()
foreach ($coin in $coins) {
    $cs = Get-CoinStatus -coin $coin
    $coinStatuses += $cs
    if ($cs.Found -and (-not $cs.Stale)) {
        $healthyCoins += $coin
    } elseif ($cs.Found -and $cs.Stale) {
        $staleCoinsList += $coin
        if ($cs.AgeMinutes -ge $StaleRestartMinutes) {
            $staleRestartCandidates += $coin
        }
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

    # Daily digest: send once per UTC day
    if (Should-SendDailyDigest -and -not $DryRun) {
        Send-DailyDigest -CoinStatuses $coinStatuses
    }

    # Per-coin stale restart: if runners are alive but a specific coin is stale >StaleRestartMinutes
    if ($staleRestartCandidates.Count -gt 0) {
        $restartTimes = Get-CoinRestartTimes
        foreach ($coin in $staleRestartCandidates) {
            $lastRestart = $restartTimes[$coin]
            $cooldownMinutes = 15
            $shouldRestart = $true
            if ($lastRestart) {
                $elapsed = ((Get-Date).ToUniversalTime() - $lastRestart).TotalMinutes
                if ($elapsed -lt $cooldownMinutes) {
                    Write-WatchdogLog "STALE_COIN: $coin stale but restart cooldown active ($([math]::Round($elapsed,1))m ago, wait ${cooldownMinutes}m)"
                    $shouldRestart = $false
                }
            }
            if ($shouldRestart) {
                $staleAge = ($coinStatuses | Where-Object { $_.Coin -eq $coin }).AgeMinutes
                Write-WatchdogLog "STALE_COIN: $coin not updated in ${staleAge}m - restarting"
                Send-Discord "WARNING: Argus $coin stale ${staleAge}m - restarting coin runner"
                Set-CoinRestartTime -coin $coin -existing $restartTimes
                if (-not $DryRun) {
                    Restart-Coin -coin $coin
                } else {
                    Write-WatchdogLog "DRY RUN: would restart $coin"
                }
            }
        }
    } elseif ($staleCoinsList.Count -gt 0) {
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