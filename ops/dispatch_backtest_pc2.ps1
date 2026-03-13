param(
    [int]$Limit = 0,
    [switch]$SingleRun,          # pass through to run_backtest.ps1 - skips Run 2 + determinism
    [hashtable]$EnvOverrides = @{}  # e.g. @{USE_TRENDLINES="true"; TL_PENALTY_RESIST_SLOPE_NEG="5"}
)

$PC2 = "ksmith2322@yahoo.com@desktop-17cjmup"
$BRANCH = "phase6-hardening"

Write-Host "--- DISPATCH TO PC2 ---"

# Push latest from PC1
Set-Location C:\Argus\repo
git add -A
$status = git status --porcelain
if ($status) {
    $ts = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    git commit -m "pre-dispatch sync $ts"
}
git push origin $BRANCH

# Build env override clauses for remote command
$envClauses = ""
if ($Limit -gt 0) {
    $envClauses += "`$env:BACKTEST_LIMIT='$Limit'; "
}
foreach ($k in $EnvOverrides.Keys) {
    $v = $EnvOverrides[$k]
    $envClauses += "`$env:${k}='${v}'; "
}
if ($envClauses) {
    Write-Host "Env overrides: $envClauses"
}

$singleRunFlag = if ($SingleRun) { " -SingleRun" } else { "" }
$remotePs = "Set-Location C:/Argus/repo; git pull origin $BRANCH; ${envClauses}./ops/run_backtest.ps1${singleRunFlag}"

# Use scheduled task for reliability (survives SSH disconnect)
Write-Host "Creating scheduled task on PC2..."
$taskCmd = "powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -Command `"$remotePs`""

# Pull first (via direct SSH), then create+run task
ssh $PC2 "powershell -NonInteractive -NoProfile -Command `"Set-Location C:/Argus/repo; git pull origin $BRANCH`""

# Delete old task if exists, create new, run
ssh $PC2 "schtasks /delete /tn `"ArgusBacktest`" /f" 2>$null
ssh $PC2 "schtasks /create /tn `"ArgusBacktest`" /tr `"$taskCmd`" /sc once /st 00:00 /f"
ssh $PC2 "schtasks /run /tn `"ArgusBacktest`""

Write-Host ""
Write-Host "--- DISPATCHED (via scheduled task) ---"
Write-Host "Check status: .\ops\check_pc2.ps1"
Write-Host "Pull results:  .\ops\pull_pc2_results.ps1"