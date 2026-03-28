# ops/watchdog.ps1 -- Monitor Argus runners and auto-restart on crash
# Monitors ALL heartbeats. Sends Discord alerts on failures.
# Updated 2026-03-28 for full fleet (8 FX + 7 futures) + Discord alerts

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

# FX runner configs (full fleet)
$fxConfigs = @(
    "argus_flow/configs/gbpusd_range_paper_v1.json",
    "argus_flow/configs/eurusd_t4_paper_v1.json",
    "argus_flow/configs/eurjpy_t4_paper_v1.json",
    "argus_flow/configs/gbpjpy_t4_paper_v1.json",
    "argus_flow/configs/cadjpy_t4_paper_v1.json",
    "argus_flow/configs/audjpy_t4_paper_v1.json",
    "argus_flow/configs/usdjpy_ny_paper_v1.json",
    "argus_flow/configs/audusd_ny_paper_v1.json"
)

# Futures runner configs
$futuresConfigs = @(
    "argus_flow/configs/mes_range_paper_v1.json",
    "argus_flow/configs/mnq_range_paper_v1.json",
    "argus_flow/configs/mym_range_paper_v1.json",
    "argus_flow/configs/m2k_range_paper_v1.json",
    "argus_flow/configs/mgc_range_paper_v1.json",
    "argus_flow/configs/mcl_range_paper_v1.json",
    "argus_flow/configs/nkd_range_paper_v1.json"
)

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

function Is-RunnerAlive($type) {
    $procs = Get-Process python* -ErrorAction SilentlyContinue
    foreach ($p in $procs) {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -match "runner_unified") {
                if ($type -eq "fx" -and $cmd -notmatch "client-id" -and $cmd -match "gbpusd.*eurusd.*eurjpy.*gbpjpy") { return $true }
                if ($type -eq "futures" -and $cmd -match "client-id 2") { return $true }
            }
        } catch {}
    }
    return $false
}

function Restart-Runner($type) {
    if ($type -eq "fx") {
        $argsList = @("-m", "argus_flow.runner_unified", "--configs") + $fxConfigs
        Log "Restarting FX runner (8 pairs)..."
    } else {
        $argsList = @("-m", "argus_flow.runner_unified", "--client-id", "2", "--configs") + $futuresConfigs
        Log "Restarting Futures runner (7 instruments)..."
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
        Log "ALERT: FX runner NOT FOUND!"
        Send-Discord "**FX runner is DOWN!** Attempting restart..." "red"
        if ($restartTimestamps.Count -lt $maxRestartsPerHour) {
            Restart-Runner "fx"
            $restartTimestamps += $now
        } else {
            Log "MAX RESTARTS reached ($maxRestartsPerHour/hour). Not restarting FX."
            Send-Discord "**CRITICAL: FX runner down, max restarts ($maxRestartsPerHour/hr) exhausted!** Manual intervention needed." "red"
        }
    }

    # Futures runner check
    if (-not $futuresAlive) {
        Log "ALERT: Futures runner NOT FOUND!"
        if ($restartTimestamps.Count -lt $maxRestartsPerHour) {
            Restart-Runner "futures"
            $restartTimestamps += $now
        } else {
            Log "MAX RESTARTS reached. Not restarting Futures."
        }
    }

    # Periodic heartbeat log (every 5 min)
    if ($now.Minute % 5 -eq 0 -and $now.Second -lt 65) {
        $fxStr = if ($fxAlive) { "UP" } else { "DOWN" }
        $futStr = if ($futuresAlive) { "UP" } else { "DOWN" }
        Log "HEARTBEAT | FX=$fxStr | Futures=$futStr"
    }
}
