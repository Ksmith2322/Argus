param(
    [switch]$Json,
    [switch]$RequireLiveCapital
)

$ErrorActionPreference = "Stop"

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $Repo "..\.venv\Scripts\python.exe"
if (Test-Path $VenvPython) {
    $Python = $VenvPython
} else {
    $Python = "python"
}

Push-Location $Repo
try {
    & $Python -m helio.ops_reliability
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $LadderArgs = @("-m", "helio.capital_ladder")
    if ($Json) {
        $LadderArgs += "--json"
    }
    & $Python @LadderArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    if ($RequireLiveCapital) {
        $ReportPath = Join-Path $Repo "argus_flow\logs\capital_ladder_report.json"
        $Report = Get-Content -Raw -Path $ReportPath | ConvertFrom-Json
        if ([double]$Report.approved_capital_usd -le 0) {
            Write-Error "Capital ladder approved `$0. Live capital remains blocked."
            exit 2
        }
    }
} finally {
    Pop-Location
}
