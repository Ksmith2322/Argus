# Codex Full Audit - 2026-05-20

Scope: second full environment sweep after recent updates, focused on hidden blockers, extra gates suppressing trades/returns, safety bypasses, evidence quality, and operational drift.

Repo state at audit:
- Branch: `audit/argus-system-review`
- Modified during audit: `argus_flow/runner_unified.py`
- Pre-existing untracked file: `_tmp_state.py`
- Test friction: `.pytest_tmp/` and `argus_flow/logs/_tmp_pair_onboarding/...` have access-denied behavior during recursive sweeps.

## Executive Verdict

Argus is safer than the prior sweep in several ways: the sunset file is explicit, entry dual-write code exists, static safety tests exist, and the post-reset launcher documents a smaller roster. But the bot is not yet operating at full potential because several control planes disagree:

1. Allocation says Argus FX is killed, but the FX runner was still submitting real orders.
2. Broker truth shows positions that local strategy state cannot fully use or explain.
3. The canonical evidence ledger still has zero `ENTRY` rows, so performance math is not yet promotion-grade.
4. The intended post-reset roster is not enforced everywhere; the old launcher can still start sunset runners.
5. Some profitable survivors are blocked by stale broker/state divergence rather than edge failure.

The biggest theme is not "no edge." The biggest theme is "truth and gating are split across too many layers." Fixing that should increase both safety and trade capture.

## Fix Applied In This Sweep

### FX allocation factor now gates real sizing

File: `argus_flow/runner_unified.py`

The FX runner used `get_effective_risk_pct()` but did not multiply by `get_allocation_factor()`. Because of that, these allocation settings did not actually suppress Argus FX entries:

- `argus_flow/configs/allocation_factors.json:27` - `argus_gbpusd: 0.0`
- `argus_flow/configs/allocation_factors.json:28` - `argus_usdjpy: 0.0`
- `argus_flow/configs/allocation_factors.json:29` - `argus_cadjpy: 0.0`

Patch:
- `argus_flow/runner_unified.py:1524` imports `get_allocation_factor`
- `argus_flow/runner_unified.py:1526` multiplies tier risk by allocation factor
- `argus_flow/runner_unified.py:1534` no longer uses `or self.risk_pct`, which revived zero risk
- `argus_flow/runner_unified.py:1535-1541` returns size 0 with policy `allocation_factor_zero`
- Startup sizing validation now allows `allocation_factor_zero` instruments to remain online for reconciliation/exit management while still blocking new entries.

Verification:
- AST parse passed with `python -c "import ast..."`
- `py_compile` could not complete because Python tried to update an existing `__pycache__` file and Windows returned access denied.

Operator requirement:
- Restart `argus_flow.runner_unified` before relying on this patch. Running processes will still have the old logic.

## Critical Findings

### 1. Argus FX was still live despite allocation factor 0.0

Severity: Critical

Evidence:
- Allocation file kills all three FX pairs at `argus_flow/configs/allocation_factors.json:27-29`.
- Live logs show real order submissions after that sunset decision:
  - `argus_flow/logs/runner_unified.log:10856` - USDJPY real entry submitted.
  - `argus_flow/logs/runner_unified.log:11015` - CADJPY real entry submitted.
- Broker snapshot confirms live positions:
  - `argus_flow/logs/_broker/broker_snapshot.json` currently has `USD.JPY` long 30,268 and `CAD.JPY` long 41,572.
- Risk report is yellow due correlated JPY exposure:
  - `argus_flow/logs/risk_oversight_report.json` message: `JPY: 2 correlated positions... exceeds limit of 1`.

Impact:
- Sunset decisions were not enforceable for Argus FX.
- The system could continue taking positions in killed strategies.
- The JPY correlation gate detected the result after the fact, not before entry.

Status:
- Code path patched in this audit.
- Existing broker positions still need operator handling or a controlled strategy-owned unwind.

### 2. Broker truth and strategy state are suppressing the strongest survivor

Severity: Critical

Evidence:
- `forge/logs/gld_pm_long/state.json` says `open_trade: null`.
- Broker snapshot shows `GLD` long 22 shares.
- `forge/logs/gld_pm_long/runner.log` repeatedly reports `BROKER_HAS_POSITION: GLD qty=22.0, skipping entry to avoid doubling`.

Impact:
- `gld_pm_long` is one of the intended survivors, but it cannot enter new trades while this stale broker position exists.
- Because local state has no `open_trade`, the runner may also not manage the position as a normal strategy-owned trade.
- This looks like an operational blocker masquerading as lack of opportunity.

Required fix:
- Decide whether the GLD position is an active strategy position or an orphan.
- Either adopt it into `state.json` with the correct stop/target/lineage, or close it through the existing orphan close tooling.
- Add a daily preflight that fails if `broker_has_position && state.open_trade is null` for an active strategy.

### 3. Canonical fills still have zero ENTRY rows

Severity: Critical

Evidence:
- `argus_flow/logs/canonical_fills.jsonl` grouped by `side` shows 336 `EXIT` rows and 0 `ENTRY` rows.
- ENTRY write code now exists in:
  - `helio/ibkr_execution.py`
  - `argus_flow/runner_unified.py`

Impact:
- ROI, expectancy, slippage, hold-time, fill-quality, and promotion gates remain exit-only.
- The system cannot prove intended-vs-filled price, entry timing, or real risk at entry.
- The reset window is not optional; this is the clean-data line.

Most likely causes:
- Running processes may not have been restarted after entry-write patches.
- Some fills are entering through timeout/orphan adoption paths rather than the normal fill handler.
- Backfilled/reconciled exits are mixed with live rows.

Required fix:
- Before 5/31 reset, run one controlled paper/live micro fill through each active execution path and assert one ENTRY plus one EXIT.
- Add a hard dashboard warning: `canonical_fills ENTRY count == 0 over last N exits`.
- Promotion gate should fail closed when entry coverage is below 95 percent.

### 4. Argus FX timeout/adoption path creates managed positions after "cancel"

Severity: Critical

Evidence:
- `argus_flow/logs/runner_unified.log:10856` submits USDJPY.
- `argus_flow/logs/runner_unified.log:10870` times out and cancels after 60s.
- `argus_flow/logs/runner_unified.log:10875` adopts the broker fill as an orphan.
- Same pattern for CADJPY at `argus_flow/logs/runner_unified.log:11015`, `11031`, `11036`.

Impact:
- A filled order can be treated as timeout/cancel first, then adopted with synthetic stop/target.
- ENTRY dual-write is likely bypassed or delayed on this path.
- Synthetic management is useful as a rescue mechanism, but it should be treated as an exception state, not normal execution.

Required fix:
- Treat `ENTRY_TIMEOUT -> ADOPTED_ORPHAN` as a red alert counter.
- Extend the ENTRY ledger writer to the adoption path.
- Investigate the IBKR `TIF=DAY` warning/root cause for FX orders and use the intended FX TIF explicitly.
- Promotion should exclude trades whose entry was orphan-adopted unless they are separately tagged.

### 5. Current broker positions include unmanaged or questionable exposures

Severity: Critical

Evidence from `argus_flow/logs/_broker/broker_snapshot.json`:
- `GLD` long 22
- `SPY` long 26
- `QQQ` long 7
- `USD.JPY` long 30,268
- `CAD.JPY` long 41,572

Impact:
- GLD blocks the active survivor.
- USDJPY/CADJPY came from sunset FX pairs and violate JPY concentration.
- QQQ appears not owned by an active strategy in the prior orphan audit.
- SPY may be the passive benchmark, but it should be explicitly marked as benchmark-owned.

Required fix:
- Run the orphan audit after restarting patched runners.
- Create a broker-position ownership table with one row per symbol: owner, state file, stop/exit owner, intended allocation, and whether it counts toward performance.
- Fail preflight when any position has no live owner.

### 6. NQ overnight is not cleanly live; it is mostly signal-only plus rejected real entries

Severity: High

Evidence:
- `forge/logs/nq_overnight/runner.log:3022` signal-only long on 2026-05-19.
- `forge/logs/nq_overnight/runner.log:3040` real entry failed: `Cancelled`.
- `forge/logs/nq_overnight/runner.log:3121` signal-only long on 2026-05-20.
- `forge/logs/nq_overnight/runner.log:3131`, `3136`, `3141` real entry failed: `Cancelled`.

Impact:
- The strategy may look active in signals but fail at the real execution boundary.
- Real-money evidence is weaker than strategy logs imply.
- If backtest/paper trades are counted beside rejected real entries, promotion math gets polluted.

Required fix:
- Break out `SIGNAL_ONLY`, `PAPER_FILLED`, `REAL_SUBMITTED`, `REAL_FILLED`, and `REAL_REJECTED` in the strategy scorecard.
- Add a gate: real-fill rate must exceed a minimum threshold before any scale-up.
- Root-cause the `Cancelled` failure separately from edge quality.

## High Findings

### 7. Old launcher can still start sunset runners

Severity: High

Evidence:
- `ops/start_all_runners.ps1:36-87` still includes broad runners such as Argus FX, `jpy_pm_short`, `nq_london_close`, `aud_asian_breakout`, `vix_revert`, `mamba`, `tori`, `cuebanks`, and others.
- `ops/start_post_reset_runners.ps1:6-12` documents the intended smaller roster.
- `ops/start_post_reset_runners.ps1:96-104` explicitly stops many of the old runners.

Impact:
- Operator muscle memory or a scheduled task using `start_all_runners.ps1` can revive sunset strategies.
- Killed strategies can still produce logs, heartbeat noise, stale state, and in some cases direct execution risk.

Required fix:
- Rename `start_all_runners.ps1` to a legacy/quarantine launcher, or make it delegate to `start_post_reset_runners.ps1`.
- Add a startup assertion that refuses to start any runner with allocation factor 0.0 unless launched in explicit `--shadow` or `--signal-only` mode.

### 8. Ops scripts still check live port 7496

Severity: High

Evidence:
- `ops/autostart_runner.ps1:154-165`
- `ops/premarket_check.ps1:124-135`
- `ops/sunday_countdown.ps1:47-50`
- `ops/watchdog_managed.ps1:547-560`

Impact:
- These scripts can warn on the wrong port or steer the operator toward live TWS when paper is intended.
- The code safety tests focus on Python files, not PowerShell operator scripts.

Required fix:
- Centralize the intended port in one config/env value and make PowerShell scripts read it.
- Add a PowerShell static check for bare `7496` outside documented live-only scripts.

### 9. Direct `placeOrder` allowlist still contains future live risks

Severity: High

Evidence:
- `argus_flow/tests/test_static_safety_invariants.py` allowlists direct `placeOrder` in several strategy files as known tech debt.
- Examples include `forge/gdx_gld_runner.py`, `forge/rebalance_runner.py`, `forge/vix_revert_runner.py`, `forge/fomc_drift/runner.py`, `forge/spy_trend_follower/runner.py`, and `forge/tom_international/runner.py`.

Impact:
- The static test passes because these are allowlisted, not because all execution routes are centralized.
- If one of these strategies is reactivated later, the real-money boundary needs another sweep.

Required fix:
- Convert allowlisted strategy callsites to `submit_bracket` or a central execution adapter before reactivation.
- Add a "reactivation checklist" gate: no direct `placeOrder` outside central executor.

### 10. Access-denied folders are hiding audit surface

Severity: High

Evidence:
- Recursive sweeps hit access denied on `.pytest_tmp/` and `argus_flow/logs/_tmp_pair_onboarding/pair_onboarding_mw3icdje`.
- `git status` warns it cannot open `.pytest_tmp/`.

Impact:
- Static scans and tests can silently skip paths or fail early.
- This makes "full audit" claims weaker than they should be.

Required fix:
- Clean or fix ACLs on temp audit directories.
- Route pytest temp output to a known writable folder and add it to ignore rules if needed.

## Medium Findings

### 11. PEAD and XS momentum are implemented candidates but still allocation 0.0

Severity: Medium

Evidence:
- `argus_flow/configs/allocation_factors.json:9-10` has `forge_pead` and `forge_xs_momentum` at 0.0.
- Audit notes at `argus_flow/configs/allocation_factors.json:41-42` say they are post-reset candidates.

Impact:
- They cannot generate live/paper evidence unless separately launched in signal-only/paper mode with explicit accounting.
- If the goal is a 30-day evidence sprint, these need intentional activation rules.

Required fix:
- Add a separate candidate allocation file or mode: `paper_candidate: true`, `real_allocation: 0.0`, `paper_allocation: X`.
- Dashboard should distinguish "killed" from "paper candidate paused until reset."

### 12. Signal summaries show many runners are active but nonproductive

Severity: Medium

Evidence from recent `signals.csv` sweeps:
- `nq_london_close`: 100 recent rows are `NO_TRIGGER_not_signal_hour`.
- `wick_gbpusd`: 100 recent rows are `NO_TRIGGER`.
- `gdx_gld`: 100 recent rows are `NO_SIGNAL`.
- `gld_pm_long`: recent rows heavily `ENTRY_REJECTED` because of broker position.
- `nq_overnight`: recent rows mostly `NO_TRIGGER`, with real-entry rejected bursts.

Impact:
- CPU/log/operator attention is being spent on strategies that cannot currently contribute returns.
- "No trades" is not always edge absence; sometimes it is window logic, launch mode, stale state, or execution rejection.

Required fix:
- Add per-strategy "blocker reason" rollups to the dashboard.
- Any strategy with 30 days of no eligible entry should auto-move to shadow unless it is explicitly seasonal.

### 13. `start_post_reset_runners.ps1` still keeps Argus FX running

Severity: Medium

Evidence:
- `ops/start_post_reset_runners.ps1:12` says Argus FX is kept running with allocation 0.0 to preserve state/exit/reconcile logic.
- Before this audit patch, that assumption was unsafe.

Impact:
- Keeping a killed strategy runner alive is reasonable only if entry suppression is proven.
- This should be tested as a first-class invariant.

Required fix:
- Add a test or smoke script: allocation factor 0.0 for Argus FX must produce `allocation_factor_zero` and no order.
- Consider a dedicated `--reconcile-only` mode that cannot evaluate entries.

## Gates That May Be Preventing Returns

These are not all bugs. Some are good gates doing their job, but they should be visible as return suppressors.

1. `gld_pm_long` broker-position anti-doubling gate: currently blocks a survivor.
2. NQ real-entry rejection/cancel gate: likely suppresses intended live trades.
3. Fleet daily halt and flatten gates: seen in NQ logs; valid but should be counted as opportunity cost.
4. Cluster/correlation caps: currently catching JPY after entry; should become pre-entry for FX.
5. Allocation 0.0 sunset gates: good, but must be enforced consistently.
6. Port/API availability: NQ falls to signal-only when IBKR connection fails.
7. Market-hours/TIF mismatch: GLD and NQ logs include many `market_closed`, `Inactive`, `PendingSubmit`, and `Cancelled` failures.
8. State divergence gates: prevent doubling, but can freeze a strategy indefinitely.

## Performance Data To Rebuild Before Trusting Promotion

Minimum post-reset evidence table per strategy:

- ENTRY count, EXIT count, and entry coverage ratio.
- Real-fill rate: real fills / real submissions.
- Rejection breakdown by reason: cancelled, market closed, cap breach, fleet halted, broker error, timeout.
- Slippage in dollars and bps from intended entry to fill.
- Commission and fees per trade.
- Gross edge, net edge, and edge after slippage/fees.
- Time in market and capital tied up.
- Worst day/week/month.
- Max drawdown and drawdown duration.
- Top 1, top 2, and top 5 trade contribution.
- Performance with top 1 and top 2 trades removed.
- Correlation to SPY, QQQ, GLD, USD, JPY, and every active strategy.
- Regime tags: VIX bucket, trend/chop, CPI/FOMC/NFP/OPEX, session.
- Capacity curve at 1x, 2x, 5x, and 10x current size.

Promotion should fail closed if any of these are missing for a real-money candidate.

## Fix-Now Queue

1. Restart `argus_flow.runner_unified` so the allocation-factor patch is active.
2. Decide ownership/exit for current USDJPY and CADJPY positions opened by sunset strategies.
3. Resolve GLD 22-share broker/local divergence so `gld_pm_long` can trade or manage the position.
4. Re-run orphan audit and create a position ownership table for GLD, SPY, QQQ, USDJPY, CADJPY.
5. Prove canonical ENTRY rows with one controlled entry/exit through every active execution path.
6. Root-cause NQ `REAL_ENTRY FAILED: Cancelled` on the latest post-fix runner.
7. Disable or quarantine `ops/start_all_runners.ps1` so it cannot revive sunset runners.
8. Replace hardcoded 7496 checks in PowerShell ops scripts.

## Table For End-Of-Month Reset

1. Convert remaining direct `placeOrder` strategy callsites before reactivation.
2. Split "killed", "shadow", "paper candidate", and "reconcile only" into explicit launch modes.
3. Add scorecard columns for real-fill rate, signal-only rate, rejection reason, and ENTRY coverage.
4. Add a pre-entry broker ownership gate for every symbol and currency group.
5. Add a test that allocation factor 0.0 prevents order creation for every runner family.
6. Add a clean temp/log ACL check to preflight.
7. Rebuild canonical fills from reset forward only; keep pre-reset data quarantined as legacy evidence.

## Bottom Line

The bot is not merely being held back by strategy alpha. It is being held back by inconsistent enforcement between allocation, launch scripts, broker state, and evidence capture. The most profitable next move is not adding another sleeve. It is making every active sleeve answer four questions every day:

1. Am I allowed to trade?
2. Am I actually able to trade?
3. Did broker truth match my local state?
4. Did the evidence ledger capture the whole lifecycle?

Until those four are green, returns will be capped by operational drag more than by market edge.
