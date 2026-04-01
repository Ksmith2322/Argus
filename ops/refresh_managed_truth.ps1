param(
    [switch]$IncludeSummary,
    [int]$AcceptExistingAgeSeconds = 900
)

Set-Location "C:\Argus\repo"

$python = "C:\Argus\.venv\Scripts\python.exe"
$args = @("-m", "argus_flow.ops.refresh_managed_truth", "--accept-existing-age-s", "$AcceptExistingAgeSeconds")
if ($IncludeSummary) {
    $args += "--include-summary"
}

& $python @args
exit $LASTEXITCODE
