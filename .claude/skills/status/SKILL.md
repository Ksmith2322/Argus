---
name: status
description: Quick one-line status of the Argus multiverse. Shows runners alive, trades, positions, and any critical issues.
allowed-tools: Bash
---

## Pre-loaded one-line status

!`curl -s http://localhost:8080/api/fleet_health 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); ok=sum(1 for s in d['systems'].values() if s['status']=='OK'); n=len(d['systems']); print(f'fleet={ok}/{n} OK')"` !`curl -s http://localhost:8080/api/positions_open 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); print(f' | open={d[\"count\"]}/risk=\${d[\"total_risk_usd\"]}')"` !`curl -s http://localhost:8080/api/gateway_status 2>/dev/null | C:/Argus/.venv/Scripts/python.exe -c "import sys,json; d=json.load(sys.stdin); print(f' | broker=\${d[\"broker_equity_usd\"]} | healthy={d[\"all_healthy\"]}')"`

---

Quick status check — one concise summary:

1. `curl -s http://127.0.0.1:8080/api/system_health` — get runners alive/total, status
2. `curl -s http://127.0.0.1:8080/api/greek_family` — get Helio family alive/total
3. Count total trades across `argus_flow/logs/*/trades.csv` and `helio/logs/*/trades.csv`
4. Check for any open positions (non-FLAT state files)

Output ONE line like:
"Argus: 24/38 runners | Helio: 12/16 alive | 21 trades | 0 in trade | Status: WARN"

If there are critical issues (runners dead, broker disconnected), add a second line with the issue.
