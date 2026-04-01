# Argus IBKR FX — Disaster Recovery Runbook
# Last updated: 2026-03-24

## Quick Reference

| Scenario | Action | ETA |
|----------|--------|-----|
| Runner crash | Watchdog auto-restarts (ops/watchdog.ps1) | 1-2 min |
| TWS disconnect | Runner reconnects with backoff (max 200 retries) | 1-5 min |
| PC reboot | Autostart (ops/autostart_runner.ps1) on login | 2 min |
| Corrupt state.json | Runner forces FLAT on next start | Automatic |
| All heartbeats stale | Watchdog kills + restarts after 30 min | 30 min |
| TWS Gateway crash | Restart TWS manually, runner reconnects | 5 min |
| Config file modified | Cohort resets — all trades after change are new cohort | Manual review |
| Git commit changes | git_sha field changes — cohort compliance flags it | Manual review |

## Recovery Procedures

### 1. Runner Won't Start
```
# Check if TWS/Gateway is running
# Check if port 7496 is listening
netstat -an | findstr 7496

# Check runner logs
type argus_flow\logs\gbpusd\heartbeat.json

# Manual start
C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --configs argus_flow/configs/gbpusd_range_paper_v1.json argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/eurjpy_t4_paper_v1.json
```

### 2. Position Stuck (state.json says LONG but should be FLAT)
```
# Run position monitor to check broker vs runner
python -m argus_flow.ops.position_monitor

# If broker is FLAT but state says LONG: runner will auto-correct on next start
# If broker has real position: DO NOT modify state.json — investigate first
```

### 3. Orphaned Position (broker has position, runner says FLAT)
```
# This is CRITICAL — means a position exists that no runner is managing
# 1. Check position_monitor output
python -m argus_flow.ops.position_monitor

# 2. If paper account: position will expire at market close
# 3. If live account: manually close via TWS UI immediately
```

### 4. Full System Recovery (new machine or reinstall)
```
# 1. Clone repo
git clone https://github.com/Ksmith2322/Argus.git C:\Argus\repo

# 2. Create venv
python -m venv C:\Argus\.venv
C:\Argus\.venv\Scripts\pip.exe install -r requirements.txt

# 3. Verify configs
python -m argus_flow.ops.config_check

# 4. Register scheduled tasks
powershell -ExecutionPolicy Bypass -File ops\register_tasks.ps1

# 5. Launch fleet
powershell -ExecutionPolicy Bypass -File ops\launch_fleet.ps1

# 6. Verify
python -m argus_flow.ops.smoke_test
python -m argus_flow.ops.artifact_divergence
```

### 5. Cohort Reset (code or config changed)
```
# If trade logic changed, cohort must reset:
# 1. Note the git_sha that changed
# 2. Existing trades remain but won't count toward promotion
# 3. New trades will have new git_sha
# 4. promotion_gate_v2.py will flag git_sha_consistent as FAIL
# 5. Once 60 new valid trades accumulate with same git_sha, promotion re-eligible
```

### 6. Data Loss Recovery
```
# state.json lost: runner starts FLAT (safe default)
# trades.csv lost: trade history gone, cohort resets
# signals.csv lost: recreated on next signal evaluation
# heartbeat.json lost: recreated every 5 minutes

# USB backup location: check ops/backup_to_usb.ps1 for USB path
# Git backup: daily@02:00 via auto_git_backup.ps1
```

## Monitoring Checklist (daily)
- [ ] Dashboard shows all 3 pairs RUNNING (http://localhost:8080)
- [ ] Heartbeats < 10 min old
- [ ] No DIVERGENT artifacts (python -m argus_flow.ops.artifact_divergence)
- [ ] Discord alerts working (check #argus channel)
- [ ] No KILL flags (python -m argus_flow.ops.kill_discipline)

## Contacts
- IBKR Support: ibkr.com/support
- TWS Gateway docs: interactivebrokers.github.io/tws-api/
