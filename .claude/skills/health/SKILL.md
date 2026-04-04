---
name: health
description: Check system health across all Argus + Greek family strategies. Shows runner status, heartbeats, trade counts, and issues.
allowed-tools: Bash Read
---

Check the health of the entire Argus multiverse:

1. **Check all Python runner processes**:
   ```bash
   powershell.exe -NoProfile -Command "Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object { try { $cmd=(Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.Id)\").CommandLine; if($cmd -match 'runner') { $name = if($cmd -match 'apollo'){'APOLLO'}elseif($cmd -match 'hermes'){'HERMES'}elseif($cmd -match 'helio\.runner[^_]'){'HELIO'}elseif($cmd -match 'runner_unified'){'ARGUS'}elseif($cmd -match 'watchdog'){'WATCHDOG'}else{'OTHER'}; Write-Host \"$name PID=$($_.Id)\" } } catch {} }"
   ```

2. **Check dashboard**: `curl -s http://127.0.0.1:8080/api/system_health`

3. **Check Argus heartbeats** — read heartbeat.json from `argus_flow/logs/{eurusd,audjpy,usdjpy,gbpusd}/`

4. **Check Helio family heartbeats** — read heartbeat.json from `helio/logs/*/`

5. **Count trades per strategy** — read trades.csv from each log dir

6. **Report issues**: stale heartbeats (>10min for Argus, >2hr for Helio), dead runners, duplicate processes

Present as a clean summary table showing: Strategy | Symbol | Status | Position | Trades | Last Heartbeat
