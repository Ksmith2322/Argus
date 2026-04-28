# ops/run_backtest.ps1
param(
    [switch]$SingleRun   # skip Run 2 + determinism check (for comparison tests)
)
$ErrorActionPreference = "Stop"

# ------------------------------------------------------------
# 1) Canonical working directory + venv
# ------------------------------------------------------------
Set-Location C:\Argus\repo
. C:\Argus\.venv\Scripts\Activate.ps1

# ------------------------------------------------------------
# 2) Helpers
# ------------------------------------------------------------
function Reset-ArgusEnv {
    # line above: function Reset-ArgusEnv {
    Remove-Item Env:ARGUS_RUN_ID -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_LOG_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_BT_ARTIFACT_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LIVE_EVENTS_CSV -ErrorAction SilentlyContinue
    Remove-Item Env:LIVE_SIGNALS_CSV -ErrorAction SilentlyContinue
    Remove-Item Env:ARGUS_DISABLE_LIVE_ARTIFACTS -ErrorAction SilentlyContinue
}

function Ensure-ArtifactDir {
    param([string]$dir)
    # line above: param([string]$dir)
    if (!(Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
}

function Get-LatestBtSummaryPath {
    param([string]$logsDir)
    # line above: param([string]$logsDir)
    $latest = Get-ChildItem $logsDir -Filter "bt_summary_bt_*.json" -ErrorAction SilentlyContinue |
        Sort LastWriteTime -Descending |
        Select -First 1
    if (-not $latest) { return $null }
    return $latest.FullName
}

function Get-RunIdFromSummaryPath {
    param([string]$summaryPath)
    # line above: param([string]$summaryPath)
    $bn = [IO.Path]::GetFileNameWithoutExtension($summaryPath)
    return $bn.Replace("bt_summary_","")
}

function Assert-RunPack {
    param(
        [string]$logsDir,
        [string]$runId
    )
    # line above: param(
    $pack = Get-ChildItem $logsDir -Filter "*_$runId.*" -ErrorAction SilentlyContinue
    if (-not $pack -or $pack.Count -lt 6) {
        throw "FAIL: incomplete run pack for $runId in $logsDir"
    }
    return $pack
}

function Normalize-BtSummary {
    param([string]$path)
    # line above: param([string]$path)
    $j = Get-Content $path -Raw | ConvertFrom-Json

    # ---- volatile / run-specific fields (remove for determinism hashing) ----
    foreach ($k in @(
        "run_id",
        "mode",
        "signals_csv",
        "events_csv",
        "equity_csv",
        "trades_csv",
        "event_counts_csv",
        "entry_attempts_csv",
        "entry_attempt_detail_csv",
        "run_header_json",
        "artifact_dir"
    )) {
        if ($j.PSObject.Properties.Name -contains $k) {
            $j.PSObject.Properties.Remove($k)
        }
    }

    # If you later add timestamps in summary, add them here (examples):
    foreach ($k in @("start_ts","end_ts","generated_ts","created_ts")) {
        if ($j.PSObject.Properties.Name -contains $k) {
            $j.PSObject.Properties.Remove($k)
        }
    }

    return ($j | ConvertTo-Json -Depth 50 -Compress)
}

function Sha256Hex {
    param([string]$s)
    # line above: param([string]$s)
    $bytes = [Text.Encoding]::UTF8.GetBytes($s)
    $hash  = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    return [System.BitConverter]::ToString($hash).Replace("-","")
}

function Sha256FileHex {
    param([string]$path)
    # line above: param([string]$path)
    if (!(Test-Path $path)) { return $null }
    $h = Get-FileHash -Algorithm SHA256 -Path $path
    return $h.Hash
}

function Assert-NoRepoLogsBleed {
    param([string]$repoLogsDir)
    # line above: param([string]$repoLogsDir)
    if (!(Test-Path $repoLogsDir)) { return }  # repo\logs may not exist in clean setups
    $bad_live = Get-ChildItem $repoLogsDir -Filter "live_*.csv" -ErrorAction SilentlyContinue
    if ($bad_live) {
        throw "FAIL: live artifacts written to repo\logs ($repoLogsDir)"
    }
}

function Assert-LatestPointer {
    param(
        [string]$logsDir,
        [string]$expectedRunId
    )
    # line above: param(
    $p = Join-Path $logsDir "bt_summary_latest.json"
    if (!(Test-Path $p)) {
        throw "FAIL: bt_summary_latest.json missing in $logsDir (latest pointer policy broken)"
    }
    $j = Get-Content $p -Raw | ConvertFrom-Json
    $rid = $null
    if ($j.PSObject.Properties.Name -contains "run_id") { $rid = [string]$j.run_id }
    if (-not $rid) {
        throw "FAIL: bt_summary_latest.json has no run_id (can't validate latest pointer)"
    }
    if ($rid -ne $expectedRunId) {
        throw "FAIL: bt_summary_latest.json points to $rid but expected $expectedRunId"
    }
}

function Assert-EquityArtifact {
    param(
        [string]$logsDir,
        [string]$runId
    )
    # line above: param(
    $p = Join-Path $logsDir ("equity_{0}.csv" -f $runId)
    if (!(Test-Path $p)) {
        # BT_LITE_MODE skips equity CSV - not an error
        # Check both process env and .env file (Python dotenv loads .env, PS doesn't)
        $liteMode = $env:BT_LITE_MODE
        if (-not $liteMode) {
            $envFile = Join-Path $PSScriptRoot "..\\.env"
            if (Test-Path $envFile) {
                $match = Select-String -Path $envFile -Pattern "^BT_LITE_MODE\s*=\s*(.+)" -ErrorAction SilentlyContinue
                if ($match) { $liteMode = $match.Matches[0].Groups[1].Value.Trim() }
            }
        }
        if ($liteMode -eq "true") {
            Write-Host "SKIP: equity artifact check (BT_LITE_MODE=true)"
            return
        }
        throw "FAIL: missing equity artifact: $p"
    }

    $lines = Get-Content $p
    if ($lines.Count -lt 2) { throw "FAIL: equity file too short: $p" }

    $hdr = $lines[0].Trim()
    # Canonical schema check (allow a small alias set)
    $okHdr = @(
        "epoch,equity_usd,cash_usd,position_qty",
        "epoch,equity,cash,qty"
    ) -contains $hdr
    if (-not $okHdr) {
        throw "FAIL: equity header not canonical. got=[$hdr] file=$p"
    }

    # Monotonic epoch check (strictly increasing)
    $prev = $null
    for ($i=1; $i -lt $lines.Count; $i++) {
        $row = $lines[$i].Trim()
        if (-not $row) { continue }
        $cols = $row.Split(",")
        if ($cols.Count -lt 1) { continue }
        $epochStr = $cols[0].Trim()
        [int64]$epoch = 0
        if (-not [int64]::TryParse($epochStr, [ref]$epoch)) {
            throw "FAIL: non-integer epoch at line $($i+1) in $p : [$epochStr]"
        }
        if ($prev -ne $null -and $epoch -le $prev) {
            throw "FAIL: non-monotonic epoch at line $($i+1) in $p : prev=$prev now=$epoch"
        }
        $prev = $epoch
    }
}

function Normalize-TextForDeterminism {
    param(
        [string]$path,
        [string]$rid1,
        [string]$rid2
    )
    # line above: param(
    if (!(Test-Path $path)) { return $null }
    $t = Get-Content $path -Raw

    # Replace run_ids so run-scoped filenames/embedded ids don't cause false diffs
    if ($rid1) { $t = $t.Replace($rid1, "<RID>") }
    if ($rid2) { $t = $t.Replace($rid2, "<RID>") }

    # Common timestamp patterns (best-effort): ISO-8601 Z stamps and +00:00 offset
    $t = [regex]::Replace($t, "\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})", "<TSZ>")

    # Bare dates (YYYY-MM-DD) not already part of a timestamp - catches RISK_DAY_RESET wall-clock leak
    $t = [regex]::Replace($t, "\b\d{4}-\d{2}-\d{2}\b", "<DATE>")

    # git_sha and config_hash can change if a commit lands between Run 1 and Run 2
    $t = [regex]::Replace($t, '"git_sha":\s*"[0-9a-f]+"', '"git_sha":"<GIT>"')
    $t = [regex]::Replace($t, '"config_hash":\s*"[0-9a-f]+"', '"config_hash":"<CFG>"')

    return $t
}

function Assert-DeterministicArtifacts {
    param(
        [string]$logsDir,
        [string]$rid1,
        [string]$rid2
    )
    # line above: param(
    # Equity should be identical WITHOUT normalization
    $eq1 = Join-Path $logsDir ("equity_{0}.csv" -f $rid1)
    $eq2 = Join-Path $logsDir ("equity_{0}.csv" -f $rid2)
    $heq1 = Sha256FileHex -path $eq1
    $heq2 = Sha256FileHex -path $eq2
    if (-not $heq1 -or -not $heq2) { throw "FAIL: equity hash missing (equity files not found?)" }
    if ($heq1 -ne $heq2) {
        throw "FAIL: equity drift detected (hash differs). $rid1=$heq1 $rid2=$heq2"
    }

    # Events/signals: normalize run_id + timestamps then hash
    foreach ($stem in @("bt_events","bt_signals","trades","event_counts","entry_attempts","entry_attempt_detail","run_header")) {
        $a = Join-Path $logsDir ("{0}_{1}.csv" -f $stem, $rid1)
        $b = Join-Path $logsDir ("{0}_{1}.csv" -f $stem, $rid2)

        # run_header is json in your pack
        if ($stem -eq "run_header") {
            $a = Join-Path $logsDir ("run_header_{0}.json" -f $rid1)
            $b = Join-Path $logsDir ("run_header_{0}.json" -f $rid2)
        }

        if (!(Test-Path $a) -or !(Test-Path $b)) { continue }

        $na = Normalize-TextForDeterminism -path $a -rid1 $rid1 -rid2 $rid2
        $nb = Normalize-TextForDeterminism -path $b -rid1 $rid1 -rid2 $rid2
        $ha = Sha256Hex -s $na
        $hb = Sha256Hex -s $nb

        if ($ha -ne $hb) {
            throw "FAIL: artifact drift in $stem after normalization. $rid1=$ha $rid2=$hb"
        }
    }
}

# ------------------------------------------------------------
# 3) Canonical artifact contract (NO DRIFT)
# ------------------------------------------------------------
Reset-ArgusEnv

$env:ARGUS_ARTIFACT_ROOT   = "C:\Argus\repo"
$env:ARGUS_BT_ARTIFACT_DIR = "C:\Argus\repo\ops\logs"
$env:ARGUS_LOG_DIR         = "C:\Argus\repo\ops\logs"
$env:ARGUS_MODE            = "bt"

Ensure-ArtifactDir -dir $env:ARGUS_BT_ARTIFACT_DIR

# CRITICAL: Sandbox "live" artifacts (prevents mutation)
$env:LIVE_EVENTS_CSV  = Join-Path $env:ARGUS_BT_ARTIFACT_DIR "bt_sandbox_live_events.csv"
$env:LIVE_SIGNALS_CSV = Join-Path $env:ARGUS_BT_ARTIFACT_DIR "bt_sandbox_live_signals.csv"
$env:ARGUS_DISABLE_LIVE_ARTIFACTS = "1"

# Optional window (0 = full dataset)
if (-not $env:BACKTEST_LIMIT) { $env:BACKTEST_LIMIT = "0" }

# ------------------------------------------------------------
# 4) Run #1
# ------------------------------------------------------------
Write-Host "------------------------------------------------------------"
Write-Host "ARGUS BACKTEST (RUN 1)"
Write-Host "MODE: $env:ARGUS_MODE"
Write-Host "ARTIFACT_DIR: $env:ARGUS_BT_ARTIFACT_DIR"
Write-Host "BACKTEST_LIMIT: $env:BACKTEST_LIMIT"
Write-Host "------------------------------------------------------------"

python -m backtest.runner

$logs = $env:ARGUS_BT_ARTIFACT_DIR
$sum1 = Get-LatestBtSummaryPath -logsDir $logs
if (-not $sum1) { throw "FAIL: No bt_summary found after RUN 1 in $logs" }
$rid1 = Get-RunIdFromSummaryPath -summaryPath $sum1
$pack1 = Assert-RunPack -logsDir $logs -runId $rid1

# Equity schema + monotonic epoch must pass
Assert-EquityArtifact -logsDir $logs -runId $rid1

if (-not $SingleRun) {
    # ------------------------------------------------------------
    # 5) Run #2 (same contract, fresh run_id)
    # ------------------------------------------------------------
    # Ensure we don't carry a run id forward
    Remove-Item Env:ARGUS_RUN_ID -ErrorAction SilentlyContinue

    Write-Host "------------------------------------------------------------"
    Write-Host "ARGUS BACKTEST (RUN 2)"
    Write-Host "------------------------------------------------------------"

    python -m backtest.runner

    $sum2 = Get-LatestBtSummaryPath -logsDir $logs
    if (-not $sum2) { throw "FAIL: No bt_summary found after RUN 2 in $logs" }
    $rid2 = Get-RunIdFromSummaryPath -summaryPath $sum2
    $pack2 = Assert-RunPack -logsDir $logs -runId $rid2

    # Equity schema + monotonic epoch must pass
    Assert-EquityArtifact -logsDir $logs -runId $rid2

    # ------------------------------------------------------------
    # 6) Determinism check (normalized summary hash must match)
    # ------------------------------------------------------------
    $n1 = Normalize-BtSummary -path $sum1
    $n2 = Normalize-BtSummary -path $sum2
    $h1 = Sha256Hex -s $n1
    $h2 = Sha256Hex -s $n2

    Write-Host "------------------------------------------------------------"
    Write-Host "DETERMINISM CHECK (SUMMARY)"
    Write-Host "RUN1=$rid1"
    Write-Host "RUN2=$rid2"
    Write-Host "HASH1=$h1"
    Write-Host "HASH2=$h2"
    Write-Host "------------------------------------------------------------"

    if ($h1 -ne $h2) {
        throw "FAIL: summary differs after normalization (non-deterministic)"
    }

    Write-Host "OK: deterministic summary (normalized)"

    # ------------------------------------------------------------
    # 6b) Determinism check (ARTIFACTS)
    # ------------------------------------------------------------
    Write-Host "------------------------------------------------------------"
    Write-Host "DETERMINISM CHECK (ARTIFACTS)"
    Write-Host "------------------------------------------------------------"
    Assert-DeterministicArtifacts -logsDir $logs -rid1 $rid1 -rid2 $rid2
    Write-Host "OK: deterministic artifacts (normalized where needed; equity raw-identical)"

    # ------------------------------------------------------------
    # 6c) Latest pointer policy (bt_summary_latest.json must point to RUN2)
    # ------------------------------------------------------------
    Assert-LatestPointer -logsDir $logs -expectedRunId $rid2
    Write-Host "OK: bt_summary_latest.json points to RUN2"
} else {
    Write-Host "------------------------------------------------------------"
    Write-Host "SINGLE-RUN MODE: skipping Run 2 + determinism check"
    Write-Host "------------------------------------------------------------"
    $rid2 = $rid1
    $pack2 = $pack1
}

# ------------------------------------------------------------
# 7) Verify NO repo\logs bleed
# ------------------------------------------------------------
$repoLogs = "C:\Argus\repo\logs"
Assert-NoRepoLogsBleed -repoLogsDir $repoLogs
Write-Host "OK: no repo\logs bleed detected"

# ------------------------------------------------------------
# 8) Print latest run pack (quality-of-life)
# ------------------------------------------------------------
Write-Host "------------------------------------------------------------"
Write-Host "LATEST RUN PACK"
$pack2 | Sort Name | Select Name,Length
Write-Host "------------------------------------------------------------"
"LAST_RUN_ID=$rid2"

# ------------------------------------------------------------
# 9) Nightly coin rotation health check
# ------------------------------------------------------------
Write-Host "------------------------------------------------------------"
Write-Host "COIN ROTATION HEALTH CHECK"
try {
    & "C:\Argus\.venv\Scripts\python.exe" "C:\Argus\repo\ops\rotate_coin.py" --apply 2>&1
    Write-Host "Rotation check complete."
} catch {
    Write-Host "WARNING: rotation check failed: $_"
}