# ARGUS MAP
# Last updated: 2026-04-02 -- IBKR fleet primary, crypto dormant

This map reflects your current working reality:
- PRIMARY SYSTEM: IBKR Trading Engine (argus_flow/runner_unified.py)
  - 8 FX pairs (paper) + 12 futures (watcher) + 16 FX variants (watcher) + 2 killed
  - Grouped into 2 runner processes (clientId 1=FX, clientId 2=futures)
  - Dashboard: ops/dashboard.py (port 8080) with live candlestick chart, action buttons, graveyard
  - Advanced features: multi-timeframe (5m/15m/30m/1h/4h), spread gate, news filter, entry sequencing
  - Schema v4: entry-feature stamping, execution quality tracking, conviction scoring
  - 3-mode shutdown: PAUSE_ENTRIES / GRACEFUL_EXIT / KILL_SWITCH
  - Auto stage transitions: watcher -> paper -> real -> quarantine -> killed (graveyard)
  - Managed truth refresh with walk-forward reuse (6hr cache) and auto-apply transitions
  - Smoke test uses read-only IBKR sessions with collision-safe client IDs
  - Futures historical data path working (download_ibkr_bars.py)
- DORMANT: Coinbase crypto engine (root-level *.py files) -- venue kills edge at 60bps
- Legacy backtest/analytics in root still reference crypto; IBKR backtest is argus_flow/backtest/engine.py

---

## 0) Repo layout (source-of-truth roots)

Repo root (canonical):
- `C:\Argus\repo`

Python venv (canonical):
- `C:\Argus\.venv\Scripts\python.exe`

Primary system (IBKR):
- `C:\Argus\repo\argus_flow\`

Legacy system (Coinbase crypto, DORMANT):
- `C:\Argus\repo\` (root-level *.py)
- `C:\Argus\repo\backtest\`

Ops + dashboard:
- `C:\Argus\repo\ops\`

Backtest artifacts (legacy crypto):
- `C:\Argus\repo\ops\logs\`

IBKR runtime artifacts:
- `C:\Argus\repo\argus_flow\logs\{symbol}\`

GitHub remote:
- `https://github.com/Ksmith2322/Argus` -- branch `phase6-hardening`

Two-machine layout:
- PC1 (WINDOWS-5RCTEK3): runners 24/7 + dev work
- PC2 (DESKTOP-17CJMUP): offloaded backtest jobs via SSH dispatch
- .env tracked in git (paper trading, no real keys)
- argus_flow/logs/ NOT in git -- artifacts are local to each machine

---

## 1) Entry points (what you actually run)

IBKR FX runner (primary):
- `C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --configs argus_flow/configs/cadjpy_t4_paper_v1.json argus_flow/configs/usdjpy_ny_paper_v1.json argus_flow/configs/audjpy_t4_paper_v1.json argus_flow/configs/audusd_ny_paper_v1.json argus_flow/configs/eurjpy_t4_paper_v1.json argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/gbpjpy_t4_paper_v1.json argus_flow/configs/gbpusd_range_paper_v1.json`

IBKR Futures runner (primary):
- `C:\Argus\.venv\Scripts\python.exe -m argus_flow.runner_unified --client-id 2 --configs argus_flow/configs/mes_range_paper_v1.json argus_flow/configs/mnq_range_paper_v1.json argus_flow/configs/mym_range_paper_v1.json argus_flow/configs/m2k_range_paper_v1.json argus_flow/configs/mgc_range_paper_v1.json argus_flow/configs/mcl_range_paper_v1.json`

Dashboard:
- `C:\Argus\.venv\Scripts\python.exe ops/dashboard.py`
- Serves on port 8080

Fleet launcher (all-in-one):
- `ops/launch_fleet.ps1`

Legacy crypto (dormant):
- `C:\Argus\.venv\Scripts\python.exe .\runner_live.py`

---

## 2) IBKR Architecture (PRIMARY SYSTEM)

    IBKR TWS (port 7496)
        |
        v
    runner_unified.py (2 grouped processes)
        |
        +-- Process 1: FX (clientId 1)
        |   +-- InstrumentRunner (EUR/USD) -- BarBuffer, State, signals.csv, trades.csv
        |   +-- InstrumentRunner (GBP/USD)
        |   +-- InstrumentRunner (EUR/JPY)
        |   +-- InstrumentRunner (GBP/JPY)
        |   +-- InstrumentRunner (CAD/JPY)
        |   +-- InstrumentRunner (AUD/JPY)
        |   +-- InstrumentRunner (USD/JPY)
        |   +-- InstrumentRunner (AUD/USD)
        |
        +-- Process 2: Futures (clientId 2)
            +-- InstrumentRunner (MES)
            +-- InstrumentRunner (MNQ)
            +-- InstrumentRunner (MYM)
            +-- InstrumentRunner (M2K)
            +-- InstrumentRunner (MGC)
            +-- InstrumentRunner (MCL)
        |
        +-- Auto-reconnect wrapper (exponential backoff)
        +-- Per-runner fault isolation (try/except per tick)
        +-- Config-hash freeze enforcement (hashes.json)
        +-- Experiment validity tracking (config_hash, session_id, valid/invalid)

Data Flow:
    IBKR TWS --> reqMktData() per instrument --> ticker objects
                 reqHistoricalData() on startup --> seed BarBuffers
        |
        v
    Per-instrument: tick() every second
        +-- Build 1-min bars from ticks
        +-- Compute features (range_pct, vol_z, range_accel, dist_from_low)
        +-- Check trigger (T4 or range_accel depending on config)
        +-- Session gating, regime classification, spread/vol filters
        +-- Manage stops/targets/timeouts
        |
        v
    Artifacts (per instrument):
        argus_flow/logs/{symbol}/state.json    -- position persistence + heartbeat
        argus_flow/logs/{symbol}/signals.csv   -- all signal evaluations
        argus_flow/logs/{symbol}/trades.csv    -- closed trades with validity fields
        argus_flow/logs/{symbol}/evidence_registry.json -- governance truth

## 3) Dashboard

    ops/dashboard.py (FastAPI + SSE, ~5000 lines)
        |
        +-- /api/ibkr_fleet         --> reads all runner state/signals/trades
        +-- /api/daily_performance  --> per-day, per-market P&L journal
        +-- /api/system_health      --> health bar data
        +-- /api/fx_analytics       --> per-pair equity curves, drawdown
        +-- SSE /stream             --> real-time status updates
        |
        +-- Single-page control center:
        |      System health bar (status, API, valid trades, blocked)
        |      Account balance + delta
        |      Account balance history chart
        |      Fleet P&L history chart (pips)
        |      14 runner cards (status, position, features, PnL)
        |      Daily performance journal (per-market breakdown)
        |      Trade journal (last 20 trades)
        |      Signal feed (recent signals)
        +-- Auto-refresh every 10 seconds (fleet), 60s (daily perf)

## 4) Staged Deployment Pipeline

    WATCHER --> PAPER QA --> REAL MONEY --> QUARANTINE / KILLED

    deployment_registry.json tracks stage per config
    promotion_gate_v2.py: 15 automated checks incl. walk-forward
    deployment_pipeline.py: enforces stage transitions, shared risk ladder, and live materialization on PROMOTE

    Current state (2026-03-31):
      7 watcher configs (futures + CAD/JPY)
      7 paper QA configs (all FX pairs)
      0 real-money configs

## 5) Ops Tools (argus_flow/ops/)

    smoke_test.py ------------> Pre-launch 7-point check (read-only, collision-safe)
    health_check.py ----------> Fleet overview + IBKR connection
    heartbeat_monitor.py -----> Runner alive/dead (signal file ages)
    position_monitor.py ------> IBKR vs runner state reconciliation
    broker_truth.py ----------> Canonical broker state reader
    divergence_guard.py ------> Replay vs live comparison
    correlation_guard.py -----> USD/JPY pair exposure limits
    evidence_registry.py -----> Per-runner governance truth writer
    risk_oversight.py --------> Managed-fleet risk oversight report
    deployment_pipeline.py ---> watcher --> paper --> real materialization
    promotion_gate_v2.py -----> Walk-forward-aware promotion checks (canonical)
    artifact_divergence.py ---> Registry vs trades.csv integrity
    alert_escalation_v2.py ---> Durable alert state + event history
    kill_discipline.py -------> 4 automated kill rules
    daily_report.py ----------> Nightly cohort compliance
    weekly_digest.py ---------> Sunday Discord performance report
    walkforward_validation.py -> Walk-forward reports for active cohort

## 6) Ops Automation (ops/)

    launch_fleet.ps1 ---------> One-command fleet start (runners + dashboard + discord)
    watchdog_managed.ps1 -----> Stage-aware singleton supervisor (canonical)
    watchdog.ps1 -------------> Legacy 14-runner watchdog
    premarket_check.ps1 ------> Sunday 4:30 PM + daily 2:30 AM: TWS/port/runner check
    sunday_countdown.ps1 -----> T-30/T-15/T-5 market open alerts
    autostart_runner.ps1 -----> Auto-start on login
    autostart_dashboard.ps1 --> Auto-start dashboard on login
    register_tasks.ps1 -------> Task Scheduler registration (all tasks)

## 7) Scheduled Tasks (PC1)

    ArgusGitBackup:       daily@02:00  --> ops/auto_git_backup.ps1
    ArgusManagedTruth:    every 10 min --> ops/refresh_managed_truth.ps1
    ArgusCohortReport:    daily@23:00  --> ops/run_cohort_report.ps1
    ArgusUSBBackup:       daily@03:00  --> ops/backup_to_usb.ps1
    ArgusVerifyBackup:    Sunday@04:00 --> ops/verify_backup.ps1
    ArgusWeeklyDigest:    Sunday@06:00 --> ops/run_weekly_digest.ps1
    ArgusWatchdog:        on startup   --> ops/watchdog_managed.ps1
    Runner auto-start:    on login     --> ops/autostart_runner.ps1
    Dashboard auto-start: on login     --> ops/autostart_dashboard.ps1
    TWS restart:          daily@4:30PM CT --> IBKR TWS connection reset

## 8) Key Accounts

    IBKR Live: U24860535 (Read-Only API for safety)
    IBKR Paper: DUP472829 (backup)
    TWS Port: 7496
    Dashboard: http://localhost:8080
    Kraken: research closed, 0 balance
    Coinbase: dormant

## 9) IBKR Runner Groups

    FX (clientId 1, 8 pairs):
      EUR/USD  T4 Full Stack    London session (8-14 UTC)
      GBP/USD  Range + Accel    London session
      EUR/JPY  T4 Full Stack    London session
      GBP/JPY  T4 Full Stack    London session
      CAD/JPY  T4 Full Stack    London session
      AUD/JPY  T4 Full Stack    Asia session
      USD/JPY  Range + Accel    Asia/NY session
      AUD/USD  Range + Accel    Asia/NY session

    Futures (clientId 2, 6 instruments):
      MES   S&P 500 Micro       US session
      MNQ   Nasdaq Micro         US session
      MYM   Dow Micro            US session
      M2K   Russell 2000 Micro   US session
      MGC   Gold Micro           US session
      MCL   Crude Oil Micro      US session

    Killed:
      NKD   Nikkei Micro  -- KILLED 2026-03-29 (0/7 WR, -550 pips)

## 10) Correlation Groups

    USD pairs:    EUR/USD + GBP/USD + AUD/USD (count as 1.5 systems)
    JPY crosses:  EUR/JPY + AUD/JPY + CAD/JPY + GBP/JPY (yen-correlated)
    US indices:   MNQ + MES + MYM + M2K (highly correlated)

---

## LEGACY SECTIONS (Coinbase crypto -- DORMANT)

The following sections document the original Coinbase crypto system.
Root-level *.py files, backtest/, execution/, analytics/ all belong to this system.
It is preserved for potential reactivation if venue economics change (< 20bps RT fees).

### Legacy runtime call graph

    backtest/runner.py
    --> config.load_config()
    --> _ensure_bt_cfg() (backtest-safe overlay)
    --> state.BotState.from_config(cfg)
    --> backtest.loader.load_candles_csv(...)
    --> engine.step(state, tick, cfg, paused=False)
    --> io_logs.log_signal_snapshot(snap, ...)
    --> backtest.results.BacktestResults

### Legacy one source of truth ownership

    Config/env:     config.py --> load_config()
    State:          state.py --> BotState
    Decision/step:  engine.py --> step()
    Logging:        io_logs.py (canonical artifact writers)
    Backtest:       backtest/results.py (metrics + invariants)

### Legacy sandbox rule

    Backtest must not mutate:
    - ops/logs/live_events.csv
    - ops/logs/live_signals.csv
    Enforcement via env vars in backtest/runner.py

### Legacy known constraints

    - Backtest EXECUTION_MODE must be ENGINE (_ensure_bt_cfg forces it)
    - forced_request_state clear ordering in runner_live.py
    - Shell env hygiene: unset ARGUS_BT_ARTIFACT_DIR before bash backtests
    - Reconciliation tolerance: 2e-8 for ROUND_DOWN ULP drift
