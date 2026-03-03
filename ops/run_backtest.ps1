# ops/run_backtest.ps1
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------
# 1) Canonical working directory + venv
# ------------------------------------------------------------
Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1

# ------------------------------------------------------------
# 2) Canonical artifact contract (NO DRIFT)
# ------------------------------------------------------------
$env:ARGUS_ARTIFACT_ROOT   = "C:\Argus\repo"
$env:ARGUS_BT_ARTIFACT_DIR = "C:\Argus\repo\ops\logs"
$env:ARGUS_LOG_DIR         = "C:\Argus\repo\ops\logs"

# Ensure log directory exists
if (!(Test-Path $env:ARGUS_BT_ARTIFACT_DIR)) {
    New-Item -ItemType Directory -Force -Path $env:ARGUS_BT_ARTIFACT_DIR | Out-Null
}

# ------------------------------------------------------------
# 3) CRITICAL: Sandbox "live" artifacts (prevents mutation)
# ------------------------------------------------------------
# Backtest must NEVER write to ops\logs\live_*.csv
$env:LIVE_EVENTS_CSV  = Join-Path $env:ARGUS_BT_ARTIFACT_DIR "bt_sandbox_live_events.csv"
$env:LIVE_SIGNALS_CSV = Join-Path $env:ARGUS_BT_ARTIFACT_DIR "bt_sandbox_live_signals.csv"

# Make intent explicit
$env:ARGUS_MODE = "bt"

# Optional: some codebases key off this to disable "live pointer" writes
$env:ARGUS_DISABLE_LIVE_ARTIFACTS = "1"

# ------------------------------------------------------------
# 4) Run canonical backtest entrypoint
# ------------------------------------------------------------
python -m backtest.runner

# ------------------------------------------------------------
# 5) Echo last run id (quality-of-life)
# ------------------------------------------------------------
$sum = Join-Path $env:ARGUS_BT_ARTIFACT_DIR "bt_summary_latest.json"
if (Test-Path $sum) {
    $run = (Get-Content $sum | ConvertFrom-Json).run_id
    "LAST_RUN_ID=$run"
} else {
    "WARN: missing bt_summary_latest.json at $sum"
}