# ARGUS MAP — UPDATED (Phases 7–16 complete, Phase 16.5 analyzed, Phase 17 active)
# Last updated: 2026-03-12 — two-machine setup, Phase 16.5 results, Phase 19 planned

This map reflects your current working reality:
- You run backtest from `C:\Argus\repo` (canonical CWD) using the venv python
- Backtest writes run-scoped artifacts under `C:\Argus\repo\ops\logs\`
- Backtest must never mutate canonical live files; backtest I/O is sandboxed
- Engine is now “intent + accounting + proof”; execution belongs to Phase 8 adapters
- Backtest always runs in ENGINE mode (forced by `_ensure_bt_cfg()`); ADAPTER mode is live-only

---

## 0) Repo layout (source-of-truth roots)

Repo root (canonical):
- `C:\Argus\repo`

Python venv (canonical):
- `C:\Argus\.venv\Scripts\python.exe`

Backtest package:
- `C:\Argus\repo\backtest\`

Ops + artifacts (canonical):
- `C:\Argus\repo\ops\logs\`

GitHub remote:
- `https://github.com/Ksmith2322/Argus` — branch `phase6-hardening`
- PC1 pushes; PC2 runs `git pull origin phase6-hardening` to sync

Two-machine layout (confirmed 2026-03-12):
- PC1 (main): runner_live.py 24/7 + dev work + heavy backtests
- PC2 (secondary): offloaded backtest jobs via git pull + manual dispatch
- .env NOT in git (gitignored) — copy manually via flash drive (E:\Argus)
- ops/logs NOT in git — artifacts are local to each machine

---

## 1) Entry points (what you actually run)

Backtest (canonical)
Preferred (script run):
- `C:\Argus\.venv\Scripts\python.exe .\backtest\runner.py`

Also valid (module run):
- `C:\Argus\.venv\Scripts\python.exe -m backtest.runner`
  - requires: `backtest/__init__.py` ✅

Live (canonical)
- `C:\Argus\.venv\Scripts\python.exe .\runner_live.py`
  - or via `C:\Argus\ops\run_live.cmd` wrapper if you pin CWD/logs

Debug one-step
- `C:\Argus\.venv\Scripts\python.exe .\_debug_step.py`

---

## 2) Runtime call graph (high-level)

Backtest loop (current Phase 7 contract)
`backtest/runner.py`
→ `config.load_config()`
→ `_ensure_bt_cfg()` (backtest-safe overlay: mode flags + sandbox paths)
→ `state.BotState.from_config(cfg)`
→ `backtest.loader.load_candles_csv(...)`
→ `backtest.feed.ticks_from_close_series(...)`
→ (optional) damage/injection (ARGUS_PROFILE on snapshots or ticks)
→ `engine.step(state, tick, cfg, paused=False, http=http)`
→ `io_logs.log_signal_snapshot(snap, ...)`  (sandboxed in BT)
→ `io_logs.log_event(snap.events, ...)`     (per-run events_<run>.csv)
→ `backtest.results.BacktestResults` reads events/signals/equity/trades to compute:
   - invariants
   - entry attempt accounting
   - summary metrics
→ writes:
   - `equity_<run>.csv`
   - `trades_<run>.csv`
   - `bt_summary_<run>.json`
→ updates pointer:
   - `bt_summary_latest.json`

Live loop (Phase 7: intent-only / paper ledger)
`runner_live.py`
→ feed (exchange / broker / synthetic)
→ `engine.step(...)`
→ `io_logs.log_signal_snapshot(...)` (live paths)
→ `io_logs.log_event(...)`           (live paths)
→ notify hooks (optional)
→ (Phase 8) execution adapter consumes intents and produces orders/fills artifacts

---

## 3) “One source of truth” ownership (hard boundaries)

Config / env
- `config.py` → `load_config()`
  - owns: defaults + env override surface + mode flags

State ownership
- `state.py` → `BotState`
  - owns: candles builders, strategy engines, confluence engines, regime/structure/liquidity engines
  - owns: ledger + risk + caches (`last_st_*`, last candle close, cooldown epoch, etc.)

Decision/step orchestration
- `engine.py` → `step(state, tick, cfg, paused, http=...)`
  - owns: classification + gating + event emission + snapshot population
  - does NOT own: durable execution (Phase 8), exchange truth, or artifact schemas

Logging schema + durable CSV writing (canonical)
- `io_logs.py`
  - owns: schema headers, row mapping, path resolution, header upgrades, sandbox rules
  - only allowed “snap → CSV row” translator

Backtest metrics & invariants (canonical)
- `backtest/results.py`
  - owns: attempt accounting, event counts, derived stats, summary()

Subsystems (market logic)
- `strategy_phase2.py`
- `confluence.py` / `adaptive_confluence.py`
- `regime.py`
- `structure.py`
- `liquidity.py`
- `session.py`
- `risk.py`
- `ledger.py`
- `indicators.py`
- `candles.py`

Runtime mode / ops infrastructure
- `runtime_mode.py` → 5-level mode system (FULL/NO_NEW_ENTRY/REDUCE_ONLY/OBSERVATION_ONLY/RECONCILIATION_ONLY); crash-safe persistence; escalation-only transitions; action gating; JSONL audit trail
- `ops/health.py` → health check framework (feed staleness, fill latency, consecutive losses, slippage anomaly, invariant violations); auto-escalates mode
- `ops/invariants.py` → continuous invariant engine (position/fill match, duplicate fill IDs, order/fill linkage, timestamp ordering, cash/exposure limits)
- `ops/run_manifest.py` → config freeze (SHA-256), code hash, run manifests, drift detection
- `ops/alerting.py` → centralized alert dispatch; severity levels; dedup throttle; Discord webhook
- `ops/watchdog.py` → supervisor process; auto-restart with exponential backoff; mode-aware gating

Analytics pipeline (Truth Class 3 — derived from canonical artifacts, never feeds back into runtime)
- `analytics/friction_report.py` → latency/slippage/spread distributions from fills + trade_journal; writes friction_report_<run>.json + friction_summary.html
- `analytics/research_report.py` → walk-forward + stress test summary; reads bt_summary_*.json
- `analytics/risk_model.py` → per-trade PnL stats, win rate, Sharpe, Monte Carlo equity paths (10k+), risk-of-ruin, Kelly fraction; warns when < 50 trades; writes risk_model_<date>.json
- `analytics/attribution.py` → regime/session/entry_reason/hold_time/MAE-MFE/fee-drag attribution; writes attribution_<date>.json + .html

Backtest research tools
- `backtest/friction_injector.py` → injects empirical friction percentiles (p50/p95/p99) into backtest feed
- `backtest/walk_forward.py` → N-window walk-forward validation; each window independently backtested
- `backtest/stress_runner.py` → spread widening, vol spike/compression, regime distortion injection

Reporting / dashboard
- `reporting/generate_report.py` → single self-contained HTML report (no CDN, no server); reads all analytics artifacts; panels: equity+drawdown, trade table, regime/session attribution, MAE/MFE scatter, risk model summary, friction, reconciliation status; writes ops/logs/report_<date>.html

Autonomous build agent
- `ops/argus_builder.py` → Claude Agent SDK wrapper; reads roadmap.txt; autonomous phase implementation; run from a separate PowerShell (not inside Claude Code session)

---

## 4) Artifact contract (Phase 7 backtest)

Canonical artifact directory:
- `C:\Argus\repo\ops\logs\`

Runner emits per-run artifacts (expected per run_id):
- `events_<run_id>.csv`
- `signals_<run_id>.csv`
- `equity_<run_id>.csv`
- `trades_<run_id>.csv`
- `bt_summary_<run_id>.json`

Latest pointer (overwritten each run):
- `bt_summary_latest.json`

Optional / if present (allowed but must be stable):
- `run_header_<run_id>.json`
- `event_counts_<run_id>.csv`
- `entry_attempts_<run_id>.csv`
- `entry_attempt_detail_<run_id>.csv`

Decision point (remove drift):
- If any tooling expects `bt_latest.json`, either:
  - add it as a tiny pointer: `{"run_id":"...","bt_summary":"bt_summary_<run>.json"}`, OR
  - delete/update the tooling. Do NOT leave “phantom expectations”.

---

## 5) Sandbox rule (non-negotiable)

Backtest must not mutate canonical live files:
- `...\ops\logs\live_events.csv`
- `...\ops\logs\live_signals.csv`

Enforcement (backtest mode):
- `LIVE_EVENTS_CSV = C:\Argus\repo\ops\logs\live_events_bt_sandbox.csv`
- `LIVE_SIGNALS_CSV = C:\Argus\repo\ops\logs\live_signals_bt_sandbox.csv`

`io_logs.py` must respect those env/cfg paths in:
- `events_csv_path()`
- `signals_csv_path()`

Net effect:
- backtest calls to `log_signal_snapshot()` / `log_event()` never touch production live_*.csv

---

## 6) Attempt accounting boundary (final rule)

Attempt boundary = classification events only (engine-emitted)
Countable attempt outcomes:
- `WOULD_BUY` / `SHOULD_BUY` (if you treat intent as an attempt)
- `ENTRY_FILLED`
- `MISSED_BUY_*`

Debug-only (must not be counted):
- `ENTRY_ATTEMPT`
- `ENTRY_METRICS` (storeable, but must not create double counting)

Invariants (must hold):
- `entry_attempts == entry_filled + entry_blocked_total`
- if emitting both debug events: `ENTRY_ATTEMPT == ENTRY_METRICS`
- `entry_attempt_gap == 0`
- `attempt_invariants_ok == True`

---

## 7) Control-plane / services (only if you run them)

If present:
- Windows Services: `ArgusControlPlane`, `cphs`, `cplspcon`

Rule:
- CP artifacts and backtest artifacts cannot share “latest” pointers or run_id namespaces.
- If CP writes logs, give it its own root:
  - `C:\Argus\cp\logs\` (example)
  - never `C:\Argus\repo\ops\logs\`

---

## 8) Ops toggles (kill/pause)

Current implementation (typical pattern in `io_logs.py`):
- `is_kill_switch_on(cfg)` checks a file path
- `is_paused(cfg)` checks a file path

Hard rule:
- Toggle paths must be CWD-invariant (absolute) OR derived from repo root (not from file location).
If you keep them relative, they will break under Task Scheduler CWD drift.

Recommended canonical (Phase 8-ready):
- `KILL_SWITCH_FILE = C:\Argus\repo\ops\KILL_SWITCH.txt`
- `PAUSE_FILE      = C:\Argus\repo\ops\PAUSE.txt`

---

## 9) Determinism / diffing artifacts (PowerShell)

Avoid `fc` alias pitfalls.

Option A — cmd fc:
- `cmd.exe /c fc .\ops\logs\events_$run1.csv .\ops\logs\events_$run2.csv`

Option B — Compare-Object:
- `Compare-Object (Get-Content $e1) (Get-Content $e2) | Select -First 50`

Rule:
- Determinism diff should be clean except fields you intentionally vary:
  - `run_id`
  - timestamps (if present)
Everything else should match if inputs match.

---

## 10) “Done” definition for Phase 7 closure

From `C:\Argus\repo`, repeated twice:

1) Invocation is stable (no module-path drift)
2) All required per-run artifacts exist under `C:\Argus\repo\ops\logs\`
3) `bt_summary_latest.json` reflects newest run (by contents, not mtime)
4) Events + equity are deterministic (diff clean except run_id/timestamps)
5) Sandbox confirmed: no writes to `live_events.csv` / `live_signals.csv`

---

## 11) Phase 8 insertion point (so you don’t architecture-drift)

Phase 8 adds a new layer without corrupting Phase 7:

Strategy → Engine → ExecutionAdapter → Ledger → Artifacts

What changes:
- Engine continues emitting intents (`WOULD_BUY/SELL` or `SHOULD_*`)
- ExecutionAdapter consumes intents and produces:
  - orders
  - fills
  - reconciled positions
- Ledger becomes “execution-fed” (fills drive state), not “engine-fed” (engine calling buy/sell directly)

New Phase 8 artifacts (in `ops\logs\`):
- `orders_<run>.csv`
- `fills_<run>.csv`
- `positions_<run>.csv`
- `trade_journal_<run>.csv`
- `daily_summary_<date>.json`

Non-negotiable:
- Exactly-once dedupe keys:
  - `client_order_id`
  - `fill_id`

---

## 12) Known constraints and invariants (updated 2026-03-10)

Backtest EXECUTION_MODE must be ENGINE:
- `_ensure_bt_cfg()` in `backtest/runner.py` forces `cfg["EXECUTION_MODE"] = "ENGINE"`
- ADAPTER mode in backtest breaks fill accounting: engine emits intents that never update the ledger,
  causing unbounded re-entries on every eligible tick
- Live uses EXECUTION_MODE=ADAPTER (set in `.env`); backtest overrides this

forced_request_state clear ordering (runner_live.py):
- `_restore_runtime_meta_from_recovery()` clears `forced_request_state` when recovery=FLAT
- The snapshot restore of `forced_request_state` is skipped when recovery=FLAT so the clear holds
- Without this fix, a stale inflight forced-state from Gate A testing would survive restart

Shell env hygiene:
- Never run backtest with `ARGUS_BT_ARTIFACT_DIR` set in the shell from a prior session
- `ops/run_backtest.ps1` calls `Reset-ArgusEnv` first — use it for canonical validation runs
- Stale `ARGUS_BT_ARTIFACT_DIR` silently redirects artifacts to the wrong directory

Gate A test script:
- `ops/test_gate_a_phase8.ps1` — canonical Phase 8 acceptance harness
- `Start-Process` requires separate files for `-RedirectStandardOutput` and `-RedirectStandardError`
- Runner stdout goes to `gate_a_*.out.log`; stderr goes to `gate_a_*.err.log`

Reconciliation flat_end_cash baseline (reconciliation.py):
- `_last_flat_cash_baseline()` with `lifecycle_end_ts=None` (no trade_journal rows) must use the
  LAST account row, not a forward scan — forward scan picks the BOOTSTRAP row (also flat, cash=$start)
  before any trading occurred, giving the wrong flat_end_cash
- Fix: when `lifecycle_end_ts is None`, short-circuit to `srows[-1]` (last account row = post-close state)

Reconciliation 2 ULP rounding tolerance (reconciliation.py):
- `flat_start_cash_plus_realized_pnl_equals_flat_end_cash` check uses `tolerance=2e-8`
- `ROUND_DOWN` truncation at each fill step (BUY then SELL) accumulates up to 2 units at the 8th decimal
  place between `positions.realized_pnl` and the actual account cash delta — not a real discrepancy

Trade journal close_to_flat detection (runner_live.py):
- `close_to_flat` uses `prior_qty_ledger` / `post_qty_ledger`, NOT `prior_qty_effective` / `post_qty_effective`
- PaperAdapter processes fills synchronously inside `place_order` via `_try_fill_or_rest`
  so adapter `_positions` is already updated before the fill-back loop sees the fill
- `prior_qty_effective` (adapter qty) is already 0 for SELL fills, making `close_to_flat` False
- VirtualLedger is driven by `_apply_fill_to_ledger` inside the loop, so ledger quantities
  correctly capture the open→flat transition

---

## 13) Phase 9 trade lifecycle finalization (completed 2026-03-10)

Per-trade artifact fields written to `trade_journal_<run_id>.csv`:
- Entry/exit timestamps (ISO-8601)
- Entry/exit prices
- Fees (entry_fee, exit_fee, total_fee)
- MAE/MFE percentages (final_mae_pct / final_mfe_pct captured before ledger reset)
- Regime at entry
- Trade duration (seconds)
- Slippage (configured bps)
- Entry/exit mid prices
- Stable trade identity (trade_id = PaperAdapter PT-* sequence)
- Exit reason (from snap.exit_reason or engine event)

Schema owned by: `trade_journal.py`
Journal write path: `runner_live.py` → `_maybe_write_closed_trade_journal` → `_append_trade_journal_row`
Entry context captured at BUY fill: `_capture_entry_context_from_fill` (stored on state._open_trade_ctx)
MAE/MFE snapshotted: `_apply_fill_to_ledger` SELL branch, before state.mae_pct/mfe_pct reset
Gate A proof: `trade_journal_<run_id>.csv` written on SELL → FLAT transition, all fields populated

---

END