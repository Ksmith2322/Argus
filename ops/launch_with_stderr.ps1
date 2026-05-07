# launch_with_stderr.ps1 — wrapper that launches a Python module with stderr
# captured to a per-day rotating log file.
#
# Why: gdx_gld dies silently every ~22:00 UTC. The runner's own runner.log
# only catches what the Python logger writes via log.error/etc. — anything
# the OS or libraries emit to stderr (segfault, asyncio error, ib_insync
# Connection_lost) goes nowhere when launched via Start-Process -WindowStyle
# Hidden. This wrapper captures stderr to argus_flow/logs/<name>_stderr.log
# so the next silent-death leaves a diagnostic trace.
#
# Usage:
#   .\ops\launch_with_stderr.ps1 -Name gdx_gld -Module forge.gdx_gld_runner -Args '--signal-only','--loop','--interval-min','60'
#
# Stderr captures to: argus_flow/logs/<Name>_stderr_yyyymmdd.log (rotates daily)
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$Module,
    [string[]]$ModuleArgs = @()
)

$repo = "C:\Argus\repo"
$python = "C:\Argus\.venv\Scripts\python.exe"
$logDir = Join-Path $repo "argus_flow\logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$dateTag = Get-Date -Format "yyyyMMdd"
$stderrLog = Join-Path $logDir "${Name}_stderr_${dateTag}.log"

# Build full arg list: -m <module> <args>
$pyArgs = @('-m', $Module) + $ModuleArgs

$env:IBKR_PORT = '7497'

Write-Host "Launching $Name (module=$Module)" -ForegroundColor Cyan
Write-Host "  stderr -> $stderrLog" -ForegroundColor DarkGray
Write-Host "  pid follows..." -ForegroundColor DarkGray

$proc = Start-Process -FilePath $python -ArgumentList $pyArgs `
    -WorkingDirectory $repo -WindowStyle Hidden `
    -RedirectStandardError $stderrLog -PassThru
Write-Host "  PID=$($proc.Id) started at $(Get-Date -Format 'HH:mm:ss')" -ForegroundColor Green
