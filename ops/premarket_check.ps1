# ops/premarket_check.ps1 -- Pre-market health check and runner restart
# Runs before market opens to ensure runners are connected and healthy.
# Schedule: Sunday 8:30 PM ET (00:30 UTC Mon) and daily 7:30 AM CT (12:30 UTC)

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$logFile = "C:\Argus\repo\argus_flow\logs\premarket_check.log"
$envFile = "C:\Argus\repo\.env"

# Load Discord webhook
$discordWebhook = ""
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match "^DISCORD_WEBHOOK_URL=(.+)$") {
            $discordWebhook = $Matches[1].Trim()
        }
    }
}

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

function Send-Discord($message, $color) {
    if (-not $discordWebhook) { return }
    try {
        $colorInt = switch ($color) { "red" { 16711680 } "yellow" { 16776960 } "green" { 65280 } default { 8421504 } }
        $body = @{ embeds = @(@{ title = "Argus Pre-Market Check"; description = $message; color = $colorInt; timestamp = (Get-Date -Format "o") }) } | ConvertTo-Json -Depth 5
        Invoke-RestMethod -Uri $discordWebhook -Method Post -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
    } catch { Log "Discord failed: $_" }
}

# --- Load configs from deployment pipeline (source of truth) ---
$fxConfigs = @()
$futuresConfigs = @()
$pipelineOk = $false

try {
    $allConfigs = @(& $python -m argus_flow.ops.deployment_pipeline --emit-configs watcher,paper 2>$null)
    if ($LASTEXITCODE -eq 0 -and $allConfigs.Count -gt 0) {
        foreach ($cfg in $allConfigs) {
            $cfgPath = $cfg.Trim()
            if (-not $cfgPath) { continue }
            try {
                $parsed = Get-Content $cfgPath -Raw -ErrorAction Stop | ConvertFrom-Json
                $itype = [string]$parsed.instrument_type
            } catch {
                $itype = "unknown"
            }
            if ($itype -eq "future") {
                $futuresConfigs += $cfgPath
            } else {
                $fxConfigs += $cfgPath
            }
        }
        $pipelineOk = $true
        Log "Loaded configs from deployment pipeline: $($fxConfigs.Count) FX, $($futuresConfigs.Count) futures"
    } else {
        Log "WARNING: deployment_pipeline --emit-configs returned no configs (exit=$LASTEXITCODE)"
    }
} catch {
    Log "WARNING: deployment_pipeline --emit-configs failed: $_"
}

if (-not $pipelineOk) {
    # Fallback: read deployment_registry.json directly (same source, no Python needed)
    $registryPath = "C:\Argus\repo\argus_flow\logs\deployment_registry.json"
    if (Test-Path $registryPath) {
        try {
            $registry = Get-Content $registryPath -Raw | ConvertFrom-Json
            $allFromRegistry = @($registry.launcher.paper_configs)
            if ($allFromRegistry.Count -gt 0) {
                foreach ($cfgPath in $allFromRegistry) {
                    $cfgPath = $cfgPath.Trim()
                    if (-not $cfgPath) { continue }
                    try {
                        $parsed = Get-Content $cfgPath -Raw -ErrorAction Stop | ConvertFrom-Json
                        $itype = [string]$parsed.instrument_type
                    } catch {
                        $itype = "unknown"
                    }
                    if ($itype -eq "future") {
                        $futuresConfigs += $cfgPath
                    } else {
                        $fxConfigs += $cfgPath
                    }
                }
                $pipelineOk = $true
                Log "Loaded configs from deployment_registry.json fallback: $($fxConfigs.Count) FX, $($futuresConfigs.Count) futures"
            } else {
                Log "WARNING: deployment_registry.json has no paper_configs"
            }
        } catch {
            Log "WARNING: Failed to parse deployment_registry.json: $_"
        }
    } else {
        Log "WARNING: deployment_registry.json not found at $registryPath"
    }
}

if (-not $pipelineOk) {
    Log "CRITICAL: No config source available (pipeline failed, registry missing). Cannot start runners."
    Send-Discord "**CRITICAL: Pre-market check ABORTED.** No config source available. Run deployment_pipeline manually." "red"
    exit 1
}

Log "=========================================="
Log "PRE-MARKET HEALTH CHECK"
Log "=========================================="

# Step 1: Check if TWS/Gateway is running
$twsRunning = Get-Process -Name "tws" -ErrorAction SilentlyContinue
if (-not $twsRunning) {
    Log "CRITICAL: TWS is NOT running!"
    Send-Discord "**CRITICAL: TWS is not running!** Cannot trade. Start TWS manually." "red"
    exit 1
}
Log "TWS process: RUNNING"

# Step 2: Check if API port 7496 is listening
$portListening = $false
try {
    $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -WarningAction SilentlyContinue
    $portListening = $conn.TcpTestSucceeded
} catch {}

if (-not $portListening) {
    Log "WARNING: Port 7496 not listening. API may be disabled in TWS."
    Send-Discord "**WARNING: IBKR API port 7496 not responding.** Check TWS API settings (Enable ActiveX and Socket Clients)." "yellow"
}
Log "API port 7496: $(if ($portListening) { 'LISTENING' } else { 'NOT LISTENING' })"

# Step 3: Kill any stale runner processes
$runners = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}

if ($runners) {
    Log "Found $($runners.Count) existing runner processes. Killing for fresh start..."
    foreach ($r in $runners) {
        try { Stop-Process -Id $r.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    Start-Sleep -Seconds 3
}

# Step 4: Start fresh runners
$expectedRunners = 0
if ($fxConfigs.Count -gt 0) {
    Log "Starting FX runner ($($fxConfigs.Count) pairs)..."
    $fxArgs = @("-m", "argus_flow.runner_unified", "--configs") + $fxConfigs
    Start-Process -FilePath $python -ArgumentList $fxArgs -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    $expectedRunners++
    Start-Sleep -Seconds 10
} else {
    Log "No FX configs found. Skipping FX runner."
}

if ($futuresConfigs.Count -gt 0) {
    Log "Starting Futures runner ($($futuresConfigs.Count) instruments)..."
    $futArgs = @("-m", "argus_flow.runner_unified", "--client-id", "2", "--configs") + $futuresConfigs
    Start-Process -FilePath $python -ArgumentList $futArgs -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
    $expectedRunners++
    Start-Sleep -Seconds 10
} else {
    Log "No Futures configs found. Skipping Futures runner."
}

# Step 5: Verify runners started
$newRunners = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}

$runnerCount = if ($newRunners) { $newRunners.Count } else { 0 }
Log "Runners started: $runnerCount processes"

if ($runnerCount -ge $expectedRunners -and $expectedRunners -gt 0) {
    Log "PRE-MARKET CHECK: PASS"
    Send-Discord "Pre-market check PASSED. $runnerCount runner processes active. API port: $(if ($portListening) { 'OK' } else { 'CHECK NEEDED' })." "green"
} else {
    Log "PRE-MARKET CHECK: FAIL - only $runnerCount runners"
    Send-Discord "**Pre-market check FAILED!** Only $runnerCount runner processes started. Manual check required." "red"
}

Log "=========================================="
