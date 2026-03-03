# ARGUS MAP — UPDATED (repo-root + Phase 7 backtest artifact contract)

This map reflects your **current working reality**:
- You run backtest successfully from **`C:\Argus\repo`** via **`.\backtest\runner.py`**
- Backtest writes **run-scoped artifacts** under **`C:\Argus\repo\ops\logs\`**
- Backtest must **never mutate** canonical live files because `LIVE_*_CSV` are sandboxed

---

## 0) Repo layout (source-of-truth roots)

**Repo root (canonical):**
- `C:\Argus\repo`

**Python venv (canonical):**
- `C:\Argus\.venv\Scripts\python.exe`

**Backtest package:**
- `C:\Argus\repo\backtest\`

**Ops + artifacts (canonical):**
- `C:\Argus\repo\ops\logs\`

---

## 1) Entry points (what you actually run)

### Backtest (canonical)
**Preferred (script run):**
- `C:\Argus\.venv\Scripts\python.exe .\backtest\runner.py`

**Also valid (module run)**
- `C:\Argus\.venv\Scripts\python.exe -m backtest.runner`
  - requires: `backtest/__init__.py` ✅ (you have it)

### Live (canonical)
(Depends on your live runner name; map assumes you have a live loop somewhere like `runner_live.py`.)
- `C:\Argus\.venv\Scripts\python.exe .\runner_live.py`
  - or scheduled wrapper under `ops\` (if you use one)

---

## 2) Runtime call graph (high-level)

### Backtest loop
`backtest/runner.py`
→ `config.load_config()`
→ `_ensure_bt_cfg()` (backtest-safe overlay)
→ `state.BotState.from_config(cfg)`
→ `backtest.loader.load_candles_csv(...)`
→ `backtest.feed.ticks_from_close_series(...)`
→ (optional) `apply_damage_to_ticks(...)`
→ `engine.step(state, tick, cfg, paused=False, http=http)`
→ `io_logs.log_signal_snapshot(snap, ...)`  (sandboxed live_* in BT)
→ `io_logs.log_bt_event(...)` (event sink; should be per-run or sandbox)
→ `backtest.results.BacktestResults` aggregates events/snapshots
→ writer methods:
   - `write_equity_curve_csv`
   - `write_trades_csv`
   - `write_event_counts_csv`
   - `write_entry_attempt_stats_csv`
   - `write_entry_attempt_detail_csv` (if implemented)
→ `_write_bt_summary()` emits `bt_summary_<run>.json` + `bt_summary_latest.json`

### Live loop (conceptual)
`runner_live.py` (or equivalent)
→ feed (exchange / broker)
→ `engine.step(...)`
→ `io_logs.log_signal_snapshot(...)` (real live_* default or env override)
→ `io_logs.log_bt_event(...)` (events sink)
→ notify/trade executor (if enabled)

---

## 3) “One source of truth” ownership

### Config / env
- `config.py` → `load_config()`
  - owns defaults + env override surface

### State ownership
- `state.py` → `BotState`
  - owns: ledger, positions, caches, last ticks, regime/session derived state

### Decision/step orchestration
- `engine.py` → `step(state, tick, cfg, paused, http=...)`
  - owns: calls into indicators/strategy/regime/liquidity/session/etc
  - emits: snapshot + events list

### Logging schema + durable CSV writing (canonical)
- `io_logs.py`
  - owns schemas:
    - `_signals_header()`
    - `_events_header()`
  - owns path resolution:
    - `logs_dir()`
    - `signals_csv_path()`
    - `events_csv_path()`
  - owns translation:
    - `snapshot_to_signal_row()`  (**only** allowed snap→CSV mapping)
  - owns safety:
    - `ensure_signals_header_matches_file()` (append-only upgrade)

### Backtest metrics & invariants (canonical)
- `backtest/results.py` → `BacktestResults`
  - owns: attempt accounting, event counts, derived stats, summary()

### Market logic (subsystems)
- `strategy_phase2.py` (entry/exit logic)
- `confluence.py` / `adaptive_confluence.py`
- `regime.py`
- `structure.py`
- `liquidity.py`
- `session.py`
- `risk.py`
- `ledger.py`
- `indicators.py`
- `candles.py`

---

## 4) Artifact contract (Phase 7)

### Canonical artifact directory (backtest)
**`ARGUS_BT_ARTIFACT_DIR` default:**
- `C:\Argus\repo\ops\logs\`

Runner emits **per-run** artifacts (expected per run_id):
- `events_<run_id>.csv`
- `signals_<run_id>.csv`
- `equity_<run_id>.csv`
- `trades_<run_id>.csv`
- `event_counts_<run_id>.csv`
- `entry_attempts_<run_id>.csv`
- `entry_attempt_detail_<run_id>.csv` (optional)
- `run_header_<run_id>.json`
- `bt_summary_<run_id>.json`

Latest pointer (single file, overwritten each run):
- `bt_summary_latest.json`  ✅ (you have this)

**Decision point (keep it clean):**
- Either **add** `bt_latest.json` (tiny pointer `{ "run_id": "..." }`)
- Or **delete/stop using** any tooling that expects `bt_latest.json`
  - your error showed tooling expecting it even though it doesn’t exist

---

## 5) Sandbox rule (non-negotiable)

Backtest must not mutate the canonical live files:
- `...\ops\logs\live_events.csv`
- `...\ops\logs\live_signals.csv`

### How it is enforced now
In backtest mode (`ARGUS_MODE=bt`), runner sets (or expects):
- `LIVE_EVENTS_CSV = C:\Argus\repo\ops\logs\live_events_bt_sandbox.csv`
- `LIVE_SIGNALS_CSV = C:\Argus\repo\ops\logs\live_signals_bt_sandbox.csv`

And `io_logs.py` respects those env vars in:
- `events_csv_path()`
- `signals_csv_path()`

**Net effect:** backtest calls to `log_signal_snapshot()` / `log_bt_event()` go to sandbox files, not production `live_*.csv`.

---

## 6) Attempt accounting boundary (final rule)

**Attempt boundary = classification events only** (emitted by engine):
One of:
- `WOULD_BUY`
- `SHOULD_BUY`
- `ENTRY_FILLED`
- `MISSED_BUY_*`

**Debug-only (must not be counted / sampled):**
- `ENTRY_ATTEMPT`
- `ENTRY_METRICS` (you may store it, but don’t let it create double counting)

**Invariants (must hold):**
- `entry_attempts == entry_filled + entry_blocked_total`
- `ENTRY_ATTEMPT == ENTRY_METRICS` (if you emit both as debug per attempt)
- `entry_attempt_gap == 0`
- `attempt_invariants_ok == True`

You’re already seeing these pass in your summary output.

---

## 7) Control-plane / services (only if you run them)

If you have a local control plane (seen previously):
- Windows Services: `ArgusControlPlane`, `cphs`, `cplspcon`
- Those are **not** part of the backtest runner artifact contract unless you explicitly route them into the same `ops\logs` root with a separate namespace.

Rule: **CP artifacts and backtest artifacts cannot share run_id folders or “latest” pointers.**

---

## 8) Ops toggles (kill/pause)

Current implementation (from `io_logs.py`):
- `is_kill_switch_on(cfg)` checks:
  - `os.path.exists(os.path.join(os.path.dirname(__file__), cfg["KILL_SWITCH_FILE"]))`

- `is_paused(cfg)` checks:
  - `os.path.exists(os.path.join(os.path.dirname(__file__), cfg["PAUSE_FILE"]))`

**Implication:** these files live relative to `io_logs.py` location unless you redesign them.
If `io_logs.py` is at repo root, toggles resolve near:
- `C:\Argus\repo\KILL_SWITCH` (or whatever cfg sets)
- `C:\Argus\repo\PAUSE`

---

## 9) Determinism / diffing artifacts (PowerShell note)

In PowerShell, `fc` is commonly aliased and can bite you (as you saw).
Use one of these instead:

**Option A — cmd fc**
- `cmd.exe /c fc .\ops\logs\events_$run1.csv .\ops\logs\events_$run2.csv`

**Option B — Compare-Object**
- `Compare-Object (Get-Content $e1) (Get-Content $e2) | Select -First 50`

---

## 10) “Done” definition for Phase 7 closure

You’re “done” when all are true, from `C:\Argus\repo`, repeated twice:

1) Backtest invocation is stable (no module-path drift)
2) All per-run artifacts exist under `C:\Argus\repo\ops\logs\`
3) `bt_summary_latest.json` points to the most recent run and contains correct paths
4) Events + equity artifacts are deterministic (diff clean except run_id/timestamps)

---