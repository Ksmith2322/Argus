# ops/watchdog.ps1 -- Monitor Argus runners and auto-restart on crash
# Monitors managed grouped runners + heartbeats. Sends Discord alerts on failures.
# Updated 2026-03-31 for grouped FX/futures paper fleet supervision.

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logFile = "C:\Argus\repo\argus_flow\logs\watchdog.log"
$envFile = "C:\Argus\repo\.env"

# Load Discord webhook from .env
$discordWebhook = ""
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^DISCORD_WEBHOOK_URL=(.+)$") {
            $discordWebhook = $Matches[1].Trim()
        }
    }
}

$staleThresholdSeconds = 900  # 15 minutes
$checkIntervalSeconds = 60
$maxRestartsPerHour = 3

# Active configs — dynamically discovered via deployment pipeline
# Fallback to hardcoded if pipeline fails
$fxConfigs = @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs watcher,paper 2>$null)
if ($fxConfigs.Count -eq 0) {
    # Fallback: hardcoded active configs (updated 2026-04-09)
    $fxConfigs = @(
        "argus_flow/configs/audjpy_mtf_paper_v1.json",
        "argus_flow/configs/usdjpy_mtf_paper_v1.json",
        "argus_flow/configs/gbpusd_range_paper_v1.json",
        "argus_flow/configs/cadjpy_mtf_paper_v1.json"
    )
}

# No futures configs (all killed in fleet consolidation 2026-04-07)
$futuresConfigs = @()

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

function Get-ConfigSignature([string[]]$configs) {
    return (@(
        $configs |
        ForEach-Object {
            $leaf = Split-Path $_ -Leaf
            if ([string]::IsNullOrWhiteSpace($leaf)) { $_ } else { $leaf }
        } |
        ForEach-Object { $_.ToLower() } |
        Sort-Object -Unique
    ) -join '|')
}

function Get-AliveRunnerLocks() {
    $results = @()
    $lockFiles = Get-ChildItem "argus_flow/logs/_locks" -Filter "runner_*.json" -ErrorAction SilentlyContinue
    foreach ($file in $lockFiles) {
        try {
            $lock = Get-Content $file.FullName -Raw | ConvertFrom-Json
            if (-not $lock) { continue }
            $lockPid = [int]$lock.pid
            $proc = Get-Process -Id $lockPid -ErrorAction SilentlyContinue
            if (-not $proc) { continue }
            $results += [pscustomobject]@{
                Path = $file.FullName
                Id = $lockPid
                Configs = @($lock.configs)
            }
        } catch {}
    }
    return @($results)
}

function Get-LogDirForConfig([string]$configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        $symbol = [string]$cfg.symbol
        if (-not $symbol) { return $null }
        return "argus_flow/logs/$($symbol.ToLower())"
    } catch {
        return $null
    }
}

function Get-WatchedLogDirs() {
    $dirs = @()
    foreach ($cfg in @($fxConfigs + $futuresConfigs | Select-Object -Unique)) {
        $dir = Get-LogDirForConfig $cfg
        if ($dir) { $dirs += $dir }
    }
    return @($dirs | Select-Object -Unique)
}

function Send-Discord($message, $color) {
    if (-not $discordWebhook) { return }
    try {
        $colorInt = switch ($color) {
            "red"    { 16711680 }
            "yellow" { 16776960 }
            "green"  { 65280 }
            default  { 8421504 }
        }
        $body = @{
            embeds = @(@{
                title = "Argus Alert"
                description = $message
                color = $colorInt
                timestamp = (Get-Date -Format "o")
            })
        } | ConvertTo-Json -Depth 5

        Invoke-RestMethod -Uri $discordWebhook -Method Post -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
    } catch {
        Log "Discord alert failed: $_"
    }
}

function Is-RunnerAlive($type) {
    $expectedConfigs = if ($type -eq "fx") { $fxConfigs } else { $futuresConfigs }
    $expectedSignature = Get-ConfigSignature $expectedConfigs
    $locks = Get-AliveRunnerLocks
    foreach ($lock in $locks) {
        $actualSignature = Get-ConfigSignature $lock.Configs
        if ($actualSignature -eq $expectedSignature) { return $true }
    }
    return $false
}

function Restart-Runner($type) {
    if ($type -eq "fx") {
        $argsList = @("-m", "argus_flow.runner_unified", "--configs") + $fxConfigs
        Log "Restarting FX runner (8 pairs)..."
    } else {
        $argsList = @("-m", "argus_flow.runner_unified", "--configs") + $futuresConfigs
        Log "Restarting Futures runner (6 instruments)..."
    }

    try {
        Start-Process -FilePath $python -ArgumentList $argsList -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
        Start-Sleep -Seconds 10

        if (Is-RunnerAlive $type) {
            Log "$type runner restarted successfully"
            Send-Discord "**$type runner restarted** after crash detection." "yellow"
            return $true
        } else {
            Log "ERROR: $type runner failed to restart!"
            Send-Discord "**CRITICAL: $type runner FAILED to restart!** Manual intervention required." "red"
            return $false
        }
    } catch {
        Log "ERROR restarting $type runner: $_"
        Send-Discord "**CRITICAL: $type runner restart exception:** $_" "red"
        return $false
    }
}

# Track restart count per hour
$restartTimestamps = @()
$lastHeartbeatFile = "argus_flow/logs/watchdog_last_heartbeat.txt"
$wasDown = @{ "fx" = $false; "futures" = $false }  # Track for "back online" alerts
$script:portWarnSent = $false      # Track API port warning state
$script:staleWarnSent = $false     # Track stale heartbeat warning state
$script:staleWarnTime = $null      # When stale warning was sent (for downtime tracking)
$script:tradeCounts = @{}          # Track trade counts for new-trade detection
$script:dailySummarySent = $false  # Track daily summary sent

Log "=========================================="
Log "Argus Watchdog started (full fleet)"
Log "FX: $($fxConfigs.Count) pairs | Futures: $($futuresConfigs.Count)"
Log "Discord: $(if ($discordWebhook) { 'ENABLED' } else { 'DISABLED' })"
Log "=========================================="
Send-Discord "Argus Watchdog started. Monitoring $($fxConfigs.Count) FX + $($futuresConfigs.Count) futures." "green"

while ($true) {
    Start-Sleep -Seconds $checkIntervalSeconds

    $now = Get-Date
    $fxAlive = Is-RunnerAlive "fx"
    $futuresAlive = Is-RunnerAlive "futures"

    # Write watchdog heartbeat
    Set-Content -Path $lastHeartbeatFile -Value (Get-Date -Format "o") -ErrorAction SilentlyContinue

    # Clean old restart timestamps
    $restartTimestamps = @($restartTimestamps | Where-Object { ($now - $_).TotalHours -lt 1 })

    # FX runner check
    if (-not $fxAlive) {
        $wasDown["fx"] = $true
        Log "ALERT: FX runner NOT FOUND!"
        Send-Discord "**FX runner is DOWN!** Attempting restart..." "red"
        if ($restartTimestamps.Count -lt $maxRestartsPerHour) {
            Restart-Runner "fx"
            $restartTimestamps += $now
        } else {
            Log "MAX RESTARTS reached ($maxRestartsPerHour/hour). Not restarting FX."
            Send-Discord "**CRITICAL: FX runner down, max restarts ($maxRestartsPerHour/hr) exhausted!** Manual intervention needed." "red"
        }
    } elseif ($wasDown["fx"]) {
        # Was down, now back — send confirmation
        $wasDown["fx"] = $false
        Log "FX runner BACK ONLINE"
        Send-Discord "FX runner is **BACK ONLINE** and healthy." "green"
    }

    # Futures runner check
    if (-not $futuresAlive) {
        $wasDown["futures"] = $true
        Log "ALERT: Futures runner NOT FOUND!"
        if ($restartTimestamps.Count -lt $maxRestartsPerHour) {
            Restart-Runner "futures"
            $restartTimestamps += $now
        } else {
            Log "MAX RESTARTS reached. Not restarting Futures."
        }
    } elseif ($wasDown["futures"]) {
        $wasDown["futures"] = $false
        Log "Futures runner BACK ONLINE"
        Send-Discord "Futures runner is **BACK ONLINE** and healthy." "green"
    }

    # ── Gap Fix #1: API port check (detect TWS API disabled mid-session) ──
    $portListening = $false
    try {
        $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -WarningAction SilentlyContinue
        $portListening = $conn.TcpTestSucceeded
    } catch {}

    if (-not $portListening -and $fxAlive) {
        if (-not $script:portWarnSent) {
            Log "ALERT: API port 7496 NOT LISTENING but runners are alive!"
            Send-Discord "**WARNING: IBKR API port 7496 not responding!** Runners alive but may not be receiving data. Check TWS API settings." "red"
            $script:portWarnSent = $true
        }
    } elseif ($portListening -and $script:portWarnSent) {
        Log "API port 7496 restored"
        Send-Discord "API port 7496 is **BACK** and listening." "green"
        $script:portWarnSent = $false
    }

    # ── Gap Fix #4: Stale heartbeat detection (runners alive but no data) ──
    $staleCount = 0
    $freshCount = 0
    # Check ALL active instruments, not just FX
    $hbDirs = Get-WatchedLogDirs
    foreach ($dir in $hbDirs) {
        $hbFile = Join-Path $dir "heartbeat.json"
        if (Test-Path $hbFile) {
            try {
                $hb = Get-Content $hbFile -Raw | ConvertFrom-Json
                $hbTime = [DateTime]::Parse($hb.ts)
                $hbAge = ((Get-Date).ToUniversalTime() - $hbTime).TotalSeconds
                if ($hbAge -lt $staleThresholdSeconds) { $freshCount++ } else { $staleCount++ }
            } catch { $staleCount++ }
        }
    }

    # Market hours check (Sun 9PM - Fri 5PM ET)
    $utcHour = (Get-Date).ToUniversalTime().Hour
    $dayOfWeek = (Get-Date).DayOfWeek
    $marketExpected = -not (($dayOfWeek -eq "Saturday") -or ($dayOfWeek -eq "Sunday" -and $utcHour -lt 21) -or ($dayOfWeek -eq "Friday" -and $utcHour -ge 22))

    # Stale alert — only during market hours
    if ($marketExpected -and $fxAlive -and $freshCount -eq 0 -and $staleCount -gt 0) {
        if (-not $script:staleWarnSent) {
            Log "ALERT: All heartbeats STALE ($staleCount stale, $freshCount fresh) but runners alive!"
            Send-Discord "**WARNING: Runners alive but ALL heartbeats stale!** Data may not be flowing. Possible API disconnect." "yellow"
            $script:staleWarnSent = $true
            $script:staleWarnTime = $now
        }
    }

    # Recovery alert — fires anytime heartbeats come back after a stale warning, regardless of market hours
    if ($freshCount -gt 0 -and $script:staleWarnSent) {
        $downMinutes = if ($script:staleWarnTime) { [int](($now - $script:staleWarnTime).TotalMinutes) } else { 0 }
        Log "Heartbeats RESTORED ($freshCount fresh, $staleCount stale) after ${downMinutes}min"
        Send-Discord "Heartbeats **RESTORED**. Data flowing again ($freshCount/$($freshCount+$staleCount) instruments). Was stale for ~${downMinutes} min." "green"
        $script:staleWarnSent = $false
        $script:staleWarnTime = $null
    }

    # ── Gap Fix #2: Trade event notifications ──
    # Check for new trades every 5 min
    if ($now.Minute % 5 -eq 0 -and $now.Second -lt 65) {
        foreach ($dir in $hbDirs) {
            $tradeFile = Join-Path $dir "trades.csv"
            if (Test-Path $tradeFile) {
                $lineCount = (Get-Content $tradeFile | Measure-Object -Line).Lines - 1
                $pair = Split-Path $dir -Leaf
                $stateKey = "trades_$pair"
                $prevCount = if ($script:tradeCounts.ContainsKey($stateKey)) { $script:tradeCounts[$stateKey] } else { $lineCount }

                if ($lineCount -gt $prevCount) {
                    $newTrades = $lineCount - $prevCount
                    # Read last trade
                    $lastLine = Get-Content $tradeFile | Select-Object -Last 1
                    $fields = $lastLine -split ","
                    $pnl = if ($fields.Count -gt 4) { $fields[4] } else { "?" }
                    $direction = if ($fields.Count -gt 1) { $fields[1] } else { "?" }
                    $exitReason = if ($fields.Count -gt 5) { $fields[5] } else { "?" }

                    $pnlColor = if ([double]::TryParse($pnl, [ref]$null) -and [double]$pnl -gt 0) { "green" } else { "red" }
                    Send-Discord "**Trade Closed:** $($pair.ToUpper()) $direction | PnL: $pnl pips | Exit: $exitReason" $pnlColor
                    Log "TRADE: $pair $direction pnl=$pnl exit=$exitReason"
                }
                $script:tradeCounts[$stateKey] = $lineCount
            }
        }
    }

    # ── Gap Fix #3: Daily session summary (9 AM CT = 14:00 UTC) ──
    if ($utcHour -eq 14 -and $now.Minute -ge 0 -and $now.Minute -lt 2 -and -not $script:dailySummarySent) {
        $totalTrades = 0
        $totalPnl = 0.0
        $pairSummary = @()
        foreach ($dir in $hbDirs) {
            $tradeFile = Join-Path $dir "trades.csv"
            $pair = Split-Path $dir -Leaf
            if (Test-Path $tradeFile) {
                $rows = Import-Csv $tradeFile
                $todayTrades = $rows | Where-Object { $_.ts -like "$(Get-Date -Format 'yyyy-MM-dd')*" }
                if ($todayTrades) {
                    $count = ($todayTrades | Measure-Object).Count
                    $pnlField = if ($todayTrades[0].PSObject.Properties.Name -contains "pnl_pips") { "pnl_pips" } else { "pnl_pts" }
                    $pnl = ($todayTrades | ForEach-Object { [double]$_.$pnlField } | Measure-Object -Sum).Sum
                    $totalTrades += $count
                    $totalPnl += $pnl
                    if ($count -gt 0) { $pairSummary += "$($pair.ToUpper()): $count trades ($([math]::Round($pnl,1)) pips)" }
                }
            }
        }
        if ($totalTrades -gt 0) {
            $color = if ($totalPnl -ge 0) { "green" } else { "red" }
            $details = $pairSummary -join "`n"
            Send-Discord "**Daily Session Summary (London Close)**`nTrades: $totalTrades | Net PnL: $([math]::Round($totalPnl,1)) pips`n$details" $color
            Log "DAILY SUMMARY: $totalTrades trades, $([math]::Round($totalPnl,1)) pips"
        }
        $script:dailySummarySent = $true
    }
    if ($utcHour -ne 14) { $script:dailySummarySent = $false }

    # Periodic heartbeat log (every 5 min)
    if ($now.Minute % 5 -eq 0 -and $now.Second -lt 65) {
        $fxStr = if ($fxAlive) { "UP" } else { "DOWN" }
        $futStr = if ($futuresAlive) { "UP" } else { "DOWN" }
        Log "HEARTBEAT | FX=$fxStr | Futures=$futStr | Port=$portListening | Fresh=$freshCount Stale=$staleCount"
    }
}
