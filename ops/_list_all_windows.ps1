# List all PowerShell windows with their titles and what they're doing
$allPS = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'"
foreach ($p in $allPS) {
    $cmd = if ($p.CommandLine.Length -gt 150) { $p.CommandLine.Substring(0, 150) + "..." } else { $p.CommandLine }

    # Try to get window title
    try {
        $proc = [System.Diagnostics.Process]::GetProcessById($p.ProcessId)
        $title = $proc.MainWindowTitle
        if ($title) {
            Write-Host "PID=$($p.ProcessId) TITLE='$title'" -ForegroundColor Cyan
        } else {
            Write-Host "PID=$($p.ProcessId) (no window title)" -ForegroundColor Gray
        }
    } catch {
        Write-Host "PID=$($p.ProcessId) (process gone)" -ForegroundColor DarkGray
    }
    Write-Host "  CMD: $cmd"
    Write-Host ""
}

# Also show Python processes
Write-Host "=== PYTHON PROCESSES ===" -ForegroundColor Yellow
$pyProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'"
foreach ($p in $pyProcs) {
    $cmd = if ($p.CommandLine.Length -gt 120) { $p.CommandLine.Substring(0, 120) + "..." } else { $p.CommandLine }
    Write-Host "  PID=$($p.ProcessId) CMD=$cmd"
}