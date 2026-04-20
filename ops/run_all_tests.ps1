# Run the full argus_flow test suite.
# Exits 0 on pass, non-zero on any failure. Suitable for pre-commit / CI.
#
# Usage:
#   .\ops\run_all_tests.ps1            # run full suite
#   .\ops\run_all_tests.ps1 -Verbose   # per-test output
#   .\ops\run_all_tests.ps1 -Pattern test_fleet_sizing  # single file

param(
    [switch]$Verbose,
    [string]$Pattern = ""
)

Set-Location "C:\Argus\repo"
$python = "C:\Argus\.venv\Scripts\python.exe"

$verbosityFlag = if ($Verbose) { "-v" } else { "" }

if ($Pattern) {
    Write-Host "Running test pattern: $Pattern"
    $output = & $python -m unittest "argus_flow.tests.$Pattern" $verbosityFlag 2>&1
} else {
    Write-Host "Running full argus_flow.tests suite..."
    $output = & $python -m unittest discover -s argus_flow\tests -t . $verbosityFlag 2>&1
}

$exitCode = $LASTEXITCODE

# Always show the last ~20 lines (summary + any failures)
$lines = @($output)
$tail = [Math]::Max(0, $lines.Count - 30)
$lines[$tail..($lines.Count - 1)] | ForEach-Object { Write-Host $_ }

if ($exitCode -eq 0) {
    Write-Host ""
    Write-Host "[PASS] All tests passed." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[FAIL] Test run failed (exit $exitCode)." -ForegroundColor Red
}

exit $exitCode
