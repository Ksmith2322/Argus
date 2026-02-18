You don’t need to “see anything” to update the map—your latest runner changes *force* a couple updates because the truth moved:

* **Artifacts are not under `C:\Argus\ops\logs\` anymore** (per the runner you posted). They’re under **`C:\Argus\argus-lab\ops\logs\`** via `ARGUS_ARTIFACT_ROOT` default.
* Backtest now emits **per-run `events_<run_id>.csv` + `signals_<run_id>.csv`** (and optionally still writes canonical `io_logs.py` sinks if you call them).
* Kill/Pause file locations in the map are likely wrong unless you verified `io_logs.py` (you wrote “ABSOLUTE, VERIFIED”, but the map says `C:\Argus\repo\KILL_SWITCH.txt`; your current ecosystem has been pushing “artifact_root = argus-lab”; those are different roots).

Below is an **ARGUS_MAP vNext** that matches the runner you pasted + the bt artifacts you’re actually reading.

---

# ARGUS MAP (call graph + sources of truth) — UPDATED FOR CURRENT BACKTEST ARTIFACTS

===============================================================================

1. ENTRYPOINTS (WHAT YOU ACTUALLY RUN)
   ===============================================================================

PRIMARY (SCHEDULER-SAFE WRAPPERS) ✅

* Live (manual run):

  * cmd.exe /c C:\Argus\ops\run_live.cmd

* Backtest (manual run):

  * cmd.exe /c C:\Argus\ops\run_backtest.cmd

PRIMARY (AUTOMATION) ✅

* Nightly backtest:

  * Task Scheduler: Argus_Nightly_Backtest
  * Runs: C:\Windows\System32\cmd.exe /c C:\Argus\ops\run_backtest.cmd

* Weekly backup verify:

  * Task Scheduler: Argus_Weekly_Backup_Verify
  * Runs: powershell.exe -ExecutionPolicy Bypass -File C:\Argus\ops\backup_verify.ps1

DEVELOPER CONVENIENCE (OPTIONAL)

* Live (direct module run):

  * Set-Location C:\Argus\repo
  * C:\Argus.venv\Scripts\python.exe -m runner_live

* Backtest (direct module run):

  * Set-Location C:\Argus\repo
  * C:\Argus.venv\Scripts\python.exe -m backtest.runner

LEGACY / DO NOT USE (HAZARDOUS IF PRESENT)

* Anything that references "nova_scripts" namespace
* Any old sys.path shim entrypoints

===============================================================================
2) CANONICAL SOURCES OF TRUTH (ONLY ONE EACH)
=============================================

Config loading / env surface:

* config.py  -> load_config()

Bot state ownership:

* state.py   -> BotState

Core decision step / orchestration:

* engine.py  -> step(state, tick, cfg, ...)

Snapshot schema / event objects:

* decisions.py -> DecisionSnapshot + EngineEvent (or equivalent)

Signal + event CSV schema AND writers:

* CANONICAL:

  * io_logs.py
* NON-CANONICAL (DUPLICATE / DRIFT RISK):

  * logger.py (legacy; should not be used)

Market / trading logic:

* strategy_phase2.py
* confluence.py
* adaptive_confluence.py
* structure.py
* regime.py
* liquidity.py
* session.py

Data and math:

* candles.py
* indicators.py
* ledger.py
* pnl_shadow.py

Risk:

* risk.py

Optional trade tracking:

* trade_tracker.py

Notify:

* notify.py

===============================================================================
3) CORE CALL CHAINS (WHAT CALLS WHAT)
=====================================

LIVE LOOP
runner_live
-> feed_coinbase.py        (HTTP spot / preload / ticks)
-> state.BotState
-> engine.step(...)
-> candles.py
-> indicators.py
-> strategy_phase2.py
-> structure.py
-> liquidity.py
-> session.py
-> regime.py
-> confluence.py
-> adaptive_confluence.py
-> ledger.py
-> risk.py
-> decisions.py
-> io_logs.py              (write signals/events CSV)
-> notify.py               (optional)

BACKTEST LOOP (CURRENT REALITY)
backtest.runner
-> backtest.loader         (CSV -> CandleRow)
-> backtest.feed           (CandleRow -> PriceTick)
-> state.BotState
-> engine.step(...)
-> io_logs.py              (canonical sink via log_bt_event/log_signal_snapshot)
-> backtest.results        (derive metrics from events + snapshots)

Additionally, backtest.runner emits run-scoped artifacts (Phase 7.1+):
-> events_<run_id>.csv
-> signals_<run_id>.csv
-> run_header_<run_id>.json
-> bt_summary_<run_id>.json
-> bt_summary_latest.json

TOOLS (OPTIONAL)
backtest.download_candles
-> requests -> CSV in repo/data/

_debug_step
-> single engine.step() with one tick

===============================================================================
4) I/O SURFACES (WHERE FILES ARE WRITTEN) — CORRECTED
=====================================================

CANONICAL ARTIFACT ROOT (PHASE 7.1 CONTRACT, FROM backtest.runner) ✅

* ARGUS_ARTIFACT_ROOT defaults to:

  * C:\Argus\argus-lab

Backtest artifact directory:

* C:\Argus\argus-lab\ops\logs\

  * bt_<run_id>.log                      (if you actually write to it)
  * events_<run_id>.csv                  (runner writes directly)
  * signals_<run_id>.csv                 (runner writes directly)
  * run_header_<run_id>.json
  * bt_summary_<run_id>.json
  * bt_summary_latest.json

NOTE: Your analysis one-liner confirms this is where you’re reading:

* C:\Argus\argus-lab\ops\logs\bt_summary_latest.json
* C:\Argus\argus-lab\ops\logs\events_<run_id>.csv

LIVE WRAPPER LOGS (SEPARATE SURFACE)

* C:\Argus\ops\logs\

  * live_*.log  (from run_live.cmd)
  * bt_*.log    (from run_backtest.cmd wrapper logs, if you log wrapper output here)

CSV OUTPUTS (io_logs.py PATHS)

* io_logs.py still writes its own canonical sinks (signals_csv_path/events_csv_path)
* backtest.runner ALSO writes per-run CSVs (events_<run_id>.csv, signals_<run_id>.csv)
  -> These two can diverge if both are enabled and not intentionally unified.

CANDLE DATA / FIXTURES

* C:\Argus\repo\data\eth_usd_1m.csv   (default in runner **main**)

KILL / PAUSE FILES (STATUS: MUST MATCH io_logs.py IMPLEMENTATION)

* Map SHOULD NOT assert these paths unless you verified io_logs.py.

  * If you want them tied to artifact root, they should live under:
    C:\Argus\argus-lab\KILL_SWITCH.txt / PAUSE.txt (or /ops/)
  * If you want them tied to repo root, they live under:
    C:\Argus\repo\KILL_SWITCH.txt / PAUSE.txt
    Pick one and make every reader use the same resolver.

BACKUPS ✅

* C:\ArgusBackups\

  * argus_backup_YYYYMMDD_HHMMSS.zip

===============================================================================
5) CONFIG & ENV KNOBS (MATERIAL BEHAVIOR CHANGES ONLY)
======================================================

Execution / timing

* BACKTEST_MODE
* CANDLE_SECONDS
* STALE_TICK_SECONDS

Liquidity

* USE_LIQUIDITY_FILTERS / USE_LIQUIDITY
* LIQ_MODE (BLOCK | PENALIZE)
* LIQ_MAX_SPREAD_BPS
* LIQ_MIN_VOL_1M
* LIQ_MIN_VOL_MULT
* LIQ_VOL_BASELINE_WINDOW
* LIQ_SYNTH_SPREAD_FLOOR_BPS
* LIQ_SYNTH_SPREAD_ATR_MULT_BPS

Backtest-only helpers

* ARGUS_ARTIFACT_ROOT                 (NEW: governs backtest artifact root)
* ARGUS_BT_ARTIFACT_DIR               (optional override; defaults to <artifact_root>\ops\logs)
* BT_SYNTH_SPREAD_BPS
* BT_TRUNCATE_LOGS
* BT_WRITE_LOGS
* BT_PRINT_EVENTS
* BT_LOG_FLUSH_N
* BT_SIGNAL_LOG_EVERY_N
* BT_EQUITY_EVERY_N
* DISABLE_TRADE_TRACKER_IN_BACKTEST

Confluence

* CONFLUENCE_MIN_SCORE
* REQUIRE_CONFLUENCE
* USE_SHOULD_EVENTS / BT_USE_SHOULD_EVENTS

Adversarial testing

* ARGUS_PROFILE
* ARGUS_SEED

===============================================================================
6) KNOWN HAZARDS (CURRENT REALITY)
==================================

* logger.py duplicates CSV writing -> schema drift risk

* Two event streams exist in backtest:

  1. canonical io_logs.py sink via log_bt_event/log_signal_snapshot
  2. per-run CSVs written directly by runner (events_<run_id>.csv, signals_<run_id>.csv)
     If you don’t unify them, “truth” depends on which file you read.

* KILL/PAUSE path ambiguity (repo-root vs artifact-root) will bite you in automation.
  The map must not claim a path until the resolver is single-source.

* Summary metrics must be derived from artifacts/events (avoid hidden counters)

* Parity blocker remains:

  * median_entry_vol_baseline still None
  * median_entry_atr_norm still None
    You now proved the attempt boundary is firing (ENTRY_ATTEMPT 8, ENTRY_METRICS 4, MISSED_BUY_LIQUIDITY 4).
    That means the missing medians are now almost certainly a *data plumbing* issue, not “no attempts”.

===============================================================================
7) STATUS (AS OF TODAY)
=======================

✅ Backtest artifacts confirmed under:

* C:\Argus\argus-lab\ops\logs\

  * bt_summary_latest.json
  * events_<run_id>.csv

✅ Attempt boundary present in events:

* ENTRY_ATTEMPT exists
* ENTRY_METRICS exists
* MISSED_BUY_LIQUIDITY exists

NEXT (EASY WIN TARGET)

* Unify “truth source” for backtest events:

  * Either stop writing io_logs.py bt sink during backtest
  * OR stop writing per-run CSV and only rely on io_logs.py
  * OR make io_logs.py write to the per-run file via run_id (best long-term)

---

## Two “easy wins” I’d do *right now* (no extra code archaeology)

1. **Update the map’s file paths** to reflect `ARGUS_ARTIFACT_ROOT = C:\Argus\argus-lab` as the *actual* backtest artifact root (done above).

2. **Stop asserting KILL/PAUSE locations** in the map until you’ve chosen the single resolver.
   The current map claims `C:\Argus\repo\KILL_SWITCH.txt` as “verified”; your system is otherwise moving toward `argus-lab` as the run root. That mismatch is a future “why didn’t it stop?” incident.

If you want, paste just the top of `io_logs.py` where it defines `KILL_SWITCH` / `PAUSE` / `logs_dir()` / `events_csv_path()` and I’ll lock the map to the exact resolver you’re actually running.
