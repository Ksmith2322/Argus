---
name: watchdog
description: Check the meta-infrastructure — watchdog running? fleet_monitor running? scheduled tasks firing? Catches "supervisor died and nobody knows" scenarios.
allowed-tools: Bash
---

Verify the fleet is actually being supervised.

1. **Is watchdog.ps1 running?**
   ```bash
   wmic process where "name='powershell.exe'" get ProcessId,CommandLine /format:csv 2>&1 | grep -i watchdog | head -3
   ```
   Flag if no match — watchdog is down.

2. **Watchdog log health**:
   ```bash
   tail -15 C:/Argus/repo/argus_flow/logs/watchdog.log 2>&1 | tail -10
   ```
   Watch for:
   - "MAX RESTARTS reached" → give-up state
   - "FX runner NOT FOUND" repeating → false-positive loop (expected-set mismatch)
   - "HEARTBEAT" lines every ~60s → healthy

3. **fleet_monitor process**:
   ```bash
   wmic process where "name='python.exe'" get ProcessId,CommandLine /format:csv 2>&1 | grep -i fleet_monitor | head -3
   ```

4. **fleet_status.json freshness** (fleet_monitor writes this):
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import os, time
   p = 'C:/Argus/repo/argus_flow/logs/fleet_status.json'
   age = time.time() - os.path.getmtime(p)
   print(f'  fleet_status.json age: {age:.0f}s ({\"STALE\" if age > 600 else \"fresh\"})')"
   ```

5. **Scheduled task last-run results**:
   ```bash
   for t in ArgusCohortReport ArgusWatchdog ArgusGldPmLoop; do
     echo "=== $t ==="
     schtasks /query /TN "$t" /FO LIST /V 2>&1 | grep -E "Status|Last Run Time|Last Result|Logon Mode|Next Run" | head -5
   done
   ```
   Flag:
   - Last Result ≠ 0 (task failed)
   - Logon Mode = "Interactive only" (won't survive disconnect)
   - Next Run Time N/A on ONLOGON tasks (won't fire automatically)

6. **Verdict**: "All supervisors healthy" OR list specific what's dead + recommended restart.

If watchdog or fleet_monitor is dead, suggest:
```
# Restart watchdog
powershell.exe -NonInteractive -NoProfile -ExecutionPolicy Bypass -File C:/Argus/repo/ops/watchdog.ps1

# Restart fleet_monitor
cd C:/Argus/repo && C:/Argus/.venv/Scripts/python.exe -m helio.fleet_monitor --interval-s 60 --no-restart
```

Context: 2026-04-21 incident — runner_unified died quietly at 02:25 UTC, watchdog was dead itself (since 3/31), nothing auto-restarted. This skill catches that class of blindness in <30s.
