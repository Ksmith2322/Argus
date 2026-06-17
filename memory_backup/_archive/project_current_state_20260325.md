---
name: Current System State (2026-03-25)
description: Active runners, process architecture, what's running and how to restart
type: project
---

## Active Processes (as of 2026-03-25 evening)
- **FX Runner** (clientId 1, PID varies): 8 FX pairs — GBPUSD, EURUSD, EURJPY, GBPJPY, CADJPY, AUDJPY, USDJPY, AUDUSD
- **Futures Runner** (clientId 2, PID varies): 7 futures — MES, MNQ, MYM, M2K, MGC, MCL, NKD
- **Dashboard**: ops/dashboard.py on localhost:8080

## Start Commands
```bash
# FX runner
python -m argus_flow.runner_unified --configs argus_flow/configs/gbpusd_range_paper_v1.json argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/eurjpy_t4_paper_v1.json argus_flow/configs/gbpjpy_t4_paper_v1.json argus_flow/configs/cadjpy_t4_paper_v1.json argus_flow/configs/audjpy_t4_paper_v1.json argus_flow/configs/usdjpy_ny_paper_v1.json argus_flow/configs/audusd_ny_paper_v1.json

# Futures runner (MUST use --client-id 2)
python -m argus_flow.runner_unified --client-id 2 --configs argus_flow/configs/mes_range_paper_v1.json argus_flow/configs/mnq_range_paper_v1.json argus_flow/configs/mym_range_paper_v1.json argus_flow/configs/m2k_range_paper_v1.json argus_flow/configs/mgc_range_paper_v1.json argus_flow/configs/mcl_range_paper_v1.json argus_flow/configs/nkd_range_paper_v1.json

# Dashboard
python ops/dashboard.py
```

## Key Changes Made 2026-03-25
- Crypto runners RETIRED (no processes, no scheduled tasks)
- Reconnect fix: fresh session_id minted on reconnect (was tainting all trades)
- Signal frequency fix: promotion_gate now counts ENTRY-only signals
- Dashboard: RESEARCH tab removed, single-page fleet view
- DR folder: argus_flow/dr/ (moved DR_RUNBOOK.md there + PERFORMANCE_SUMMARY.md)
- Regime classifier added to all feature computation (TRENDING/RANGING/CHOPPY)
- Futures switched from vol_burst to range_accel strategy
- New configs: mes/mnq/mym/m2k/mgc/mcl/nkd_range_paper_v1.json
- schemas.py SCHEMA_VERSION bumped to 2 (regime columns added to signals.csv)

**Why:** Captures full system state for session continuity.
**How to apply:** Use start commands above if runners need restart. Check this before making assumptions about what's running.
