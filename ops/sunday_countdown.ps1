# ops/sunday_countdown.ps1 -- Sunday pre-market countdown alerts
# Checks system health at 30/15/5 minutes before FX market open
# Market opens Sunday 5 PM ET = 4 PM CT
# Schedule this at 3:30 PM CT on Sundays

$ErrorActionPreference = "Continue"
Set-Location "C:\Argus\repo"

$envFile = "C:\Argus\repo\.env"
$logFile = "C:\Argus\repo\argus_flow\logs\sunday_countdown.log"

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
        $colorInt = switch ($color) { "red" { 16711680 } "yellow" { 16776960 } "green" { 65280 } "blue" { 3447003 } default { 8421504 } }
        $body = @{ embeds = @(@{ title = "Argus Sunday Countdown"; description = $message; color = $colorInt; timestamp = (Get-Date -Format "o") }) } | ConvertTo-Json -Depth 5
        Invoke-RestMethod -Uri $discordWebhook -Method Post -ContentType "application/json" -Body $body -TimeoutSec 10 | Out-Null
    } catch { Log "Discord failed: $_" }
}

function Check-Health {
    $issues = @()

    # TWS running?
    $tws = Get-Process -Name "tws" -ErrorAction SilentlyContinue
    if (-not $tws) { $issues += "TWS NOT RUNNING" }

    # API port?
    $portOk = $false
    try {
        $conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 7496 -WarningAction SilentlyContinue
        $portOk = $conn.TcpTestSucceeded
    } catch {}
    if (-not $portOk) { $issues += "API port 7496 NOT LISTENING" }

    # Runners alive?
    $runners = Get-Process python* -ErrorAction SilentlyContinue | Where-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            $cmd -match "runner_unified"
        } catch { $false }
    }
    $runnerCount = if ($runners) { $runners.Count } else { 0 }
    if ($runnerCount -lt 2) { $issues += "Only $runnerCount runner processes (need 2+)" }

    return @{
        TWS = [bool]$tws
        Port = $portOk
        Runners = $runnerCount
        Issues = $issues
        Healthy = ($issues.Count -eq 0)
    }
}

Log "=========================================="
Log "SUNDAY COUNTDOWN STARTED"
Log "=========================================="

# T-30 minutes
$h = Check-Health
$statusIcon = if ($h.Healthy) { "OK" } else { "ISSUES FOUND" }
Log "T-30: TWS=$($h.TWS) Port=$($h.Port) Runners=$($h.Runners) Status=$statusIcon"

if ($h.Healthy) {
    Send-Discord "**T-30 min to market open**`nTWS: Running`nAPI: Listening`nRunners: $($h.Runners) active`nStatus: ALL SYSTEMS GO" "blue"
} else {
    $issueList = $h.Issues -join "`n- "
    Send-Discord "**T-30 min to market open - ISSUES DETECTED**`n- $issueList`n`n**Action needed before market opens!**" "red"

    # Try to fix: run premarket check
    Log "Running premarket_check to attempt fix..."
    & powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File "C:\Argus\repo\ops\premarket_check.ps1"
}

# Wait 15 minutes
Start-Sleep -Seconds 900

# T-15 minutes
$h = Check-Health
$statusIcon = if ($h.Healthy) { "OK" } else { "ISSUES" }
Log "T-15: TWS=$($h.TWS) Port=$($h.Port) Runners=$($h.Runners) Status=$statusIcon"

if ($h.Healthy) {
    Send-Discord "**T-15 min to market open**`nAll systems healthy. Ready to trade." "green"
} else {
    $issueList = $h.Issues -join "`n- "
    Send-Discord "**T-15 min - STILL HAVE ISSUES**`n- $issueList`n`n**Manual intervention may be needed!**" "red"
}

# Wait 10 minutes
Start-Sleep -Seconds 600

# T-5 minutes
$h = Check-Health
Log "T-5: TWS=$($h.TWS) Port=$($h.Port) Runners=$($h.Runners)"

if ($h.Healthy) {
    Send-Discord "**T-5 min to market open**`nAll green. Argus is ready." "green"
} else {
    $issueList = $h.Issues -join "`n- "
    Send-Discord "**T-5 min - CRITICAL: NOT READY FOR MARKET OPEN**`n- $issueList" "red"
}

Log "Sunday countdown complete"
