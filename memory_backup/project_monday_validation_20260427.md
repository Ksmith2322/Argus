---
name: Monday 2026-04-27 validation — first real fills check
description: First market day after the 2026-04-24 full-fleet IBKR execution conversion. Monitor in this order. Fix issues as they appear, not preemptively.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## When user returns Monday (or any post-conversion day)

Run this exact sequence to validate real-execution. Skip ahead if early checks pass cleanly.

### 1. Did anything fire overnight via cohort report?
- Check `ares/logs/orders.csv` (was added, may not exist yet)
- Check `forge/logs/gdx_gld/runner.log` for last cycle
- Check `argus_flow/logs/cohort_report_*.json` from 2026-04-25 (Sat) and 2026-04-26 (Sun)

### 2. Broker truth check (the canary)
```powershell
(Invoke-WebRequest -Uri 'http://localhost:8080/api/gateway_status' -UseBasicParsing).Content | ConvertFrom-Json
```
- `broker_equity_usd` ≠ $11,815.11 → **real orders landed** (success)
- `broker_equity_usd` = $11,815.11 still → **no real fills yet** (could be normal if no signals fired, OR could mean orders are silently failing)

### 3. Per-system order logs
Each Greek runner writes to its own `orders.csv`:
- `apollo/logs/orders.csv`
- `hermes/logs/orders.csv`
- `titan/logs/orders.csv`
- `ares/logs/orders.csv`

If trades are firing but these CSVs are empty → IBKRExecutor isn't actually submitting. Check for "REAL_ENTRY FAILED" or connection errors in each runner's log.

### 4. Fleet pulse
- `/api/fleet_health` → all 25 systems OK
- `/api/positions_open` → if open positions > 0 and `total_risk_usd > 0`, sized correctly
- `/api/exit_reasons` → distribution post-2026-04-24 should reflect new sizing

### 5. Sizing sanity check
Most likely visible regression: open SPY positions should be ~$11,800 max (was ~$23,500 before sizing fix). Multi_orb each position should be ~$2,950 max. If you see big positions back, the runner didn't pick up the v6 sizing config.

## Most likely failure modes (predicted)

In order of likelihood:

1. **MYM contract qualification fails** in mamba/tori/cuebanks — first time those new live paths run. `ib.qualifyContracts(contract)` may need a specific expiry month for futures, not just symbol. Symptom: log shows "REAL_ENTRY EXCEPTION" with contract qualification error.

2. **TWS disconnects under load** — we have 22+ client IDs. TWS default max is 32 but stress matters. Symptom: multiple runners go DOWN simultaneously, gateway_status shows broker_equity=$0 and stale.

3. **Greek scanners (apollo/hermes/titan) fail silently** — the IBKRExecutor in `helio/ibkr_executor.py` may have stale assumptions about TWS state since it was last used. Symptom: scanner logs show signals but `orders.csv` stays empty.

4. **Boot sequencing on TWS restart** — already known. argus must come up first to write broker_truth, otherwise downstream runners fail with `BrokerEquityUnavailableError`. Manual fix: `python -m argus_flow.ops.risk_oversight` from repo dir to refresh.

## Fix-as-you-go posture

User explicitly said: "validate once again Monday and fix any issues that might come up." Do not preemptively fix things that aren't broken. Wait for the failure, diagnose the actual signal, then fix.

## What NOT to do Monday

- Do not start new strategy work (valuation/penny/dividend roadmap)
- Do not refactor execution code unless something is broken
- Do not add more dashboard panels — feedback is "lean dashboard, one place per data point"
- Do not convert any new strategy types — the 22 we have is the fleet
