# ops/cleanup_artifacts.ps1 -- Remove old backtest artifacts, keep N most recent runs
#
# Usage:
#   .\ops\cleanup_artifacts.ps1              # keep 10 most recent (default)
#   .\ops\cleanup_artifacts.ps1 -Keep 5      # keep 5 most recent
#   .\ops\cleanup_artifacts.ps1 -DryRun      # show what would be deleted
param(
    [int]$Keep = 10,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$logsDir = "C:\Argus\repo\ops\logs"

if (!(Test-Path $logsDir)) {
    Write-Host "No logs directory found." -ForegroundColor Yellow
    exit 0
}

# Find all unique run IDs from summary files (sorted newest first)
$summaries = Get-ChildItem $logsDir -Filter "bt_summary_bt_*.json" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "latest" } |
    Sort-Object LastWriteTime -Descending

$allRunIds = @()
foreach ($s in $summaries) {
    $rid = $s.BaseName -replace "bt_summary_", ""
    $allRunIds += $rid
}

# Also find run IDs from run_header files (in case summary doesn't exist yet)
# These are either in-progress or orphaned runs -- always protect in-progress ones
$inProgressIds = @()
$headers = Get-ChildItem $logsDir -Filter "run_header_bt_*.json" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending
foreach ($h in $headers) {
    $rid = $h.BaseName -replace "run_header_", ""
    $sumExists = Test-Path (Join-Path $logsDir "bt_summary_$rid.json")
    if (-not $sumExists) {
        # In-progress or orphaned -- always keep (never delete running backtests)
        $inProgressIds += $rid
    }
    if ($allRunIds -notcontains $rid) {
        $allRunIds += $rid
    }
}

if ($allRunIds.Count -le $Keep) {
    Write-Host "Only $($allRunIds.Count) run(s) found, keeping all (threshold=$Keep)." -ForegroundColor Green
    exit 0
}

$keepIds = @($allRunIds | Select-Object -First $Keep)
# Always protect in-progress runs
foreach ($ipId in $inProgressIds) {
    if ($keepIds -notcontains $ipId) { $keepIds += $ipId }
}
$removeIds = $allRunIds | Where-Object { $keepIds -notcontains $_ }

if ($inProgressIds.Count -gt 0) {
    Write-Host "In-progress runs (protected): $($inProgressIds -join ', ')" -ForegroundColor Yellow
}
Write-Host "Runs found: $($allRunIds.Count)  |  Keeping: $($keepIds.Count)  |  Removing: $($removeIds.Count)" -ForegroundColor Cyan

$totalSize = 0
$totalFiles = 0

foreach ($rid in $removeIds) {
    $files = Get-ChildItem $logsDir -Filter "*$rid*" -ErrorAction SilentlyContinue
    foreach ($f in $files) {
        $totalSize += $f.Length
        $totalFiles++
        if ($DryRun) {
            Write-Host "  [DRY] Would delete: $($f.Name)  ($([math]::Round($f.Length / 1MB, 2)) MB)" -ForegroundColor Magenta
        } else {
            Remove-Item $f.FullName -Force
        }
    }
}

$sizeMB = [math]::Round($totalSize / 1MB, 1)

if ($DryRun) {
    Write-Host ""
    Write-Host "DRY RUN: would delete $totalFiles files ($sizeMB MB)" -ForegroundColor Magenta
} else {
    Write-Host ""
    Write-Host "Deleted $totalFiles files ($sizeMB MB)" -ForegroundColor Green
}