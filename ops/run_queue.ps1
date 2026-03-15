# ops/run_queue.ps1 -- Pop and run backtest jobs from backtest_queue.jsonl
#
# Usage:
#   .\ops\run_queue.ps1                # run all queued jobs sequentially
#   .\ops\run_queue.ps1 -MaxJobs 2     # run at most 2 jobs then stop
#   .\ops\run_queue.ps1 -DryRun        # show what would run without running
#
# Queue format (backtest_queue.jsonl -- one JSON object per line):
#   {"label": "human-readable name", "env": {"KEY": "VAL", ...}, "single_run": true}
#
# Each job:
#   1. Sets env overrides from "env" dict
#   2. Runs ops/run_backtest.ps1 [-SingleRun]
#   3. Runs ops/analyze_scores.py on result
#   4. Removes the job line from the queue file
#   5. Logs result to ops/logs/queue_results.log
#
# The queue file is consumed top-down. Add new jobs by appending lines.
param(
    [int]$MaxJobs = 0,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = "C:\Argus\repo"
$queueFile = Join-Path $repoRoot "ops\backtest_queue.jsonl"
$logFile = Join-Path $repoRoot "ops\logs\queue_results.log"
$pyExe = "C:\Argus\.venv\Scripts\python.exe"

Set-Location $repoRoot

if (!(Test-Path $queueFile)) {
    Write-Host "No queue file at $queueFile" -ForegroundColor Yellow
    exit 0
}

function Pop-QueueJob {
    # line above: function Pop-QueueJob {
    $allLines = @(Get-Content $queueFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" })
    if ($allLines.Count -eq 0) { return $null }
    $jobObj = $allLines[0] | ConvertFrom-Json
    if ($allLines.Count -gt 1) {
        $allLines[1..($allLines.Count - 1)] | Set-Content $queueFile -Encoding utf8
    } else {
        Set-Content $queueFile -Value "" -Encoding utf8
    }
    return $jobObj
}

function Write-QueueLog {
    param([string]$msg)
    # line above: param([string]$msg)
    $logTs = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    $entry = "[$logTs] $msg"
    Add-Content -Path $logFile -Value $entry
    Write-Host $entry
}

function Invoke-QueueJob {
    param(
        [PSObject]$job,
        [string]$label
    )
    # line above: param(

    $singleRun = [bool]$job.single_run
    $envBackup = @{}

    # Set job label for run_header
    $envBackup["ARGUS_JOB_LABEL"] = [Environment]::GetEnvironmentVariable("ARGUS_JOB_LABEL", "Process")
    [Environment]::SetEnvironmentVariable("ARGUS_JOB_LABEL", $label, "Process")

    # Set env overrides
    if ($job.env) {
        foreach ($prop in $job.env.PSObject.Properties) {
            $envKey = $prop.Name
            $envVal = $prop.Value
            $envBackup[$envKey] = [Environment]::GetEnvironmentVariable($envKey, "Process")
            [Environment]::SetEnvironmentVariable($envKey, $envVal, "Process")
        }
    }

    $startTime = Get-Date
    $jobSuccess = $false
    $runId = ""

    # Clean sandbox bleed before each job (belt-and-suspenders)
    Clear-SandboxBleed

    try {
        Write-QueueLog "START: $label"

        # Run backtest (always single-run for queue jobs — no determinism check needed)
        $btArgs = @("-SingleRun")
        Write-Host "  btArgs: $btArgs  (singleRun=$singleRun)"
        & "$repoRoot\ops\run_backtest.ps1" @btArgs

        $jobSuccess = $true

        # Get the run ID from latest summary
        $latestSummary = Get-ChildItem "$repoRoot\ops\logs" -Filter "bt_summary_bt_*.json" |
            Where-Object { $_.Name -notmatch "latest" } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($latestSummary) {
            $runId = $latestSummary.BaseName -replace "bt_summary_", ""
        }

        # Run post-backtest analysis
        Write-Host ""
        Write-Host "--- POST-BACKTEST ANALYSIS ---" -ForegroundColor Cyan
        & $pyExe "$repoRoot\ops\analyze_scores.py" --latest
    }
    catch {
        Write-Host "JOB FAILED: $_" -ForegroundColor Red
        Write-QueueLog "FAIL: $label -- $_"
        & $pyExe "$repoRoot\ops\notify.py" --error "Queue job FAILED: $label -- $_" 2>$null
    }

    # Restore env (always runs, replaces finally block)
    foreach ($bk in $envBackup.Keys) {
        $origVal = $envBackup[$bk]
        if ($null -eq $origVal) {
            Remove-Item "Env:$bk" -ErrorAction SilentlyContinue
        } else {
            [Environment]::SetEnvironmentVariable($bk, $origVal, "Process")
        }
    }

    $elapsed = (Get-Date) - $startTime
    $elapsedStr = "{0:hh\:mm\:ss}" -f $elapsed

    if ($jobSuccess) {
        Write-QueueLog "DONE: $label | run_id=$runId | elapsed=$elapsedStr"
        # Discord notification
        if ($runId) {
            & $pyExe "$repoRoot\ops\notify.py" --backtest-complete $runId 2>$null
        }
    }

    return $jobSuccess
}

# ---- Pre-queue sandbox cleanup ----
# Remove stale live_*.csv from repo\logs that cause Assert-NoRepoLogsBleed failures
function Clear-SandboxBleed {
    $repoLogs = Join-Path $repoRoot "logs"
    if (!(Test-Path $repoLogs)) { return }
    $staleFiles = Get-ChildItem $repoLogs -Filter "live_*.csv" -ErrorAction SilentlyContinue
    if ($staleFiles) {
        Write-Host "Cleaning stale sandbox artifacts from repo\logs:" -ForegroundColor Yellow
        foreach ($f in $staleFiles) {
            Write-Host "  Removing: $($f.Name)" -ForegroundColor Yellow
            Remove-Item $f.FullName -Force
        }
    }
}

if (-not $DryRun) {
    Clear-SandboxBleed

    $refreshScript = "$repoRoot\ops\refresh_candles.ps1"
    if (Test-Path $refreshScript) {
        Write-Host "Refreshing candle data before queue start..." -ForegroundColor Cyan
        try {
            & $refreshScript
        } catch {
            Write-Host "WARNING: candle refresh failed: $_" -ForegroundColor Yellow
        }
    }
}

# ---- Main loop ----

$jobsRun = 0

while ($true) {
    if ($MaxJobs -gt 0 -and $jobsRun -ge $MaxJobs) {
        Write-Host "Reached MaxJobs=$MaxJobs -- stopping." -ForegroundColor Cyan
        break
    }

    $pendingLines = @(Get-Content $queueFile -ErrorAction SilentlyContinue | Where-Object { $_.Trim() -ne "" })
    if ($pendingLines.Count -eq 0) {
        Write-Host "Queue empty -- done." -ForegroundColor Green
        break
    }

    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "QUEUE: $($pendingLines.Count) job(s) remaining"
    Write-Host "========================================" -ForegroundColor Cyan

    $currentJob = Pop-QueueJob
    if (-not $currentJob) { break }

    $jobLabel = if ($currentJob.label) { $currentJob.label } else { "unnamed-$jobsRun" }

    Write-Host ""
    Write-Host "--- JOB: $jobLabel ---" -ForegroundColor Yellow
    if ($currentJob.env) {
        foreach ($prop in $currentJob.env.PSObject.Properties) {
            Write-Host "  ENV: $($prop.Name) = $($prop.Value)"
        }
    }
    Write-Host "  SingleRun: $($currentJob.single_run)"

    if ($DryRun) {
        Write-Host "  [DRY RUN -- skipping]" -ForegroundColor Magenta
    } else {
        Invoke-QueueJob -job $currentJob -label $jobLabel
    }

    $jobsRun++
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Queue runner finished. Jobs run: $jobsRun"
Write-Host "Results log: $logFile"
Write-Host "========================================" -ForegroundColor Green

# Post-queue leaderboard + Discord summary
if ($jobsRun -gt 0 -and -not $DryRun) {
    Write-Host ""
    Write-Host "--- POST-QUEUE LEADERBOARD ---" -ForegroundColor Cyan
    & $pyExe "$repoRoot\ops\post_queue_report.py" --min-trades 10

    # Auto-generate evolution visualization
    Write-Host ""
    Write-Host "--- BACKTEST EVOLUTION VIZ ---" -ForegroundColor Cyan
    try {
        & $pyExe "$repoRoot\ops\plot_backtest_evolution.py" --latest 30
    } catch {
        Write-Host "WARNING: evolution viz failed: $_" -ForegroundColor Yellow
    }

    # Auto-cleanup: keep 20 most recent runs to prevent disk bloat
    Write-Host ""
    Write-Host "--- ARTIFACT CLEANUP ---" -ForegroundColor Cyan
    try {
        & "$repoRoot\ops\cleanup_artifacts.ps1" -Keep 20
    } catch {
        Write-Host "WARNING: cleanup failed: $_" -ForegroundColor Yellow
    }
}