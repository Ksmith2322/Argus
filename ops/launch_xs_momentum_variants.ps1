# Launch the 3 xs_momentum universe variants in paper-trading --loop mode.
#
# Background: the 2026-05-24 universe-expansion sweep identified 3 universes
# that survive the 20y disciplined gate (sectors_spdr_11, style_factors_8,
# legacy_sectors_countries_15). This script launches each as a separate
# paper-trading process. Each variant has:
#   - Its own state directory (forge/logs/xs_momentum_<variant>/)
#   - Its own IBKR client_id (122/123/124 — no collision with base 121)
#   - Its own PID lock (helio/runner_lock.py — refuses duplicate launches)
#   - Its own allocation_factor (0.5x as of allocation_factors.json v15)
#
# To run: from a PowerShell with IBKR_PORT and venv on PATH:
#   .\ops\launch_xs_momentum_variants.ps1
#
# To stop one variant: Stop-Process on its PID; the watchdog will NOT restart
# it (unless wired into watchdog.ps1 — currently NOT wired so these are
# operator-supervised launches).

$ErrorActionPreference = "Continue"
$venvPython = "C:\Argus\.venv\Scripts\python.exe"
$repoRoot = "C:\Argus\repo"

# 2026-05-25: cut over from TWS (7497) to IB Gateway (4002) for unattended
# production. TWS still available via ops/start_tws_via_ibc.ps1 for manual
# UI sessions; override IBKR_PORT here only if you need to point at TWS.
if (-not $env:IBKR_PORT) { $env:IBKR_PORT = "4002" }
Write-Host "Using IBKR_PORT=$($env:IBKR_PORT)"
Write-Host "Using venv Python: $venvPython"
Write-Host ""

$variants = @(
    @{name="sectors";  client_id=122; universe="11 SPDR sectors"}
    @{name="style";    client_id=123; universe="8 style-factor ETFs (best DD profile)"}
    @{name="legacy15"; client_id=124; universe="10 sectors + 5 country ETFs"}
)

foreach ($v in $variants) {
    Write-Host ("Launching forge_xs_momentum_{0} (client_id={1}): {2}" -f $v.name, $v.client_id, $v.universe)
    Start-Process -FilePath $venvPython `
        -ArgumentList "-m", "forge.xs_momentum.runner", "--variant", $v.name, "--loop" `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden
    Start-Sleep -Seconds 2  # stagger so PID-lock writes don't race
}

Write-Host ""
Write-Host "Launched 3 variants. Verify via:"
Write-Host "  Get-CimInstance Win32_Process | ? { `$_.CommandLine -match 'xs_momentum.*--variant' } | Format-Table"
Write-Host ""
Write-Host "PID locks:"
Write-Host "  forge/logs/xs_momentum_sectors/runner.lock"
Write-Host "  forge/logs/xs_momentum_style/runner.lock"
Write-Host "  forge/logs/xs_momentum_legacy15/runner.lock"
Write-Host ""
Write-Host "Heartbeats (should populate within 1 minute of launch):"
Write-Host "  forge/logs/xs_momentum_sectors/heartbeat.json"
Write-Host "  forge/logs/xs_momentum_style/heartbeat.json"
Write-Host "  forge/logs/xs_momentum_legacy15/heartbeat.json"
