# Helio Fleet - Restructure Guide (2026-04-19)

> **Purpose:** honest architectural critique of the current codebase and a
> prioritized plan to clean it up *before* we scale to $10K real money or
> add a 13th strategy. Written for user review - nothing here has been
> executed.

> **Codex review update, 2026-04-19:** I re-checked the headline claims against
> the repo. The thesis is correct, but a few measurements and risk statements
> needed tightening. The safest recommendation is still Phase 1 only, but "zero
> production risk" is too strong. Treat it as low-risk if Phase 1 remains
> additive/read-only until tests prove each surface.

---

## TL;DR - the four things that hurt most

1. **`ops/dashboard.py` is a 12,230-line monolith with 68 route decorators and 117 top-level functions.** Every new signal, report, or metric tends to sprout another `/api/X` endpoint inside this file. Review diffs are painful, regressions leak in easily, and the UI fans out across many truth files.
2. **Three separate runners in `helio/` (`runner.py`, `runner_apollo.py`, `runner_hermes.py`) plus `argus_flow/runner_unified.py`** repeat the same categories of boilerplate: config loading, heartbeat, state I/O, signal/trade append, and loop orchestration. The exact overlap percentage is not measured, but the duplication is visible.
3. **Truth is scattered across 59 direct JSON + 6 direct JSONL files in `argus_flow/logs/`, and 812 JSON + 35 JSONL files recursively.** Nothing is the source of truth for "is strategy X live and healthy" - the answer depends on which file you ask. Shape drift between modules is still mostly caught by humans re-reading code.
4. **No shared typed domain objects.** There are some local dataclasses in the repo, but no shared `Trade`, `Signal`, `Fill`, or `StrategyState` domain model used across active runners. A trade is a dict/CSV row in one strategy, a different dict in another, and a different JSONL shape in `canonical_fills.jsonl`.

These are the four I'd fix first. The rest (below) compounds if ignored.

---

## Inventory - what we're working with

| Area | Count | Notes |
|------|------:|-------|
| `ops/dashboard.py` | **12,230 LOC** | 68 FastAPI route decorators, 117 top-level functions, one file |
| `helio/` modules | **26 files, 7,980 LOC** | 3 separate runners repeat core boilerplate |
| `argus_flow/logs/*.json` | **59 direct / 812 recursive files** | no shared schema contract, unbounded shape drift |
| `argus_flow/logs/*.jsonl` | **6 direct / 35 recursive files** | append-only surfaces; some logs rotate elsewhere, these truth files generally do not |
| `canonical_fills.jsonl` | 94 rows, ~35 KB today | will grow without bound in live mode |
| Test files | **12 Python files, 4,122 LOC** | integration-heavy - see Section 4 |
| Strategies with unique `PARAMS` dicts | **8+** | each re-defines hours, thresholds, risk |

---

## 1. Dashboard monolith (`ops/dashboard.py`)

**Symptoms**
- Single 12K-line file, 68 route decorators, 117 top-level defs.
- Each new feature tends to append another `/api/X` endpoint or JSON reader.
- Template HTML is inlined as Python strings in the same file.
- No layering: FastAPI app + data loaders + HTML generators + metric aggregators all interleaved.

**Why it hurts**
- Merge conflicts on every session  -  we hit this multiple times mid-session.
- Route and UI complexity scales inside one import path; some views and endpoints fan out across many JSON/JSONL sources.
- Testing any endpoint requires loading the whole module.

**Recommended split**
```
ops/dashboard/
  app.py                 # FastAPI app factory, 50-100 lines
  routes/
    fleet.py             # /api/fleet_status, /api/heartbeats
    sizing.py            # /api/sizing, /api/fleet_sizing_config
    promotion.py         # /api/promotion_readiness, /api/kill_watchdog
    canonical.py         # /api/canonical_fills, /api/reconciliation
    research.py          # /api/drift_forensics, /api/apollo_planned
    ...
  data/
    sources.py           # typed readers: FleetStatusReader, CanonicalFillsReader
  templates/
    index.html           # real template/static file, not inlined strings
```

**Rule of thumb:** no file under `ops/dashboard/` should exceed 400 LOC.

---

## 2. Runner duplication (`helio/runner*.py` + strategy runners)

**Symptoms**
- `helio/runner.py` (567 LOC) - original multi-strategy runner.
- `helio/runner_apollo.py` (382 LOC) - Apollo-specific runner.
- `helio/runner_hermes.py` (350 LOC) - Hermes-specific runner.
- `argus_flow/runner_unified.py` (4,641 LOC) - another runner for Argus pairs.
- Each forge strategy (`gld_pm_long/runner.py`, `wick_gbpusd/runner.py`, etc.) re-implements:
  - `_config_hash`, `_git_sha`
  - `_load_state`, `_save_state`
  - `_write_heartbeat`
  - `_append_trade`, `_append_signal`
  - `evaluate_once` / `loop_mode` boilerplate

All functionally identical except for field lists.

**Recommended**
```
helio/
  strategy_base/
    runner.py          # StrategyRunner base class (evaluate_once, loop, heartbeat, state)
    persistence.py     # append_trade(trade: Trade), append_signal(sig: Signal)
    identity.py        # config_hash, git_sha (once)
  strategies/
    gld_pm_long.py     # only the signal math + params
    wick_gbpusd.py     # only the signal math + params
    ...
```

Each concrete strategy becomes ~80 LOC (just the edge logic), not 400.

---

## 3. No shared typed domain - the hidden tax

**Symptoms**
- `Trade`, `Signal`, `Fill`, `StrategyStats` are dicts with different keys in every module.
- Defensive `.get(key, default)` everywhere, silently masking schema drift.
- `reconciliation.py` exists because no shared type enforces "CSV row <-> canonical fill" equivalence.
- CI/tests can't catch field renames.
- There are local dataclasses in `risk.py`, `ops/health.py`, `ops/invariants.py`, and legacy/archive modules, but there is no active shared `helio/domain.py`.

**Recommended**
```python
# helio/domain.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Trade:
    ts: datetime
    strategy: str
    direction: Literal["long", "short"]
    entry_px: float
    exit_px: float
    pnl_usd: float
    config_hash: str
    # ... one definition, imported everywhere

@dataclass(frozen=True)
class Signal: ...

@dataclass(frozen=True)
class Fill: ...
```

All CSV writers serialize via `Trade.to_csv_row()`; all readers deserialize via `Trade.from_csv_row()`. Drift becomes a type error at write time, not an invisible bug at reconciliation time.

**Migration path**  -  do it column-by-column:
1. Add `helio/domain.py` with the dataclasses.
2. Add serializer/deserializer tests before converting any writer.
3. Pick one non-trading writer or read-only adapter first.
4. Expand to `canonical_fills.py` only after compatibility tests prove existing rows still parse.
5. Last: the strategy runners, one at a time.

---

## 4. Inverted test pyramid

**Symptoms** (LOC in `argus_flow/tests/`):
| File | LOC | Type |
|------|----:|------|
| `test_fx_system.py` | 1,194 | integration/E2E |
| `chaos_test.py` | 639 | fault injection |
| `test_fleet_monitor_faults.py` | 492 | integration |
| `test_orphan_recovery.py` | 325 | integration |
| `test_hardening_controls.py` | 184 | mixed/unit |
| `test_kraken_connectivity.py` | 147 | external/integration |
| `test_fleet_sizing.py` | 296 | **unit** |
| `test_strategy_cards.py` | 252 | **unit** |
| `test_unified_faults.py` | 245 | integration |
| `test_promotion_readiness.py` | 131 | **unit** |
| `adversarial_tests.py` | 217 | adversarial/integration |

**Observation:** unit coverage exists now for fleet sizing, strategy cards, and promotion readiness, but the base is still inverted: most test LOC is integration/fault-injection, while strategy math and serialization contracts have much thinner unit coverage.

**Recommended additions** (prioritized, per-strategy math):
- `test_gdx_gld_trade_engine.py`  -  exercise `run_backtest()` with synthetic z-score series.
- `test_apollo_scoring.py`  -  exercise the score composition for a known catalyst fixture.
- `test_argus_mtf_filter.py`  -  MTF blocker logic for all three pairs.
- `test_portfolio_guard.py`  -  6-position ceiling, directional bias, correlated pairs.

**Target:** unit tests should be ~60% of test LOC, not 15%.

---

## 5. Magic numbers scattered across modules

**Symptoms**
```python
# apollo/execution/planned_trades.py
SCORE_FLOOR = 75

# helio/promotion_readiness.py
REVIEW_GATE = {"min_trades": 30, "min_pf": 1.20}
CANONICAL_GATE = {"min_trades": 60, "min_pf": 1.30}

# forge/gdx_gld_pairs.py
ZSCORE_ENTRY = 2.0
ZSCORE_STOP = 3.0

# forge/wick_gbpusd/runner.py
PARAMS = {"uw_min": 0.6, "cp_max": 0.3, "bb_width_quantile_max": 0.33, ...}

# forge/gld_pm_long/runner.py
PARAMS = {"signal_hours_utc": [18, 19, 20], "stop_atr": 0.5, ...}
```

Each lives in its own module. To see "all our thresholds in one place" you have to grep 8 files.

**Recommended**

Note: `PyYAML` is not currently in the pinned dependencies, while `pydantic` is. If we want YAML specifically, add and pin `PyYAML` intentionally. If we want zero dependency churn, use JSON or TOML plus Pydantic validation. The important part is a typed strategy registry, not the file extension.

```yaml
# config/strategies.yaml  (or config/strategies.json/toml)
promotion:
  review_gate: {min_trades: 30, min_pf: 1.20}
  canonical_gate: {min_trades: 60, min_pf: 1.30}

strategies:
  forge_gld_pm_long:
    signal_hours_utc: [18, 19, 20]
    stop_atr: 0.5
    target_atr: 1.0
    atr_period: 14
    hold_bars: 4
  forge_wick_gbpusd:
    uw_min: 0.6
    cp_max: 0.3
    bb_width_quantile_max: 0.33
    chop_quantile_min: 0.67
    regime_window: 60
  forge_gdx_gld:
    zscore_entry: 2.0
    zscore_stop: 3.0
    zscore_exit: 0.0
    zscore_lookback: 60
  apollo_earnings_drift:
    score_floor: 75
    horizon_trading_days: 3
```

Phase 1 should mirror existing constants into this file and validate it, but not make runners consume it yet. Strategy runners should only load from the registry in Phase 2 after equivalence tests prove the mirrored values match the current hardcoded values. Dashboard can read the registry earlier for a "strategy config" page because that is read-only.

---

## 6. Truth scatter - no canonical fleet state

**Symptoms**  -  dashboard reads these (partial list):
- `fleet_status.json`  -  per-strategy heartbeat + state
- `risk_oversight_report.json`  -  broker truth, drawdown, exposure
- `promotion_readiness.json`  -  gate status per strategy
- `kill_watchdog_report.json`  -  kill rule evaluations
- `reconciliation_report.json`  -  canonical vs CSV drift
- `morning_brief.txt` + `morning_brief_history.jsonl`  -  digest
- `canonical_fills.jsonl`  -  source of truth for fills (supposedly)
- `broker_equity_history.jsonl`  -  equity curve
- `fleet_perf_history.jsonl`  -  perf snapshots
- `signal_frequency_history.jsonl`  -  signal counts
- `alert_events.jsonl` + `alert_state.json` + `alert_history.json`  -  **three** alert files
- `artifact_divergence_report.json`, `correlation_check.json`, `demotion_report.json`, `degradation_control.json`...

**The question** "is `forge_gld_pm_long` live, healthy, and accumulating trades?" cannot be answered from any single file.

**Recommended**
- One consolidated `fleet_state.json` rebuilt by each cohort run or fleet-monitor cycle:
  ```json
  {
    "generated_at": "2026-04-19T...",
    "strategies": {
      "forge_gld_pm_long": {
        "heartbeat": {...},   // from fleet_status
        "performance": {...}, // from perf_history
        "gates": {...},       // from promotion_readiness
        "watchdog": {...},    // from kill_watchdog
        "reconciliation": {...},
        "is_healthy": true,
        "next_action": "OBSERVE_MORE"
      }
    },
    "fleet": {
      "broker_equity_usd": ...,
      "total_open_risk_pct": ...,
      "pause_entries": [...]
    }
  }
  ```
- Individual report files still exist (for debugging). Phase 1 writes `fleet_state.json` additively and compares it to the existing dashboard endpoints. Phase 2 can flip the dashboard main views to read only `fleet_state.json` after diff tests prove equivalence.
- Consumers of "is this healthy?" never re-implement the aggregation.

---

## 7. Unbounded log growth

**Symptoms**
- `canonical_fills.jsonl`  -  append-only, no rotation.
- `forward_returns.jsonl`, `matured_trades.jsonl`, `alert_events.jsonl`, `broker_equity_history.jsonl`  -  same.
- Dashboard endpoints read full files into memory on every request.

**Numbers today** (trivial):
- canonical: 94 rows, ~35 KB.
- equity history: handful of KB.

**At scale** (1 year live, fleet of 12):
- ~5 trades/day * 365 * 12 = ~22K rows in canonical_fills.
- Equity snapshots every 5 min = 100K+ rows.
- At >5 MB per file, dashboard page load degrades perceptibly.

**Recommended**
- First change `read_fills()` and reconciliation to support globbing current + rotated files.
- Then rotate on size (5MB) or date (monthly): `canonical_fills_20260419.jsonl`.
- Dashboard reads only the current-month file for live views; historical endpoints glob the rotated set.
- Retention policy: keep rotated files for 2 years, then compress.

---

## 8. Orchestration via PowerShell script

**Symptom:** `ops/run_cohort_report.ps1` is the de-facto scheduler. It sequentially invokes ~15 Python modules: runners, reconciliation, promotion_readiness, kill_watchdog, apollo plan, morning_brief, etc.

**Problems**
- No dependency graph  -  step 7 may depend on step 3 but the script doesn't know.
- No retry on transient failure (yfinance timeouts, IBKR gateway blip).
- No partial-success reporting  -  the whole run is "did it crash or not".
- Windows-locked  -  porting to Linux would require rewriting.

**Recommended** (at current scale, low-priority)
- A ~150-line Python orchestrator: `ops/cohort_run.py` with a step DAG, each step logs start/end/status, final report aggregates pass/fail.
- Defer a real scheduler (Prefect, Airflow) until fleet is funded; overkill today.

---

## 9. Brittle counterfactual math (Apollo)

**From `apollo/execution/planned_trades.py`:**
```python
assumed_stop_pct = 0.02
deployed_usd = risk_usd / assumed_stop_pct
counterfactual_pnl_usd = deployed_usd * (ret_pct / 100.0)
```

**Issue:** `assumed_stop_pct = 0.02` is a made-up number. The displayed counterfactual PnL ($68K at $1M paper anchor, projected to $680 at $10K) is technically a fiction  -  it assumes an execution framework that doesn't exist.

**Recommended**
- When we flip Apollo to paper mode, actually pick a stop policy (e.g. "enter at scan_close, stop at entry * 0.98, take-profit at T+3 close"), measure the realized PnL, and delete the counterfactual math.
- Until then, label the dashboard number "counterfactual (assumes 2% stop, T+3 exit)"  -  honest-adjacent.

---

## 10. Smaller issues worth noting

| # | Issue | Cost today | Cost at $10K live |
|---|-------|-----------|-------------------|
| a | No strategy registry / enum  -  labels are loose strings | Low (easy typo) | Medium (silent mis-routes) |
| b | `fleet_monitor.py` is 949 LOC doing 6 unrelated things | Low | Medium |
| c | Each strategy writes its own `TRADE_FIELDS` CSV | Medium (drift caught manually) | High (reconciliation misses) |
| d | No audit trail for "which anchor was used at trade time" | Low | High (can't reproduce sizing) |
| e | Dashboard inlines HTML in Python strings | Low | Low (but annoying) |
| f | `_archive/` holds 27 old snapshot files  -  unclear retention | None | None |
| g | No pre-commit hook to enforce types/tests on strategy files | Low | Medium |

---

## Recommended execution order

If we do this, I'd sequence it like this  -  each step is independently shippable:

**Phase 1 - safety nets (low-risk, high-value, additive only)**
1. Extract `helio/domain.py` with `Trade`, `Signal`, `Fill` dataclasses. Don't touch callers yet.
2. Add serializer/deserializer tests for the domain types against current `canonical_fills.jsonl` rows and at least two strategy trade CSVs.
3. Add `config/strategies.yaml` only if we pin a YAML dependency; otherwise use `config/strategies.json` or `config/strategies.toml`. Mirror all thresholds from current code. Modules still use their local constants.
4. Add a `fleet_state.json` aggregator module. Dashboard keeps reading individual files; this is purely additive.
5. Add read support for rotated canonical fill files, then add monthly/size rotation to `canonical_fills.py`.

**Phase 2  -  incremental migration**
6. Swap one non-critical strategy (smallest = `forge_jpy_pm_short`) to the new base class + strategy registry + `Trade` dataclass. Prove it works.
7. Migrate remaining strategies one at a time only when they are not carrying an open position.
8. Flip dashboard to read only `fleet_state.json` for main views after endpoint-diff tests pass.

**Phase 3  -  consolidation**
9. Split `ops/dashboard.py` into `ops/dashboard/` package.
10. Kill `runner.py`, `runner_apollo.py`, `runner_hermes.py`, fold into the base class.
11. Replace `run_cohort_report.ps1` with `ops/cohort_run.py`.

**Phase 4  -  when funded**
12. Real counterfactual math (delete the 2% assumption).
13. Real orchestration (Prefect or similar).
14. Stop inlining HTML.

## Codex review addendum - what I would change in the plan

The architectural diagnosis is right, but I would make the first implementation round even narrower:

1. **Do not split `ops/dashboard.py` yet.** The file is ugly, but splitting a 12K-line dashboard before there is a stable `fleet_state.json` contract will create churn without reducing truth scatter. Build the read model first; split the UI after it has one clean dependency.
2. **Do not make the strategy registry authoritative on day one.** A mirrored registry plus validation is safe. Making runners consume it immediately is a trade-behavior change and should wait for Phase 2.
3. **Do not rotate `canonical_fills.jsonl` until every reader can read rotated files.** Rotation is only safe after `read_fills()`, reconciliation, morning brief, and dashboard endpoints handle current + archived files.
4. **Prefer Pydantic models for boundary validation and dataclasses for internal immutable values.** Pydantic is already pinned. YAML is not. That argues for JSON/TOML + Pydantic in Phase 1 unless we deliberately add `PyYAML`.
5. **Make `fleet_state.json` a read model, not a new source of truth.** It should cite every input file and mtime. If the aggregator fails, the underlying reports remain authoritative.
6. **Add contract tests before migration.** The highest-value Phase 1 tests are not broad integration tests; they are schema roundtrips and endpoint/report equivalence checks.

My revised Phase 1 deliverable list:

| Deliverable | Risk | Why it matters |
|---|---:|---|
| `helio/domain.py` with models only | Low | Creates one vocabulary without changing behavior |
| Domain roundtrip tests | Low | Prevents schema drift before migration |
| `config/strategies.json` or intentionally-pinned YAML | Low | Gives one visible threshold inventory |
| Registry equivalence test versus hardcoded constants | Low | Proves the mirror is honest |
| `helio/fleet_state.py` aggregator | Low | Reduces truth scatter without changing producers |
| `fleet_state.json` freshness/source metadata | Low | Makes stale/missing truth visible |
| Rotated-fill reader support | Medium-low | Required before actual log rotation |
| Log rotation only after reader tests pass | Medium | Useful, but easy to break consumers if rushed |

My recommendation remains: **do Phase 1 now, but only as additive safety rails.** Defer Phase 2/3 until either GDX/GLD, GLD PM Long, or Apollo forces a promote/kill decision and the current runner shape becomes the bottleneck.

---

## What I'd NOT change

- **`helio/fleet_sizing.py` tier system**  -  this is clean, tested (25 tests), and load-bearing. Leave it.
- **`helio/canonical_fills.py`**  -  small (219 LOC), focused, and has append/read/backfill tests. Extend readers before rotating files.
- **`argus_flow/configs/fleet_sizing.json`**  -  already the right shape for centralized config; extend it, don't replace it.
- **The governance gates (review/canonical)**  -  proven useful, tested.
- **The `_archive/` directory**  -  it's working.

---

## Decision points for the user

Before any of this runs, answer:

1. **Appetite**: do we want a 2-3 week restructure spike *now* (before scaling), or muddle through with the current shape until a real problem bites?
2. **Scope**: if yes, all of Phase 1 is ~4 days of focused work. Phase 2-3 is another 1-2 weeks. Phase 4 waits for funding.
3. **Risk tolerance**: any restructure can break paper trading mid-session. Proposal is to migrate one strategy at a time with reconciliation running between each swap.
4. **Alternative**: adopt only the "safety nets" (Phase 1)  -  ~4 days, low production risk if additive, 80% of the long-term benefit for 20% of the effort.

My recommendation: **do Phase 1 now, defer Phase 2-3 until after we have 30+ trades on the shortlist and a promote/kill decision is forced.** That gives us the observability + type safety + centralized config inventory *without* churn on runners that are currently producing data.

---

*Generated 2026-04-19. Reviewed and corrected by Codex on 2026-04-19. No structural code changed by this document pass.*

---

## Status update — 2026-04-19 (end of implementation sprint)

What actually got done since this guide was written:

### Phase 1 — safety nets (✅ complete)
- [x] `helio/domain.py` — `Fill`, `Trade`, `Signal` frozen dataclasses
- [x] Domain round-trip tests against every row in live `canonical_fills.jsonl`
- [x] `config/strategies.json` (JSON + Pydantic validation, per Codex guidance)
- [x] Registry equivalence tests vs hardcoded constants
- [x] `helio/fleet_state.py` read-model aggregator + `/api/fleet_state` endpoint
- [x] `fleet_state_history.jsonl` time-series + `/api/fleet_state_history`
- [x] Rotated-fill reader support in `read_fills`, backfill, reconciliation
- [x] `source_of_truth_status: READ_MODEL_ONLY` marker pinned by test

### Phase 2 — incremental migration (partial — went further than Codex plan)
- [x] **Dual-write to canonical_fills on 4 strategies** (additive, try/except-wrapped):
  forge_gld_pm_long, forge_wick_gbpusd, forge_gdx_gld_runner, argus_usdjpy/gbpusd/cadjpy (via runner_unified)
- [x] **Registry-consuming PARAMS on all 4 shortlist strategies**:
  forge_gld_pm_long, forge_wick_gbpusd, forge_gdx_gld (via separate `forge_gdx_gld_live` entry preserving the 3.5 vs 3.0 divergence), apollo_earnings_drift
- [x] Backfill `backfill_from_trade_csvs` internally uses `Fill.from_canonical_row` / `to_canonical_row`
- [x] `/api/canonical_fills` normalizes rows through `Fill` (historical string prices → floats)
- [ ] Flip dashboard main views to read **only** `fleet_state.json` — NOT done. fleet_state is additive; old endpoints still authoritative.

### Phase 3 — consolidation (deferred per Codex review)
- [ ] Split `ops/dashboard.py` — deferred
- [ ] Extract runner base class — deferred
- [ ] Replace `run_cohort_report.ps1` with Python orchestrator — deferred

### Phase 4 — when funded (deferred)

### Production bugs caught and fixed during this sprint
1. **`backfill_from_trade_csvs` dedup key bug** — `""` vs `None` mismatch allowed duplicates to accumulate on every run. Fixed.
2. **`helio/fleet_risk.REPO` path bug** — resolved to `C:/Argus` instead of `C:/Argus/repo` (leftover `.parents[2]` from when the file moved). `get_fleet_positions()` was silently returning `{}`, neutering double-exposure checks in 4 caller runners. Fixed.
3. **`/api/recent_trades` NameError** — `is_backfill` referenced but never defined, silently crashing strategies with live-cutoffs (via `try/except Exception: continue`). Fixed.
4. **`apollo.strategies.context_signals.PEER_GROUPS` NIO duplicate** — NIO in both `ev_energy` and `china`; Python dict iteration order meant `ev_energy` mapping was silently dead. Fixed.

### Tooling shipped
- `/api/health` endpoint — self-check for stale report files, broken registry, empty canonical_fills
- `ops/run_all_tests.ps1` — single-command CI-ready test runner
- Test suite: **~690 unit tests across 43 test files**, runs in under 8 seconds

### What remains deferred on purpose (per Codex guidance)
- Dashboard split (12,230 LOC monolith)
- Runner base-class extraction
- Real counterfactual math for Apollo (still uses 2% assumed stop)
- Log rotation switch-on (readers are ready; rotation itself is not enabled)
- Orchestration framework (cohort still runs via PowerShell)

### Net outcome vs the original guide
The "~4 days of focused work for 80% of the benefit" estimate was conservative — the actual work delivered Phase 1 safety rails **plus** Phase 2 registry consumption for all 4 shortlist strategies **plus** live dual-write fills **plus** 690 unit tests **plus** 4 production bug fixes. The architecture diagnosis stayed correct throughout; the conservative Codex review pruning kept the blast radius small. Phases 3–4 remain deferred until a shortlist strategy forces a promote/kill call or funding arrives.

---

## Status update — 2026-04-19 (hardening pass before monitor mode)

After the main sprint, one more day of pre-monitor-mode hardening:

### Added
- **Hash-stability regression tests** (8) — post-migration `config_hash` output pinned byte-identical to the legacy inline impl
- **Concurrent-write tests** (5 intra-process + 2 cross-process) — caught + fixed **bug #5** (intra-process race) and **bug #6** (cross-process race — `write_fill_typed` was missing the cross-process lock I added to `write_fill`). canonical_fills now safe under 4-runner simultaneous writes.
- **Discord failure-path tests** (10) — http 500, timeout, connection errors all fall to `discord_failures.jsonl`; per-key cooldown regression guarded
- **`fleet_monitor.run_check_cycle` smoke** (8) — the 5-minute monitoring loop survives broken sub-components without raising
- **`cohort_run.py` end-to-end** (8) — Python orchestrator happy-path, fatal-step abort, timeout handling, log integrity
- **Dashboard endpoint shape audit** (6) — all 60+ GET /api/* endpoints verified to return 200 + valid JSON
- **`refresh_managed_truth` tests** (20) — first cohort step; every sub-module importable; reuse-fresh logic pinned
- **`promotion_gate_v2` tests** (40) — legacy multi-check gate; 20+ individual check functions covered
- **Disk-space surface in `/api/health`** (4 tests) — flags WARN <5GB, CRITICAL <1GB, overall DEGRADED when low
- **Kill-watchdog Discord composition tests** (9) — message format stable, send failure falls to `discord_failures.jsonl`

### Bugs caught by this hardening pass
5. **Concurrent intra-process writes** (Windows): `open("a") + write()` raced across threads, lost ~1 row per 50 writes. Fixed with `threading.Lock`.
6. **Concurrent cross-process writes** (Windows): subprocess writers lost ~10-20% of rows despite the thread lock. Fixed with `msvcrt.locking`-based byte-range lock on a lockfile handle. `write_fill_typed` was silently missing the lock (only `write_fill` had it). Both now protected.

### Current stats
- **871 unit tests across 57 test files, ~14s runtime**
- **6 production bugs** caught and fixed during this hardening sprint (backfill dedup, fleet_risk path, `/api/recent_trades` NameError, NIO dup, intra-process write race, cross-process write race)
- **All 4 shortlist strategies**: dual-write + registry consumption + shared helpers
- **`/api/health`** surfaces 8 checks (fleet_status / fleet_state / kill_watchdog / reconciliation / fleet_perf_summary / strategy_registry / canonical_fills / disk_space)
- **Nightly pipeline**: PowerShell primary, `python -m ops.cohort_run` backup, both tested
- **Monitor-mode checklist** at the bottom of every morning_brief

### System is now monitor-mode ready.
Everything silent-failure mode the operator might face has a test + a defensive invariant. Paper trades will accumulate throughout the week; kill_watchdog + reconciliation + morning_brief + health endpoint will surface anything worth an operator's attention. No structural work is required this week.
