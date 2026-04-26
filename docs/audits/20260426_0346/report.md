# Argus Full Audit — 20260426_0346

## Fleet
- Equity: **$11,815.11** | healthy: True | pause_entries: False
  - usdjpy: pos=FLAT age=36s broker=True blocked=False
  - gbpusd: pos=FLAT age=36s broker=True blocked=False
  - cadjpy: pos=FLAT age=36s broker=True blocked=False
- Python procs: 12
- PS procs: 1

## Risk
- drawdown_pause=False peak=4.966666666666754R current=0.36333333333333334R dd%=92.68
- breaker_armed=False (floor=10.0R) oversight=GREEN

## Trading Activity
- 24h: 0 trades | 0 wins (0%) | $+0.00
- 7d: 105 trades | 47 wins (45%) | $+349.50
- 30d: 105 trades | 47 wins (45%) | $+349.50
- canonical: 105 live + 0 backfill = 105 total

## Data Integrity
- Divergent (CSV vs canonical): 0 — none
- Reconciliation: DRIFT (['argus_usdjpy', 'argus_gbpusd', 'forge_gld_pm_long', 'forge_jpy_pm_short'])
- Apollo forward_returns: 231 lines, 95.8h age (STALE)

## Strategy Readiness
- Dispositions: {'scope_down': 14, 'paper_only': 1, 'activated': 2, 'kill': 1}
- Holdouts frozen: 8
- Strategies with 10+ live trades: 0

## Signal Conversion (24h)
- Signals: 0 | Entries: 0 | conv=0.0%
- Top blocks: {}

## Scheduled Tasks
- ArgusCohortReport: status=Ready last_result=1 logon_mode=Interactive/Background
- ArgusWatchdog: status=Ready last_result=1 logon_mode=Interactive/Background
- ArgusGldPmLoop: status=Ready last_result=-1 logon_mode=Interactive only
- ArgusNqLondonCloseLoop: status=Ready last_result=-1 logon_mode=Interactive only
- ArgusAudOrbLoop: status=Ready last_result=-1 logon_mode=Interactive only
- ArgusMetaWatchdog: **MISSING**
- Flags: ['ArgusCohortReport: last_result=1', 'ArgusWatchdog: last_result=1', 'ArgusGldPmLoop: last_result=-1', "ArgusGldPmLoop: Interactive only (won't survive logoff)", 'ArgusNqLondonCloseLoop: last_result=-1', "ArgusNqLondonCloseLoop: Interactive only (won't survive logoff)", 'ArgusAudOrbLoop: last_result=-1', "ArgusAudOrbLoop: Interactive only (won't survive logoff)", 'ArgusMetaWatchdog: not registered']

## Code Hygiene
- Errors 24h: 13
- Stale locks (>48h): 5
- Uncommitted files: 0
- Commits ahead of origin: 0

## External Deps
- IBKR port 7497 listening: True
- yfinance: reachable=True latency=3.9s bars=7
- Discord failures logged: 0

## Config Drift
- Configs on disk: 3
- Runner launched with: 3
- Pending (parked in _pending/): ['audusd_mtf_paper_v1.json', 'eurusd_mtf_paper_v1.json']
