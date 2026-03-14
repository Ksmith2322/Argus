# ops/autostart_dashboard.ps1 — Launch Argus dashboard on login
# Place a shortcut to this script in the Windows Startup folder.
$ErrorActionPreference = "Stop"
Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1
Start-Process -FilePath "C:\Argus\.venv\Scripts\python.exe" -ArgumentList "ops/dashboard.py","--port","8080" -WindowStyle Hidden