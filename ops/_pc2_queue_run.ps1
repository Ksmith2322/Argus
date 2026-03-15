$ErrorActionPreference = 'Stop'
Set-Location C:/Argus/repo
. C:/Argus/.venv/Scripts/Activate.ps1
try {
    ./ops/run_queue.ps1 *>&1 | Tee-Object -FilePath C:/Argus/repo/ops/logs/pc2_queue_20260314.log
} catch {
    Add-Content -Path C:/Argus/repo/ops/logs/pc2_queue_20260314.log -Value "FATAL: $($_.Exception.Message)"
}