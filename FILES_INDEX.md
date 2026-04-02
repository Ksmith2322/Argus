FILES INDEX (one line per file; no prose)
# Last updated: 2026-03-18 — added ops tooling (dispatch, check, pull, restart, autostart, refresh), trendlines.py

FORMAT:
file | TYPE | ROLE | CALLED BY | CALLS INTO | OWNS STATE | READS | WRITES | RISK TAGS

--- REPO ROOT (C:\Argus\repo) ---

CLAUDE.md | DOC | Claude Code guidance: commands, architecture, ownership rules, hazard files | n/a | n/a | NO | none | none | docs

ARGUS_MAP.md | DOC | canonical architecture map: call graph, artifact contract, sandbox rules, phase boundaries, known constraints | n/a | n/a | NO | none | none | docs

FILES_INDEX.md | DOC | one-line-per-file index: type, role, ownership, I/O, risk tags | n/a | n/a | NO | none | none | docs

__init__.py | SUPPORT | package marker (only relevant if running as a package) | n/a | n/a | NO | none | none | packaging

config.py | CORE | config loader + defaults + env override surface | runner_live, backtest/runner, _debug_step, engine/state (indirect) | none | NO | env,.env (via dotenv) | none | config,env

engine.py | CORE | decision orchestrator: TF snapshots + overlays (regime/structure/liquidity/session) + emits intents + events | runner_live, backtest/runner, _debug_step | candles,indicators,strategy_phase2,confluence,adaptive_confluence,regime,structure,liquidity,session,risk,decisions | YES | cfg,ticks,state caches | none | orchestration,state-coupling,accounting,time

state.py | CORE | BotState factory + owns sub-engines + caches last_* snapshots | runner_live,backtest/runner,_debug_step,engine | candles,confluence,ledger,risk,strategy_phase2,regime,adaptive_confluence,structure,liquidity,trade_tracker | YES | cfg | none | state,fanout

decisions.py | CORE | DecisionSnapshot + EngineEvent schema + stable field mapping (signals/events) | engine,runner_live,backtest/runner,io_logs | none | YES (schema only) | none | none | schema,stability

utils.py | SUPPORT | safe conversions + misc helpers | many | none | NO | none | none | misc

candles.py | CORE | Candle + CandleBuilder (1m/5m/1h aggregation) | state,engine,backtest/runner,feed_coinbase,backtest/feed | none | YES | ticks,OHLCV rows | none | time,state

trendlines.py | CORE | TrendlineEngine: pivot high/low detection on 1h candles, descending resistance + ascending support projection, proximity/breakout signals, score effects (+5/-8/+12/-15); disabled via USE_TRENDLINES=false | state,engine,confluence | candles | YES | cfg,1h candles | none | state,heuristics,scoring

indicators.py | CORE | indicator engine (SMA/EMA/ATR/volatility, etc.) | strategy_phase2,regime,structure (via state inputs),engine | none | YES | candle closes | none | state,math

strategy_phase2.py | CORE | strategy rules → StrategyState (signal/trend/score/reasons) | state,engine,backtest/runner | indicators | YES | cfg,candle closes | none | signal-model,gating-policy

confluence.py | CORE | confluence scoring from TF states (+ structure hints) | state,engine | structure | YES | cfg,TF states,structure | none | state,scoring

adaptive_confluence.py | CORE | regime-aware confluence adjustment (base → adjusted score/gate/reason) | state,engine | confluence,regime | YES | cfg,confluence,regime | none | state,policy

regime.py | CORE | regime detection (trend/range labels + trend_strength + vol) | state,engine | indicators (via state inputs) | YES | cfg,prices/indicators | none | state,policy

structure.py | CORE | market structure engine: S/R, breaks, retests, rejection checks | state,engine,confluence | candles,indicators (via state inputs) | YES | cfg,candles | none | state,heuristics

liquidity.py | CORE | liquidity engine/result: spread/vol_1m/baseline/atr_norm + penalties + reasons | state,engine | none | YES | cfg,bid/ask/vol,ATR inputs | none | time,state,marketdata,parity-risk

session.py | POLICY | session classifier + score bonus + risk multiplier | engine | none | NO (cache lives on snapshot/state) | epoch,cfg | none | time,policy

ledger.py | CORE | VirtualLedger: positions/cash/exposure/PnL + fill accounting | state,backtest/runner,backtest/results | pnl_shadow | YES | cfg,price,qty | none | accounting,state

pnl_shadow.py | SUPPORT | fill + pnl math helpers (pure functions) | ledger,engine/backtest tooling | none | NO | cfg,px | none | accounting

risk.py | CORE | RiskManager: day reset, max trades, loss limits, lockout/cooldown | state,engine | none | YES | cfg,epoch | none | accounting,time,state

io_logs.py | IO (CANONICAL) | canonical artifact writers + pause/kill + path helpers | runner_live,backtest/runner | utils,decisions | NO | filesystem,env | signals/events/equity/trades summaries (canonical paths) | CSV,schema,filesystem,CWD-risk

logger.py | IO (LEGACY) | duplicate/legacy writer (avoid) | unknown/legacy | utils (fallback import) | NO | filesystem | legacy logs | DUPLICATE-IO,drift-risk

feed_coinbase.py | INTEGRATION | Coinbase HTTP fetch + preload/history helpers | runner_live,backtest/download_candles | requests,candles | NO | network | none | network,rate-limit

feed_ws.py | MISSING/ARCHIVED | optional legacy Coinbase websocket import; file is absent in current repo and runner_live falls back when import fails | runner_live | none | NO | none | none | archived,docs-drift

feed_ibkr.py | INTEGRATION | IBKR Client Portal market data + history preload helpers; session manager, conid lookup, live tick fetch, candle/history preload | runner_live,state | requests,feed_coinbase.PriceTick | NO | network,env | none | network,broker-integration

ml_governor.py | CORE (ML-1) | optional ML governor: loads sklearn pickle artifact (data/ml_governor.pkl), scores entry snapshots, supports LOG_ONLY/SCORE_MODIFY/GATE recommendations | engine | pickle,numpy | NO | data/ml_governor.pkl,cfg | none | ML,gating

correlation_guard.py | CORE | cross-coin entry guard: reads sibling runtime_state JSONs; blocks entries when concurrent open positions >= CROSS_COIN_MAX_OPEN | engine | json | NO | state/runtime_state_*.json | none | risk,multi-coin

btc_momentum_guard.py | CORE | BTC momentum gate: blocks alt-coin entries when BTC trends down | engine | none | NO | cfg | none | risk,multi-coin

notify.py | INTEGRATION | Discord webhook notifier | runner_live | requests | NO | cfg,network | network side-effect | side-effects,network

trade_tracker.py | STATE+IO | hold/missed-buy counters + text report (operator feedback) | state (owned), engine (bump), runner_live | os,datetime | YES | cfg,filesystem | trade_tracker.txt | side-effects,feedback-loop

_debug_step.py | TOOL | one-shot debug tick through engine.step | manual | config,state,engine,feed_coinbase | NO | cfg | stdout | debug

reconciliation.py | TOOL | Phase 8 reconciliation: flat-cash lifecycle check, fills/positions/trade_journal cross-validation, adapter truth surface selection | manual,test_gate_a_phase8.ps1 | csv,json | NO | ops/logs (fills,positions,account,trade_journal CSVs), state/runtime_state | reconcile_phase8_<run>.json | accounting,forensics,phase8-acceptance
NOTE reconciliation.py: _last_flat_cash_baseline with lifecycle_end_ts=None returns last account row (post-close FILL_APPLIED) — NOT the forward scan which incorrectly picks BOOTSTRAP (also flat, cash=$start)

trade_journal.py | SCHEMA+IO | closed-trade row schema (17 required + 10 Phase 9 optional fields), validation, CSV append | runner_live | csv | NO | state._open_trade_ctx,fill,snap | trade_journal_<run_id>.csv | accounting,forensics,phase9

runtime_mode.py | CORE (Phase 16) | 5-level runtime mode system (FULL/NO_NEW_ENTRY/REDUCE_ONLY/OBSERVATION_ONLY/RECONCILIATION_ONLY); crash-safe persistence to RUNTIME_MODE_FILE; escalation-only transitions; action gating (is_entry_allowed/is_exit_allowed/is_order_submission_allowed); JSONL audit trail | runner_live,ops/health,ops/watchdog | none | YES | filesystem | runtime_mode.json (JSONL history) | ops,fail-closed,restart-safety

runner_live.py | ENTRYPOINT | live orchestration (preload → loop → engine.step → write artifacts/notify); Phase 8: startup reconciliation + adapter fill-back loop + crash-safe snapshot; Phase 9: trade lifecycle journal write on SELL→FLAT | ops/run_live.cmd | config,state,feed_coinbase,engine,io_logs,notify,utils,execution/recovery,trade_journal | YES | cfg,env,network | live artifacts via io_logs + adapter CSVs + trade_journal_<run>.csv | time,network,CSV,shutdown-semantics,restart-safety
NOTE runner_live.py: forced_request_state is cleared when recovery=FLAT and NOT restored from snapshot (intentional — prevents stale Gate A test state from surviving restart)
NOTE runner_live.py: close_to_flat detection uses prior_qty_ledger/post_qty_ledger NOT prior_qty_effective/post_qty_effective — PaperAdapter processes fills synchronously inside place_order so adapter._positions is already updated by the time the fill-back loop runs; ledger is driven by _apply_fill_to_ledger inside the loop so it correctly captures the open→flat transition

main.py | ENTRYPOINT (LEGACY/WRONG) | runpy shim pointing to nova_scripts.* | manual | runpy | NO | none | none | HAZARD-OLD-PACKAGE

run_main.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- BACKTEST PACKAGE (C:\Argus\repo\backtest) ---

backtest/__init__.py | SUPPORT | package marker | n/a | n/a | NO | none | none | packaging

backtest/loader.py | SUPPORT | load candle CSV → CandleRow list | backtest/runner | csv | NO | data CSV (repo-local or BACKTEST_CSV) | none | data,format-risk

backtest/feed.py | SUPPORT | CandleRow → PriceTick stream (close-based; bid/ask may be synthesized) | backtest/runner | none | NO | candles,cfg | none | time,liquidity-sim

backtest/results.py | CORE | derive metrics from artifacts + ledger (expectancy, DD, winrate, medians, invariants) | backtest/runner | csv,re,json,statistics | YES | events + signals + ledger outputs | bt_summary dict/json; attempt stats | accounting,metrics,parity-risk

backtest/runner.py | ENTRYPOINT (CANONICAL) | backtest orchestration + run_id + canonical artifact emission; _ensure_bt_cfg() forces EXECUTION_MODE=ENGINE (ADAPTER mode breaks fill accounting in BT) | ops/run_backtest.ps1, manual | loader,feed,config,state,engine,io_logs,results,feed_coinbase | YES | candles CSV,cfg,env | ops\logs\bt_events_<run>.csv; bt_signals_<run>.csv; equity_<run>.csv; trades_<run>.csv; bt_summary_<run>.json; bt_summary_latest.json | CSV,accounting,reproducibility,artifact-contract,EXECUTION_MODE-override

backtest/download_candles.py | TOOL | download Coinbase candles → CSV | manual | requests | NO | network | repo-local CSV | network,data

backtest/from_live_events.py | TOOL | reconstruct trades from live events | manual | csv,re | NO | live_events*.csv | derived trades CSV | accounting,forensics

backtest/run_backtest.py | ENTRYPOINT (LEGACY/WRONG) | sys.path shim to nova_scripts.* (do not use) | manual | runpy,sys.path | NO | filesystem | none | HAZARD-OLD-PACKAGE

--- OPS LAYER (C:\Argus\ops) ---

ops/run_live.cmd | OPS | canonical live wrapper (CWD pin + log routing) | you,Task Scheduler | runner_live | NO | filesystem | ops\logs\live_*.log | scheduler-safe,CWD-invariant

ops/run_backtest.cmd | OPS | canonical backtest wrapper (CWD pin + log routing) | you,Task Scheduler | backtest.runner | NO | filesystem | ops\logs\bt_*.log (stdout/stderr); artifacts via runner into ops\logs | scheduler-safe,CWD-invariant

ops/backup_repo.ps1 | OPS | repo+ops backup to zip (timestamped) | you,Task Scheduler | Compress-Archive | NO | filesystem | C:\ArgusBackups\argus_backup_*.zip | backup

ops/backup_verify.ps1 | OPS | verify latest backup zip integrity + required files | you,Task Scheduler | Expand-Archive | NO | filesystem | ops\logs\backup_verify*.log | restore-confidence

ops/proof/* | OPS | phase proof artifacts (git log,pip freeze,task XML,last bt summary/log) | manual | n/a | NO | filesystem | phase proof pack zip | evidence

ops/test_gate_a_phase8.ps1 | OPS | Phase 8 acceptance harness: force BUY → kill → restart → recover OPEN → force SELL → FLAT → reconcile → PASS/FAIL | manual | runner_live,reconciliation | NO | filesystem,env | gate_a_*.out.log; gate_a_*.err.log; gate_a_summary_*.json | phase8-acceptance,restart-proof
NOTE test_gate_a_phase8.ps1: Start-Process requires separate stdout/stderr files; stdout→*.out.log, stderr→*.err.log

ops/test_gate_a_phase10.ps1 | OPS | Phase 10 adversarial restart harness: 4 kill-point scenarios (kill_before_journal, kill_after_journal, cold_restore, kill_after_submit) each ending with reconcile PASS | manual | runner_live,reconciliation | NO | filesystem,env | phase10_*.out.log; phase10_summary_*.json | phase10-acceptance,restart-proof
NOTE test_gate_a_phase10.ps1: delete runtime snapshot before final-runner phase — runner finally-block saves EXITING+applied_fill_ids at kill time; stale snapshot causes Wait-ForFlat to return immediately before runner does any work

ops/__init__.py | SUPPORT | package marker for ops module | n/a | n/a | NO | none | none | packaging

ops/health.py | CORE (Phase 16) | health check framework: feed staleness, fill latency, consecutive losses, slippage anomaly, invariant violations; returns HealthStatus + recommended mode escalation | runner_live,ops/watchdog | runtime_mode,ops/alerting | NO | cfg,fills.csv,account.csv | none | ops,fail-closed

ops/invariants.py | CORE (Phase 16) | continuous invariant engine: position/fill match, duplicate fill IDs, order/fill linkage, timestamp ordering, cash/exposure limits; runs on startup and periodically | runner_live | csv | NO | fills.csv,positions.csv,orders.csv | none | accounting,forensics,fail-closed

ops/run_manifest.py | CORE (Phase 16) | config freeze (SHA-256 hash), code hash, run manifests (run_id/config_hash/code_hash/start_time/artifact list); drift detection between runs | runner_live | hashlib,json | NO | cfg,filesystem | run_manifest_<run>.json | ops,audit,reproducibility

ops/alerting.py | CORE (Phase 16) | centralized alert dispatch; severity levels (INFO/WARNING/ERROR/CRITICAL); dedup throttle; Discord webhook integration | runner_live,ops/health,ops/watchdog,ops/invariants | notify,json | NO | cfg | none (network side-effect) | ops,network,side-effects

ops/watchdog.py | CORE (Phase 16) | supervisor process: monitors runner_live subprocess, auto-restart with exponential backoff, mode-aware gating (no restart in RECONCILIATION_ONLY), alert on repeated crashes | ops/watchdog (entry) | subprocess,runtime_mode,ops/alerting | NO | filesystem | none | ops,supervisor,restart-safety

ops/argus_builder.py | TOOL | autonomous build agent wrapper (Claude Agent SDK); reads roadmap.txt; implements next phase; run from separate PowerShell terminal (not inside Claude Code session) | manual | claude_agent_sdk,anyio | NO | roadmap.txt,CLAUDE.md | ops/logs/builder_<ts>.log | tool,autonomous

ops/run_backtest.ps1 | OPS | canonical backtest wrapper: Reset-ArgusEnv + two deterministic runs + artifact/invariant/determinism validation; -SingleRun skips Run 2 + determinism (halves time) | you,CI,dispatch_backtest_pc2 | backtest.runner | NO | filesystem | ops\logs\bt_*.log; artifacts via runner | scheduler-safe,determinism,CWD-invariant

ops/dispatch_backtest_pc2.ps1 | OPS | push code to GitHub, SSH to PC2, create+run scheduled task for backtest; -SingleRun, -EnvOverrides hashtable, -Limit | manual | git,ssh,schtasks | NO | filesystem,network | none | two-machine,dispatch

ops/check_pc2.ps1 | OPS | one-command PC2 status: RUNNING + progress % if active, or latest run summary if idle | manual | ssh | NO | network | none | two-machine,monitoring

ops/pull_pc2_results.ps1 | OPS | pull backtest results from PC2 via SCP (summary, trades, equity, entry attempts, event counts); saves to ops/logs/pc2/ | manual | scp | NO | network | ops/logs/pc2/*.csv,*.json | two-machine,results

ops/restart_runner.ps1 | OPS | graceful stop + restart runner_live.py; CimInstance CommandLine match; 30s timeout then force kill; shows key .env config | manual | python,CimInstance | NO | filesystem | none | ops,restart-safety

ops/autostart_runner.ps1 | OPS | auto-start managed watcher/paper runner on login; 30s delay, duplicate check, config list pulled from deployment_pipeline | Startup folder | python,powershell | NO | filesystem,deployment_registry | ops/logs/autostart_*.log | ops,survivability

ops/refresh_candles.ps1 | OPS | download latest candles; -DaysBack (default 30), -SyncPC2 (commit+push+SSH pull) | Task Scheduler daily@03:00,manual | download_candles,git,ssh | NO | network | data/eth_usd_1m.csv | data,two-machine

ops/auto_git_backup.ps1 | OPS | nightly git commit + push; only commits if changes; logs to ops/logs/git_backup_*.log | Task Scheduler daily@02:00 | git | NO | filesystem | ops/logs/git_backup_*.log | backup,survivability

ops/run_comparison_tests.ps1 | OPS | sequential fixed_liq + trendlines comparison backtests; resets env between runs | manual | backtest.runner | NO | .env | none | strategy-research

ops/dashboard.py | CORE (Phase 17) | FastAPI staged fleet dashboard: watcher/paper QA/real-money lanes on one page, broker-backed real account truth, separate $10k paper model, governance/alert visibility, and no synthetic USD fallbacks; serves on port 8080 | manual,autostart | fastapi,uvicorn,argus_flow logs | NO | broker_snapshot,alert_state,evidence_registry,signals,trades | none (HTTP only) | ops,monitoring,web

ops/launch_multi.ps1 | OPS | launch parallel runners (ETH, BTC) in separate PowerShell windows with per-coin env (PRODUCT_ID, ARGUS_LOG_DIR, coin overlay) | manual | runner_live | NO | filesystem | none | multi-coin,launch

ops/coin_rotation.py | OPS | passive coin ranking helper: reads coin_pool.json, scores stored per-coin metrics, returns operator-facing rotation candidates without mutating state | dashboard | json | NO | coin_pool.json | none | multi-coin,monitoring

ops/coin_pool.json | CONFIG | coin pool registry: max_active, active coins, per-coin metrics | coin_rotation,dashboard | n/a | NO | filesystem | none | multi-coin,config

ops/run_queue.ps1 | OPS | backtest queue runner: reads ops/backtest_queue.jsonl, refreshes candles, runs each job sequentially | manual,Task Scheduler | backtest.runner | NO | backtest_queue.jsonl | ops/logs/bt_summary*.json | backtest,automation

ops/ml_train_governor.py | TOOL | governor model training pipeline: loads trade dataset, runs stratified CV + walk-forward validation, trains GradientBoostingClassifier, saves data/ml_governor.pkl + feature report | manual | sklearn,numpy | NO | data/ml_trades.csv or custom input | data/ml_governor.pkl,data/ml_governor_features.json | ML,training

ops/ml_extract_features.py | TOOL | extract ML features from backtest/live trade data | manual | pandas | NO | trade_journal*.csv,signals*.csv | feature CSV | ML,data-prep

--- ANALYTICS PACKAGE (C:\Argus\repo\analytics) ---

analytics/__init__.py | SUPPORT | package marker | n/a | n/a | NO | none | none | packaging

analytics/friction_report.py | ANALYTICS (Phase 11) | latency/slippage/spread distributions from fills.csv + trade_journal_*.csv; p50/p95/p99 by regime/session; writes friction_report_<run>.json + friction_summary.html | manual,reporting/generate_report | csv,json | NO | ops/logs/fills*.csv,trade_journal*.csv | ops/logs/friction_report_<run>.json | analytics,Truth-Class-3

analytics/research_report.py | ANALYTICS (Phase 12) | walk-forward + stress test summary; aggregates bt_summary_*.json across windows; writes research_report_<date>.json | manual,backtest/walk_forward,backtest/stress_runner | json | NO | ops/logs/bt_summary_*.json | ops/logs/research_report_<date>.json | analytics,Truth-Class-3

analytics/risk_model.py | ANALYTICS (Phase 13) | per-trade PnL stats, win rate, expectancy, variance, Sharpe, drawdown distribution (historical+Monte Carlo 10k+ paths), risk-of-ruin, Kelly fraction, time-to-ruin; warns when < 50 trades; writes risk_model_<date>.json | manual,reporting/generate_report | csv,json,random | NO | ops/logs/trade_journal_*.csv | ops/logs/risk_model_<date>.json | analytics,Truth-Class-3,monte-carlo

analytics/attribution.py | ANALYTICS (Phase 14) | regime/session/entry_reason/hold_time/MAE-MFE/fee-drag attribution; buckets per-trade PnL by condition; writes attribution_<date>.json + attribution_<date>.html | manual,reporting/generate_report | csv,json | NO | ops/logs/trade_journal_*.csv,ops/logs/bt_summary_*.json | ops/logs/attribution_<date>.json,attribution_<date>.html | analytics,Truth-Class-3

--- BACKTEST RESEARCH TOOLS (additions to backtest package) ---

backtest/friction_injector.py | TOOL (Phase 11) | injects empirical friction percentiles (p50/p95/p99) from friction_report.json into backtest feed; constant/conditional/Monte Carlo friction modes | backtest/stress_runner | json | NO | friction_report_*.json | none (feed modifier) | backtest,research

backtest/walk_forward.py | TOOL (Phase 12) | N-window walk-forward validation; each window independently backtested; aggregates results into research summary | manual,analytics/research_report | subprocess,json | NO | candles CSV | bt_summary per window | backtest,research,reproducibility

backtest/stress_runner.py | TOOL (Phase 12) | spread widening (1.5x/2x/3x), vol spike/compression, regime distortion injection; runs backtest under each stress scenario | manual,analytics/research_report | subprocess,json | NO | candles CSV | bt_summary per scenario | backtest,research,stress-test

--- REPORTING PACKAGE (C:\Argus\repo\reporting) ---

reporting/__init__.py | SUPPORT | package marker | n/a | n/a | NO | none | none | packaging

reporting/generate_report.py | REPORTING (Phase 15) | single self-contained HTML report (no CDN, no server); reads all analytics artifacts; panels: equity+drawdown chart, trade table, daily/weekly PnL, regime/session attribution bar charts, MAE/MFE scatter, risk model summary, friction panel, friction-vs-baseline comparison, reconciliation status table; writes ops/logs/report_<date>.html | manual (CLI), post-backtest hook | json,csv | NO | ops/logs/trade_journal*.csv,equity*.csv,bt_summary*.json,friction_report*.json,attribution*.json,risk_model*.json | ops/logs/report_<date>.html | reporting,Truth-Class-3,self-contained

--- EXECUTION PACKAGE (C:\Argus\repo\execution) ---

execution/adapter.py | CORE | ExecutionAdapter ABC + canonical data models (AccountState, PositionState, OrderRequest, OrderState, FillState) | state,runner_live | none | NO | none | none | schema,stability

execution/paper_adapter.py | CORE | PaperAdapter: full order lifecycle (NEW→FILLED/CANCELED), position/account tracking, fill plans, exactly-once fill IDs, artifact persistence (orders/fills/positions/account CSVs) | state | adapter | YES | cfg,filesystem | ops\logs\orders.csv; fills.csv; positions.csv; account.csv; order_events.csv; fill_plans.json; id_state.json | accounting,artifact-contract,restart-safety

execution/recovery.py | CORE | reconcile_on_startup(): rebuild runtime truth from fills.csv canonical source; resolves FLAT/OPEN/ENTERING/EXITING with strict precedence: fills > positions > snapshot | runner_live | adapter,csv | NO | fills.csv,orders.csv,positions.csv,account.csv,snapshot | recovery_<run>.json | restart-safety,forensics,canonical-truth

execution/exec_io.py | IO | execution artifact logging helpers (orders/fills/positions/account CSV writers) | paper_adapter,runner_live | io_logs | NO | filesystem | ops\logs execution CSVs | CSV,schema

execution/intent.py | SCHEMA | intent models (BuyIntent, SellIntent) | engine,runner_live | none | NO | none | none | schema

execution/checkpoint.py | SUPPORT | checkpoint helpers for atomic state saves | runner_live | none | NO | filesystem | checkpoint files | restart-safety

--- ARTIFACT ROOT (CANONICAL: C:\Argus\repo\ops\logs) ---

ops/logs/events_<run_id>.csv | ARTIFACT | per-run event stream (classification boundary source-of-truth) | backtest/runner | n/a | NO | filesystem | immutable per run | artifact-contract,forensics

ops/logs/signals_<run_id>.csv | ARTIFACT | per-run signal snapshots (full header schema) | backtest/runner | n/a | NO | filesystem | immutable per run | artifact-contract,analysis

ops/logs/equity_<run_id>.csv | ARTIFACT | per-run equity curve (epoch,equity,cash,qty) | backtest/runner | n/a | NO | filesystem | immutable per run | accounting,reproducibility

ops/logs/trades_<run_id>.csv | ARTIFACT | per-run closed trades summary | backtest/runner | n/a | NO | filesystem | immutable per run | accounting

ops/logs/bt_summary_<run_id>.json | ARTIFACT | per-run summary metrics (invariants, medians, DD, etc.) | backtest/runner | n/a | NO | filesystem | immutable per run | metrics,closure

ops/logs/bt_summary_latest.json | POINTER | latest run summary pointer (full summary copy) | backtest/runner | n/a | NO | filesystem | overwritten each run | pointer-consistency

ops/logs/live_events.csv | LIVE ARTIFACT | live event stream (live only; never touched by bt if sandboxed) | runner_live | n/a | NO | filesystem | append-only | live-state

ops/logs/live_signals.csv | LIVE ARTIFACT | live signal snapshots (live only; never touched by bt if sandboxed) | runner_live | n/a | NO | filesystem | append-only | live-state

--- ORPHANED FILES (not imported by anything active; candidates for deletion) ---

equity_logger.py | ORPHANED | legacy equity logging; not imported by any current module | none | unknown | NO | unknown | unknown | ORPHANED,cleanup-candidate
NOTE equity_logger.py: grep confirms zero imports from active code — safe to delete

control_plane.py | ORPHANED | legacy control plane stub; only references itself | none | unknown | NO | unknown | unknown | ORPHANED,cleanup-candidate
NOTE control_plane.py: grep confirms zero imports from active code — safe to delete

requirements.txt | CONFIG | minimal pip dependencies for Argus (pandas, numpy, matplotlib, requests, python-dotenv, pytz) | pip install -r | n/a | NO | none | none | packaging,pc2-setup

ops/run_comparison_tests.ps1 | OPS | sequential fixed_liq + trendlines comparison backtests; resets env between runs; Run after baseline completes | manual | backtest.runner | NO | .env | none | strategy-research

--- STALE SCRATCH FILES (repo root) ---

bt_run_output.txt | SCRATCH | captured backtest stdout from manual run; superseded by ops/logs artifacts | none | none | NO | none | none | cleanup-candidate

--- MISPLACED ARTIFACT DIRECTORIES (repo root — should not exist here) ---

logs/ | STALE-DIR | pre-March-10 backtest artifacts written before ops/logs/ was canonical path | none | none | NO | filesystem | none | cleanup-candidate
NOTE logs/: contains events/signals/bt_summary/equity/trades from 2026-02-22 through 2026-03-11 runs + old ops_backtest*.log files; safe to archive/delete once confirmed no longer needed for forensics

--- ARGUS FLOW (IBKR TRADING SYSTEM) - added 2026-03-24 ---

# Runners
runner_unified.py | CORE | Single-process multi-instrument runner | CLI | ib_insync | YES | IBKR,configs | logs/ | execution
runner_eurusd.py | RUNNER | EUR/USD (superseded by unified) | CLI | ib_insync | YES | IBKR | logs/eurusd/ | superseded
runner_gbpusd.py | RUNNER | GBP/USD (superseded) | CLI | ib_insync | YES | IBKR | logs/gbpusd/ | superseded
runner_mnq.py | RUNNER | MNQ (superseded) | CLI | ib_insync | YES | IBKR | logs/mnq/ | superseded
runner_fx_generic.py | RUNNER | Generic FX (any pair) | CLI | ib_insync | YES | IBKR | logs/ | execution
runner_futures_generic.py | RUNNER | Generic futures (any contract) | CLI | ib_insync | YES | IBKR | logs/ | execution
runner_crypto_ibkr.py | RUNNER | IBKR Paxos crypto | CLI | ib_insync | YES | IBKR | logs/ | needs-subscription

# Configs (17 strategy configs)
configs/*_paper_v1.json | CONFIG | Per-instrument strategy parameters | runners | n/a | NO | n/a | n/a | frozen-cohort
argus_flow/configs/discovery_fx_universe.json | CONFIG | Weekly FX discovery universe + template/ranking controls for Friday onboarding into watcher stage | weekly_pair_onboarding | n/a | NO | maintained candidate list | none | onboarding,discovery,staging
configs/hashes.json | DATA | Config SHA256 hashes | config_check | n/a | NO | configs | n/a | ops

# Ops (12 tools)
argus_flow/ops/smoke_test.py | OPS | Pre-launch verification: read-only IBKR gateway/account summary, config validation, live log/state readability, and sample FX/futures market-data checks with collision-safe client IDs and no runner-state mutation | CLI | ib_insync | NO | IBKR,configs,argus_flow/logs/* | stdout | ops,smoke,launch
ops/health_check.py | OPS | Fleet status | CLI | ib_insync | NO | IBKR,logs | stdout | ops
ops/heartbeat_monitor.py | OPS | Runner alive/dead check | CLI | n/a | NO | logs | heartbeat.json | ops
ops/position_monitor.py | OPS | Managed-fleet IBKR position reconciliation using deployment registry + broker truth | CLI | ib_insync | NO | IBKR,state,deployment_registry | position_monitor.json | ops
ops/divergence_guard.py | OPS | Replay vs live comparison | CLI | n/a | NO | configs,logs | divergence_report.json | governance
ops/correlation_guard.py | OPS | USD pair exposure limit | CLI | n/a | NO | state | correlation_check.json | ops
ops/daily_report.py | OPS | Managed paper-QA daily P&L summary for the current cohort | CLI | n/a | NO | logs,deployment_registry | daily_report.json | ops
ops/discord_alerts.py | OPS | Trade notifications | CLI | requests | NO | .env,logs | Discord | ops
ops/config_check.py | OPS | Config validation + hash | CLI | n/a | NO | configs | hashes.json | governance
ops/refresh_ibkr_data.py | OPS | Pull IBKR historical bars | CLI | ib_insync | NO | IBKR | data/ | data

# Analytics (8 research tools)
analytics/replay_breakout_flow.py | RESEARCH | Cascade replay engine | CLI | n/a | NO | bars | replay_out/ | closed
analytics/run_sweep.py | RESEARCH | Parameter grid sweep | CLI | replay | NO | bars | sweep_results | closed
analytics/classify_edge.py | RESEARCH | Edge classifier | CLI | n/a | NO | sweep | classification | closed
analytics/delay_simulator.py | RESEARCH | Entry delay impact | CLI | n/a | NO | events | delay_summary | closed
analytics/displacement_analysis.py | RESEARCH | Payoff-first event finder | CLI | n/a | NO | bars | displacement | research
analytics/eurusd_payoff_test.py | RESEARCH | EUR/USD backtest | CLI | n/a | NO | data | payoff_results | research
analytics/eurusd_stress_test.py | RESEARCH | 7-test stress suite | CLI | payoff_test | NO | data | stdout | research
analytics/index_futures_analysis.py | RESEARCH | Multi-instrument analysis | CLI | ib_insync | NO | IBKR | indices/ | research

# Strategies (3 ICT/MambaFX - backtested, mixed results)
strategies/fvg_detector.py | RESEARCH | Fair Value Gap (ICT) | test_all | n/a | NO | bars | stdout | research
strategies/liquidity_sweep.py | RESEARCH | Liquidity sweep (ICT) | test_all | n/a | NO | bars | stdout | research
strategies/volume_profile.py | RESEARCH | Volume Profile (MambaFX) | test_all | n/a | NO | bars | stdout | research

# Tests (3 suites)
tests/adversarial_tests.py | TEST | Tier 1 data injection (5 tests) | CLI | runner_eurusd | NO | n/a | stdout | test
tests/test_unified_faults.py | TEST | Unified runner faults (5 tests) | CLI | runner_unified | NO | n/a | stdout | test
tests/test_kraken_connectivity.py | TEST | Kraken API test | CLI | kraken_client | NO | Kraken | stdout | test

# Docs
COHORT_SPEC.md | DOC | Cohort governance (valid/invalid, promotion gates) | n/a | n/a | NO | n/a | n/a | governance
PAPER_GATES.md | DOC | Paper trading acceptance/kill gates | n/a | n/a | NO | n/a | n/a | governance

--- 2026-03-31 DELTA ---

ops/process_lock.py | OPS SUPPORT | OS-level singleton lock helper for runner/dashboard launch safety | ops/dashboard, runner entrypoints | win32/kernel mutex APIs | NO | runtime metadata | none | ops,singleton
argus_flow/sizing.py | CORE SUPPORT | Risk-based FX/futures sizing math from equity/risk budget | runner_unified, tests | n/a | NO | equity,risk inputs | size outputs | sizing,risk
argus_flow/ops/fleet_registry.py | OPS CORE | Canonical managed-fleet discovery and stage defaults (watcher/paper/real), plus modeled risk-policy metadata and stage-aware log dirs | dashboard,runner_unified,promotion gate,launchers | configs,deployment_registry | NO | argus_flow/configs, argus_flow/logs/deployment_registry.json | normalized runner metadata | ops,staging,fleet
argus_flow/ops/deployment_pipeline.py | OPS CORE | Builds deployment_registry.json, enforces the staged lifecycle (watcher observe-only -> paper QA -> real -> quarantine/killed), applies the shared earned-risk ladder, materializes live configs on PROMOTE, and emits stage-aware launcher config sets | launch_fleet,autostart_runner,run_cohort_report | configs,promotion gate,walkforward,demotion check | NO | argus_flow/configs, argus_flow/logs/* | deployment_registry.json | ops,staging,promotion
argus_flow/ops/refresh_managed_truth.py | OPS CORE | Canonical managed-truth refresh runner: regenerates governance, staging, oversight, and alert surfaces behind one lock-protected pipeline used by launch_fleet, watchdog, scheduler, and nightly summary | launch_fleet,watchdog_managed,run_cohort_report,refresh_managed_truth.ps1 | subprocess,process_lock | NO | argus_flow/logs/* | managed_truth_refresh.json, managed_truth_refresh.log | ops,governance,staging
argus_flow/ops/broker_truth.py | OPS CORE | Canonical broker/account truth helpers: broker_state, broker_snapshot merge/load, freshness checks | runner_unified,evidence_registry,position_monitor,dashboard | json filesystem | NO | argus_flow/logs/_broker, broker_state.json | merged truth artifacts | ops,broker-truth
argus_flow/ops/evidence_registry.py | OPS GOVERNANCE | Per-runner governance truth writer: runtime, broker truth, walk-forward, promotion readiness, and stage metadata into one evidence_registry.json per managed runner | dashboard,alerting,daily review | logs,broker truth,promotion gate | NO | argus_flow/logs/* | evidence_registry.json | governance,evidence,staging
argus_flow/ops/artifact_divergence.py | OPS GOVERNANCE | Local artifact integrity checker: cohort row counts, trade journal serials, config hash drift, signal/heartbeat freshness, registry consistency | ops/run_cohort_report, alert escalation, risk oversight | trades,state,evidence_registry,hashes | NO | argus_flow/logs/*, configs/hashes.json | artifact_divergence_report.json | governance,artifact-integrity
argus_flow/ops/walkforward_validation.py | OPS RESEARCH | Managed-fleet walk-forward validator; writes per-runner walkforward_report.json and feeds watcher->paper promotion readiness | ops/run_cohort_report, manual | replay / configs | NO | configs, logs | walkforward_report.json | research,validation
argus_flow/ops/onboard_pair.py | OPS GOVERNANCE | Single-pair onboarding helper: validates config, registers filename-keyed hash, runs payoff-contract backtest checks, and can mark new configs as managed watcher/discovery entries | manual,weekly_pair_onboarding | fx_backtest,hashes,fleet_registry | NO | configs,data | hashes.json,stdout | onboarding,governance
argus_flow/ops/weekly_pair_onboarding.py | OPS GOVERNANCE | Weekly FX discovery/onboarding engine: skips symbols already in repo rotation, refreshes IBKR data when needed, evaluates onboarding + walk-forward, and adds only the top N new candidates to watcher | run_weekly_pair_onboarding.ps1,scheduler | download_ibkr_bars,onboard_pair,walkforward_validation,refresh_managed_truth | NO | discovery_fx_universe,data,configs | weekly_pair_onboarding_latest.json,watcher configs,onboarding_report.json | onboarding,discovery,staging
argus_flow/ops/promotion_gate_v2.py | OPS GOVERNANCE | Canonical paper-to-live promotion gate with walk-forward-aware hard checks and evidence gaps | ops/run_cohort_report, generate_live_config, dashboard/evidence surfaces | trades,walkforward,evidence | NO | argus_flow/logs/* | promotion_gate_report.json | governance,promotion
argus_flow/ops/alert_escalation_v2.py | OPS GOVERNANCE | Canonical incident state builder + Discord alert escalation; writes alert_state.json and alert_events.jsonl with opened/resolved/manual-action flow | ops/run_cohort_report | discord_alerts, governance reports | NO | position_monitor,risk_oversight,divergence,artifact,promotion reports | alert_state.json, alert_events.jsonl | governance,alerting,ops
argus_flow/ops/risk_oversight.py | OPS GOVERNANCE | Managed-fleet runtime/risk oversight report across all staged runners; feeds alert escalation and dashboard health | ops/run_cohort_report,dashboard | evidence,broker truth,position monitor | NO | argus_flow/logs/* | risk_oversight_report.json | governance,risk,ops
ops/launch_fleet.ps1 | OPS | stage-aware fleet launcher: refreshes canonical managed truth first, reconciles grouped watcher/paper runners from runtime lock metadata, starts real runner if promoted configs exist, optionally starts dashboard/discord watcher, then runs smoke test | manual | powershell,python | NO | deployment_registry,lock metadata,managed_truth_refresh | fleet_launch.log | ops,launch,staging
ops/autostart_runner.ps1 | OPS | boot-time grouped paper/watcher runner starter: reads managed config set from deployment_pipeline and reconciles existing runner groups via runtime lock metadata before starting anything new | scheduler,startup | powershell,python | NO | deployment_registry,lock metadata | autostart_<ts>.log | ops,launch,startup
ops/watchdog_managed.ps1 | OPS | Stage-aware singleton watchdog for the managed fleet: runs managed truth refresh every 10 minutes, uses deployment_registry truth, requires consecutive failures before alerting, falls back to dashboard lock truth, supervises Discord heartbeat, and reconciles drift via launch_fleet.ps1 | scheduler,startup,manual | powershell,python | NO | deployment_registry,lock metadata,discord heartbeat,managed_truth_refresh | watchdog_managed.log,watchdog_last_heartbeat.txt | ops,supervision,staging
ops/refresh_managed_truth.ps1 | OPS | PowerShell wrapper for the canonical managed-truth refresh runner; used by Task Scheduler and nightly summary automation | scheduler,manual | powershell,python | NO | argus_flow/logs/* | managed_truth_refresh.json, managed_truth_refresh.log | ops,governance,staging
ops/run_weekly_pair_onboarding.ps1 | OPS | Friday wrapper for weekly_pair_onboarding.py; evaluates new FX candidates and stages the top new passers into watcher before the normal promotion pipeline takes over | scheduler,manual | powershell,python | NO | discovery_fx_universe,data,configs | weekly_pair_onboarding.log, weekly_pair_onboarding_latest.json | ops,onboarding,discovery
ops/run_cohort_report.ps1 | OPS | nightly summary wrapper over refresh_managed_truth with Discord summary enabled | scheduler,manual | powershell,python | NO | argus_flow/logs/* | cohort_report.log, managed_truth_refresh.json | ops,governance,staging
ops/dashboard.py | CORE (Phase 17) | FastAPI single-page staged control center ordered as system health -> config/source-of-truth -> prod -> QA -> watchers -> signal feed, with broker-backed live account truth, separate $10k paper-model account (0.5% active risk, 3% earned cap), compact stage-split journals, report freshness, active issues, manual-action queue, alert history, and no synthetic USD fallback when journal pnl_usd is missing | manual,autostart | fastapi,uvicorn,argus_flow logs | NO | broker_snapshot,alert_state,evidence_registry,deployment_registry,signals,trades | none (HTTP only) | ops,monitoring,qa-visibility
