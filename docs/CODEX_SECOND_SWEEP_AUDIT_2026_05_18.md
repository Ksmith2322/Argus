# Codex Second-Sweep Audit - 2026-05-18

Scope: verify `docs/AUDIT_FOR_CODEX_2026_05_18.md` against the current worktree, fix unambiguous safety defects, and table policy/statistics work for the 5/31 reset.

Bottom line: Claude's audit is directionally right. The main correction is that some MYM/front-month work was already present in shared execution paths, but direct MNQ runners still had the same class of bug. The evidence ledger is not promotion-grade because it has zero ENTRY rows and contains known phantom/oversized rows. Treat all pre-reset ROI as contaminated unless a strategy has independent broker-fill evidence.

## Fixes Applied In This Sweep

| Fix | Files |
| --- | --- |
| Paper default restored from `7496` to `7497` for Hermes/Apollo and the legacy executor. | `helio/runner_hermes.py`, `helio/runner_apollo.py`, `helio/ibkr_executor.py` |
| Real-money module kill flag now actually blocks when `REAL_MONEY_ENABLED=False`; before, an enabled allowlist bypassed the module flag. | `helio/real_money.py` |
| Direct Forge runner `submit_bracket` calls now pass `strategy_label`, so real-money boundary logs/caps and per-strategy cluster caps can attribute the order. | `forge/*/runner.py` touched in this sweep |
| Direct MNQ runners now use `qualify_front_month_future()` instead of ambiguous `make_contract("MNQ")` + `qualifyContracts()`. | `forge/nq_overnight/runner.py`, `forge/nq_london_close/runner.py` |

Validation: AST parse passed for the 13 files touched by this sweep. Normal `compileall` failed because Windows denied writes into existing `__pycache__` files, not because of syntax errors.

## Verified Critical Findings

| ID | Finding | Verdict | Evidence | Action |
| --- | --- | --- | --- | --- |
| C1 | Direct MNQ runners still used ambiguous futures qualification. | Fixed now | `forge/nq_overnight/runner.py:177`, `:279`; `forge/nq_london_close/runner.py:256`, `:359` now call `qualify_front_month_future`. | Watch next paper order for fully populated `conId/localSymbol/expiry`. |
| C2 | Argus FX bypasses the real-money boundary. | Still live | `argus_flow/runner_unified.py:2230` submits via local `_submit_real_entry`; `:2297`, `:2339`, `:2347`, `:2462`, `:2813`, `:2837` call `ib.placeOrder` directly. | FIX-NOW before any Argus real mode: call `enforce_real_money_boundary` before every transmit or route through shared executor. |
| C3 | Canonical fills has no ENTRY rows. | Verified | Current `canonical_fills.jsonl`: 329 rows, grouped as `EXIT=329`, `ENTRY=0`. Writers found only `side="EXIT"` in Forge runners and Argus close path. | FIX-BY-5/31: dual-write broker-derived ENTRY and EXIT, then rebuild clean ledger after reset. |
| C4 | Known phantom remains in canonical. | Verified | `2026-05-05T16:30:35Z`, `forge_nq_london_close`, `size=417`, `pnl_usd=8324.36`, `broker_anchor_at_fill_usd=null`. | FIX-NOW for analytics: quarantine/exclude via phantom resolution ledger before any ROI report. |
| C5 | Stuck/orphan position risk exists. | Verified/partial | Broker snapshot shows GLD 22, SPY 26, QQQ 7. `ops/orphan_audit.py` flags QQQ as orphan. `forge/logs/spy_mean_rev/state.json` still has `open_trade` from 2026-04-30 even though strategy is killed. | FIX-NOW operationally: manually attribute or flatten QQQ/SPY/GLD, then make killed-strategy state impossible to count as owned exposure. |
| C6 | GLD 2.2x sizing override conflicts with cluster caps. | Verified, not fixed | `argus_flow/configs/fleet_sizing.json:33` says `forge_gld_pm_long=2.2`; `helio/cluster_exposure.py:167` says `forge_gld_pm_long=0.4`; ETF single-instrument cap is also 0.6x and METALS cap is 0.9x. | POLICY DECISION: either approve GLD as a special concentration sleeve and update all cap layers, or lower fleet sizing back to what cluster caps allow. |
| C7 | Port split-brain defaulted some paper-looking runners to live. | Fixed now | Defaults now `7497` at `helio/runner_hermes.py:243`, `helio/runner_apollo.py:285`, `helio/ibkr_executor.py:71`. | Add unit/static test banning bare `7496` defaults outside explicit real-money modules. |
| C8 | FX exit TIF behavior still deserves scrutiny. | Still open | Initial exit still builds `tif="DAY"` at `argus_flow/runner_unified.py:2460`; retry logic exists later but the cascade root is not proven closed. | FIX-BY-5/31: make FX exits use explicit TIF policy and test pending/cancel/retry paths with fake IB. |

## Extra Findings Claude Missed

| ID | Finding | Severity | Evidence | Action |
| --- | --- | --- | --- | --- |
| X1 | Real-money module flag was ineffective when allowlist was enabled. | Critical | Pre-fix `helio/real_money.py:213` used `if not REAL_MONEY_ENABLED and not al.global_enabled`. | Fixed now; add a regression test. |
| X2 | Shared bracket calls lacked `strategy_label` in most direct Forge runners. | High | Direct calls at `forge/*/runner.py` were missing labels, making real-money attribution and per-strategy caps weaker. | Fixed now for direct Forge runners; add static test that every `submit_bracket` call includes `strategy_label`. |
| X3 | Orphan ownership is symbol-based, not position-intent based. | High | `ops/orphan_audit.py` marks SPY as OK if any active SPY owner exists, even if the actual stale state came from killed `spy_mean_rev`. | Add position ownership IDs: strategy, entry order id, contract id, config hash, session id. |
| X4 | Evidence reset boundary is not encoded as a first-class object. | High | Canonical ledger mixes backfill rows, null broker anchors, phantoms, and paper fills without a hard `reset_epoch`. | Create `evidence_epoch.json`; every ROI report must declare epoch and exclusion set. |
| X5 | Several guards fail open on exception. | High | Examples: cluster-cap checks log "allowing trade" on exception in direct execution paths. | Decide which guards are fail-closed. Real-money, size, cluster, and broker-equity guards should fail closed. |
| X6 | No invariant test proves "killed strategy cannot restart or own exposure." | High | `fleet_monitor.py` has `no_restart` for some killed strategies, but state files can still report open trades and broker positions can remain. | Add kill workflow test: killed => no restart, no new entry, no owned exposure unless explicit unwind ticket exists. |
| X7 | ENTRY/EXIT lifecycle is not reconstructible from broker order IDs. | High | Canonical rows have no ENTRY rows; many rows have `broker_anchor_at_fill_usd=null`; direct state owns stop/target ids only after entry. | Add order lifecycle table: signal, entry submit, entry fill, child submit, child fill/cancel, state transition. |

## Performance Evidence Required Per Strategy

Minimum promotion-grade audit card:

1. Thesis: why this edge should exist, whether it is public/crowded, and expected half-life.
2. Data lineage: source, timezone, bar construction, holiday/half-day behavior, and live-vs-replay parity.
3. Implementation parity: exact production code path vs backtest path, config hash, git sha, and feature availability at decision time.
4. Fill truth: ENTRY and EXIT rows from broker/fill simulator, slippage, spread, commission, partials, rejections, cancellations.
5. Sample quality: valid trades after reset, exclusions, duplicates, phantoms, null anchors, outliers.
6. Edge metrics: expectancy, PF, win rate, median trade, mean trade, skew, top-1/top-2 contribution, bootstrap CI, Wilson CI.
7. Risk metrics: max drawdown dollars/percent/duration, worst day/week/month, VaR/CVaR, time to recovery.
8. Robustness: parameter sensitivity, OOS/IS split, regime buckets, event days, randomized-entry and direction-flip negative controls.
9. Portfolio fit: correlation with SPY, T-bills hurdle, same-instrument buy/hold, other Argus strategies, effective independent bets.
10. Capacity: current size, 2x/5x/10x slippage model, volume/time-of-day concentration, margin impact, scaling ceiling.
11. Operational burden: uptime, restarts, state recovery, bracket survival on runner death, data outage behavior.
12. Decision rules: kill, promote, rework, quarantine, max allocation, and next review date.

## Fix-Now Queue

1. Argus FX: enforce real-money boundary before direct `placeOrder` calls or delete the direct execution path.
2. Evidence ledger: add ENTRY writes for all live/paper broker paths; refuse ROI promotion reports when ENTRY count is zero.
3. Phantom quarantine: explicitly exclude the 5/5 `nq_london_close` 417-contract row and any null-anchor backfill row from promotion math.
4. Orphans: flatten or explicitly ticket QQQ 7, attribute SPY 26 and GLD 22, then update state/ownership truth.
5. GLD cap decision: align fleet sizing, single-instrument cap, METALS cap, total-notional cap, and margin cap.
6. Static safety tests: no `7496` default outside real-money modules; every `submit_bracket` call passes `strategy_label`; no direct `placeOrder` without boundary wrapper.

## Fix By 5/31

1. Build `evidence_epoch` reset machinery and require every report to name the epoch.
2. Add live-vs-replay parity report per strategy: signal count parity, direction parity, fill expectancy parity.
3. Add per-strategy order lifecycle audit: pending, filled, partial, cancelled, rejected, orphaned child orders.
4. Add broker-state reconciliation for all Forge runners, not just Argus FX.
5. Make killed strategy state inert: killed configs may manage exits only if they have an explicit unwind ticket.
6. Build GLD policy test once the cap decision is made.
7. Add FX exit TIF/retry simulation tests.
8. Add capacity/margin stress test for each promoted strategy.

## Post-Reset Table

1. Recompute all ROI and promotion gates only from clean epoch data.
2. Run outlier sensitivity on every strategy: drop top 1, top 2, and top 10% winners.
3. Add bootstrap confidence intervals for expectancy and PF.
4. Add negative controls: randomized entries, direction flip, delayed entry, simple buy/hold same time-in-market.
5. Add correlation/effective-bets report across strategy daily PnL.
6. Add regime buckets: trend/chop, high/low vol, Fed/CPI/NFP/OPEX, overnight/weekend.
7. Add external dependency audit: yfinance vs IBKR vs paid feed drift.
8. Add operator-time accounting: alerts/day, manual interventions, restart count, investigation hours.

## Confidence Answer

I would not trust the bot with full capital yet. I would trust the infrastructure direction after these fixes more than before, but the current blocker is evidence integrity, not ambition. The bot earns capital only after the reset window proves broker-derived ENTRY/EXIT truth, clean attribution, no phantoms, and at least 2-3 independent strategies beating SPY/T-bills after costs.
