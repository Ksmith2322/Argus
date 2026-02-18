FILES INDEX (one line per file; no prose)

FORMAT:
file | TYPE | ROLE | CALLED BY | CALLS INTO | OWNS STATE | READS | WRITES | RISK TAGS

--- REPO ROOT (C:\Argus\repo) ---

**init**.py | SUPPORT | package marker (only relevant if running as a package) | n/a | n/a | NO | none | none | packaging

config.py | CORE | config loader + defaults + env override surface | runner_live, backtest/runner, _debug_step, engine/state (indirect) | none | NO | env, .env (via dotenv) | none | config,env

engine.py | CORE | main decision step: TF states + overlays (regime/structure/liquidity/session) + entry/exit + events | runner_live, backtest/runner, _debug_step | candles, indicators, strategy_phase2, confluence, adaptive_confluence, regime, structure, liquidity, session, ledger, risk, decisions, trade_tracker (via state) | YES | cfg, ticks, state sub-engine caches | none | orchestration,state-coupling,accounting,time

state.py | CORE | BotState factory + owns sub-engines + caches last_* snapshots | runner_live, backtest/runner, _debug_step, engine | candles, confluence, ledger, risk, strategy_phase2, trade_tracker, regime, adaptive_confluence, structure, liquidity | YES | cfg | none | state,fanout

decisions.py | CORE | DecisionSnapshot + EngineEvent schema + field mapping for logs/events | engine, runner_live, backtest/runner, io_logs | none | YES | none | none | schema,stability

utils.py | SUPPORT | time helpers + safe string conversion | many | none | NO | none | none | time

candles.py | CORE | Candle + CandleBuilder (1m/5m/1h aggregation) | state, engine, backtest/runner | none | YES | ticks, OHLCV rows | none | time,state

indicators.py | CORE | indicator engine (SMA/EMA/etc) | strategy_phase2, candles, engine, backtest/runner | none | YES | candle closes | none | state,math

strategy_phase2.py | CORE | strategy rules → StrategyState (soft scoring + gates) | state, engine, backtest/runner | indicators | YES | cfg, candle closes | none | signal-model,gating-policy

confluence.py | CORE | confluence scoring from TF StrategyStates (+ structure hints) | state, engine | structure | YES | cfg, TF states, structure | none | state,scoring

adaptive_confluence.py | CORE | regime-aware confluence adjustment | state, engine | confluence, regime | YES | cfg, confluence, regime | none | state,policy

regime.py | CORE | regime detection (trend/range labels) | state, engine | indicators (via state/strategy inputs) | YES | cfg, prices/indicators | none | state

structure.py | CORE | market structure engine: S/R, breaks, retests, rejection checks | state, engine, confluence | candles | YES | cfg, candles | none | state,heuristics

liquidity.py | CORE | liquidity engine/result: spread/vol_1m/vol_baseline/atr_norm/penalties + reasons | state, engine | none | YES | cfg, tick bid/ask/vol, ATR inputs | none | time,state,marketdata,parity-risk

session.py | POLICY | session classifier + score bonus + risk multiplier | engine | none | NO (cached into state) | epoch, cfg | none | time,policy

ledger.py | CORE | VirtualLedger: fills, exposure, PnL, position lifecycle | state, engine, backtest/runner, backtest/results tooling | none | YES | cfg, price, qty | none | accounting,state

pnl_shadow.py | SUPPORT | fill + pnl math helpers (no state) | ledger, engine, backtest tooling | none | NO | cfg, px | none | accounting

risk.py | CORE | RiskManager: day reset, max trades, loss limits, cooldown | state, engine | none | YES | cfg, epoch | none | accounting,time,state

io_logs.py | IO (CANONICAL) | canonical signals/events writers + kill/pause + path helpers | runner_live, backtest/runner | utils, decisions | NO | filesystem, env, CWD | canonical CSV/log sinks (per io_logs path helpers; may be CWD-sensitive) | CSV,schema,filesystem,CWD-invariant-risk

logger.py | IO (LEGACY) | duplicate writer (avoid) | unknown/legacy | utils (fallback import) | NO | filesystem | legacy log outputs (do not use) | DUPLICATE-IO,drift-risk

feed_coinbase.py | INTEGRATION | Coinbase HTTP price fetch + preload/history | runner_live, backtest tools (download) | requests, candle helpers | NO | network | none | network,rate-limit

notify.py | INTEGRATION | Discord webhook notifier | runner_live | requests | NO | cfg, network | network side-effect | side-effects,network

trade_tracker.py | STATE+IO | hold/missed-buy counters + txt report | state, engine (indirect), runner_live | os,re,datetime | YES | filesystem,cfg | trade_tracker.txt (path depends on config/env + resolver) | side-effects,feedback-loop,drift-risk

_debug_step.py | TOOL | one-shot debug tick through engine.step | manual | config,state,engine,feed_coinbase | NO | cfg | stdout | debug

runner_live.py | ENTRYPOINT | live loop orchestration (preload → loop → log snapshots/events) | ops/run_live.cmd (scheduler-safe) | config,state,feed_coinbase,engine,io_logs,notify,utils | YES | cfg, env, network, filesystem | live logs + canonical CSV sinks (via io_logs) | time,network,CSV,shutdown-semantics

main.py | ENTRYPOINT (LEGACY/WRONG) | runpy shim pointing to nova_scripts.* | manual | runpy | NO | none | none | HAZARD-OLD-PACKAGE

run_main.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- BACKTEST PACKAGE (C:\Argus\repo\backtest) ---

backtest/**init**.py | SUPPORT | package marker | n/a | n/a | NO | none | none | packaging

backtest/loader.py | SUPPORT | load candle CSV → CandleRow list | backtest/runner | csv | NO | C:\Argus\repo\data*.csv (default) or BACKTEST_CSV | none | data,format-risk

backtest/feed.py | SUPPORT | CandleRow → PriceTick stream (supports candle-close ticks; bid/ask may be synthesized in runner) | backtest/runner | none | NO | candles,cfg | none | time,liquidity-sim

backtest/results.py | CORE | derive metrics from events and/or snapshots (expectancy, DD, winrate, medians) | backtest/runner | csv,re,json,statistics | YES | in-memory events + (optionally) events_<run_id>.csv if you add reload tooling | bt_summary dict/json | accounting,metrics,parity-risk

backtest/runner.py | ENTRYPOINT | backtest loop orchestration + run_id + artifact emission | ops/run_backtest.cmd (scheduler-safe), manual python -m backtest.runner | loader,feed,config,state,engine,io_logs,results,feed_coinbase | YES | candles CSV, cfg, env | C:\Argus\argus-lab\ops\logs\run_header_<run_id>.json; bt_summary_<run_id>.json + bt_summary_latest.json; events_<run_id>.csv; signals_<run_id>.csv; equity_<run_id>.csv (path via ARGUS_ARTIFACT_ROOT) | CSV,accounting,reproducibility,artifact-contract,collision-risk

backtest/download_candles.py | TOOL | download Coinbase candles → CSV | manual | requests | NO | network | C:\Argus\repo\backtest\data*.csv (or target path) | network,data

backtest/from_live_events.py | TOOL | reconstruct trades from live events | manual | csv,re | NO | live_events*.csv (path per io_logs/live sink) | derived trades CSV | accounting,forensics

backtest/run_backtest.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- OPS LAYER (C:\Argus\ops) ---

ops/run_live.cmd | OPS | canonical live wrapper (CWD pin + log routing) | you, Task Scheduler (future) | runner_live module | NO | filesystem | C:\Argus\ops\logs\live_*.log | scheduler-safe,CWD-invariant

ops/run_backtest.cmd | OPS | canonical backtest wrapper (CWD pin + log routing) | you, Task Scheduler | backtest.runner | NO | filesystem | C:\Argus\ops\logs\bt_*.log (wrapper stdout/stderr); backtest artifacts under ARGUS_ARTIFACT_ROOT | scheduler-safe,CWD-invariant

ops/backup_repo.ps1 | OPS | repo+ops backup to zip (timestamped) | you, Task Scheduler | Compress-Archive | NO | filesystem | C:\ArgusBackups\argus_backup_*.zip | backup

ops/backup_verify.ps1 | OPS | verifies latest backup zip integrity + required files | you, Task Scheduler | Expand-Archive (temp) | NO | filesystem | ops/logs/backup_verify*.log (optional) | restore-confidence

ops/proof/* | OPS | phase proof artifacts (git log, pip freeze, task XML, last bt summary/log) | manual | n/a | NO | filesystem | ops/phase6_proofpack.zip | evidence

--- ARTIFACT ROOT (DEFAULT: C:\Argus\argus-lab) ---

argus-lab/ops/logs/* | ARTIFACTS | run-scoped backtest artifacts (Phase 7.1 contract) | backtest/runner | n/a | NO | filesystem | run_header_<run_id>.json; bt_summary_<run_id>.json; bt_summary_latest.json; events_<run_id>.csv; signals_<run_id>.csv; equity_<run_id>.csv | artifact-contract,forensics,reproducibility

argus-lab/ops/reports/* | REPORTS (FUTURE) | daily report + charts (Phase 7.2) | report generator (future) | json,csv | NO | bt_summary_*.json, events_<run_id>.csv, equity_<run_id>.csv | daily_report_YYYYMMDD.md/html; equity_*.html/png | evidence,iter-loop
