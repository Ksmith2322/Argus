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

    # 2026-04-20: flipped from --dry-run to --live so apollo actually places
    # paper orders against DUP472829 on high-conviction post-ER signals. This
    # lets the research_only disposition accumulate real OOS evidence.
    Write-Host "Running Apollo earnings scanner..."
    $apolloOutput = & $python -m apollo.runner --live --days 14 2>&1
    foreach ($line in @($apolloOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] apollo: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] apollo WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Apollo forward-return backfill..."
    $apolloBackfillOutput = & $python -m apollo.ops.backfill_forward_returns 2>&1
    foreach ($line in @($apolloBackfillOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] apollo_backfill: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] apollo_backfill WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Generating fleet performance summary..."
    $perfOutput = & $python -m helio.fleet_perf_summary 2>&1
    foreach ($line in @($perfOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] fleet_perf: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] fleet_perf WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Generating signal frequency report..."
    $sigFreqOutput = & $python -m argus_flow.ops.signal_frequency_tracker 2>&1
    foreach ($line in @($sigFreqOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] signal_freq: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] signal_freq WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Generating promotion-readiness report..."
    $promoOutput = & $python -m helio.promotion_readiness 2>&1
    foreach ($line in @($promoOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] promotion_readiness: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] promotion_readiness WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running kill-rule watchdog..."
    $killOutput = & $python -m helio.kill_watchdog 2>&1
    foreach ($line in @($killOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] kill_watchdog: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] kill_watchdog WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Apollo planned-trades (research-only execution skeleton)..."
    $apolloPlanOutput = & $python -m apollo.execution.planned_trades 2>&1
    foreach ($line in @($apolloPlanOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] apollo_plan: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] apollo_plan WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Backfilling canonical fills log..."
    $fillsOutput = & $python -m helio.canonical_fills --backfill 2>&1
    foreach ($line in @($fillsOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] canonical_fills: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] canonical_fills WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running Argus drift forensics (usdjpy/gbpusd/cadjpy)..."
    $driftOutput = & $python -m argus_flow.ops.usdjpy_drift_forensics --all-pairs --window 14 2>&1
    foreach ($line in @($driftOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] drift_forensics: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] drift_forensics WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Running reconciliation (canonical_fills vs trades.csv)..."
    $reconOutput = & $python -m helio.reconciliation 2>&1
    foreach ($line in @($reconOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] reconciliation: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] reconciliation DRIFT detected (exit $LASTEXITCODE, non-fatal -- see report)"
    }

    Write-Host "Generating morning brief..."
    $briefOutput = & $python -m helio.morning_brief 2>&1
    foreach ($line in @($briefOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] morning_brief: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] morning_brief WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    Write-Host "Building fleet_state.json (Phase 1 read-model aggregator)..."
    $fleetStateOutput = & $python -m helio.fleet_state 2>&1
    foreach ($line in @($fleetStateOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] fleet_state: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] fleet_state WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    # 2026-04-20: flipped from --dry-run to --execute so Hermes places paper
    # orders on its score>=80 + long + GAP_DOWN gated signals against
    # DUP472829. Accumulates live-subset evidence.
    Write-Host "Running Hermes gap scanner..."
    $hermesOutput = & $python -m hermes.runner --execute --min-score 75 2>&1
    foreach ($line in @($hermesOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] hermes: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] hermes WARNING: exit code $LASTEXITCODE (non-fatal)"
    }

    # 2026-04-20: flipped from --dry-run to --execute. Ares runs a monthly
    # rebalance guard internally (argus_flow.runner.run_evaluation:149) so the
    # daily cohort call is a no-op except on rebalance days. With SCOPE_DISABLE_RISK_OFF_EXIT=True
    # the validated rotation-only subset is enforced.
    Write-Host "Running Ares sector rotation..."
    $aresOutput = & $python -m ares.runner --execute 2>&1
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

    # 2026-04-20: gld_pm_long no longer fires from this nightly cohort.
    # Strategy's signal_hours_utc = [18, 19, 20] but nightly runs ~23:00 UTC,
    # so last_ts.hour is always 20 and hours 18/19 are never seen as "last bar."
    # Correct launch is --loop mode during the 18-21 UTC window. See
    # scheduled task "ArgusGldPmLoop" (registered separately). The nightly
    # one-shot is retained below as --evaluate for position management only
    # (manages open positions; won't open new ones outside signal window anyway).
    Write-Host "Running GLD PM Long position-management sweep (--evaluate)..."
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

    # Oracle (Polymarket) paused 2026-04-19: Polymarket is geoblocked for US
    # users, so there is no execution path. Kept in repo for a potential
    # Kalshi pivot. To reactivate: uncomment this block and restore the
    # "oracle" entry in helio/fleet_monitor.py SYSTEMS.
    # Write-Host "Running Oracle Polymarket scanner..."
    # $oracleOutput = & $python -m oracle.runner --dry-run 2>&1
    # foreach ($line in @($oracleOutput)) {
    #     if ($line) {
    #         Add-Content -Path $logFile -Value "[$timestamp] oracle: $line"
    #     }
    # }
    # if ($LASTEXITCODE -ne 0) {
    #     Add-Content -Path $logFile -Value "[$timestamp] oracle WARNING: exit code $LASTEXITCODE (non-fatal)"
    # }

    # 2026-04-20: gdx_gld has real IBKR execution (ib_insync placeOrder path)
    # but wasn't scheduled. Running --live so signals convert to paper fills.
    # 2026-04-21: nq_london_close + aud_asian_breakout are session-specific.
    # A 23:00 UTC --evaluate is a no-op for both (out of session window).
    # They must run via --loop mode, registered as ONLOGON scheduled tasks:
    #   ArgusNqLondonCloseLoop  — fires 5m cadence during 16:00 UTC hour
    #   ArgusAudOrbLoop         — fires hourly during 01-07 UTC
    # See ops/register_remaining_tasks.ps1 for the schtasks commands.
    # Removed the nightly --evaluate invocations since they don't produce signals
    # outside the strategies' session windows.

    Write-Host "Running GDX/GLD pair runner..."
    $gdxOutput = & $python -m forge.gdx_gld_runner --live 2>&1
    foreach ($line in @($gdxOutput)) {
        if ($line) {
            Add-Content -Path $logFile -Value "[$timestamp] gdx_gld: $line"
        }
    }
    if ($LASTEXITCODE -ne 0) {
        Add-Content -Path $logFile -Value "[$timestamp] gdx_gld WARNING: exit code $LASTEXITCODE (non-fatal)"
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

    # 2026-04-20: flipped from --dry-run to --live. Titan uses IBKRExecutor
    # (titan/runner.py:105) to place paper bracket orders. With
    # SCOPE_TREND_FOLLOW_LONG_ONLY=True only the validated subset fires.
    Write-Host "Running Titan runner (check exits + evaluate entries)..."
    $titanRunnerOutput = & $python -m titan.runner --live 2>&1
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
