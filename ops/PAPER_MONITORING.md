# Paper Monitoring

Use this during the observation week when the goal is to watch, not change.

## Main Command

Run:

```powershell
.\ops\paper_monitor_status.ps1
```

Or:

```powershell
C:\Argus\.venv\Scripts\python.exe ops\paper_monitor_status.py
```

## What Good Looks Like

- Every line shows `OK`.
- Managed FX may show `blocked:FRIDAY_CLOSE (expected)` over the weekend. That is normal.
- `Portfolio guard BLOCKED` can be normal when family or directional limits are already in use.
- `Divergence WATCH` is a monitoring signal, not an outage.

## What Needs Attention

- Any system showing `STALE` or `DOWN`.
- `broker_connected=false` on active trading runners.
- `reconciliation` drifting away from `CLEAN_FLAT` when flat.
- Managed FX runners staying at zero signal flow after the market is open.

## Helpful Drill-Down Files

- `argus_flow/logs/fleet_monitor.log`
- `argus_flow/logs/deployment_registry.json`
- `argus_flow/logs/divergence_report.json`
- `helio/logs/portfolio_guard.json`

## Notes

- The snapshot command exits with code `1` if any monitored system is stale or down.
- This is a read-only monitoring command. It does not restart or modify anything.
