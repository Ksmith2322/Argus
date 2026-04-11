param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = "C:\Argus\.venv\Scripts\python.exe"

& $python (Join-Path $repoRoot "ops\paper_monitor_status.py") @Args
exit $LASTEXITCODE
