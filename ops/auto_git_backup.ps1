# ops/auto_git_backup.ps1
# Automated nightly git commit + push from PC1.
# Schedule via Task Scheduler to run nightly at 02:00.
# Safe to run repeatedly -- only commits if there are changes.

$ErrorActionPreference = "Continue"
Set-Location C:\Argus\repo

$logFile = "C:\Argus\repo\ops\logs\git_backup_$((Get-Date).ToString('yyyyMMdd')).log"

function Log($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

Log "=== Argus nightly git backup ==="

# Check for any changes
$status = git status --porcelain 2>&1
if (-not $status) {
    Log "No changes to commit."
} else {
    Log "Changes detected:"
    $status | ForEach-Object { Log "  $_" }
    git add -A 2>&1 | ForEach-Object { Log $_ }
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssZ")
    git commit -m "auto-backup $ts" 2>&1 | ForEach-Object { Log $_ }
}

# Always push to keep remote in sync
$pushOut = git push origin phase6-hardening 2>&1
$pushOut | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) {
    Log "WARNING: git push failed (exit=$LASTEXITCODE)"
} else {
    Log "Push OK"
}

Log "=== Backup complete ==="
