FILES INDEX (one line per file; no prose)

FORMAT:
file | TYPE | ROLE | CALLED BY | CALLS INTO | OWNS STATE | READS | WRITES | RISK TAGS

--- REPO ROOT (C:\Argus\repo) ---

__init__.py | SUPPORT | package marker (only relevant if running as a package) | n/a | n/a | NO | none | none | packaging

config.py | CORE | config loader + defaults + env override surface | runner_live, backtest/runner, _debug_step, engine/state (indirect) | none | NO | env,.env (via dotenv) | none | config,env

engine.py | CORE | main decision step: TF states + overlays (regime/structure/liquidity/session) + entry/exit + events | runner_live, backtest/runner, _debug_step | candles,indicators,strategy_phase2,confluence,adaptive_confluence,regime,structure,liquidity,session,ledger,risk,decisions,trade_tracker | YES | cfg,ticks,state caches | none | orchestration,state-coupling,accounting,time

state.py | CORE | BotState factory + owns sub-engines + caches last_* snapshots | runner_live,backtest/runner,_debug_step,engine | candles,confluence,ledger,risk,strategy_phase2,trade_tracker,regime,adaptive_confluence,structure,liquidity | YES | cfg | none | state,fanout

decisions.py | CORE | DecisionSnapshot + EngineEvent schema + field mapping for logs/events | engine,runner_live,backtest/runner,io_logs | none | YES | none | none | schema,stability

utils.py | SUPPORT | time helpers + safe string conversion | many | none | NO | none | none | time

candles.py | CORE | Candle + CandleBuilder (1m/5m/1h aggregation) | state,engine,backtest/runner | none | YES | ticks,OHLCV rows | none | time,state

indicators.py | CORE | indicator engine (SMA/EMA/etc) | strategy_phase2,candles,engine,backtest/runner | none | YES | candle closes | none | state,math

strategy_phase2.py | CORE | strategy rules → StrategyState (soft scoring + gates) | state,engine,backtest/runner | indicators | YES | cfg,candle closes | none | signal-model,gating-policy

confluence.py | CORE | confluence scoring from TF StrategyStates (+ structure hints) | state,engine | structure | YES | cfg,TF states,structure | none | state,scoring

adaptive_confluence.py | CORE | regime-aware confluence adjustment | state,engine | confluence,regime | YES | cfg,confluence,regime | none | state,policy

regime.py | CORE | regime detection (trend/range labels) | state,engine | indicators (via state inputs) | YES | cfg,prices/indicators | none | state

structure.py | CORE | market structure engine: S/R, breaks, retests, rejection checks | state,engine,confluence | candles | YES | cfg,candles | none | state,heuristics

liquidity.py | CORE | liquidity engine/result: spread/vol_1m/vol_baseline/atr_norm/penalties + reasons | state,engine | none | YES | cfg,bid/ask/vol,ATR inputs | none | time,state,marketdata,parity-risk

session.py | POLICY | session classifier + score bonus + risk multiplier | engine | none | NO (cached into state) | epoch,cfg | none | time,policy

ledger.py | CORE | VirtualLedger: fills, exposure, PnL, position lifecycle | state,engine,backtest/runner,backtest/results | none | YES | cfg,price,qty | none | accounting,state

pnl_shadow.py | SUPPORT | fill + pnl math helpers (no state) | ledger,engine,backtest tooling | none | NO | cfg,px | none | accounting

risk.py | CORE | RiskManager: day reset, max trades, loss limits, cooldown | state,engine | none | YES | cfg,epoch | none | accounting,time,state

io_logs.py | IO (CANONICAL) | canonical signals/events writers + kill/pause + path helpers | runner_live,backtest/runner | utils,decisions | NO | filesystem,env | live_* CSV (live) OR sandboxed live_* (bt); events CSV; header upgrades | CSV,schema,filesystem,CWD-risk

logger.py | IO (LEGACY) | duplicate writer (avoid) | unknown/legacy | utils (fallback import) | NO | filesystem | legacy log outputs | DUPLICATE-IO,drift-risk

feed_coinbase.py | INTEGRATION | Coinbase HTTP price fetch + preload/history | runner_live,backtest tools (download) | requests,candle helpers | NO | network | none | network,rate-limit

notify.py | INTEGRATION | Discord webhook notifier | runner_live | requests | NO | cfg,network | network side-effect | side-effects,network

trade_tracker.py | STATE+IO | hold/missed-buy counters + txt report | state,engine (indirect),runner_live | os,re,datetime | YES | filesystem,cfg | trade_tracker.txt | side-effects,feedback-loop

_debug_step.py | TOOL | one-shot debug tick through engine.step | manual | config,state,engine,feed_coinbase | NO | cfg | stdout | debug

runner_live.py | ENTRYPOINT | live loop orchestration (preload → loop → log snapshots/events) | ops/run_live.cmd | config,state,feed_coinbase,engine,io_logs,notify,utils | YES | cfg,env,network | live logs + canonical CSV sinks (via io_logs) | time,network,CSV,shutdown-semantics

main.py | ENTRYPOINT (LEGACY/WRONG) | runpy shim pointing to nova_scripts.* | manual | runpy | NO | none | none | HAZARD-OLD-PACKAGE

run_main.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- BACKTEST PACKAGE (C:\Argus\repo\backtest) ---

backtest/__init__.py | SUPPORT | package marker | n/a | n/a | NO | none | none | packaging

backtest/loader.py | SUPPORT | load candle CSV → CandleRow list | backtest/runner | csv | NO | data CSV (default repo-local or BACKTEST_CSV) | none | data,format-risk

backtest/feed.py | SUPPORT | CandleRow → PriceTick stream (close-based; bid/ask may be synthesized) | backtest/runner | none | NO | candles,cfg | none | time,liquidity-sim

backtest/results.py | CORE | derive metrics from events + snapshots (expectancy, DD, winrate, medians) | backtest/runner | csv,re,json,statistics | YES | in-memory events + signals + ledger outputs | bt_summary dict/json; entry attempt stats CSV | accounting,metrics,parity-risk

backtest/runner.py | ENTRYPOINT (CANONICAL) | backtest loop orchestration + run_id + artifact emission | ops/run_backtest.cmd, manual python -m backtest.runner or .\backtest\runner.py | loader,feed,config,state,engine,io_logs,results,feed_coinbase | YES | candles CSV,cfg,env | ops\logs\events_<run>.csv; signals_<run>.csv; equity_<run>.csv; trades_<run>.csv; bt_summary_<run>.json; bt_summary_latest.json | CSV,accounting,reproducibility,artifact-contract

backtest/download_candles.py | TOOL | download Coinbase candles → CSV | manual | requests | NO | network | repo-local CSV | network,data

backtest/from_live_events.py | TOOL | reconstruct trades from live events | manual | csv,re | NO | live_events*.csv | derived trades CSV | accounting,forensics

backtest/run_backtest.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* (do not use) | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- OPS LAYER (C:\Argus\ops) ---

ops/run_live.cmd | OPS | canonical live wrapper (CWD pin + log routing) | you,Task Scheduler | runner_live | NO | filesystem | ops\logs\live_*.log | scheduler-safe,CWD-invariant

ops/run_backtest.cmd | OPS | canonical backtest wrapper (CWD pin + log routing) | you,Task Scheduler | backtest.runner | NO | filesystem | ops\logs\bt_*.log (stdout/stderr); artifacts via runner into ops\logs | scheduler-safe,CWD-invariant

ops/backup_repo.ps1 | OPS | repo+ops backup to zip (timestamped) | you,Task Scheduler | Compress-Archive | NO | filesystem | C:\ArgusBackups\argus_backup_*.zip | backup

ops/backup_verify.ps1 | OPS | verifies latest backup zip integrity + required files | you,Task Scheduler | Expand-Archive | NO | filesystem | ops\logs\backup_verify*.log | restore-confidence

ops/proof/* | OPS | phase proof artifacts (git log,pip freeze,task XML,last bt summary/log) | manual | n/a | NO | filesystem | phase proof pack zip | evidence

--- ARTIFACT ROOT (CANONICAL: C:\Argus\repo\ops\logs) ---

ops/logs/events_<run_id>.csv | ARTIFACT | per-run event stream (classification boundary source-of-truth) | backtest/runner | n/a | NO | filesystem | immutable per run | artifact-contract,forensics

ops/logs/signals_<run_id>.csv | ARTIFACT | per-run signal snapshots (full header schema) | backtest/runner | n/a | NO | filesystem | immutable per run | artifact-contract,analysis

ops/logs/equity_<run_id>.csv | ARTIFACT | per-run equity curve (epoch,equity,cash,qty) | backtest/runner | n/a | NO | filesystem | immutable per run | accounting,reproducibility

ops/logs/trades_<run_id>.csv | ARTIFACT | per-run closed trades summary | backtest/runner | n/a | NO | filesystem | immutable per run | accounting

ops/logs/bt_summary_<run_id>.json | ARTIFACT | per-run summary metrics (invariants, medians, DD, etc.) | backtest/runner | n/a | NO | filesystem | immutable per run | metrics,closure

ops/logs/bt_summary_latest.json | POINTER | latest run summary pointer (full summary copy) | backtest/runner | n/a | NO | filesystem | overwritten each run | pointer-consistency

ops/logs/live_events.csv | LIVE ARTIFACT | live event stream (never touched by bt if sandboxed) | runner_live | n/a | NO | filesystem | append-only | live-state

ops/logs/live_signals.csv | LIVE ARTIFACT | live signal snapshots (never touched by bt if sandboxed) | runner_live | n/a | NO | filesystem | append-only | live-state