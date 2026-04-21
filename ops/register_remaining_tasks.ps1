# Register remaining scheduled tasks (2026-04-20 session hand-off).
#
# MUST BE RUN FROM AN ELEVATED POWERSHELL.
# The Claude Code sandbox correctly blocks privilege-escalation / persistence
# changes, so these three commands were parked for manual execution.
#
# Right-click PowerShell -> Run as Administrator, then:
#   cd C:\Argus\repo
#   .\ops\register_remaining_tasks.ps1
#
# Safe to re-run — each uses /F to overwrite if the task already exists.

$ErrorActionPreference = "Stop"

# 1) GLD PM Long: runs the runner's --loop mode so it wakes at signal hours
#    18/19/20 UTC per the runner's internal schedule. ONLOGON trigger means
#    it starts when you log in; no stored password needed.
Write-Host "Registering ArgusGldPmLoop..."
schtasks.exe /create /TN "ArgusGldPmLoop" `
  /TR "C:\Argus\.venv\Scripts\python.exe -m forge.gld_pm_long.runner --loop" `
  /SC ONLOGON /RL LIMITED /F

# 2) Watchdog: auto-restart runner_unified if it dies. ONLOGON so it's up
#    whenever the session is. For always-on, change /SC to ONSTART once
#    you're ready to grant Run-Whether-User-Logged-On-Or-Not (needs password).
Write-Host "Registering ArgusWatchdog..."
schtasks.exe /create /TN "ArgusWatchdog" `
  /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\watchdog.ps1" `
  /SC ONLOGON /RL HIGHEST /F

# 3) ArgusCohortReport: current setup is "Interactive only" so it fails with
#    ERROR_NO_SUCH_LOGON_SESSION (-2147020576) when the RDP session is
#    disconnected. Flip to "Run whether user is logged on or not." Requires
#    your Windows password — prompt is interactive below.
Write-Host "Re-registering ArgusCohortReport (will prompt for password)..."
$user = "$env:USERDOMAIN\$env:USERNAME"
schtasks.exe /change /TN "ArgusCohortReport" /RU $user /RP *

Write-Host ""
Write-Host "Done. Verify with:"
Write-Host "  schtasks /query /TN ArgusGldPmLoop /FO LIST /V | Select-String 'Status|Next Run|Last Run|Last Result'"
Write-Host "  schtasks /query /TN ArgusWatchdog /FO LIST /V | Select-String 'Status|Next Run|Last Run|Last Result'"
Write-Host "  schtasks /query /TN ArgusCohortReport /FO LIST /V | Select-String 'Status|Next Run|Last Run|Last Result|Logon Mode'"
