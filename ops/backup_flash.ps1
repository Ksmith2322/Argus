# ops/backup_flash.ps1
# Run primary backup + mirror to flash drive (E:\Argus)
# Usage: .\ops\backup_flash.ps1

$ErrorActionPreference = "Stop"
$Root = "C:\Argus\repo"
$PrimaryBackupRoot = "C:\Argus\repo\ops\backups"
$FlashRoot = "E:\Argus\backups"
$Python = "C:\Argus\.venv\Scripts\python.exe"

Set-Location $Root

Write-Host "[backup_flash] Starting primary backup..."
& $Python ops\backup_restore.py full
if ($LASTEXITCODE -ne 0) {
    Write-Host "[backup_flash] WARN: primary backup returned exit code $LASTEXITCODE"
}

# Mirror primary backup to flash drive
if (Test-Path "E:\Argus") {
    Write-Host "[backup_flash] Mirroring to flash drive E:\Argus\backups ..."
    if (-not (Test-Path $FlashRoot)) {
        New-Item -ItemType Directory -Path $FlashRoot -Force | Out-Null
    }
    robocopy $PrimaryBackupRoot $FlashRoot /MIR /R:2 /W:2 /NP /LOG+:"$FlashRoot\robocopy_mirror.log"
    $rc = $LASTEXITCODE
    # robocopy exit codes 0-7 are success/warnings (not errors)
    if ($rc -le 7) {
        Write-Host "[backup_flash] Flash mirror OK (robocopy exit=$rc)"
    } else {
        Write-Host "[backup_flash] WARN: robocopy exit=$rc (check $FlashRoot\robocopy_mirror.log)"
    }
} else {
    Write-Host "[backup_flash] SKIP: flash drive E:\Argus not present"
}

Write-Host "[backup_flash] Done."
