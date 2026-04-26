$tasks = @('ArgusGldPmLoop','ArgusNqLondonCloseLoop','ArgusAudOrbLoop')
$results = @()
foreach ($t in $tasks) {
    try {
        $tmp = "$env:TEMP\$t.xml"
        schtasks /Query /TN $t /XML 2>$null | Out-File -Encoding unicode -FilePath $tmp
        $content = Get-Content $tmp -Raw -Encoding Unicode
        $newContent = $content -replace '<LogonType>InteractiveToken</LogonType>','<LogonType>S4U</LogonType>'
        $newContent | Out-File -Encoding unicode -FilePath $tmp
        $out = schtasks /Create /F /TN $t /XML $tmp 2>&1
        $results += [pscustomobject]@{ Task = $t; Status = if ($LASTEXITCODE -eq 0) {'OK'} else {'FAIL'}; Output = $out }
        Remove-Item $tmp -ErrorAction SilentlyContinue
    } catch {
        $results += [pscustomobject]@{ Task = $t; Status = 'EXCEPTION'; Output = $_.Exception.Message }
    }
}
$results | Format-Table -AutoSize
"`nDONE. Verifying logon modes..."
foreach ($t in $tasks) {
    schtasks /Query /TN $t /FO LIST /V 2>$null | Select-String -Pattern 'TaskName|Logon Mode|Run Level'
    ""
}
"`nPress Enter to close..."
Read-Host | Out-Null
