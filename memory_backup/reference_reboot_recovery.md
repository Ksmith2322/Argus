---
name: Reboot recovery runbook
description: Steps to bring the trading fleet back up after a machine reboot. All data persists across reboot (canonical_fills, artifacts, CSVs); only running processes need to be restarted.
type: reference
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
## What IS persisted (no recovery needed)
- All canonical_fills in `argus_flow/logs/canonical_fills.jsonl` (95+ entries)
- All 21 strategy_confidence artifacts
- All per-strategy trade CSVs
- All code changes, schema, memory

## What MUST be restarted after reboot
Three Python processes + TWS on the user's machine.

### 1. IBKR TWS (manual, user-side)
- Launch Trader Workstation
- Log into paper account (`DUP472829`, ~$1M paper equity)
- **Accept the Paper Trading Disclaimer popup** (error 10141 blocks API otherwise)
- Verify API port 7497 listening: File → Global Configuration → API → Settings → Enable ActiveX + Socket Clients

### 2. Argus runner (3 FX pairs)
```
cd C:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m argus_flow.runner_unified \
  --configs argus_flow/configs/usdjpy_mtf_paper_v1.json \
             argus_flow/configs/gbpusd_range_paper_v1.json \
             argus_flow/configs/cadjpy_mtf_paper_v1.json
```

If it fails with `DUPLICATE_RUNNER_BLOCKED`, clear stale locks:
```
rm -f C:/Argus/repo/argus_flow/logs/_locks/runner_c5284_*
```

### 3. Dashboard
```
cd C:/Argus/repo
C:/Argus/.venv/Scripts/python.exe ops/dashboard.py --port 8080
```
Browse to `http://localhost:8080`.

### 4. Fleet monitor daemon (optional but recommended)
```
cd C:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m helio.fleet_monitor --interval-s 60 --no-restart
```
Without this, `fleet_status.json` goes stale and the dashboard's `/api/health` reports DEGRADED.

## Verification checklist
```
curl http://localhost:8080/api/gateway_status   # all_healthy: true, broker_equity: ~$1M
curl http://localhost:8080/api/health           # overall: OK
curl http://localhost:8080/api/strategy_performance | head -20
```

All 3 FX pairs should show `fresh: true` and `broker_connected: true` within ~30 seconds of runner startup.

## If anything goes wrong
- **Runner connects but subscriptions timeout**: TWS disclaimer not accepted — go back to TWS and click through it.
- **Runner says DUPLICATE_RUNNER_BLOCKED**: stale lock file — clear as above.
- **Dashboard port 8080 in use**: `netstat -ano | grep :8080 | grep LISTENING | awk '{print $NF}' | xargs -I{} taskkill //PID {} //F`
- **fleet_status.json stale**: fleet_monitor daemon died — restart it.

## Nothing at risk on reboot
Positions were FLAT across all 3 FX pairs as of the last snapshot. No open orders. No in-flight state that wouldn't survive a hard power cycle. The runners are stateless — they re-reconcile with IBKR on startup.
