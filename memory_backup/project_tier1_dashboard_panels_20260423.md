---
name: Tier 1 operational dashboard panels (5/1 review prep) — 2026-04-23
description: 5 new dashboard panels added ahead of 5/1 review — Position Monitor, Risk Exposure, Exit Reasons, Promotion Ladder, Per-strategy equity curves. Data is live, not static.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## What shipped

Five new operational panels in `ops/dashboard.py` to make the system legible at a glance before the 5/1 review. All are JSON-driven (no static HTML) and auto-refresh.

**Endpoints:**
- `/api/positions_open` — scans every `heartbeat.json` for open_trade(s); aggregates total risk, % of anchor, % of fleet 6% budget used
- `/api/exit_reasons` — per-strategy exit reason distribution post-reset cutoff (2026-04-23T14:00Z)
- `/api/promotion_ladder` — per-strategy current tier + progress_pct toward next tier
- Reuses `/api/fleet_equity_curve` for per-strategy small-multiples (client-side grouping by `.strategy` field)

**UI panels** (all inserted in the `maturity-summary-banner` block region near top of page):
1. `risk-exposure-banner` — colored bar (green <50% / yellow 50-80% / red >80% of budget)
2. `open-positions-panel` — table of every live position with direction/entry/stop/target/size/risk
3. `promotion-ladder-panel` — progress bars per strategy toward next tier
4. `exit-reasons-panel` — stacked-bar distribution (target/stop/time) per strategy
5. `per-strategy-equity-panel` — small-multiples grid (one mini equity curve per strategy, sorted by |PnL| impact) — sits under `fleet-equity-panel`

## What the data is showing right now (2026-04-23)

- 3 open positions: gld_pm_long, nq_overnight, vix_intraday — total risk $237.69 = **33.5%** of $708.91 fleet budget. Well under cap.
- Anchor: $11,815.11 (broker truth)
- Exit reasons confirm the spy_mean_rev v2 diagnosis from `project_spy_mean_rev_v2_trend_filter_20260423.md`:
  - spy_mean_rev: 18 trades, 11 stops / 7 targets (61.1% stop rate)
  - multi_orb: 19 trades, 68% stops
  - vix_intraday: 7 trades, 57% stops
- Promotion ladder: 23 strategies tracked; forge_multi_orb and forge_spy_mean_rev already show progress_pct=100 (i.e. at boundary — tier math may need a sanity check when we look ahead)

## Why this was built now

**Why:** User asked "if its data we can use for 5/1 to know what updates we have to do i say do it now." The 5/1 review is a decision point on each strategy's disposition. Operational data needs to be visible at a glance, not reconstructed from JSONL files.

**How to apply:** When user next asks about 5/1 review or fleet state, reference these panels rather than reading raw artifacts. Open the dashboard at `http://localhost:8080/` — the five new panels sit between the maturity summary banner and the fleet equity curve. If any panel shows stale data >5 min, check managed_truth_loop daemon (it writes the underlying artifacts every 3 min).

## Dashboard process notes

- Before restart there were TWO dashboard processes (zombie venv + live system-python). Cleaned up — only one venv python process should be running on port 8080 now.
- To restart cleanly: `Get-NetTCPConnection -LocalPort 8080 -State Listen` → `Stop-Process -Id <pid>` → launch via `Start-Process -FilePath 'C:\Argus\.venv\Scripts\python.exe' -ArgumentList 'ops/dashboard.py','--port','8080' -WorkingDirectory 'C:\Argus\repo' -WindowStyle Hidden`

## Unresolved / watch

- Promotion ladder shows progress_pct=100 for strategies that haven't actually promoted — the distance-to-next-tier math may be capping early. Sanity-check the endpoint's tier boundary logic if it keeps looking off.
- Per-strategy equity curves only show strategies with live trades in the last 90 days (respects the live_cutoff exclusion). If a strategy has no visible mini-chart, that's the cause — not a rendering bug.
