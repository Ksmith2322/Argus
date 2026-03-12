# ops/run_comparison_tests.ps1
# Launches fixed_liq baseline and trendlines comparison backtests sequentially.
# Run this after the baseline (bt_20260312T024150Z_cd4067b6) completes.
#
# Test 1: fixed_liq baseline
#   - Current .env (liq bugs fixed: LIQ_MIN_VOL_UNITS_1M=25, engine.py PENALIZE fix)
#   - USE_TRENDLINES=false, TL_PENALTY_RESIST_SLOPE_NEG=0
#   - Config compared against: bt_20260312T024150Z_cd4067b6 (old buggy baseline)
#
# Test 2: trendlines comparison
#   - Same as Test 1 + USE_TRENDLINES=true, TL_PENALTY_RESIST_SLOPE_NEG=5
#   - Adds Phase 18 trendline penalty in descending channels

$ErrorActionPreference = "Stop"
Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1

function Reset-ArgusEnv {
    Remove-Item Env:ARGUS_RUN_ID -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_BT_ARTIFACT_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LIVE_EVENTS_CSV -ErrorAction SilentlyContinue
    Remove-Item Env:LIVE_SIGNALS_CSV -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_DISABLE_LIVE_ARTIFACTS -ErrorAction SilentlyContinue
    Remove-Item Env:USE_TRENDLINES -ErrorAction SilentlyContinue
    Remove-Item Env:TL_PENALTY_RESIST_SLOPE_NEG -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "=== TEST 1: Fixed-liq baseline (USE_TRENDLINES=false) ===" -ForegroundColor Cyan
Reset-ArgusEnv
$env:USE_TRENDLINES = "false"
$env:TL_PENALTY_RESIST_SLOPE_NEG = "0"
C:\Argus\.venv\Scripts\python.exe -m backtest.runner
Reset-ArgusEnv

Write-Host ""
Write-Host "=== TEST 2: Trendlines comparison (USE_TRENDLINES=true, penalty=5) ===" -ForegroundColor Cyan
$env:USE_TRENDLINES = "true"
$env:TL_PENALTY_RESIST_SLOPE_NEG = "5"
C:\Argus\.venv\Scripts\python.exe -m backtest.runner
Reset-ArgusEnv

Write-Host ""
Write-Host "=== Both comparison tests complete ===" -ForegroundColor Green
Write-Host "Compare bt_summary_*.json files in ops/logs/ to see impact of fixes."
