$taskName = "ArgusQueueBT"
$psExe = "powershell.exe"
$psArgs = "-NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/_pc2_queue_run.ps1"
try {
    schtasks /Create /TN $taskName /TR "$psExe $psArgs" /SC ONCE /ST 00:00 /F | Out-Null
    schtasks /Run /TN $taskName | Out-Null
    Write-Host "Queue launched via schtasks"
} catch {
    Start-Process -FilePath $psExe -ArgumentList $psArgs.Split(" ") -WindowStyle Hidden
    Write-Host "Queue launched via Start-Process"
}