# ops/diff_last_two_events.ps1
$ErrorActionPreference = "Stop"

$logs = "C:\Argus\repo\ops\logs"

$last2 = Get-ChildItem $logs -Filter "events_bt_*.csv" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 2

if ($last2.Count -lt 2) { throw "Need at least 2 events files in $logs" }

$eA = $last2[1].FullName
$eB = $last2[0].FullName

$normA = "$eA.norm"
$normB = "$eB.norm"

# Drop emit_ts + run_id (first 2 CSV columns) so backtest payload is diffable
Get-Content $eA | ForEach-Object {
  $p = $_ -split ",", 3
  if ($p.Length -ge 3) { $p[2] } else { $_ }
} | Set-Content -Encoding UTF8 $normA

Get-Content $eB | ForEach-Object {
  $p = $_ -split ",", 3
  if ($p.Length -ge 3) { $p[2] } else { $_ }
} | Set-Content -Encoding UTF8 $normB

Get-FileHash $normA, $normB | Format-Table Path, Hash
Compare-Object (Get-Content $normA) (Get-Content $normB) | Select -First 40
