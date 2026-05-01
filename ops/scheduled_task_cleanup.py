"""scheduled_task_cleanup — propose deletion of obsolete Argus scheduled tasks.

Many Argus scheduled tasks predate the move to always-on Python runners
(`argus_flow.runner_unified`, the forge family, `managed_truth_loop`). Those
old per-strategy LogonTrigger tasks now sit registered but never fire
successfully (last_result=4294967295 = "task did not start") because the
forge runners they tried to launch are already up via different mechanisms.

This script LISTS deletion candidates and prints the exact PowerShell command
to remove them. It does NOT auto-delete — operator must review and run the
unregister command themselves. Reasoning:

  - Task deletion is destructive (no undo without re-creating the task).
  - Some "stale" tasks may be intentionally disabled pending re-design.
  - The set varies as the fleet evolves; a one-time review is safer than
    a recurring auto-cleanup.

Categories surfaced:
  DEPRECATED_RUNNER  — last_result=4294967295 (the "did not start" sentinel),
                       LogonTrigger pointing at a strategy that's now run
                       by a forge runner. Safe to delete.
  DISABLED_STALE     — task is Disabled and hasn't run in >30d. Probably
                       superseded; delete unless operator remembers why.
  BOOT_TRIGGER_STALE — BootTrigger task whose process hasn't run since the
                       last boot. NOT a delete candidate — operator should
                       restart the underlying process or reboot.
  USB_BACKUP_FAIL    — ArgusUSBBackup last_result=1 indicates "no USB drive
                       detected". Operator action: plug in USB.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.scheduled_task_cleanup
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _query_tasks() -> list[dict]:
    if sys.platform != "win32":
        print("This script only runs on Windows.")
        sys.exit(0)
    ps = (
        "Get-ScheduledTask -TaskName 'Argus*' -ErrorAction SilentlyContinue | "
        "ForEach-Object { $info = $_ | Get-ScheduledTaskInfo; "
        "$trigType = if ($_.Triggers) { $_.Triggers[0].CimClass.CimClassName -replace 'MSFT_Task','' } else { 'None' }; "
        "[PSCustomObject]@{ TaskName=$_.TaskName; State=[string]$_.State; "
        "LastResult=$info.LastTaskResult; LastRunTime=$info.LastRunTime.ToString('o'); "
        "TriggerType=$trigType } } | ConvertTo-Json -Compress"
    )
    res = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0 or not res.stdout.strip():
        print(f"PowerShell query failed (rc={res.returncode})")
        return []
    data = json.loads(res.stdout)
    if isinstance(data, dict):
        data = [data]
    return data


def _age_days(last_run: str) -> float:
    try:
        lr = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
        if lr.tzinfo is None:
            lr = lr.replace(tzinfo=timezone.utc)
        if lr.year < 2020:
            return 9999.0
        return (datetime.now(timezone.utc) - lr).total_seconds() / 86400.0
    except Exception:
        return 9999.0


def main() -> int:
    tasks = _query_tasks()
    if not tasks:
        return 1

    deprecated_runners: list[dict] = []
    disabled_stale: list[dict] = []
    boot_stale: list[dict] = []
    usb_fail: list[dict] = []
    healthy: list[dict] = []

    for t in tasks:
        name = t["TaskName"]
        last_result = str(t.get("LastResult", ""))
        state = (t.get("State") or "").upper()
        trig = t.get("TriggerType") or ""
        age = _age_days(t.get("LastRunTime", ""))

        # 4294967295 = 0xFFFFFFFF = -1 = "task did not start" sentinel
        if last_result == "4294967295" and "Logon" in trig:
            deprecated_runners.append({**t, "age_days": age})
        elif state == "DISABLED" and age > 30:
            disabled_stale.append({**t, "age_days": age})
        elif "Boot" in trig and age > 7 and last_result != "0":
            boot_stale.append({**t, "age_days": age})
        elif name == "ArgusUSBBackup" and last_result == "1":
            usb_fail.append({**t, "age_days": age})
        elif last_result == "0":
            healthy.append({**t, "age_days": age})

    print("=" * 78)
    print("  ARGUS SCHEDULED TASK CLEANUP REVIEW")
    print("=" * 78)
    print(f"  Total Argus tasks: {len(tasks)}   Healthy: {len(healthy)}")
    print()

    if deprecated_runners:
        print(f"  DEPRECATED_RUNNER ({len(deprecated_runners)}) — superseded by forge/argus runners")
        print("  (LogonTrigger tasks that never start; safe to delete)")
        for t in deprecated_runners:
            print(f"    - {t['TaskName']:32s}  age={t['age_days']:>4.0f}d")
        print()
        print("  PowerShell to delete (operator review):")
        names = ",".join(f"'{t['TaskName']}'" for t in deprecated_runners)
        print(f"    {names} | ForEach {{ Unregister-ScheduledTask -TaskName $_ -Confirm:$false }}")
        print()

    if disabled_stale:
        print(f"  DISABLED_STALE ({len(disabled_stale)}) — disabled + not run in >30d")
        for t in disabled_stale:
            print(f"    - {t['TaskName']:32s}  age={t['age_days']:>4.0f}d")
        print()

    if boot_stale:
        print(f"  BOOT_TRIGGER_STALE ({len(boot_stale)}) — process needs restart, NOT delete")
        for t in boot_stale:
            print(f"    - {t['TaskName']:32s}  age={t['age_days']:>4.0f}d  last_result={t['LastResult']}")
        print()
        print("  Restart manually:")
        for t in boot_stale:
            print(f"    Start-ScheduledTask -TaskName '{t['TaskName']}'")
        print()

    if usb_fail:
        print(f"  USB_BACKUP_FAIL ({len(usb_fail)}) — operator action: plug in USB drive")
        for t in usb_fail:
            print(f"    - {t['TaskName']:32s}  age={t['age_days']:>4.0f}d  last_result={t['LastResult']}")
        print()

    print("=" * 78)
    print("  This script does NOT auto-delete. Review the lists, then run the")
    print("  PowerShell commands above for the categories you accept.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
