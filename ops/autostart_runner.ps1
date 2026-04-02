# ops/autostart_runner.ps1
# Launches runner_unified.py (IBKR FX) if not already running.
# Intended for Task Scheduler "At startup" trigger or Startup folder.
# Waits 30s after boot for network/TWS to stabilize.

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"
$logFile = "C:\Argus\repo\argus_flow\logs\autostart_$((Get-Date).ToString('yyyyMMdd_HHmmss')).log"
$python = "C:\Argus\.venv\Scripts\python.exe"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
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
    $lockFiles = Get-ChildItem "C:\Argus\repo\argus_flow\logs\_locks" -Filter "runner_*.json" -ErrorAction SilentlyContinue
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

function Get-ManagedPaperRunnerProcesses() {
    $results = @()
    foreach ($runner in Get-AliveRunnerLocks) {
        $isReal = @($runner.Configs | Where-Object { $_ -match "live_v1" }).Count -gt 0
        if ($isReal) { continue }
        $results += $runner
    }
    return @($results)
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

function Build-ConfigGroups([string[]]$configs) {
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
    if ($fx.Count -gt 0) { $groups += [pscustomobject]@{ Label = "paper-fx"; Configs = @($fx) } }
    if ($futures.Count -gt 0) { $groups += [pscustomobject]@{ Label = "paper-futures"; Configs = @($futures) } }
    if ($other.Count -gt 0) { $groups += [pscustomobject]@{ Label = "paper-other"; Configs = @($other) } }
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

function Stop-RunnerProcesses($procs) {
    foreach ($proc in @($procs)) {
        try {
            Log "Stopping stale/mismatched paper runner PID=$($proc.Id)"
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        } catch {
            Log "WARNING: Failed to stop runner PID=$($proc.Id): $_"
        }
    }
    Start-Sleep -Seconds 2
}

Log "=== Runner auto-start check ==="

# Wait for system + TWS to stabilize after boot
Start-Sleep -Seconds 30

# Check TWS process is running
$twsRunning = Get-Process -Name "tws" -ErrorAction SilentlyContinue
if (-not $twsRunning) {
    Log "CRITICAL: TWS is NOT running. Runners would fail immediately. Exiting."
    exit 1
}
Log "TWS process: RUNNING"

# Check API port 7496 is listening
$portListening = $false
try {
    $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -WarningAction SilentlyContinue
    $portListening = $conn.TcpTestSucceeded
} catch {}

if (-not $portListening) {
    Log "WARNING: IBKR API port 7496 not listening. Runners would fail to connect. Exiting."
    exit 1
}
Log "API port 7496: LISTENING"

Log "Refreshing managed truth surfaces..."
& $python -m argus_flow.ops.refresh_managed_truth --accept-existing-age-s 600 2>&1 | ForEach-Object { Log "refresh_managed_truth> $_" }
if ($LASTEXITCODE -ne 0) {
    Log "ERROR: managed truth refresh failed (rc=$LASTEXITCODE). Aborting autostart."
    exit 1
}

Log "Building deployment-aware watcher/paper config list..."
$configs = @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs watcher,paper 2>$null)
if ($configs.Count -eq 0) {
    Log "No managed watcher/paper configs found. Nothing to launch."
    exit 0
}
$groups = Build-ConfigGroups $configs

$existing = Get-ManagedPaperRunnerProcesses
if (Test-RunnerMatchesGroups $existing $groups) {
    Log "Managed paper runner already matches desired config groups. Skipping."
    exit 0
}

if ($existing.Count -gt 0) {
    Log "Existing paper runner does not match desired managed config groups. Restart required."
    Stop-RunnerProcesses $existing
}

foreach ($group in $groups) {
    Log "Starting managed group '$($group.Label)' with $($group.Configs.Count) config(s)..."
    Start-Process -FilePath $python `
        -ArgumentList (@("-m", "argus_flow.runner_unified", "--configs") + $group.Configs) `
        -WorkingDirectory "C:\Argus\repo" `
        -WindowStyle Hidden
    Start-Sleep -Seconds 4
}

Start-Sleep -Seconds 10

# Verify
$newRunner = Get-ManagedPaperRunnerProcesses

if (Test-RunnerMatchesGroups $newRunner $groups) {
    Log "Managed paper runner started with $($newRunner.Count) process group(s)"
} else {
    Log "WARNING: Runner may not have started. Check manually."
}

Log "=== Done ==="
