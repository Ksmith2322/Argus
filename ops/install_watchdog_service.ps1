# ops/install_watchdog_service.ps1 -- Register ops/watchdog.ps1 as a Windows service via NSSM.
#
# Why: the existing ArgusWatchdog scheduled task only starts on boot. If the
# watchdog process itself dies mid-session, nothing restarts it. A Windows
# service recovers on failure automatically (SCM restart-on-crash).
#
# Prereq: install NSSM (https://nssm.cc/download). Drop nssm.exe somewhere
# on PATH, or pass -NssmPath. This script will error out early if it can't
# find nssm.
#
# Usage from an elevated PowerShell:
#   .\ops\install_watchdog_service.ps1 -Credential (Get-Credential)
#
# Idempotent: removes any existing ArgusWatchdogSvc service first, then
# re-creates. Also unregisters the legacy ArgusWatchdog scheduled task so
# there aren't two supervisors fighting.
param(
    [System.Management.Automation.PSCredential]$Credential,
    [string]$NssmPath = ""
)

$ErrorActionPreference = "Stop"

$svcName     = "ArgusWatchdogSvc"
$scriptPath  = "C:\Argus\repo\ops\watchdog.ps1"
$workingDir  = "C:\Argus\repo"
$logFile     = "C:\Argus\repo\argus_flow\logs\watchdog_service.log"
$legacyTask  = "ArgusWatchdog"

# 1. Locate nssm
if (-not $NssmPath) {
    $cmd = Get-Command nssm -ErrorAction SilentlyContinue
    if ($cmd) { $NssmPath = $cmd.Source }
}
if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    Write-Host "ERROR: nssm.exe not found." -ForegroundColor Red
    Write-Host "  Download from https://nssm.cc/download, place nssm.exe somewhere on PATH," -ForegroundColor Yellow
    Write-Host "  or rerun with -NssmPath 'C:\tools\nssm.exe'." -ForegroundColor Yellow
    exit 1
}
Write-Host "Using nssm: $NssmPath" -ForegroundColor Cyan

# 2. Guard: must be elevated
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$prin = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $prin.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: must run from an elevated (Admin) PowerShell." -ForegroundColor Red
    exit 1
}

# 3. Remove existing service (idempotent)
$existing = & sc.exe query $svcName 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing service $svcName..." -ForegroundColor Yellow
    & $NssmPath stop $svcName confirm | Out-Null
    Start-Sleep -Seconds 2
    & $NssmPath remove $svcName confirm | Out-Null
    Start-Sleep -Seconds 1
}

# 4. Install service
$psExe = (Get-Command powershell.exe).Source
$psArgs = "-NonInteractive -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""

Write-Host "Installing service $svcName..." -ForegroundColor Cyan
& $NssmPath install $svcName $psExe $psArgs
if ($LASTEXITCODE -ne 0) { Write-Host "nssm install failed ($LASTEXITCODE)" -ForegroundColor Red; exit 1 }

& $NssmPath set $svcName AppDirectory $workingDir
& $NssmPath set $svcName DisplayName "Argus Watchdog Service"
& $NssmPath set $svcName Description "Monitors Argus runners and auto-restarts on crash. Replaces ArgusWatchdog scheduled task."
& $NssmPath set $svcName Start SERVICE_AUTO_START
& $NssmPath set $svcName AppStdout $logFile
& $NssmPath set $svcName AppStderr $logFile
& $NssmPath set $svcName AppRotateFiles 1
& $NssmPath set $svcName AppRotateBytes 10485760   # 10 MB
& $NssmPath set $svcName AppExit Default Restart
& $NssmPath set $svcName AppRestartDelay 5000      # 5s delay before restart-on-crash

# Credentials: if provided, run as user; else LocalSystem.
# User-scoped is preferred so the watchdog can see user-profile state
# (venv, configs under the user's home if any). LocalSystem works but
# has no network creds and no user profile.
if ($Credential) {
    $user = $Credential.UserName
    $pass = $Credential.GetNetworkCredential().Password
    & $NssmPath set $svcName ObjectName $user $pass
    Write-Host "Service will run as $user" -ForegroundColor Green
} else {
    Write-Host "Service will run as LocalSystem (pass -Credential for user-scoped)" -ForegroundColor DarkYellow
}

# 5. Disable the legacy scheduled task (prevent double-supervisor)
$taskExists = schtasks /Query /TN $legacyTask 2>&1 | Select-String "SUCCESS|$legacyTask"
if ($taskExists) {
    Write-Host "Disabling legacy scheduled task $legacyTask..." -ForegroundColor Yellow
    schtasks /Change /TN $legacyTask /DISABLE | Out-Null
}

# 6. Start
Write-Host "Starting service..." -ForegroundColor Cyan
& $NssmPath start $svcName
Start-Sleep -Seconds 3

& sc.exe query $svcName
Write-Host ""
Write-Host "=== Install complete ===" -ForegroundColor Green
Write-Host "Verify:  Get-Service $svcName" -ForegroundColor Yellow
Write-Host "Tail:    Get-Content '$logFile' -Tail 50 -Wait" -ForegroundColor Yellow
Write-Host "Remove:  & '$NssmPath' remove $svcName confirm" -ForegroundColor Yellow
