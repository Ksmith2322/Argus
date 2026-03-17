# Dump all bt_summary JSON files as one-per-line to stdout
Get-ChildItem "C:\Argus\repo\ops\logs\bt_summary_bt_*.json" | ForEach-Object {
    Get-Content $_.FullName -Raw
}