# ops/backup_to_usb.ps1 -- Nightly backup of Argus environment to USB drive
#
# Usage:
#   .\ops\backup_to_usb.ps1                    # auto-detect USB drive
#   .\ops\backup_to_usb.ps1 -DriveLetter E     # specify drive letter
#   .\ops\backup_to_usb.ps1 -DryRun            # show what would be copied
#
# Backs up:
#   - C:\Argus\repo (code, config, data — excludes __pycache__, .git/objects)
#   - C:\Argus\repo\state (runtime state)
#   - C:\Argus\repo\ops\logs (recent N days of artifacts)
#
# Scheduled: ArgusUSBBackup daily@03:00 via Task Scheduler
param(
    [string]$DriveLetter = "",
    [int]$LogRetentionDays = 7,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = "C:\Argus\repo"
$argusRoot = "C:\Argus"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"
$logFile = Join-Path $repoRoot "ops\logs\usb_backup.log"

function Write-BackupLog {
    param([string]$msg)
    $ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ"
    $entry = "[$ts] $msg"
    Add-Content -Path $logFile -Value $entry
    Write-Host $entry
}

function Find-UsbDrive {
    # Find removable drives with enough space (>1GB free)
    $drives = Get-WmiObject Win32_LogicalDisk |
        Where-Object { $_.DriveType -eq 2 -and $_.FreeSpace -gt 1GB } |
        Sort-Object FreeSpace -Descending
    if ($drives) {
        return $drives[0].DeviceID.TrimEnd(":")
    }
    # Also check for fixed drives labeled "BACKUP" or "ARGUS"
    $labeled = Get-WmiObject Win32_LogicalDisk |
        Where-Object { $_.VolumeName -match "BACKUP|ARGUS" -and $_.FreeSpace -gt 1GB } |
        Sort-Object FreeSpace -Descending
    if ($labeled) {
        return $labeled[0].DeviceID.TrimEnd(":")
    }
    return $null
}

# Determine target drive
if (-not $DriveLetter) {
    $DriveLetter = Find-UsbDrive
    if (-not $DriveLetter) {
        $msg = "FAIL: No USB drive detected (need removable drive with >1GB free, or drive labeled BACKUP/ARGUS)"
        Write-BackupLog $msg
        try { & $pyExe "$repoRoot\ops\notify.py" --error $msg 2>$null } catch {}
        exit 1
    }
}

$targetRoot = "${DriveLetter}:\Argus_Backup"
$targetRepo = Join-Path $targetRoot "repo"
$targetLogs = Join-Path $targetRoot "repo\ops\logs"

Write-BackupLog "START: backup to ${DriveLetter}: ($targetRoot)"

# Check drive space
try {
    $drive = Get-WmiObject Win32_LogicalDisk -Filter "DeviceID='${DriveLetter}:'"
    $freeGB = [math]::Round($drive.FreeSpace / 1GB, 2)
    Write-BackupLog "Drive ${DriveLetter}: free space: ${freeGB} GB"
    if ($freeGB -lt 0.5) {
        $msg = "FAIL: USB drive ${DriveLetter}: has only ${freeGB} GB free"
        Write-BackupLog $msg
        try { & $pyExe "$repoRoot\ops\notify.py" --error $msg 2>$null } catch {}
        exit 1
    }
} catch {
    Write-BackupLog "WARNING: could not check drive space: $_"
}

if ($DryRun) {
    Write-Host "[DRY RUN] Would backup to: $targetRoot"
    Write-Host "[DRY RUN] Repo: robocopy $repoRoot $targetRepo /MIR /XD __pycache__ .git\objects node_modules /XF *.pyc"
    Write-Host "[DRY RUN] Logs: last $LogRetentionDays days only"
    exit 0
}

# Step 1: Backup repo (code, config, data) — exclude heavy/regenerable dirs
Write-BackupLog "Syncing repo..."
$robocopyArgs = @(
    $repoRoot,
    $targetRepo,
    "/MIR",
    "/XD", "__pycache__", ".git\objects", "node_modules",
    "/XF", "*.pyc",
    "/NFL", "/NDL", "/NJH", "/NJS",  # quiet output
    "/R:1", "/W:1"  # minimal retries
)

# First sync everything except ops/logs (we handle logs separately for retention)
$robocopyArgsNoLogs = $robocopyArgs + @("/XD", "ops\logs")
$result = robocopy @robocopyArgsNoLogs
# Robocopy exit codes: 0-7 are success, 8+ are errors
if ($LASTEXITCODE -ge 8) {
    Write-BackupLog "WARNING: robocopy repo sync returned exit code $LASTEXITCODE"
}

# Step 2: Backup recent logs only (last N days)
Write-BackupLog "Syncing logs (last $LogRetentionDays days)..."
$logsSource = Join-Path $repoRoot "ops\logs"
if (Test-Path $logsSource) {
    if (!(Test-Path $targetLogs)) {
        New-Item -ItemType Directory -Path $targetLogs -Force | Out-Null
    }
    $cutoff = (Get-Date).AddDays(-$LogRetentionDays)
    $recentFiles = Get-ChildItem $logsSource -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $cutoff }
    $copied = 0
    foreach ($f in $recentFiles) {
        $dest = Join-Path $targetLogs $f.Name
        Copy-Item $f.FullName $dest -Force
        $copied++
    }
    Write-BackupLog "Copied $copied log files from last $LogRetentionDays days"
}

# Step 3: Verify key files exist on backup
$keyFiles = @(
    "repo\runner_live.py",
    "repo\engine.py",
    "repo\state.py",
    "repo\config.py",
    "repo\.env",
    "repo\data\eth_usd_1m.csv"
)
$missing = @()
foreach ($kf in $keyFiles) {
    $checkPath = Join-Path $targetRoot $kf
    if (!(Test-Path $checkPath)) {
        $missing += $kf
    }
}

if ($missing.Count -gt 0) {
    $msg = "WARN: backup missing files: $($missing -join ', ')"
    Write-BackupLog $msg
    try { & $pyExe "$repoRoot\ops\notify.py" --error "USB Backup: $msg" 2>$null } catch {}
} else {
    Write-BackupLog "OK: all key files verified on backup"
}

# Step 4: Write backup timestamp
$ts = Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ"
Set-Content -Path (Join-Path $targetRoot "LAST_BACKUP.txt") -Value "Last backup: $ts`nSource: $repoRoot`nHost: $env:COMPUTERNAME"

$elapsed = (Get-Date) - (Get-Date $ts)
Write-BackupLog "DONE: backup to ${DriveLetter}: complete"

# Discord success notification
$discordMsg = "USB Backup OK: ${DriveLetter}: -- ${freeGB} GB free, ${copied} log files copied"
Write-BackupLog "Sending Discord notification: $discordMsg"
try {
    & $pyExe "$repoRoot\ops\notify.py" --test $discordMsg
    Write-BackupLog "Discord notification sent"
} catch {
    Write-BackupLog "WARNING: Discord notification failed: $_"
}