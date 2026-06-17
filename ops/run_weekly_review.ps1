# ops/run_weekly_review.ps1 -- weekly wrapper for the gate review report
#
# Runs helio.weekly_gate_review to generate
# argus_flow/logs/weekly_review_YYYYMMDD.md.
#
# Scheduled by ops/register_tasks.ps1 to run Saturday at 07:00 local.
#
# Exit code preserved from weekly_gate_review (0/1/2 for OK/WARNING/PAUSE).

$ErrorActionPreference = "Continue"
$LogDir = "C:\Argus\repo\argus_flow\logs"
$LogFile = Join-Path $LogDir "weekly_review_cron.log"

$ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts INFO  cron: weekly_review start" | Out-File -Append $LogFile -Encoding utf8

Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
& $python -m helio.weekly_gate_review 2>&1 | Out-File -Append $LogFile -Encoding utf8
$rc = $LASTEXITCODE

$ts2 = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"$ts2 INFO  cron: weekly_review done (exit=$rc)" | Out-File -Append $LogFile -Encoding utf8
exit $rc
