# Register as a nightly scheduled task (run from elevated prompt):
#   schtasks /Create /TN "ArgusCohortReport" /TR "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:\Argus\repo\ops\run_cohort_report.ps1" /SC DAILY /ST 23:00 /F

Set-Location "C:\Argus\repo"

$logFile = "C:\Argus\repo\argus_flow\logs\cohort_report.log"
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# 1. Run cohort compliance report
try {
    $report = & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.daily_report 2>&1
    Add-Content -Path $logFile -Value "[$timestamp] daily_report: $report"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] daily_report ERROR: $_"
}

# 2. Run divergence guard
try {
    $div = & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.divergence_guard 2>&1
    Add-Content -Path $logFile -Value "[$timestamp] divergence_guard: $div"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] divergence_guard ERROR: $_"
}

# 3. Run correlation guard
try {
    $corr = & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.correlation_guard 2>&1
    Add-Content -Path $logFile -Value "[$timestamp] correlation_guard: $corr"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] correlation_guard ERROR: $_"
}

# 4. Refresh research validation for the active cohort
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.walkforward_validation --all-active 2>&1 | Out-Null
    Add-Content -Path $logFile -Value "[$timestamp] walk-forward validation refreshed"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] walk-forward validation ERROR: $_"
}

# 5. Run kill discipline + promotion gate + artifact divergence
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.kill_discipline 2>&1 | Out-Null
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.promotion_gate_v2 2>&1 | Out-Null
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.artifact_divergence 2>&1 | Out-Null
    Add-Content -Path $logFile -Value "[$timestamp] governance checks complete"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] governance checks ERROR: $_"
}

# 6. Generate canonical evidence registry (must run AFTER all governance reports)
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.evidence_registry 2>&1 | Out-Null
    Add-Content -Path $logFile -Value "[$timestamp] evidence registry generated"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] evidence registry ERROR: $_"
}

# 7. Broker and fleet oversight surfaces
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.position_monitor 2>&1 | Out-Null
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.risk_oversight 2>&1 | Out-Null
    Add-Content -Path $logFile -Value "[$timestamp] broker/risk oversight generated"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] broker/risk oversight ERROR: $_"
}

# 8. Alert escalation (checks all reports, writes alert_state/events, sends Discord if issues found)
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.alert_escalation_v2 2>&1 | Out-Null
    Add-Content -Path $logFile -Value "[$timestamp] alert escalation complete"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] alert escalation ERROR: $_"
}

# 9. Send Discord summary
try {
    & "C:\Argus\.venv\Scripts\python.exe" -m argus_flow.ops.discord_alerts --summary 2>&1
    Add-Content -Path $logFile -Value "[$timestamp] Discord summary sent"
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] Discord summary ERROR: $_"
}

Add-Content -Path $logFile -Value "[$timestamp] === Nightly cohort report complete ==="
