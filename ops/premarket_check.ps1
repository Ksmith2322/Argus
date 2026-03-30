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
$futuresConfigs = @(
    "argus_flow/configs/mes_range_paper_v1.json",
    "argus_flow/configs/mnq_range_paper_v1.json",
    "argus_flow/configs/mym_range_paper_v1.json",
    "argus_flow/configs/m2k_range_paper_v1.json",
    "argus_flow/configs/mgc_range_paper_v1.json",
    "argus_flow/configs/mcl_range_paper_v1.json"
    # NKD KILLED 2026-03-29
)

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
Log "Starting FX runner (8 pairs)..."
$fxArgs = @("-m", "argus_flow.runner_unified", "--configs") + $fxConfigs
Start-Process -FilePath $python -ArgumentList $fxArgs -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
Start-Sleep -Seconds 10

Log "Starting Futures runner (7 instruments)..."
$futArgs = @("-m", "argus_flow.runner_unified", "--client-id", "2", "--configs") + $futuresConfigs
Start-Process -FilePath $python -ArgumentList $futArgs -WorkingDirectory "C:\Argus\repo" -WindowStyle Hidden
Start-Sleep -Seconds 10

# Step 5: Verify runners started
$newRunners = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
        $cmd -match "runner_unified"
    } catch { $false }
}

$runnerCount = if ($newRunners) { $newRunners.Count } else { 0 }
Log "Runners started: $runnerCount processes"

if ($runnerCount -ge 2) {
    Log "PRE-MARKET CHECK: PASS"
    Send-Discord "Pre-market check PASSED. $runnerCount runner processes active. API port: $(if ($portListening) { 'OK' } else { 'CHECK NEEDED' })." "green"
} else {
    Log "PRE-MARKET CHECK: FAIL - only $runnerCount runners"
    Send-Discord "**Pre-market check FAILED!** Only $runnerCount runner processes started. Manual check required." "red"
}

Log "=========================================="
