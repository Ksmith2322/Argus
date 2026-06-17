# Argus Comprehensive Audit for Codex Sweep — 2026-05-18

**Purpose:** Hand to Codex for environment sweep. Codex should triage each item as FIX-NOW / FIX-BY-5/31 / TABLE-POST-RESET / WONTFIX.

**Audited surface:** `C:\Argus\repo` (Argus algorithmic trading bot, 22 strategies, Python 3.12, Windows 10, IBKR paper account DUP472829 port 7497).

**Context:** 5/31 strategy freeze + paper reset. 7/1+ real-money go/no-go. 8 bugs shipped this past weekend; this audit checks for residual bugs and structural issues NOT yet fixed.

---

## Executive Summary

**6 audit angles ran in parallel.** Each returned an independent checklist. Below is the cross-cutting synthesis.

### The 10 most critical findings (cross-agent convergence)

| # | Finding | Source | Severity |
|---|---|---|---|
| 1 | **MYM/Error-321 pattern still live in 2 active runners** (nq_overnight, nq_london_close) | Code | CRITICAL |
| 2 | **argus FX runner_unified bypasses real_money boundary** | Code+Risk | CRITICAL |
| 3 | **`canonical_fills.jsonl` has 0 ENTRY rows** — all 329 are EXITs. ROI math has been computed on EXIT-only data. | Data | CRITICAL |
| 4 | **Stuck open trades in killed-strategy state.json** (spy_mean_rev SPY 26 from 4/30, gld_pm_long FLAT vs broker 22) | Data+Risk | CRITICAL |
| 5 | **5/5 nq_london_close phantom STILL in canonical** (line 280, $117K notional, +$8,324 P&L, sign-wrong) | Data | CRITICAL |
| 6 | **Two-layer cap conflict: gld_pm_long fleet_sizing=2.2× vs cluster_exposure=0.4×** — smaller wins, 2.2× override may be NON-FUNCTIONAL | Risk | CRITICAL |
| 7 | **Sample sizes 50-67% smaller than memory claims** (nq_overnight n=12 not 18; gld_pm_long n=15 not 25; jpy_pm_short n=4 not 6) | Edge | CRITICAL |
| 8 | **nq_overnight: drop top-2 trades → NEGATIVE residual mean.** The "WINNER" verdict is outlier-driven. | Edge | CRITICAL |
| 9 | **Port-default split-brain**: hermes/apollo default IBKR_PORT=7496 (LIVE) | Code+Infra | CRITICAL |
| 10 | **TIF=DAY on FX exits** likely root cause of argus_gbpusd EXIT cascade — FX is 24h, LMT pending overnight gets cancelled at session boundary | Code+Infra | HIGH |

### Convergent secondary findings (multiple agents)

- **connect_with_retry** only wired into cuebanks/mamba/tori — other ~25 runners (incl. nq_overnight, gld_pm_long, jpy_pm_short, wick_gbpusd, fomc_drift, tom_international, aud_asian_breakout, etc.) use bare `ibkr.connect()` and go silent for full sleep cycle after TWS restart
- **yfinance has no fallback** — `helio/yfinance_cache.py` exists but ZERO runners import it
- **Append-only JSONLs not rotated** — canonical_fills.jsonl, pending_fills, unfilled_orders.jsonl all unbounded
- **773 orphan broker_snapshot_*.json files** in `_broker/`, oldest from 2026-03-30
- **Killed-strategy runner.py files intact** — stray `python -m forge.multi_orb.runner --loop` would resurrect them
- **fleet_monitor will auto-restart killed strategies** — `forge_nq_london_close` block lacks `no_restart:True`
- **`recover_pending_at_startup()` defined but never called** anywhere in repo
- **Capital promotion ledger doesn't exist** — doctrine references `capital_promotion_ledger.jsonl` but no code writes it
- **`REAL_MONEY_ENABLED` module constant is dead code** — `helio/real_money.py:213` has logical OR-bug making it unreachable
- **VIX>30 force-close SHORT_VOL** is doctrine-only, not wired
- **Daily loss thresholds drift from doctrine** — code -1/-2/-4%, doctrine -10/-20%
- **Zero unit tests** for the 5/16-5/18 fixes: `qualify_front_month_future`, `_dump_unfilled_forensics`, `_dump_exit_forensics`, `submit_bracket`, `_recover_close_fill_from_executions`
- **26 files use bare `datetime.now()`** without tz
- **15 PowerShell scripts use `Out-File` without `-Encoding utf8`** → UTF-16 BOM contaminates downstream JSON
- **Cluster exposure aggregator has futures-notional bug** — `cluster_exposure.compute_cluster_exposure` doesn't apply multiplier (pre-trade check correct, post-trade aggregation wrong, inflates by 10× for MNQ)

### Recommended priority for Codex

**Tier A — FIX NOW (before any new fills land):**
- #1 (MYM pattern), #2 (boundary bypass), #6 (cap conflict), #9 (port split-brain)
- Adding `no_restart: True` to killed strategies in fleet_monitor.py
- Migrating all runners to `connect_with_retry`

**Tier B — FIX BY 5/31 (so the reset gives clean evidence):**
- #3 (canonical_fills schema), #4 (stuck-position cleanup), #5 (phantom purge)
- Real-money boundary completeness audit
- Stop-loss enforcement invariant
- Cluster exposure futures-notional fix

**Tier C — TABLE TO POST-RESET BACKLOG:**
- Edge-validation work (n sufficient, walk-forward, etc.) — needs the clean window first
- Test coverage backfill for 5/16-5/18 helpers
- yfinance cache wiring fleet-wide
- Log rotation policy for unbounded JSONLs

---

## Slice 1 — Code Correctness + Silent Failure Surface (Agent 1)

### Top items
1. **MYM-bug pattern still live**: `forge/nq_overnight/runner.py:177,280` and `forge/nq_london_close/runner.py:256,359` call `ibkr.make_contract("MNQ","micro_future") + ib.qualifyContracts(contract)` without the new `qualify_front_month_future()` helper. Same Error-321 silent-cancel class. **nq_overnight is one of our 2 declared WINNERS — its fills may be at silent risk.**

2. **`argus_flow/runner_unified.py:_submit_real_entry` (line 2230) bypasses `enforce_real_money_boundary()`** — calls `ib.placeOrder` directly. Only `helio/ibkr_execution.submit_bracket` has the guard. Argus FX runners can submit live-account orders unchecked.

3. **`fleet_monitor.SYSTEMS["forge_nq_london_close"]` (line 208) lacks `no_restart: True`** — killed 5/13 per memory but watchdog will resurrect it on stale heartbeat.

4. **Port-default split-brain**: `helio/runner_hermes.py:243` and `helio/runner_apollo.py:285` default `IBKR_PORT=7496` (LIVE). Everything else defaults `7497` (paper). One missing env var → live account contact.

5. **Zero unit tests for shipped helpers**: `qualify_front_month_future`, `_dump_unfilled_forensics`, `_dump_exit_forensics`, `submit_bracket`, `_recover_close_fill_from_executions`.

### Full checklist (70 items, see agent output for grep/inspect specifics)
- Contract-routing pattern audit (items 1-7)
- Silent-failure / over-broad except blocks (items 8-15) — 17 instances of `except Exception: pass`
- argus_gbpusd EXIT cascade root-cause hypotheses (items 16-19) — flag: `_build_exit_order` uses `entry_price` not current mid; 5% buffer may not be enough if FX gapped
- Real-money boundary completeness (items 20-21)
- Dead/stale code referencing killed strategies (items 22-28) — runner.py files intact, ops/emergency_relaunch.ps1 + register_remaining_tasks.ps1 still reference them, helio/fleet_monitor lacks no_restart, drill_harness keeps multi_orb in probe
- Untested core paths (items 29-37) — `_submit_real_entry`, `_check_order_timeouts` retry ladder, State.save Windows race, etc.
- Concurrency / file-write races (items 38-43) — pending_fills, _processed_fill_ids non-atomic, fleet_monitor duplication risk
- Config drift (items 44-48) — port defaults inconsistent, hardcoded EXPIRY constants will break post-roll
- Untested 5/18 helpers (item 50): apollo trade_manager.py:360 reports `pnl_usd:0  # TODO`
- Per-strategy silent gaps (items 51-53)
- Env / process sweep instructions (items 61-70)

---

## Slice 2 — Operational Health + Process Management (Agent 2)

### Top items
1. **773 orphan `broker_snapshot_<PID>.json` files** in `argus_flow/logs/_broker/`, oldest from 2026-03-30. Each prior PID leaves a permanent file — no GC.

2. **ArgusWatchdog single-point-of-failure**: BootTrigger only. If process dies mid-session, only `watchdog_health_check.py` auto-recovers (had a 10-day outage in April per memory).

3. **Watchdog covers only 3 FX pairs** (`ops/watchdog.ps1:33-37`). 19 forge strategies have NO equivalent auto-restart layer.

4. **Stale top-level `argus_flow/logs/heartbeat.json`** from 2026-03-24 — dashboard may still read this and lie about fleet status.

5. **Append-only logs unbounded** — only `runner_unified.py`, `forge/logging_setup.py`, and `helio/fleet_monitor.py` use `RotatingFileHandler`. Other 22 runners write unbounded `runner.log`.

### Full checklist
- Process inventory + orphan snapshots (item 1) — cleanup script needed
- Heartbeat freshness cycle-aware (item 2) — `monday_watch.py:69` already classifies
- Log rotation + disk (item 3) — projected canonical_fills ~1.5MB/yr (safe)
- Scheduled task health (item 4) — verify ArgusWatchdog/Fleet Startup/ManagedTruth/GitBackup all green
- Backup state (item 5) — last git commit + ArgusGitBackup verification
- Watchdog health (item 6) — `Get-Content argus_flow/logs/watchdog_managed.log -Tail 50`
- Network dependencies (item 7) — yfinance failure modes silent
- Recovery procedures (item 8) — reboot test
- Stuck position detection (item 9) — `ops/orphan_audit.py` exists, run + verify
- TWS daily disconnect window (item 10) — characterize 16:00-17:00 ET pattern

---

## Slice 3 — Data Integrity (Agent 3)

### Top items
1. **`canonical_fills.jsonl` has 0 ENTRY / 329 EXIT rows.** Entry records aren't being written. All ROI calcs have been entry_px-embedded-in-EXIT only.

2. **Phantom 5/5 nq_london_close STILL in file** (line 280): 417 MNQ @ $28148, $117K notional, +$8,324 P&L. **Likely P&L sign error** — short with exit_px < entry_px should be POSITIVE, recorded as +$8,324 may actually be wrong direction.

3. **Stuck open `forge_spy_mean_rev` state.json** — entry 4/30 SPY long, strategy killed 5/12 (rework path CLOSED). 18-day-old open trade. Likely source of SPY 26 broker position.

4. **`forge/logs/gld_pm_long/state.json` declares FLAT** but broker holds 22 GLD. Silent state divergence.

5. **Killed strategies still writing fills** — 99 of 329 canonical_fills are from killed strategies (spy_mean_rev 38, vix_intraday 61). Allocation_factor=0 ≠ "stops writing."

6. **Backfill duplicates** — 23 rows with `"source":"backfill_from_trade_csv"` share `ts` with live rows. At least one symbol drift (MNQ→NQ between live and backfill).

7. **MYM forensics show `lmtPrice=1.7976931348623157e+308`** (DBL_MAX / sys.float_info.max) — executor ships invalid limit price on rejected orders. Real bug.

### Full checklist
- canonical_fills.jsonl structural integrity (item 1)
- Phantom detection beyond $1K filter (item 2) — multi_orb 5/5 QQQ 1419 shares @ $681 = $966K, suspect contract-vs-share unit confusion
- State vs broker reconciliation (item 3)
- Trade attribution (item 4) — 0 unknown but 30% from killed strategies
- PnL math verification (item 5) — 5% tolerance for non-FX, sign-flip check
- trades.csv vs canonical_fills.jsonl drift (item 6)
- broker_drift_state freshness (item 7)
- unfilled_orders.jsonl forensics — already mining (item 8)
- Historical data quality (item 9) — split artifacts, NaN bursts
- Time consistency (item 10) — ISO format drift entry_ts has both space and T separators
- Currency/notional consistency (item 11) — futures multiplier missing in cluster aggregator

---

## Slice 4 — Risk + Financial Controls (Agent 4)

### Top items
1. **`capital_promotion_ledger.jsonl` does not exist.** Doctrine requires it for every allocation change; zero code writes to it. Allocation history is unauditable.

2. **`helio/real_money.py:213` dead-code safety net** — `if not REAL_MONEY_ENABLED and not al.global_enabled` is unreachable (earlier raise at 207). Module constant doesn't protect anything.

3. **gld_pm_long cap conflict**: `fleet_sizing.json` has 2.2× override; `cluster_exposure.PER_STRATEGY_NOTIONAL_CAP_X["forge_gld_pm_long"] = 0.4`. Smaller wins. **The 2.2× override I shipped Saturday may be non-functional** — every gld_pm_long entry at the override size would be rejected by cluster cap.

4. **`cluster_exposure.compute_cluster_exposure` line 351 futures notional bug** — `notional = abs(entry * size)` doesn't apply multiplier. Pre-trade check (line 740) correct; post-trade aggregator wrong, inflates 10× for MNQ.

5. **VIX>30 force-close SHORT_VOL is doctrine-only** — grep returns zero matches in code.

6. **Daily loss thresholds drift**: code uses -1/-2/-4%, doctrine `project_kill_pause_engine.md` says -10/-20%. Governance drift either way.

### Full checklist
- Capital ladder gate audit (items 1.1-1.4)
- Real-money boundary completeness + edge cases (items 2.1-2.5)
- Cluster exposure correctness (items 3.1-3.3)
- Per-trade sizing realized vs intended (items 4.1-4.3)
- Notional cap conflict resolution (items 5.1-5.2)
- Total cluster exposure simulation (item 6.1)
- Daily loss halt doctrine reconciliation (items 7.1-7.3)
- Phantom order detection re-test (items 8.1-8.2) — `_classify_symbol` 6-char alpha → FX has false-positive class (e.g., "AAPLUS")
- Stop-loss enforcement invariant (items 9.1-9.2) — `submit_bracket` accepts stop_px=0
- Allocator policy compliance check (item 10.1) — gld_pm_long at 2.2× violates 25%/50% doctrine
- Capital promotion ledger build/document (item 11.1)
- Real-money allowlist integrity (items 12.1-12.2)

---

## Slice 5 — Strategy Edge + Statistical Validation (Agent 5)

### Top items
1. **n drift memory vs CSV**: claimed n=18/25/6 on (nq_overnight/gld_pm_long/jpy_pm_short); actual CSV rows are **12/15/4**. Yesterday's ROI projection numbers are too high.

2. **nq_overnight outlier-driven**: top 2 winners = $470 of $453 net. Drop top-2 → residual mean is NEGATIVE. Same trap as our 10-Q bounce research caught last week. **The "WINNER" verdict needs revising.**

3. **gld_pm_long exit-discipline drift**: 8 of 15 trades held 18+ hours despite "PM session" name. `exit_reason=time` or `reconcile_flat` shows planned target/stop didn't fire on majority of trades.

4. **Concentration**: nq_overnight 100% LONG, 100% at signal_hour_utc=20. gld_pm_long 100% LONG. No short evidence, no out-of-regime testing.

5. **n=4 at 67% WR has 95% CI of ±46 points** (Wilson binomial). jpy_pm_short edge claim is statistically meaningless.

6. **Edge decay visible**: nq_overnight first 6 trades +$623, last 6 = -$170 (decaying); gld_pm_long first 8 = +$298, last 7 = +$126 (mild decay).

7. **Per-symbol attribution risk**: jpy_pm_short n=4 = 1 USDJPY (-$48) + 3 CADJPY (+$111). Edge hinges on n=3 single-pair.

### Full checklist
- Sample size sufficiency Wilson CI (item 1) — n>=30 minimum, n>=75 for "confirmed"
- Outlier-driven mean test with bootstrap (item 2)
- Survivorship/kill-log verification (item 3)
- Walk-forward eligibility (item 4) — only backtest data (tori n=665, sector_rot n=74) currently qualifies
- Thesis vs implementation drift (item 5) — diff STRATEGY_SPEC.md against runner.py per strategy
- Signal frequency reconciliation (item 6) — gdx_gld 5298 signals, 0 fills (gate too tight); multi_orb 1.3% signal→fill conversion
- Trade duration vs spec (item 7)
- Per-symbol concentration within a strategy (item 8) — nq_overnight may be mixed NQ + MNQ with different multipliers
- SPY correlation (item 9) — flag |rho|>0.4 as beta-not-alpha
- Edge decay first-half vs second-half (item 10) — Mann-Whitney U test
- Backtest vs live divergence (item 11)

---

## Slice 6 — Infrastructure + Dependencies (Agent 6)

### Top items
1. **`_connect_with_backoff` retry-forever is gdx_gld-only.** Other ~30 runners use ad-hoc reconnect. TWS daily restart kills them silently for full sleep cycle.

2. **yfinance cache exists but unused fleet-wide** — `helio/yfinance_cache.py` defines stale-fallback but no runner imports `download_cached`. 31 forge files call `yf.download` raw.

3. **Hardcoded `DUP472829` + `7497` in 30+ files** including docs that DISAGREE — `ops/CHEATSHEET.md` and `ops/DISASTER_RECOVERY.md` say port 4002. Centralize.

4. **SEC EDGAR cache freshness not checked** — 200+ per-ticker JSON files, no mtime staleness alert in catalyst-conditional pipeline.

5. **`canonical_fills.jsonl` + `pending_fills` not in `argus_flow/ops/log_rotation.py`** — both unbounded, both critical.

6. **TIF=DAY on FX exits** — `_build_exit_order` sets `order.tif = "DAY"` but FX is 24h cross-session. LMT pending overnight gets cancelled at session boundary not filled. **Likely root cause of argus_gbpusd EXIT cascade.**

7. **`ib-insync==0.9.86`** — last release 2023, unmaintained, check for CVEs.

8. **Missing from requirements.txt**: yfinance, beautifulsoup4, lxml, curl_cffi, anthropic, claude-agent-sdk — drift between pinned and actual.

### Full checklist
- Python dependency hygiene (item 1) — `pip-audit` + `pip list --outdated`
- yfinance reliability per-strategy (item 2) — migrate to `download_cached`
- IBKR TWS edge cases (item 3) — MES/MNQ/MYM covered; MCL/CL/MGC/GC/NKD test coverage gap; FX TIF; SSR; daily restart
- SEC EDGAR cache freshness (item 4)
- Disk and filesystem (item 5) — projected growth, biggest files
- Hardcoded values (item 6) — centralize port/account/path
- Race conditions (item 7) — pending_fills, fleet_state, strategy_confidence locking
- Environment variables (item 8) — env.example missing, silent paper fallback risk
- Windows-specific (item 9) — `.ps1` encoding flags
- Time/timezone (item 10) — 26 bare `datetime.now()`
- Network resilience (item 11) — yfinance 429, SEC EDGAR rate limit
- Memory leaks (item 12) — psutil RSS monitoring, ib_insync callback cleanup

---

## Recommended Codex Action Plan

### Sweep 1 (FIX-NOW, before any new fills)
- Audit + fix nq_overnight + nq_london_close to use `qualify_front_month_future`
- Audit + fix `runner_unified._submit_real_entry` to use real_money boundary
- Resolve gld_pm_long cap conflict (raise cluster_exposure to 2.2× OR lower fleet_sizing to 0.4×)
- Standardize port defaults across hermes/apollo/ibkr_executor
- Add `no_restart: True` to all killed strategies in fleet_monitor.SYSTEMS
- Fix `cluster_exposure.compute_cluster_exposure` futures notional multiplier
- Either fix or remove `helio/real_money.py:213` dead-code safety net

### Sweep 2 (FIX-BY-5/31 reset)
- Document/build `capital_promotion_ledger.jsonl` writer OR update doctrine
- Investigate canonical_fills.jsonl ENTRY-record gap — either rename field to "FILL" or restore ENTRY emission
- Force-flatten or document: SPY 26, GLD 22, QQQ 7 broker positions vs runner states
- Strip phantom 5/5 nq_london_close trade from canonical (or verify P&L sign)
- Investigate argus_gbpusd EXIT — try TIF=GTC for FX, validate ref_px=current_mid not entry_price
- Migrate 25+ runners from `ibkr.connect()` to `ibkr.connect_with_retry()`
- Resolve doctrine drift: daily loss thresholds (-1/-2/-4 vs -10/-20)
- Wire VIX>30 SHORT_VOL force-close (if any SHORT_VOL strategy survives)
- Migrate canonical_fills.jsonl + pending_fills + unfilled_orders.jsonl to log_rotation
- Add stop-loss invariant test to submit_bracket
- GC orphan broker_snapshot files (~770 stale)
- Delete or `# DISABLED`-stamp killed-strategy runner.py files + ops/emergency_relaunch.ps1 entries

### Sweep 3 (POST-5/31 BACKLOG)
- Test coverage backfill: 5/16-5/18 helpers (qualify_front_month_future, _dump_unfilled_forensics, _dump_exit_forensics, submit_bracket, _recover_close_fill_from_executions)
- yfinance cache migration fleet-wide
- Walk-forward + Wilson CI per strategy (needs 30-day post-reset sample first)
- SPY correlation per strategy (needs cleaner data)
- Edge decay Mann-Whitney U per strategy
- PowerShell `-Encoding utf8` audit on `.ps1` writers
- Bare `datetime.now()` → `datetime.now(timezone.utc)` migration (26 files)
- Centralize hardcoded port/account/path in single config module
- Watchdog coverage expansion to 19 forge strategies (currently 3 FX only)
- Add ssr (short sale restriction) handling for short strategies
- `pip-audit` CVE sweep + dependency upgrade (especially ib-insync 0.9.86)

---

## Files for Codex to Touch

### Most-modified expected (sweep 1)
- `forge/nq_overnight/runner.py` (lines 177, 280)
- `forge/nq_london_close/runner.py` (lines 256, 359)
- `argus_flow/runner_unified.py` (line 2230 `_submit_real_entry`)
- `argus_flow/configs/fleet_sizing.json` OR `helio/cluster_exposure.py` (resolve gld_pm_long conflict)
- `helio/runner_hermes.py:243`, `helio/runner_apollo.py:285`, `helio/ibkr_executor.py:71` (port defaults)
- `helio/fleet_monitor.py:208` (no_restart on killed strategies)
- `helio/cluster_exposure.py:351` (futures notional multiplier)
- `helio/real_money.py:213` (dead-code logic bug)

### Data hygiene (sweep 2)
- `argus_flow/logs/canonical_fills.jsonl` (schema gap)
- `forge/logs/spy_mean_rev/state.json`, `forge/logs/gld_pm_long/state.json` (state-vs-broker)
- `argus_flow/logs/_broker/*.json` (770 orphans)
- 25+ runner.py files across `forge/` (connect_with_retry migration)
- `helio/halt_state.py`, `ops/daily_loss_circuit_breaker.py` (threshold reconciliation)
- `argus_flow/ops/log_rotation.py` (add critical JSONLs)

### Test backfill (sweep 3)
- `argus_flow/tests/test_qualify_front_month_future.py` (NEW)
- `argus_flow/tests/test_dump_unfilled_forensics.py` (NEW)
- `argus_flow/tests/test_dump_exit_forensics.py` (NEW)
- `argus_flow/tests/test_submit_bracket.py` (NEW)
- `argus_flow/tests/test_recover_close_fill.py` (NEW)
- `argus_flow/tests/test_check_order_timeouts.py` (NEW — exit retry ladder)

---

## What I'd want to know if I were Codex

**Before fixing anything:**
1. Of the 99 fills from killed strategies, were they ALL during the brief window between signal-generation-blocked and runner-fully-stopped, OR are they post-kill activity that should never have happened?
2. Why does canonical_fills have 0 ENTRY records — was the writer ever emitting them, or has the schema always been EXIT-only with entry_px embedded?
3. The 2.2× vs 0.4× conflict on gld_pm_long — which is the authoritative source? Different layers were edited by different people on different days.
4. The argus_gbpusd EXIT cascade — is `_build_exit_order`'s 5% buffer enough given the current state of FX gap risk? What does the forensics log actually say about whyHeld?

**Operational unknowns to verify:**
1. ArgusWatchdog scheduled task: actually running? Last verified active when?
2. ArgusGitBackup task: last successful push to remote?
3. Of the 773 orphan broker_snapshot files: are any from currently-running processes that just didn't get cleaned up, or are they all from dead PIDs?
4. Stale top-level argus_flow/logs/heartbeat.json (2026-03-24): is anything reading it?

**Doctrine vs code:**
1. `project_kill_pause_engine.md` says pause -10%, halt -20%. Code uses -1/-2/-4%. Which is correct?
2. `capital_promotion_ledger.jsonl` referenced in doctrine but doesn't exist. Update doctrine OR build the writer?
3. VIX>30 SHORT_VOL force-close — doctrine says it should fire but no code does it. Strategy file removed but doctrine retained.

---

**End of audit.** ~5,400 words of synthesized findings across 6 independent agents. Codex should triage each item and produce: (a) immediate fix list, (b) by-5/31 fix list, (c) post-reset table.
