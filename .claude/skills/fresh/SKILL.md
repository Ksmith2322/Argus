---
name: fresh
description: Quick staleness scan. Flags any artifact older than its expected refresh cadence. Early-warning system for silent pipeline failures.
allowed-tools: Bash
---

Scan key artifacts for staleness and flag anything suspicious.

```bash
C:/Argus/.venv/Scripts/python.exe -c "
import os, time
from datetime import datetime
REPO = 'C:/Argus/repo'

# (path, human name, expected max age in hours)
ARTIFACTS = [
    ('argus_flow/logs/fleet_status.json',            'fleet_status',              0.5),
    ('argus_flow/logs/fleet_state.json',             'fleet_state',               1.0),
    ('argus_flow/logs/fleet_perf_summary.json',      'fleet_perf',                30),
    ('argus_flow/logs/reconciliation_report.json',   'reconciliation_report',     6),
    ('argus_flow/logs/canonical_fills.jsonl',        'canonical_fills',           48),
    ('argus_flow/logs/_risk/portfolio_risk_state.json','portfolio_risk',          0.1),
    ('argus_flow/logs/risk_oversight_report.json',   'risk_oversight',            0.2),
    ('apollo/logs/forward_returns.jsonl',            'apollo_forward_returns',    48),
    ('argus_flow/logs/kill_discipline_report.json',  'kill_discipline',           30),
    ('argus_flow/logs/promotion_readiness_report.json','promotion_readiness',     30),
    ('argus_flow/logs/signal_frequency_report.json', 'signal_frequency',          30),
    ('argus_flow/logs/slippage_report.json',         'slippage_report',           48),
    ('argus_flow/logs/watchdog.log',                 'watchdog.log',              0.5),
]

now = time.time()
print(f'{\"artifact\":30s} {\"age\":>10s}  status')
print('-' * 60)
critical = 0; warnings = 0; ok = 0
for rel, name, max_hours in ARTIFACTS:
    p = os.path.join(REPO, rel.replace('/', os.sep))
    if not os.path.exists(p):
        print(f'  {name:30s} {\"MISSING\":>10s}  CRITICAL')
        critical += 1; continue
    age_h = (now - os.path.getmtime(p)) / 3600
    if age_h > max_hours * 3:
        status = 'CRITICAL'; critical += 1
    elif age_h > max_hours:
        status = 'WARN'; warnings += 1
    else:
        status = 'OK'; ok += 1
    print(f'  {name:30s} {age_h:>8.1f}h  {status}  (max {max_hours}h)')
print()
print(f'Summary: {ok} OK, {warnings} WARN, {critical} CRITICAL')

# Holdout window status
HOLDOUT_DIR = os.path.join(REPO, 'strategy_confidence', '_holdout')
if os.path.isdir(HOLDOUT_DIR):
    import json
    from datetime import timezone
    nowdt = datetime.now(timezone.utc)
    any_open = False
    for fn in sorted(os.listdir(HOLDOUT_DIR)):
        if not fn.endswith('.json'): continue
        try:
            d = json.load(open(os.path.join(HOLDOUT_DIR, fn)))
        except: continue
        window_open = d.get('window_open', False)
        if window_open:
            any_open = True
            print(f'  HOLDOUT WINDOW OPEN: {d.get(\"strategy\",fn)}  verdict={d.get(\"verdict\",\"?\")}')
        else:
            earliest = d.get('oos_eval_earliest','')
            try:
                eta = datetime.fromisoformat(earliest.replace('Z','+00:00'))
                days = (eta - nowdt).total_seconds() / 86400
                if 0 < days <= 7:
                    print(f'  HOLDOUT WINDOW OPENS IN {days:.0f}d: {d.get(\"strategy\",fn)}')
            except: pass
    if not any_open:
        print()
        print('  (no holdout windows currently open)')
"
```

Interpret the output:
- **CRITICAL** (age > 3x expected): pipeline is broken, investigate immediately
- **WARN** (age > 1x expected): scheduled task may have missed a cycle
- **OK**: expected state

End with a one-line verdict:
- "All pipelines fresh" if zero CRITICAL/WARN
- "1 WARN: apollo_forward_returns 52h stale — check ArgusCohortReport last result" with specifics

Holdout windows open ~30 days after frozen_at (first batch ~2026-05-21). This skill flags the 7-day pre-warning so you're not caught off-guard.
