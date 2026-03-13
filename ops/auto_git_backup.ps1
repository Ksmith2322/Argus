# ops/auto_git_backup.ps1
# Automated nightly git commit + push from PC1.
# Schedule via Task Scheduler to run nightly at 02:00.
# Safe to run repeatedly — only commits if there are changes.

$ErrorActionPreference = "Stop"
Set-Location C:\Argus\repo

$timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssZ")
$logFile = "C:\Argus\repo\ops\logs\git_backup_$((Get-Date).ToString('yyyyMMdd')).log"

function Log($msg) {
    $line = "[$timestamp] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
}

Log "=== Argus nightly git backup ==="

# Check for any changes
$status = git status --porcelain 2>&1
if (-not $status) {
    Log "No changes to commit. Skipping commit, pushing anyway."
} else {
    Log "Changes detected:"
    $status | ForEach-Object { Log "  $_" }
    git add -A
    git commit -m "auto-backup $timestamp" 2>&1 | ForEach-Object { Log $_ }
}

# Always push to keep remote in sync
git push origin phase6-hardening 2>&1 | ForEach-Object { Log $_ }

Log "=== Backup complete ==="
