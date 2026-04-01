Set-Location "C:\Argus\repo"

$logFile = "C:\Argus\repo\argus_flow\logs\cohort_report.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$python = "C:\Argus\.venv\Scripts\python.exe"

try {
    $output = & $python -m argus_flow.ops.refresh_managed_truth --include-summary --accept-existing-age-s 600 2>&1
    foreach ($line in @($output)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] refresh_managed_truth: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "exit code $LASTEXITCODE"
    }
    Add-Content -Path $logFile -Value "[$timestamp] === Nightly cohort report complete ==="
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] refresh_managed_truth ERROR: $_"
    Add-Content -Path $logFile -Value "[$timestamp] === Nightly cohort report complete (WITH FAILURES) ==="
    exit 1
}
