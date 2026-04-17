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
    Write-Host "Running drift detector..."
    $driftOutput = & $python -m helio.drift_detector 2>&1
    foreach ($line in @($driftOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] drift_detector: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] drift_detector WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Apollo earnings scanner..."
    $apolloOutput = & $python -m apollo.runner --dry-run --days 14 2>&1
    foreach ($line in @($apolloOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] apollo: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] apollo WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Hermes gap scanner..."
    $hermesOutput = & $python -m hermes.runner --dry-run --min-score 75 2>&1
    foreach ($line in @($hermesOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] hermes: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] hermes WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Ares sector rotation..."
    $aresOutput = & $python -m ares.runner --dry-run 2>&1
    foreach ($line in @($aresOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] ares: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] ares WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running JPY PM Short evaluation..."
    $jpyOutput = & $python -m forge.jpy_pm_short.runner --evaluate 2>&1
    foreach ($line in @($jpyOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] jpy_pm_short: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] jpy_pm_short WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running NQ Overnight evaluation..."
    $nqOvOutput = & $python -m forge.nq_overnight.runner --evaluate 2>&1
    foreach ($line in @($nqOvOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] nq_overnight: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] nq_overnight WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running GLD PM Long evaluation..."
    $gldPmOutput = & $python -m forge.gld_pm_long.runner --evaluate 2>&1
    foreach ($line in @($gldPmOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] gld_pm_long: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] gld_pm_long WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Wick GBPUSD daily evaluation..."
    $wickOutput = & $python -m forge.wick_gbpusd.runner --evaluate 2>&1
    foreach ($line in @($wickOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] wick_gbpusd: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] wick_gbpusd WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Oracle Polymarket scanner..."
    $oracleOutput = & $python -m oracle.runner --dry-run 2>&1
    foreach ($line in @($oracleOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] oracle: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] oracle WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Titan scanner (refresh + long-only)..."
    $titanOutput = & $python -m titan.ops.scanner --refresh --long-only 2>&1
    foreach ($line in @($titanOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] titan_scanner: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] titan_scanner WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Titan runner (check exits + evaluate entries)..."
    $titanRunnerOutput = & $python -m titan.runner --dry-run 2>&1
    foreach ($line in @($titanRunnerOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] titan_runner: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] titan_runner WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    # Weekly review on Fridays
    $today = (Get-Date).DayOfWeek
    if ($today -eq "Friday") {
        Write-Host "Running weekly fleet review (Friday)..."
        $reviewOutput = & $python -m helio.weekly_review --days 7 2>&1
        foreach ($line in @($reviewOutput)) {
            if ($line) {
                Add-Content -Path $logFile -Value "[$timestamp] weekly_review: $line"
            }
        }
    }

    Write-Host "Running nightly analysis..."
    $analysisOutput = & $python -m argus_flow.ops.nightly_analysis --no-llm 2>&1
    foreach ($line in @($analysisOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] nightly_analysis: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] nightly_analysis WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Add-Content -Path $logFile -Value "[$timestamp] === Nightly cohort report complete ==="
} catch {
    Add-Content -Path $logFile -Value "[$timestamp] refresh_managed_truth ERROR: $_"
    Add-Content -Path $logFile -Value "[$timestamp] === Nightly cohort report complete (WITH FAILURES) ==="
    exit 1
}
