# Data-feed runbook

Codex gap #2 — operator playbook for keeping the production data cache
fresh and recovering when it isn't.

## What "data feed" means here

Every daily-bar strategy declares a contract in
[helio/data_feed_contract.py](../helio/data_feed_contract.py) that pins:

- **Primary source** — where the live runner pulls from (yfinance today).
- **Fallback cache** — the on-disk CSV cache at
  `helio/data_yfinance/<TICKER>_daily.csv`. This is the production
  source of truth for backtests + the recovery path when yfinance is
  down. If the cache is missing or stale, **the strategy has no
  insurance against a live data outage**.
- **Universe** — the tickers the strategy needs.
- **Freshness budget** — max age before the cache is "stale" (RED).

## Daily refresh (scheduled)

The cache is kept fresh by `ops/maintenance/refresh_data_feeds.py`.

```powershell
# One-shot manual run (incremental)
C:\Argus\.venv\Scripts\python.exe -m ops.maintenance.refresh_data_feeds

# Force a full re-pull (use only if you suspect cache corruption)
C:\Argus\.venv\Scripts\python.exe -m ops.maintenance.refresh_data_feeds --refresh
```

### Recommended Windows Task Scheduler entry

- **Name:** `ArgusDataFeedRefresh`
- **Trigger:** Daily 22:00 UTC (= 17:00 ET / 18:00 ET DST) — runs after
  the US close so the most recent bar is included.
- **Action:** Start program
  - Program: `C:\Argus\.venv\Scripts\python.exe`
  - Arguments: `-m ops.maintenance.refresh_data_feeds`
  - Start in: `C:\Argus\repo`
- **Conditions:** Run whether user is logged on or not; if missed,
  run as soon as possible.

The refresh script writes a JSON report to
`ops/reports/system_audit/data_feed_refresh.json` and returns:

- exit `0` — all contracts GREEN
- exit `1` — at least one YELLOW after refresh (still partially usable)
- exit `2` — at least one RED (live source is broken; intervene)

## Manual verification

`ops/audit/run_data_feed_contracts.py` runs the offline check only
(no network calls):

```powershell
C:\Argus\.venv\Scripts\python.exe -m ops.audit.run_data_feed_contracts
```

It also runs as part of `run_fleet_snapshot.py` — look for the
"Data-feed contracts" section.

## Decision matrix

| Symptom | Action |
|---|---|
| GREEN, all contracts | Nothing — cache is healthy. |
| YELLOW, some tickers stale | Run `refresh_data_feeds.py`. If still YELLOW, check whether the stale ticker has been delisted (e.g. ETF closed). |
| RED, tickers missing | Run `refresh_data_feeds.py --refresh`. If still RED, yfinance is rejecting the ticker — check the symbol upstream (Yahoo Finance UI) or use IBKR historical-bars download as a one-off rebuild. |
| RED, all tickers stale | Likely an extended outage of the refresh job or yfinance. Pause monthly-rebalance strategies until refresh succeeds; backtests can still use the cache as-is. |

## Pre-rebalance checklist

Before any monthly rebalance fires (currently: forge_xs_momentum on
the 1st of the month):

1. Run `run_data_feed_contracts.py`. Must be GREEN for the strategy's
   universe.
2. If YELLOW or RED, run `refresh_data_feeds.py` and re-verify.
3. Snapshot the latest cache row date in your run log — this is the
   "as-of" timestamp for the rebalance decision.

## Wiring it into the preflight

[helio/real_money_preflight.py](../helio/real_money_preflight.py)
should call `verify_contract(CONTRACTS[strategy])` as part of its
`check_evidence_quality` chain so the cache-RED state propagates into
the preflight verdict. Not wired automatically — review against the
strategy's specific blocker tolerance before activating.
