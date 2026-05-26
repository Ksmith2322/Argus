# start_gateway_via_ibc.ps1 -- Launch IB Gateway via IBC for unattended operation
#
# IB Gateway is the lighter-weight (~150MB vs TWS's ~1.5GB) API-only
# IBKR client. Preferred for unattended algo trading: faster restart,
# no UI to crash, designed for headless use.
#
# Use the existing ops/start_tws_via_ibc.ps1 when you need the TWS UI
# for manual position inspection or emergency flatten.
#
# Usage:
#   .\ops\start_gateway_via_ibc.ps1                   # foreground
#   .\ops\start_gateway_via_ibc.ps1 -Hidden           # background
#
# Prerequisites (one-time, see ops/IBC_GATEWAY_SETUP.md):
#   1. IB Gateway installed at C:\Jts\ibgateway\<VERSION>\
#   2. C:\IBC\config_gateway.ini exists with FIX=no, IbDir=<gateway path>,
#      OverrideTwsApiPort=4002, credentials filled in
#
# After this script runs, port 4002 listens within ~10 seconds.

param(
    [switch]$Hidden = $false,
    [string]$IbcPath = "C:\IBC",
    [string]$ConfigFile = "config_gateway.ini",
    [int]$WaitSeconds = 30
)

$ErrorActionPreference = "Stop"

Write-Host "=== IBC-managed Gateway launch ===" -ForegroundColor Cyan
Write-Host ""

# Sanity: IBC present
if (-not (Test-Path "$IbcPath\IBC.jar")) {
    Write-Host "FAIL: IBC not found at $IbcPath\IBC.jar" -ForegroundColor Red
    exit 1
}

# Sanity: Gateway config exists
$cfgPath = "$IbcPath\$ConfigFile"
if (-not (Test-Path $cfgPath)) {
    Write-Host "FAIL: $cfgPath not found." -ForegroundColor Red
    Write-Host "      Copy config.ini -> config_gateway.ini and edit per ops/IBC_GATEWAY_SETUP.md" -ForegroundColor Red
    exit 2
}

# Sanity: credentials configured (not PLACEHOLDER)
$cfg = Get-Content $cfgPath -Raw
if ($cfg -match "PLACEHOLDER_YOUR_PAPER_") {
    Write-Host "FAIL: $cfgPath still has PLACEHOLDER credential values." -ForegroundColor Red
    exit 3
}

# Sanity: config really targets Gateway (not TWS by accident)
if ($cfg -notmatch "(?im)^\s*FIX\s*=\s*no") {
    Write-Host "WARNING: $cfgPath does not contain 'FIX=no' -- verify IBC is configured for Gateway." -ForegroundColor Yellow
}
if ($cfg -notmatch "(?im)OverrideTwsApiPort\s*=\s*4002") {
    Write-Host "WARNING: $cfgPath does not pin OverrideTwsApiPort=4002. Continuing anyway." -ForegroundColor Yellow
}

# Locate the Gateway install
$gatewayDirs = Get-ChildItem "C:\Jts\ibgateway" -Directory -ErrorAction SilentlyContinue
if (-not $gatewayDirs) {
    Write-Host "FAIL: No IB Gateway install found at C:\Jts\ibgateway\<VERSION>\" -ForegroundColor Red
    Write-Host "      Install from https://www.interactivebrokers.com/en/index.php?f=16457" -ForegroundColor Red
    exit 4
}
$gatewayVersion = ($gatewayDirs | Sort-Object LastWriteTime -Descending | Select-Object -First 1).Name
$gatewayJars = "C:\Jts\ibgateway\$gatewayVersion\jars"
Write-Host "Using IB Gateway version: $gatewayVersion" -ForegroundColor Gray

# Bail if Gateway is already running -- IBC owns the lifecycle
$existing = Get-CimInstance Win32_Process -Filter "Name='ibgateway.exe'" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "WARNING: ibgateway.exe already running (PID $($existing.ProcessId))." -ForegroundColor Yellow
    Write-Host "         IBC needs to own the Gateway process lifecycle. Stop it first:" -ForegroundColor Yellow
    Write-Host "         Stop-Process -Id $($existing.ProcessId) -Force" -ForegroundColor Yellow
    exit 5
}

Write-Host "Launching IBC against Gateway..." -ForegroundColor Cyan
New-Item -Path "$IbcPath\Logs" -ItemType Directory -Force | Out-Null
$logFile = "$IbcPath\Logs\ibc_gateway_$(Get-Date -Format yyyyMMdd_HHmmss).log"

# Find the right Java executable. Gateway 1037+ requires Java 17 (class
# version 61), but the system 'java.exe' on PATH may be older. The Gateway
# installer drops a preferred-JRE path at
# C:\Jts\ibgateway\<VERSION>\.install4j\pref_jre.cfg — use that if present.
$prefJreCfg = "C:\Jts\ibgateway\$gatewayVersion\.install4j\pref_jre.cfg"
$javaExe = "java.exe"   # PATH default fallback
if (Test-Path $prefJreCfg) {
    $prefJreDir = (Get-Content $prefJreCfg -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($prefJreDir -and (Test-Path "$prefJreDir\bin\java.exe")) {
        $javaExe = "$prefJreDir\bin\java.exe"
        Write-Host "Using Gateway-bundled JRE: $prefJreDir" -ForegroundColor Gray
    } else {
        Write-Host "WARNING: pref_jre.cfg points at $prefJreDir but java.exe not found there; falling back to PATH java" -ForegroundColor Yellow
    }
} else {
    Write-Host "WARNING: no $prefJreCfg; using PATH java (may be wrong version for Gateway $gatewayVersion)" -ForegroundColor Yellow
}

# Build the classpath and run IBC's Gateway entry point
$classpath = "$IbcPath\IBC.jar;$gatewayJars\*"
$javaArgs = @(
    "-cp", $classpath,
    "ibcalpha.ibc.IbcGateway",
    $cfgPath
)

if ($Hidden) {
    $proc = Start-Process -FilePath $javaExe -ArgumentList $javaArgs `
                          -WorkingDirectory $IbcPath `
                          -WindowStyle Hidden -RedirectStandardOutput $logFile -PassThru
    Write-Host "  IBC PID=$($proc.Id) launched (hidden, log: $logFile)" -ForegroundColor Green
} else {
    $proc = Start-Process -FilePath $javaExe -ArgumentList $javaArgs `
                          -WorkingDirectory $IbcPath `
                          -WindowStyle Minimized -PassThru
    Write-Host "  IBC PID=$($proc.Id) launched (minimized)" -ForegroundColor Green
}

# Wait for port 4002 to come up
Write-Host ""
Write-Host "Waiting up to $WaitSeconds seconds for port 4002 to listen..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt $WaitSeconds; $i += 2) {
    Start-Sleep -Seconds 2
    $listening = Get-NetTCPConnection -LocalPort 4002 -State Listen -ErrorAction SilentlyContinue
    if ($listening) {
        Write-Host "  port 4002 listening (after ${i}s)" -ForegroundColor Green
        $ready = $true
        break
    }
}

if (-not $ready) {
    Write-Host "FAIL: port 4002 did not come up within $WaitSeconds seconds." -ForegroundColor Red
    Write-Host "      Check $logFile for IBC errors." -ForegroundColor Red
    exit 6
}

Write-Host ""
Write-Host "Gateway is up on port 4002. Set IBKR_PORT and launch runners:" -ForegroundColor Green
Write-Host "  `$env:IBKR_PORT='4002'" -ForegroundColor Gray
Write-Host "  C:\Argus\.venv\Scripts\python.exe -m forge.tail_hedge.runner --check" -ForegroundColor Gray
