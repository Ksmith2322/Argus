# Argus Full Audit — 20260421_1743

## Fleet
- Equity: **$1,002,851.09** | healthy: True | pause_entries: False
  - usdjpy: pos=FLAT age=3s broker=True blocked=False
  - gbpusd: pos=FLAT age=3s broker=True blocked=False
  - cadjpy: pos=FLAT age=3s broker=True blocked=False
- Python procs: 6
- PS procs: 1

## Risk
- drawdown_pause=False peak=4.966666666666754R current=2.05R dd%=58.72
- breaker_armed=False (floor=10.0R) oversight=GREEN

## Trading Activity
- 24h: 3 trades | 2 wins (67%) | $+14,029.20
- 7d: 4 trades | 2 wins (50%) | $+13,929.87
- 30d: 4 trades | 2 wins (50%) | $+13,929.87
- canonical: 4 live + 96 backfill = 100 total

## Data Integrity
- Divergent (CSV vs canonical): 2 — ['forge_gld_pm_long', 'argus_gbpusd']
- Reconciliation: DRIFT (['argus_usdjpy', 'forge_gld_pm_long'])
- Apollo forward_returns: 153 lines, 18.7h age (fresh)

## Strategy Readiness
- Dispositions: {'scope_down': 14, 'paper_only': 1, 'kill': 1}
- Holdouts frozen: 8
- Strategies with 10+ live trades: 0

## Signal Conversion (24h)
- Signals: 5 | Entries: 1 | conv=20.0%
- Top blocks: {'RECON_DRIFT': 6, 'MTF_BLOCK': 3, 'DRAWDOWN_PAUSE': 2}

## Scheduled Tasks
- ArgusCohortReport: status=Ready last_result=0 logon_mode=Interactive/Background
- ArgusWatchdog: status=Ready last_result=1 logon_mode=Interactive/Background
- ArgusGldPmLoop: status=Ready last_result=1 logon_mode=Interactive/Background
- ArgusNqLondonCloseLoop: **MISSING**
- ArgusAudOrbLoop: **MISSING**
- ArgusMetaWatchdog: **MISSING**
- Flags: ['ArgusWatchdog: last_result=1', 'ArgusGldPmLoop: last_result=1', 'ArgusNqLondonCloseLoop: not registered', 'ArgusAudOrbLoop: not registered', 'ArgusMetaWatchdog: not registered']

## Code Hygiene
- Errors 24h: 31
- Stale locks (>48h): 5
- Uncommitted files: 1
- Commits ahead of origin: 0

## External Deps
- IBKR port 7497 listening: True
- yfinance: reachable=True latency=0.51s bars=5
- Discord failures logged: 0

## Config Drift
- Configs on disk: 3
- Runner launched with: 3
- Pending (parked in _pending/): ['audusd_mtf_paper_v1.json', 'eurusd_mtf_paper_v1.json']
