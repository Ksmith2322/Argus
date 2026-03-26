# argus_flow/dr/ — Disaster Recovery & Performance Records

This folder contains:

| File | Purpose |
|------|---------|
| `DR_RUNBOOK.md` | Disaster recovery procedures (crash, disconnect, reboot, corrupt state) |
| `PERFORMANCE_SUMMARY.md` | Current cohort performance snapshot (replaces RESEARCH dashboard tab) |

## Quick Commands

```powershell
# Check runner health
type argus_flow\logs\eurusd\heartbeat.json

# Run daily cohort report
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.daily_report

# Run promotion gate check
C:\Argus\.venv\Scripts\python.exe -m argus_flow.ops.promotion_gate

# Manual runner start
C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --configs argus_flow/configs/gbpusd_range_paper_v1.json argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/eurjpy_t4_paper_v1.json
```
