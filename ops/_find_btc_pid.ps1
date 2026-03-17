# Find BTC runner PID
$runners = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*runner_live*' }

foreach ($r in $runners) {
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($r.ParentProcessId)" -ErrorAction SilentlyContinue
    if ($parent -and $parent.CommandLine -like '*BTC*') {
        Write-Host "BTC runner: PID=$($r.ProcessId) Parent=$($r.ParentProcessId)"
    }
    if ($parent -and $parent.CommandLine -like '*ETH*') {
        Write-Host "ETH runner: PID=$($r.ProcessId) Parent=$($r.ParentProcessId)"
    }
}