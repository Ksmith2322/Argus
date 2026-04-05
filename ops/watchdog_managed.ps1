# ops/watchdog_managed.ps1 -- Stage-aware managed fleet supervisor
# Monitors the managed watcher/paper and real-money lanes by comparing
# live runner lock metadata against the deployment registry, then calls
# launch_fleet.ps1 as the canonical reconciler when drift is detected.

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$powershellExe = "powershell.exe"
$logFile = "C:\Argus\repo\argus_flow\logs\watchdog_managed.log"
$envFile = "C:\Argus\repo\.env"

$discordWebhook = ""
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^DISCORD_WEBHOOK_URL=(.+)$") {
            $discordWebhook = $Matches[1].Trim()
        }
    }
}

$staleThresholdSeconds = 900
$checkIntervalSeconds = 60
$governanceRefreshSeconds = 600
$maxRestartsPerHour = 3
$componentFailureThreshold = 2
$watchdogMutexName = "Global\ArgusManagedWatchdog"
$script:watchdogMutex = New-Object System.Threading.Mutex($false, $watchdogMutexName)
if (-not $script:watchdogMutex.WaitOne(0, $false)) {
    Write-Host "Another Argus managed watchdog is already running. Exiting."
    exit 0
}
Register-EngineEvent PowerShell.Exiting -Action {
    try {
        $script:watchdogMutex.ReleaseMutex()
        $script:watchdogMutex.Dispose()
    } catch {}
} | Out-Null

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
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

function Get-ProcessCommandLine($procId) {
    try {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
        if ($proc) { return $proc.CommandLine }
    } catch {}
    return $null
}

function Test-LockMatchesProcess($lock, $proc) {
    try {
        if ($lock.kind -and [string]$lock.kind -ne "runner_unified") { return $false }
        $startedAt = [string]$lock.started_at
        if ([string]::IsNullOrWhiteSpace($startedAt)) { return $true }
        $lockStart = [DateTime]::Parse($startedAt).ToUniversalTime()
        $procStart = $proc.StartTime.ToUniversalTime()
        return [math]::Abs(($procStart - $lockStart).TotalMinutes) -lt 5
    } catch {
        return $true
    }
}

function Test-LockMetadataProcessAlive([string]$metaPath, [string]$expectedKind = "") {
    if (-not (Test-Path $metaPath)) { return $false }
    try {
        $lock = Get-Content $metaPath -Raw | ConvertFrom-Json
        if (-not $lock) { return $false }
        if ($expectedKind -and $lock.kind -and [string]$lock.kind -ne $expectedKind) { return $false }
        $proc = Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue
        if (-not $proc) { return $false }
        $startedAt = [string]$lock.started_at
        if (-not [string]::IsNullOrWhiteSpace($startedAt)) {
            $lockStart = [DateTime]::Parse($startedAt).ToUniversalTime()
            $procStart = $proc.StartTime.ToUniversalTime()
            if ([math]::Abs(($procStart - $lockStart).TotalMinutes) -ge 5) { return $false }
        }
        return $true
    } catch {
        return $false
    }
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
            if (-not (Test-LockMatchesProcess $lock $proc)) { continue }
            $results += [pscustomobject]@{
                Id = $lockPid
                Configs = @($lock.configs | ForEach-Object { "argus_flow/configs/$_" })
                CommandLine = Get-ProcessCommandLine $lockPid
            }
        } catch {}
    }
    return @($results)
}

function Is-Running($pattern) {
    $procs = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = Get-ProcessCommandLine $_.Id
            $cmd -match $pattern
        } catch { $false }
    }
    return @($procs)
}

function Get-ManagedRunnerProcesses([switch]$RealLane) {
    $results = @()
    foreach ($runner in Get-AliveRunnerLocks) {
        $isReal = @($runner.Configs | Where-Object { $_ -match "live_v1" }).Count -gt 0
        if ($RealLane -and -not $isReal) { continue }
        if (-not $RealLane -and $isReal) { continue }
        $results += $runner
    }
    return @($results)
}

function Test-DashboardAlive() {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8080" -TimeoutSec 5
        return $resp.StatusCode -eq 200
    } catch {
        $lockFiles = Get-ChildItem "argus_flow/logs/_locks" -Filter "dashboard_*.json" -ErrorAction SilentlyContinue
        foreach ($file in @($lockFiles)) {
            if (Test-LockMetadataProcessAlive $file.FullName "dashboard") { return $true }
        }
        return $false
    }
}

function Test-DiscordWatcherAlive() {
    $hb = "argus_flow/logs/discord_watcher_heartbeat.json"
    if (Test-Path $hb) {
        try {
            $age = (Get-Date) - (Get-Item $hb).LastWriteTime
            if ($age.TotalSeconds -lt 180) { return $true }
        } catch {}
    }
    return @(Is-Running("argus_flow\.ops\.discord_alerts.*--watch")).Count -gt 0
}

function Get-ConfigInstrumentType([string]$configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        return [string]$cfg.instrument_type
    } catch {
        return "unknown"
    }
}

function Get-CommandLineConfigs([string]$commandLine) {
    if (-not $commandLine) { return @() }
    $normalized = $commandLine.Replace('\', '/')
    $matches = [regex]::Matches($normalized, 'argus_flow/configs/[^"\s]+\.json')
    return @($matches | ForEach-Object { $_.Value } | Select-Object -Unique)
}

function Get-ConfigSignature([string[]]$configs) {
    return (@($configs | ForEach-Object { $_.Replace('\', '/') } | Sort-Object -Unique) -join '|')
}

function Build-ConfigGroups([string[]]$configs, [string]$labelPrefix) {
    $fx = @()
    $futures = @()
    $other = @()
    foreach ($cfg in @($configs)) {
        $itype = Get-ConfigInstrumentType $cfg
        if ($itype -eq "future") {
            $futures += $cfg
        } elseif ($itype -eq "forex") {
            $fx += $cfg
        } else {
            $other += $cfg
        }
    }

    $groups = @()
    if ($fx.Count -gt 0) { $groups += [pscustomobject]@{ Label = "$labelPrefix-fx"; Configs = @($fx) } }
    if ($futures.Count -gt 0) { $groups += [pscustomobject]@{ Label = "$labelPrefix-futures"; Configs = @($futures) } }
    if ($other.Count -gt 0) { $groups += [pscustomobject]@{ Label = "$labelPrefix-other"; Configs = @($other) } }
    return @($groups)
}

function Test-RunnerMatchesGroups($procs, $groups) {
    if ($groups.Count -eq 0) { return $procs.Count -eq 0 }
    if ($procs.Count -ne $groups.Count) { return $false }
    $desired = @($groups | ForEach-Object { Get-ConfigSignature $_.Configs } | Sort-Object)
    $actual = @($procs | ForEach-Object {
        if ($_.PSObject.Properties.Name -contains "Configs") {
            Get-ConfigSignature $_.Configs
        } else {
            Get-ConfigSignature (Get-CommandLineConfigs $_.CommandLine)
        }
    } | Sort-Object)
    if ($desired.Count -ne $actual.Count) { return $false }
    for ($i = 0; $i -lt $desired.Count; $i++) {
        if ($desired[$i] -ne $actual[$i]) { return $false }
    }
    return $true
}

function Get-DeploymentConfigs([string]$stageSpec) {
    return @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs $stageSpec 2>$null)
}

function Invoke-ManagedTruthRefresh() {
    Log "Launching managed truth refresh..."
    try {
        $proc = Start-Process -FilePath $python `
            -ArgumentList "-m", "argus_flow.ops.refresh_managed_truth", "--accept-existing-age-s", "600" `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden `
            -PassThru
        Log "Managed truth refresh launched (PID=$($proc.Id))"
        return $true
    } catch {
        Log "ERROR refreshing managed truth: $_"
        return $false
    }
}

function Get-LogDirForConfig([string]$configPath) {
    try {
        $cfg = Get-Content $configPath -Raw | ConvertFrom-Json
        $deployment = $cfg.deployment
        if ($deployment -and $deployment.log_dir) {
            $raw = [string]$deployment.log_dir
            if ([System.IO.Path]::IsPathRooted($raw)) { return $raw }
            return Join-Path "C:\Argus\repo" $raw
        }
        $symbol = [string]$cfg.symbol
        if (-not $symbol) { return $null }
        $isLive = $false
        if ($cfg.live) { $isLive = $true }
        elseif ([string]$cfg.version -match "live") { $isLive = $true }
        elseif ((Split-Path $configPath -Leaf) -match "live_v1") { $isLive = $true }
        if ($isLive) { return Join-Path "C:\Argus\repo" "argus_flow/logs/live_$($symbol.ToLower())" }
        return Join-Path "C:\Argus\repo" "argus_flow/logs/$($symbol.ToLower())"
    } catch {
        return $null
    }
}

function Get-WatchedLogDirs([string[]]$configs) {
    $dirs = @()
    foreach ($cfg in @($configs | Select-Object -Unique)) {
        $dir = Get-LogDirForConfig $cfg
        if ($dir) { $dirs += $dir }
    }
    return @($dirs | Select-Object -Unique)
}

function Get-LaneState([string]$lane) {
    $realLane = $lane -eq "real"
    $stageSpec = if ($realLane) { "real" } else { "watcher,paper" }
    $configs = Get-DeploymentConfigs $stageSpec
    $groups = Build-ConfigGroups $configs $lane
    $procs = Get-ManagedRunnerProcesses -RealLane:$realLane
    return [pscustomobject]@{
        Lane = $lane
        Configs = @($configs)
        Groups = @($groups)
        Procs = @($procs)
        Expected = $groups.Count -gt 0
        Healthy = (Test-RunnerMatchesGroups $procs $groups)
    }
}

function Invoke-FleetReconcile([string]$reason) {
    Log "Reconciling managed fleet ($reason)..."
    try {
        $output = & $powershellExe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File "C:\Argus\repo\ops\launch_fleet.ps1" 2>&1
        foreach ($line in @($output)) {
            if ($line) { Log "launch_fleet> $line" }
        }
        return $true
    } catch {
        Log "ERROR reconciling managed fleet: $_"
        Send-Discord "**CRITICAL: managed fleet reconcile failed**`n$reason`n$_" "red"
        return $false
    }
}

$restartTimestamps = @()
$lastHeartbeatFile = "argus_flow/logs/watchdog_last_heartbeat.txt"
$wasDown = @{
    "paper" = $false
    "real" = $false
    "dashboard" = $false
    "discord" = $false
}
$failureCounts = @{
    "paper" = 0
    "real" = 0
    "dashboard" = 0
    "discord" = 0
}
$script:portWarnSent = $false
$script:gatewayDownCount = 0
$script:gatewayWarnSent = $false
$script:helioLastCheck = Get-Date "2000-01-01T00:00:00Z"
$script:staleWarnSent = $false
$script:staleWarnTime = $null
$script:tradeCounts = @{}
$script:dailySummarySent = $false
$script:maxRestartAlertSent = $false
$script:lastGovernanceRefresh = Get-Date "2000-01-01T00:00:00Z"

Log "=========================================="
Log "Argus Managed Watchdog started"
Log "Discord: $(if ($discordWebhook) { 'ENABLED' } else { 'DISABLED' })"
Log "=========================================="
Send-Discord "Argus managed watchdog started. Monitoring watcher/paper + real lanes from deployment_registry." "green"

while ($true) {
    Start-Sleep -Seconds $checkIntervalSeconds

    # Kill switch check
    $killFile = Join-Path "C:\Argus\repo" "KILL_SWITCH"
    if (Test-Path $killFile) {
        Log "KILL_SWITCH detected. Watchdog exiting - fleet should already be halted."
        Send-Discord "**KILL_SWITCH detected.** Watchdog exiting." "red"
        exit 0
    }
    $pauseFile = Join-Path "C:\Argus\repo" "PAUSE_ENTRIES"
    if (Test-Path $pauseFile) {
        Log "PAUSE_ENTRIES detected. Skipping this cycle."
        continue
    }

    $now = Get-Date
    Set-Content -Path $lastHeartbeatFile -Value (Get-Date -Format "o") -ErrorAction SilentlyContinue
    $restartTimestamps = @($restartTimestamps | Where-Object { ($now - $_).TotalHours -lt 1 })

    if (($now.ToUniversalTime() - $script:lastGovernanceRefresh.ToUniversalTime()).TotalSeconds -ge $governanceRefreshSeconds) {
        if (Invoke-ManagedTruthRefresh) {
            $script:lastGovernanceRefresh = Get-Date
        } else {
            $script:lastGovernanceRefresh = Get-Date
            Send-Discord "**WARNING: managed truth refresh failed**`nPromotion, demotion, and alert surfaces may be stale until the next successful refresh." "yellow"
        }
    }

    $paperState = Get-LaneState "paper"
    $realState = Get-LaneState "real"
    $dashboardUp = Test-DashboardAlive
    $discordUp = Test-DiscordWatcherAlive

    $reconcileReasons = @()

    if (-not $paperState.Healthy) {
        $failureCounts["paper"]++
    } else {
        $failureCounts["paper"] = 0
    }
    if (-not $realState.Healthy -and ($realState.Expected -or $realState.Procs.Count -gt 0)) {
        $failureCounts["real"]++
    } else {
        $failureCounts["real"] = 0
    }
    if (-not $dashboardUp) {
        $failureCounts["dashboard"]++
    } else {
        $failureCounts["dashboard"] = 0
    }
    if (-not $discordUp) {
        $failureCounts["discord"]++
    } else {
        $failureCounts["discord"] = 0
    }

    if ($failureCounts["paper"] -ge $componentFailureThreshold) {
        if (-not $wasDown["paper"]) {
            Log "ALERT: watcher/paper lane drift or outage detected"
            Send-Discord "**Watcher/Paper lane is out of policy**`nExpected grouped runner set does not match the deployment registry. Reconcile scheduled." "red"
        }
        $wasDown["paper"] = $true
        $reconcileReasons += "watcher/paper lane drift"
    } elseif ($wasDown["paper"]) {
        $wasDown["paper"] = $false
        Log "Watcher/Paper lane BACK ONLINE"
        Send-Discord "Watcher/Paper lane is **BACK ONLINE** and matches the deployment registry." "green"
    }

    if ($failureCounts["real"] -ge $componentFailureThreshold) {
        if ($realState.Expected) {
            if (-not $wasDown["real"]) {
                Log "ALERT: real-money lane drift or outage detected"
                Send-Discord "**Real-money lane is out of policy**`nExpected grouped runner set does not match the deployment registry. Reconcile scheduled." "red"
            }
            $wasDown["real"] = $true
            $reconcileReasons += "real-money lane drift"
        } elseif ($realState.Procs.Count -gt 0) {
            Log "ALERT: unexpected real-money runner detected"
            Send-Discord "**Unexpected real-money runner detected**`nReal lane is not expected by the deployment registry but a live runner is still present. Reconcile scheduled." "red"
            $reconcileReasons += "unexpected real-money runner"
        } else {
            $wasDown["real"] = $false
        }
    } elseif ($wasDown["real"]) {
        $wasDown["real"] = $false
        Log "Real-money lane BACK ONLINE"
        Send-Discord "Real-money lane is **BACK ONLINE** and matches the deployment registry." "green"
    }

    if ($failureCounts["dashboard"] -ge $componentFailureThreshold) {
        if (-not $wasDown["dashboard"]) {
            Log "ALERT: dashboard process not running"
            Send-Discord "**Dashboard is DOWN**`nManaged fleet reconcile scheduled." "yellow"
        }
        $wasDown["dashboard"] = $true
        $reconcileReasons += "dashboard missing"
    } elseif ($wasDown["dashboard"]) {
        $wasDown["dashboard"] = $false
        Log "Dashboard BACK ONLINE"
        Send-Discord "Dashboard is **BACK ONLINE**." "green"
    }

    if ($failureCounts["discord"] -ge $componentFailureThreshold) {
        if (-not $wasDown["discord"]) {
            Log "ALERT: Discord watcher process not running"
            Send-Discord "**Discord watcher is DOWN**`nManaged fleet reconcile scheduled." "yellow"
        }
        $wasDown["discord"] = $true
        $reconcileReasons += "discord watcher missing"
    } elseif ($wasDown["discord"]) {
        $wasDown["discord"] = $false
        Log "Discord watcher BACK ONLINE"
        Send-Discord "Discord watcher is **BACK ONLINE**." "green"
    }

    if ($reconcileReasons.Count -gt 0) {
        if ($restartTimestamps.Count -lt $maxRestartsPerHour) {
            if (Invoke-FleetReconcile (($reconcileReasons | Select-Object -Unique) -join "; ")) {
                $restartTimestamps += $now
                $script:maxRestartAlertSent = $false
            }
        } elseif (-not $script:maxRestartAlertSent) {
            Log ("MAX RESTARTS reached ({0}/hour). Skipping reconcile." -f $maxRestartsPerHour)
            Send-Discord "**CRITICAL: max managed-fleet reconciles exhausted**`n$maxRestartsPerHour attempts in the last hour. Manual intervention needed." "red"
            $script:maxRestartAlertSent = $true
        }
    } else {
        $script:maxRestartAlertSent = $false
    }

    # ── Helio family supervision (Apollo, Hermes, Helio swing) ──────────
    # Check every 5 minutes. Restart dead runners. Uses heartbeat staleness.
    $helioCheckInterval = 300
    if (((Get-Date) - $script:helioLastCheck).TotalSeconds -ge $helioCheckInterval) {
        $script:helioLastCheck = Get-Date
        $helioRunners = @(
            @{ Name = "helio";   Module = "helio.runner";         HbDirs = @("gld","spy","mgc","mnq","mes","mym"); StaleS = 7200 },
            @{ Name = "apollo";  Module = "helio.runner_apollo";  HbDirs = @("apollo_audjpy","apollo_eurusd","apollo_usdjpy","apollo_gbpusd"); StaleS = 7200 },
            @{ Name = "hermes";  Module = "helio.runner_hermes";  HbDirs = @("hermes_gold_f"); StaleS = 7200 }
        )
        $helioLogsRoot = "C:\Argus\repo\helio\logs"
        foreach ($hr in $helioRunners) {
            $alive = $false
            foreach ($hdir in $hr.HbDirs) {
                $hbPath = Join-Path $helioLogsRoot "$hdir\heartbeat.json"
                if (Test-Path $hbPath) {
                    $hbAge = ((Get-Date) - (Get-Item $hbPath).LastWriteTime).TotalSeconds
                    if ($hbAge -lt $hr.StaleS) { $alive = $true; break }
                }
            }
            # Check if process is running regardless of heartbeat
            $procRunning = $false
            Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
                    if ($cmd -match $hr.Module.Replace(".", "\.")) { $procRunning = $true }
                } catch {}
            }
            if (-not $procRunning -and -not $alive) {
                # Skip restart on weekends for market-dependent runners
                $dow = (Get-Date).DayOfWeek
                $utcHour = (Get-Date).ToUniversalTime().Hour
                $isWeekend = ($dow -eq "Saturday") -or ($dow -eq "Sunday" -and $utcHour -lt 21)
                if (-not $isWeekend) {
                    Log ("HELIO: {0} is dead (no process, heartbeat stale). Restarting..." -f $hr.Name)
                    # Clean stale locks
                    Get-ChildItem "C:\Argus\repo\argus_flow\logs\_locks" -Filter "$($hr.Name)_*" -ErrorAction SilentlyContinue | Remove-Item -Force
                    Start-Process -FilePath $python -ArgumentList "-m", $hr.Module -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
                    Log ("HELIO: {0} restarted" -f $hr.Name)
                }
            }
        }
    }

    $portListening = $false
    try {
        $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -WarningAction SilentlyContinue
        $portListening = $conn.TcpTestSucceeded
    } catch {}

    $anyLaneHealthy = $paperState.Healthy -or ($realState.Expected -and $realState.Healthy)
    if (-not $portListening -and $anyLaneHealthy) {
        if (-not $script:portWarnSent) {
            Log "ALERT: API port 7496 NOT LISTENING while managed runners are healthy"
            Send-Discord "**WARNING: IBKR API port 7496 not responding**`nManaged runners are alive but may not be receiving data." "red"
            $script:portWarnSent = $true
        }
    } elseif ($portListening -and $script:portWarnSent) {
        Log "API port 7496 restored"
        Send-Discord "API port 7496 is **BACK** and listening." "green"
        $script:portWarnSent = $false
    }

    # IB Gateway / TWS process supervision — fail-closed on sustained outage
    $gatewayAlive = $false
    try {
        $twsProc = Get-Process -Name "tws" -ErrorAction SilentlyContinue
        $gatewayProc = Get-Process -Name "ibgateway" -ErrorAction SilentlyContinue
        $gatewayAlive = ($null -ne $twsProc) -or ($null -ne $gatewayProc)
    } catch {}
    if (-not $gatewayAlive -and -not $portListening) {
        $script:gatewayDownCount++
        if ($script:gatewayDownCount -ge 3 -and -not $script:gatewayWarnSent) {
            Log "CRITICAL: IB Gateway/TWS process NOT running for $($script:gatewayDownCount) checks. Creating PAUSE_ENTRIES."
            $pauseFile = Join-Path "C:\Argus\repo" "PAUSE_ENTRIES"
            if (-not (Test-Path $pauseFile)) {
                "gateway_supervision" | Out-File -FilePath $pauseFile -Encoding utf8
                Log "PAUSE_ENTRIES created by gateway supervision (fail-closed)"
            }
            Send-Discord "**CRITICAL: IB Gateway/TWS not running**`nEntries PAUSED (fail-closed). Manual restart required.`nDown for $($script:gatewayDownCount) consecutive checks ($checkIntervalSeconds sec each)." "red"
            $script:gatewayWarnSent = $true
        } elseif ($script:gatewayDownCount -lt 3) {
            Log ("WARNING: IB Gateway/TWS not detected (check {0}/3 before fail-closed)" -f $script:gatewayDownCount)
        }
    } else {
        if ($script:gatewayWarnSent) {
            Log "IB Gateway/TWS process restored"
            # Remove PAUSE_ENTRIES only if we created it
            $pauseFile = Join-Path "C:\Argus\repo" "PAUSE_ENTRIES"
            if (Test-Path $pauseFile) {
                $content = Get-Content $pauseFile -Raw -ErrorAction SilentlyContinue
                if ($content -match "gateway_supervision") {
                    Remove-Item $pauseFile -Force
                    Log "PAUSE_ENTRIES removed (gateway restored)"
                }
            }
            Send-Discord "IB Gateway/TWS is **BACK**. Entries resumed." "green"
            $script:gatewayWarnSent = $false
        }
        $script:gatewayDownCount = 0
    }

    $watchedConfigs = @($paperState.Configs + $realState.Configs | Select-Object -Unique)
    $hbDirs = Get-WatchedLogDirs $watchedConfigs
    $staleCount = 0
    $freshCount = 0
    $missingCount = 0
    foreach ($dir in $hbDirs) {
        $hbFile = Join-Path $dir "heartbeat.json"
        if (Test-Path $hbFile) {
            try {
                $hb = Get-Content $hbFile -Raw | ConvertFrom-Json
                $hbTime = [DateTimeOffset]::Parse($hb.ts).UtcDateTime
                $hbAge = ((Get-Date).ToUniversalTime() - $hbTime).TotalSeconds
                if ($hbAge -lt $staleThresholdSeconds) { $freshCount++ } else { $staleCount++ }
            } catch { $staleCount++ }
        } else {
            $missingCount++
        }
    }

    $utcHour = (Get-Date).ToUniversalTime().Hour
    $dayOfWeek = (Get-Date).DayOfWeek
    $marketExpected = -not (($dayOfWeek -eq "Saturday") -or ($dayOfWeek -eq "Sunday" -and $utcHour -lt 21) -or ($dayOfWeek -eq "Friday" -and $utcHour -ge 22))

    if ($marketExpected -and $anyLaneHealthy -and $freshCount -eq 0 -and $staleCount -gt 0) {
        if (-not $script:staleWarnSent) {
            Log ("ALERT: All managed-runner heartbeats are stale ({0} stale, {1} fresh)" -f $staleCount, $freshCount)
            Send-Discord "**WARNING: all managed-runner heartbeats are stale**`nRunners appear up but no fresh heartbeat is arriving." "yellow"
            $script:staleWarnSent = $true
            $script:staleWarnTime = $now
        }
    }

    if ($freshCount -gt 0 -and $script:staleWarnSent) {
        $downMinutes = if ($script:staleWarnTime) { [int](($now - $script:staleWarnTime).TotalMinutes) } else { 0 }
        Log ("Heartbeats RESTORED ({0} fresh, {1} stale) after {2}min" -f $freshCount, $staleCount, $downMinutes)
        Send-Discord "Managed-runner heartbeats are **RESTORED** ($freshCount/$($freshCount + $staleCount) fresh)." "green"
        $script:staleWarnSent = $false
        $script:staleWarnTime = $null
    }

    if ($now.Minute % 5 -eq 0 -and $now.Second -lt 65) {
        foreach ($dir in $hbDirs) {
            $tradeFile = Join-Path $dir "trades.csv"
            if (Test-Path $tradeFile) {
                $lineCount = (Get-Content $tradeFile | Measure-Object -Line).Lines - 1
                $pair = Split-Path $dir -Leaf
                $stateKey = "trades_$pair"
                $prevCount = if ($script:tradeCounts.ContainsKey($stateKey)) { $script:tradeCounts[$stateKey] } else { $lineCount }

                if ($lineCount -gt $prevCount) {
                    $lastLine = Get-Content $tradeFile | Select-Object -Last 1
                    $fields = $lastLine -split ","
                    $pnl = if ($fields.Count -gt 4) { $fields[4] } else { "?" }
                    $direction = if ($fields.Count -gt 1) { $fields[1] } else { "?" }
                    $exitReason = if ($fields.Count -gt 5) { $fields[5] } else { "?" }

                    $pnlValue = 0.0
                    $parsed = [double]::TryParse($pnl, [ref]$pnlValue)
                    $pnlColor = if ($parsed -and $pnlValue -gt 0) { "green" } else { "red" }
                    Send-Discord "**Trade Closed:** $($pair.ToUpper()) $direction | PnL: $pnl | Exit: $exitReason" $pnlColor
                    Log "TRADE: $pair $direction pnl=$pnl exit=$exitReason"
                }
                $script:tradeCounts[$stateKey] = $lineCount
            }
        }
    }

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
                    if ($count -gt 0) { $pairSummary += "$($pair.ToUpper()): $count trades ($([math]::Round($pnl,1)))" }
                }
            }
        }
        if ($totalTrades -gt 0) {
            $color = if ($totalPnl -ge 0) { "green" } else { "red" }
            $details = $pairSummary -join "`n"
            Send-Discord "**Daily Session Summary (London Close)**`nTrades: $totalTrades | Net PnL: $([math]::Round($totalPnl,1))`n$details" $color
            Log "DAILY SUMMARY: $totalTrades trades, $([math]::Round($totalPnl,1))"
        }
        $script:dailySummarySent = $true
    }
    if ($utcHour -ne 14) { $script:dailySummarySent = $false }

    if ($now.Minute % 5 -eq 0 -and $now.Second -lt 65) {
        $paperStr = if ($paperState.Healthy) { "UP" } else { "DOWN" }
        $realStr = if ($realState.Expected) { $(if ($realState.Healthy) { "UP" } else { "DOWN" }) } else { "N/A" }
        $dashStr = if ($dashboardUp) { "UP" } else { "DOWN" }
        $discordStr = if ($discordUp) { "UP" } else { "DOWN" }
        $gwStr = if ($gatewayAlive) { "UP" } else { "DOWN" }
        Log "HEARTBEAT | Paper=$paperStr | Real=$realStr | Dashboard=$dashStr | Discord=$discordStr | Port=$portListening | Gateway=$gwStr | Fresh=$freshCount Stale=$staleCount Missing=$missingCount"
    }
}
