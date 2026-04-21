---
name: audit
description: Comprehensive fleet sanity check — processes, locks, config drift, scheduled tasks, pending issues. One-stop answer to "is everything actually healthy?"
allowed-tools: Bash Read
---

Run a full operational audit across the fleet. Find problems before they bite.

## 1. Running processes
```bash
tasklist /FI "IMAGENAME eq python.exe" /FO CSV | grep -v "Image Name" | wc -l
tasklist /FI "IMAGENAME eq powershell.exe" /FO CSV | grep -v "Image Name" | head -5
```
Identify: runner_unified, dashboard, watchdog, fleet_monitor. Flag if any are missing or if there are duplicates.

## 2. Broker state
```bash
curl -s http://localhost:8080/api/gateway_status | python -c "import sys,json; d=json.load(sys.stdin); print(f'equity=\${d[\"broker_equity_usd\"]:,.2f} healthy={d[\"all_healthy\"]} pause={d[\"pause_entries_present\"]}'); [print(f'  {k} pos={v[\"position\"]} age={v[\"age_s\"]}s blocked={v[\"entries_blocked\"]}') for k,v in d['per_pair'].items()]"
```
Flag any pair stale (>600s) or with unexpected position or blocked entries.

## 3. Drawdown state
```bash
C:/Argus/.venv/Scripts/python.exe -c "import json; s=json.load(open('C:/Argus/repo/argus_flow/logs/_risk/portfolio_risk_state.json')); print(f'drawdown_pause={s[\"drawdown_pause\"]} peak={s[\"peak_pnl\"]:.2f}R current={s[\"current_pnl\"]:.2f}R')"
```

## 4. Stale locks
```bash
ls -la C:/Argus/repo/argus_flow/logs/_locks/*.lock
```
Flag any .lock file >24h old whose paired .json shows dead pid.

## 5. Scheduled tasks
```bash
for t in ArgusCohortReport ArgusWatchdog ArgusGldPmLoop; do
  echo "=== $t ==="
  schtasks /query /TN "$t" /FO LIST /V 2>&1 | grep -E "Status|Logon Mode|Last Result|Next Run Time" | head -4
done
```
Flag: Logon Mode ≠ "Interactive/Background" (should survive logoff), Last Result ≠ 0 (last run failed).

## 6. Config drift
```bash
C:/Argus/.venv/Scripts/python.exe -c "
from argus_flow.ops.fleet_registry import discover_managed_runners
for r in discover_managed_runners():
    print(f'  {r[\"symbol\"]:8s} stage={r[\"current_stage\"]:8s} config={r[\"config_file\"]}')"
```
Flag: any pair at stage≠paper that the runner is loaded with (or vice versa).

## 7. Canonical vs per-strategy divergence
Run the /reconcile skill's comparison. Flag any MISSING_CANONICAL_DUAL_WRITE or SCHEMA_DRIFT.

## 8. Apollo forward returns freshness
```bash
ls -la C:/Argus/repo/apollo/logs/forward_returns.jsonl 2>&1
wc -l C:/Argus/repo/apollo/logs/forward_returns.jsonl 2>&1
```
Flag if file is >48h old (the cohort report should refresh it nightly).

## 9. Pending holdout windows
```bash
ls C:/Argus/repo/strategy_confidence/_holdout/*.json 2>&1 | wc -l
```
If zero OOS reports exist yet, note that windows open ~2026-05-21.

## 10. Git hygiene
```bash
cd C:/Argus/repo && git status --short | head -10
cd C:/Argus/repo && git rev-list --count origin/phase6-hardening..HEAD 2>&1
```
Flag: uncommitted changes, unpushed commits >10.

## Output format
One clean summary section per check. Each check returns OK / WARN / CRITICAL with one-line reason. End with a 3-line verdict: "N checks OK, N WARN, N CRITICAL — actionable items below".
