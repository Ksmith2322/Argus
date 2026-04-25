---
name: health
description: Check system health across all Argus + Greek family strategies. Shows runner status, heartbeats, trade counts, and issues.
allowed-tools: Bash Read
---

## Live snapshot (pre-loaded at skill invocation)

**Gateway + broker truth:**
!`curl -s http://localhost:8080/api/gateway_status 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); print(f'broker_equity=\${d[\"broker_equity_usd\"]:,.2f} healthy={d[\"all_healthy\"]} oversight_age={d.get(\"risk_oversight_age_s\",\"?\")}s')" 2>/dev/null`

**Fleet status (sys → status):**
!`curl -s http://localhost:8080/api/fleet_health 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); ok=sum(1 for s in d['systems'].values() if s['status']=='OK'); n=len(d['systems']); print(f'  {ok}/{n} OK'); [print(f'  BAD: {k}={v[\"status\"]} (age={v.get(\"max_age_s\",\"?\")}s)') for k,v in d['systems'].items() if v['status']!='OK']"`

**Open positions + risk:**
!`curl -s http://localhost:8080/api/positions_open 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); print(f'  open={d[\"count\"]}  risk=\${d[\"total_risk_usd\"]}  budget_used={d[\"pct_of_budget_used\"]}%')"`

---

Use the snapshot above as the primary state. Drill deeper only if something looks off:

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
