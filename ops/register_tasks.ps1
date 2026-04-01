# ops/register_tasks.ps1 -- Register all Argus scheduled tasks
# Run from elevated (Admin) PowerShell prompt
# Usage: .\ops\register_tasks.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== Argus Task Scheduler Registration ===" -ForegroundColor Cyan
Write-Host "Requires elevated (Admin) prompt" -ForegroundColor Yellow
Write-Host ""

$tasks = @(
    @{
        Name = "ArgusGitBackup"
        Script = "C:\Argus\repo\ops\auto_git_backup.ps1"
        Schedule = "DAILY"
        Time = "02:00"
        Description = "Daily git backup"
    },
    @{
        Name = "ArgusManagedTruth"
        Script = "C:\Argus\repo\ops\refresh_managed_truth.ps1"
        Schedule = "MINUTE"
        Modifier = 10
        Description = "Refresh staged governance and oversight truth every 10 minutes"
    },
    @{
        Name = "ArgusCohortReport"
        Script = "C:\Argus\repo\ops\run_cohort_report.ps1"
        Schedule = "DAILY"
        Time = "23:00"
        Description = "Nightly cohort compliance + divergence + Discord"
    },
    @{
        Name = "ArgusUSBBackup"
        Script = "C:\Argus\repo\ops\backup_to_usb.ps1"
        Schedule = "DAILY"
        Time = "03:00"
        Description = "Daily USB backup"
    },
    @{
        Name = "ArgusVerifyBackup"
        Script = "C:\Argus\repo\ops\verify_backup.ps1"
        Schedule = "WEEKLY"
        Time = "04:00"
        Day = "SUN"
        Description = "Weekly backup integrity check"
    },
    @{
        Name = "ArgusWeeklyDigest"
        Script = "C:\Argus\repo\ops\run_weekly_digest.ps1"
        Schedule = "WEEKLY"
        Time = "06:00"
        Day = "SUN"
        Description = "Weekly FX cohort Discord report"
    },
    @{
        Name = "ArgusWatchdog"
        Script = "C:\Argus\repo\ops\watchdog_managed.ps1"
        Schedule = "ONSTART"
        Description = "Runner watchdog - auto-restart on crash"
    }
)

foreach ($task in $tasks) {
    Write-Host "  Registering: $($task.Name) - $($task.Description)" -ForegroundColor White

    $tr = "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File `"$($task.Script)`""

    $args = @("/Create", "/TN", $task.Name, "/TR", $tr, "/F")

    if ($task.Schedule -eq "DAILY") {
        $args += @("/SC", "DAILY", "/ST", $task.Time)
    }
    elseif ($task.Schedule -eq "MINUTE") {
        $args += @("/SC", "MINUTE", "/MO", "$($task.Modifier)")
    }
    elseif ($task.Schedule -eq "WEEKLY") {
        $args += @("/SC", "WEEKLY", "/D", $task.Day, "/ST", $task.Time)
    }
    elseif ($task.Schedule -eq "ONSTART") {
        $args += @("/SC", "ONSTART")
    }

    try {
        & schtasks $args 2>&1 | Out-Null
        Write-Host "    [OK] $($task.Name)" -ForegroundColor Green
    } catch {
        Write-Host "    [FAIL] $($task.Name): $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "=== Registration complete ===" -ForegroundColor Cyan
Write-Host "Verify with: schtasks /Query /FO TABLE | Select-String 'Argus'" -ForegroundColor Yellow
