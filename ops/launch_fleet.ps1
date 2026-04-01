# ops/launch_fleet.ps1 -- Launch entire Argus IBKR FX fleet
# Starts: runner_unified.py, dashboard, discord trade watcher
# Usage: .\ops\launch_fleet.ps1
#        .\ops\launch_fleet.ps1 -SkipDashboard
#        .\ops\launch_fleet.ps1 -SkipDiscord

param(
    [switch]$SkipDashboard,
    [switch]$SkipDiscord
)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = "C:\Argus\repo\argus_flow\logs"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path "$logDir\fleet_launch.log" -Value $line -ErrorAction SilentlyContinue
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

function Get-AliveRunnerLocks() {
    $results = @()
    $lockFiles = Get-ChildItem "$logDir\\_locks" -Filter "runner_*.json" -ErrorAction SilentlyContinue
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
    return $procs
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
        return $false
    }
}

function Test-DiscordWatcherAlive() {
    $hb = "$logDir\\discord_watcher_heartbeat.json"
    if (-not (Test-Path $hb)) { return $false }
    try {
        $age = (Get-Date) - (Get-Item $hb).LastWriteTime
        return $age.TotalSeconds -lt 180
    } catch {
        return $false
    }
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
    if ($fx.Count -gt 0) {
        $groups += [pscustomobject]@{ Label = "$labelPrefix-fx"; Configs = @($fx) }
    }
    if ($futures.Count -gt 0) {
        $groups += [pscustomobject]@{ Label = "$labelPrefix-futures"; Configs = @($futures) }
    }
    if ($other.Count -gt 0) {
        $groups += [pscustomobject]@{ Label = "$labelPrefix-other"; Configs = @($other) }
    }
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

function Start-RunnerGroups([string]$label, $groups, [string[]]$extraArgs = @(), [switch]$RealLane) {
    if ($groups.Count -eq 0) {
        Log "No $label configs ready. Skipping."
        return
    }
    foreach ($group in $groups) {
        Log "Starting $label group '$($group.Label)' with $($group.Configs.Count) config(s)..."
        $args = @("-m", "argus_flow.runner_unified") + $extraArgs + @("--configs") + $group.Configs
        Start-Process -FilePath $python `
            -ArgumentList $args `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 4
    }

    Start-Sleep -Seconds 8
    $check = Get-ManagedRunnerProcesses -RealLane:$RealLane
    if (Test-RunnerMatchesGroups $check $groups) {
        Log "$label started with $($check.Count) managed process(es)."
    } else {
        Log "WARNING: $label may not have started with the desired config groups."
    }
}

function Stop-RunnerProcesses($procs, [string]$label) {
    foreach ($proc in @($procs)) {
        try {
            Log "Stopping $label PID=$($proc.Id)"
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        } catch {
            Log "WARNING: Failed to stop $label PID=$($proc.Id): $_"
        }
    }
    Start-Sleep -Seconds 2
}

Log "=== Fleet Launch ==="

# 1. Refresh canonical governance/oversight truth before any stage decisions
Log "Refreshing managed truth surfaces..."
& $python -m argus_flow.ops.refresh_managed_truth --accept-existing-age-s 600 2>&1 | ForEach-Object { Log "refresh_managed_truth> $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: managed truth refresh failed (rc=$LASTEXITCODE). Aborting fleet launch."
    throw "managed truth refresh failed"
}

# 2. Refresh deployment registry and stage-aware config lists
$paperConfigs = @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs watcher,paper 2>$null)
$realConfigs = @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs real 2>$null)
$paperGroups = Build-ConfigGroups $paperConfigs "paper"
$realGroups = Build-ConfigGroups $realConfigs "real"

# 3. Managed paper runner (watcher + paper QA)
$paperRunner = Get-ManagedRunnerProcesses
if (Test-RunnerMatchesGroups $paperRunner $paperGroups) {
    Log "Paper/watcher runner already matches managed config groups. Skipping."
} else {
    if ($paperRunner.Count -gt 0) {
        Log "Paper/watcher runner config drift detected. Expected $($paperGroups.Count) process group(s), found $($paperRunner.Count) process(es). Restarting."
        Stop-RunnerProcesses $paperRunner "paper/watcher runner"
    }
    Start-RunnerGroups "paper/watcher runner" $paperGroups
}

# 4. Managed real-money runner (only if live configs exist)
$realRunner = Get-ManagedRunnerProcesses -RealLane
if (Test-RunnerMatchesGroups $realRunner $realGroups) {
    if ($realRunner.Count -gt 0) {
        Log "Real-money runner already matches managed config groups. Skipping."
    } else {
        Log "No real-money configs ready. Skipping real runner launch."
    }
} else {
    if ($realRunner.Count -gt 0) {
        Log "Real-money runner config drift detected. Expected $($realGroups.Count) process group(s), found $($realRunner.Count) process(es). Restarting."
        Stop-RunnerProcesses $realRunner "real-money runner"
    }
    if ($realGroups.Count -gt 0) {
        Start-RunnerGroups "real-money runner" $realGroups @("--client-id", "41") -RealLane
    } else {
        Log "No real-money configs ready. Skipping real runner launch."
    }
}

# 5. Dashboard
if (-not $SkipDashboard) {
    if (Test-DashboardAlive) {
        Log "Dashboard already running on http://localhost:8080. Skipping."
    } else {
        Log "Starting dashboard..."
        Start-Process -FilePath $python `
            -ArgumentList "ops/dashboard.py", "--port", "8080" `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 3
        Log "Dashboard started on http://localhost:8080"
    }
}

# 6. Discord trade watcher
if (-not $SkipDiscord) {
    if (Test-DiscordWatcherAlive) {
        Log "Discord watcher already running (heartbeat fresh). Skipping."
    } else {
        Log "Starting Discord trade watcher..."
        Start-Process -FilePath $python `
            -ArgumentList "-m", "argus_flow.ops.discord_alerts", "--watch" `
            -WorkingDirectory "C:\Argus\repo" `
            -WindowStyle Hidden
        Start-Sleep -Seconds 2
        Log "Discord watcher started"
    }
}

# 7. Run smoke test
Log "Running smoke test..."
try {
    & $python -m argus_flow.ops.smoke_test 2>&1 | ForEach-Object { Log "  $_" }
} catch {
    Log "Smoke test error: $_"
}

Log "=== Fleet Launch Complete ==="
Write-Host ""
Write-Host "Fleet Status:" -ForegroundColor Cyan
Write-Host "  Paper:     $(if ((Get-ManagedRunnerProcesses).Count -gt 0) { 'RUNNING' } else { 'NOT RUNNING' })"
Write-Host "  Real:      $(if ((Get-ManagedRunnerProcesses -RealLane).Count -gt 0) { 'RUNNING' } else { 'NOT RUNNING' })"
Write-Host "  Dashboard: $(if (Test-DashboardAlive) { 'RUNNING (http://localhost:8080)' } else { 'NOT RUNNING' })"
Write-Host "  Discord:   $(if (Test-DiscordWatcherAlive) { 'RUNNING' } else { 'NOT RUNNING' })"
