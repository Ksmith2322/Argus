# start_tws_via_ibc.ps1 -- Launch TWS via IBC for unattended operation
#
# Replaces the manual "open TWS + click disclaimer" cycle. IBC handles:
#   - Login (credentials in C:\IBC\config.ini)
#   - Disclaimer auto-accept
#   - Daily auto-restart at 23:55 (handles IBKR's nightly server reset)
#   - Reconnect on connection drop
#
# Usage:
#   .\ops\start_tws_via_ibc.ps1                   # foreground, blocks until TWS exits
#   .\ops\start_tws_via_ibc.ps1 -Hidden           # background, doesn't block
#
# Prerequisites (one-time, see ops/IBC_SETUP.md):
#   1. C:\IBC exists with IBC 3.23.0 extracted
#   2. C:\IBC\config.ini has IbLoginId + IbPassword set (NOT the PLACEHOLDER values)
#   3. C:\Jts\tws.exe exists (standard IBKR TWS install)
#
# After this script runs, port 7497 should be listening within 60 seconds.
# Use ops/preflight_paper_stress.py to verify the runner can connect.

param(
    [switch]$Hidden = $false,
    [string]$TwsPath = "C:\Jts",
    [string]$IbcPath = "C:\IBC",
    [int]$WaitSeconds = 60
)

$ErrorActionPreference = "Stop"

Write-Host "=== IBC-managed TWS launch ===" -ForegroundColor Cyan
Write-Host ""

# Sanity: TWS install present
if (-not (Test-Path "$TwsPath\tws.exe")) {
    Write-Host "FAIL: TWS not found at $TwsPath\tws.exe" -ForegroundColor Red
    exit 1
}

# Sanity: IBC install present
if (-not (Test-Path "$IbcPath\IBC.jar")) {
    Write-Host "FAIL: IBC not found at $IbcPath\IBC.jar" -ForegroundColor Red
    exit 1
}

# Sanity: credentials configured (not PLACEHOLDER)
$cfg = Get-Content "$IbcPath\config.ini" -Raw
if ($cfg -match "PLACEHOLDER_YOUR_PAPER_") {
    Write-Host "FAIL: $IbcPath\config.ini still has PLACEHOLDER values." -ForegroundColor Red
    Write-Host "      Edit IbLoginId and IbPassword to your paper-account credentials before running." -ForegroundColor Red
    Write-Host "      See ops/IBC_SETUP.md for guidance." -ForegroundColor Red
    exit 2
}

# Bail if TWS is already running -- IBC owns the launch
$existing = Get-CimInstance Win32_Process -Filter "Name='tws.exe'" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "WARNING: tws.exe already running (PID $($existing.ProcessId))." -ForegroundColor Yellow
    Write-Host "         IBC needs to own the TWS process lifecycle. Stop it first:" -ForegroundColor Yellow
    Write-Host "         Stop-Process -Id $($existing.ProcessId) -Force" -ForegroundColor Yellow
    exit 3
}

Write-Host "Launching IBC..." -ForegroundColor Cyan
$logFile = "$IbcPath\Logs\ibc_$(Get-Date -Format yyyyMMdd_HHmmss).log"
New-Item -Path "$IbcPath\Logs" -ItemType Directory -Force | Out-Null

# IBC's StartTWS.bat handles all the JVM args + classpath
$startBat = "$IbcPath\StartTWS.bat"
if ($Hidden) {
    $proc = Start-Process -FilePath $startBat -WorkingDirectory $IbcPath `
                          -WindowStyle Hidden -RedirectStandardOutput $logFile -PassThru
    Write-Host "  IBC PID=$($proc.Id) launched (hidden, log: $logFile)" -ForegroundColor Green
} else {
    $proc = Start-Process -FilePath $startBat -WorkingDirectory $IbcPath `
                          -WindowStyle Minimized -PassThru
    Write-Host "  IBC PID=$($proc.Id) launched (minimized)" -ForegroundColor Green
}

# Wait for port 7497 to come up
Write-Host ""
Write-Host "Waiting up to $WaitSeconds seconds for port 7497 to listen..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt $WaitSeconds; $i += 2) {
    Start-Sleep -Seconds 2
    $listening = Get-NetTCPConnection -LocalPort 7497 -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "  port 7497 listening (after ${i}s)" -ForegroundColor Green
        $ready = $true
        break
    }
}

if (-not $ready) {
    Write-Host "FAIL: port 7497 did not come up within $WaitSeconds seconds." -ForegroundColor Red
    Write-Host "      Check $logFile for IBC errors." -ForegroundColor Red
    exit 4
}

Write-Host ""
Write-Host "TWS is up. Run the preflight to verify API readiness:" -ForegroundColor Green
Write-Host "  C:\Argus\.venv\Scripts\python.exe -m ops.preflight_paper_stress" -ForegroundColor Gray
