---
name: 2026-05-18 Codex environment sweep + my audit sync
description: Codex executed the 6-agent comprehensive audit. 4 unambiguous safety defects fixed (port defaults, REAL_MONEY_ENABLED kill flag, strategy_label on direct submit_bracket, MNQ qualify_front_month_future). 8 items still fix-now/fix-by-5/31. Codex added 7 findings I missed (X1-X7), most important: intent-based ownership, evidence_epoch object, killed-strategy invariant.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
# Codex environment sweep — 2026-05-18

## Source docs (durable)

- **My audit (input):** `docs/AUDIT_FOR_CODEX_2026_05_18.md` (~5,400 words, 6-agent synthesis)
- **Codex sweep result (truth):** `docs/CODEX_SECOND_SWEEP_AUDIT_2026_05_18.md`

Read the Codex sweep first when this memory is recalled. It supersedes the input audit on overlapping items.

## What Codex shipped (verified fixes)

| Fix | Files | Validation |
|---|---|---|
| Paper default `7497` (was `7496` for hermes/apollo/legacy executor — split-brain risk) | `helio/runner_hermes.py`, `helio/runner_apollo.py`, `helio/ibkr_executor.py` | AST parse OK |
| `REAL_MONEY_ENABLED=False` now actually blocks (was dead code due to OR-vs-AND logic at `helio/real_money.py:213`) | `helio/real_money.py` | Regression test added |
| `strategy_label` added to direct Forge `submit_bracket` calls (was missing → real-money attribution + per-strategy caps weakened) | `forge/*/runner.py` direct callsites | AST parse OK |
| Direct MNQ runners use `qualify_front_month_future()` (was ambiguous `make_contract+qualifyContracts` → Error 321 risk) | `forge/nq_overnight/runner.py`, `forge/nq_london_close/runner.py` | AST parse OK |

`compileall` failed on Windows `__pycache__` permission denial; AST validation used as substitute.

## Findings Codex added that my audit missed

**X1 — `REAL_MONEY_ENABLED` was ineffective when allowlist enabled.** Fixed + regression test. Critical safety.

**X2 — Most direct Forge `submit_bracket` calls lacked `strategy_label`.** Fixed. Recommendation: add static test that EVERY `submit_bracket` call passes the label.

**X3 — Orphan ownership is symbol-based, not intent-based.** `ops/orphan_audit.py` marks SPY as OK if any active SPY owner exists, but the actual stale state could be from a killed strategy. Need per-position lineage IDs: strategy_id, entry_order_id, contract_id, config_hash, session_id. (My audit missed this entirely.)

**X4 — Evidence epoch needs to be a first-class object, not a one-shot script run.** `evidence_epoch.json` declares the current epoch + exclusion set. Every ROI report must name its epoch. Makes contamination structurally impossible to silently mix in. (My framing was "run `ops/maintenance/epoch_reset.py` at 5/31" — that's the trigger, not the model.)

**X5 — Several guards fail OPEN on exception.** Examples: cluster-cap checks log "allowing trade" on exception. Real-money, size, cluster, and broker-equity guards should ALL fail closed.

**X6 — No invariant test proves "killed strategy cannot restart or own exposure."** Fleet_monitor has `no_restart` for some killed strategies, but state files can still report open trades and broker positions can remain. Need: killed → no restart, no new entry, no owned exposure unless explicit unwind ticket exists. (My audit said "add no_restart" — Codex correctly identified that's insufficient.)

**X7 — ENTRY/EXIT lifecycle not reconstructible from broker order IDs.** Underlying reason for the 0-ENTRY problem. Need order lifecycle table: signal → entry submit → entry fill → child submit → child fill/cancel → state transition.

## Bottom-line confidence

From Codex: **"The current blocker is evidence integrity, not ambition."** Bot infrastructure direction is sounder than my audit framing suggested AND less proven than yesterday's "things looking better" framing suggested. Both at once. Promotion requires:
- Broker-derived ENTRY/EXIT truth
- Clean attribution (intent-based, not symbol-based)
- No phantoms
- 2-3 independent strategies beating SPY/T-bills after costs
- All AFTER a clean evidence epoch

## Fix-now queue (still live)

1. **Argus FX boundary bypass** — `argus_flow/runner_unified.py:2230 _submit_real_entry` + 5 direct `ib.placeOrder` callsites (lines 2297, 2339, 2347, 2462, 2813, 2837). Must route through `enforce_real_money_boundary` OR delete the direct path.
2. **ENTRY writes for canonical_fills** — dual-write from broker fill data for all live/paper paths. ROI reports must refuse-if-zero ENTRYs.
3. **Phantom quarantine** — explicitly exclude `2026-05-05T16:30:35Z forge_nq_london_close size=417 pnl=8324.36` and all `broker_anchor_at_fill_usd=null` backfill rows from promotion math.
4. **Orphans** — flatten or explicit ticket for QQQ 7, SPY 26 attribution, GLD 22 attribution. Then make state/ownership truth match.
5. **GLD cap policy decision** — 4 layers disagree: `fleet_sizing.json` 2.2× vs `cluster_exposure.PER_STRATEGY` 0.4× vs ETF single-instrument 0.6× vs METALS 0.9×. Pick one, align all four.
6. **Static safety tests** — no bare `7496` default outside real-money modules; every `submit_bracket` call passes `strategy_label`; no direct `placeOrder` without boundary wrapper.

## Fix-by-5/31 queue

1. `evidence_epoch` object + machinery — every ROI report names its epoch
2. Live-vs-replay parity report per strategy (signal count, direction, fill expectancy)
3. Per-strategy order lifecycle audit (pending/filled/partial/cancelled/rejected/orphaned)
4. Broker-state reconciliation for ALL forge runners (not just argus FX)
5. Killed-strategy state inertness invariant (X6)
6. GLD policy test (once cap decision made)
7. FX exit TIF/retry simulation tests (argus_gbpusd cascade — TIF=DAY still suspect)
8. Capacity/margin stress test for each promoted strategy

## Post-reset table (after 5/31 + 30-day clean window)

1. Recompute all ROI from clean epoch data ONLY
2. Outlier sensitivity per strategy (drop top 1/2/10%)
3. Bootstrap CIs for expectancy + PF
4. Negative controls: randomized entry, direction flip, delayed entry, buy/hold same time-in-market
5. Correlation / effective-bets across strategy daily PnL
6. Regime buckets: trend/chop, high/low vol, Fed/CPI/NFP/OPEX, overnight/weekend
7. External dependency drift audit (yfinance vs IBKR vs paid feed)
8. Operator-time accounting (alerts/day, manual interventions, restart count, investigation hours)

## The 12-point promotion-grade audit card (per Codex)

Every strategy needs ALL of these before real-money allocation:

1. Thesis (mechanism, crowding, expected half-life)
2. Data lineage (source, tz, bar construction, holidays, live-vs-replay parity)
3. Implementation parity (production code path == backtest path, config hash, git sha)
4. Fill truth (broker ENTRY + EXIT, slippage, spread, commission, partials, rejects)
5. Sample quality (valid trades after reset, exclusions, phantoms, null anchors)
6. Edge metrics (expectancy, PF, win rate, median, mean, skew, top-1/top-2 contribution, bootstrap+Wilson CIs)
7. Risk metrics (max DD $/%/duration, worst day/week/month, VaR/CVaR, recovery time)
8. Robustness (sensitivity, OOS/IS split, regime buckets, event days, negative controls)
9. Portfolio fit (SPY correlation, T-bills hurdle, same-instrument buy/hold, other-strategy effective-bets)
10. Capacity (current size, 2x/5x/10x slippage model, volume concentration, margin, scaling ceiling)
11. Operational burden (uptime, restarts, state recovery, bracket survival on runner death, data outage behavior)
12. Decision rules (kill/promote/rework/quarantine, max allocation, next review date)

This is the post-5/31 verdict ceremony format. Replaces the looser 5/1/5/15/5/31 verdict tiers from earlier memory.

## What this means for the revised 7/1 timeline

The 7/1+ real-money go/no-go decision now requires:
- All fix-now items completed (8 items)
- All fix-by-5/31 items completed
- `evidence_epoch.json` declares clean epoch starting 6/1
- 30-day clean window 6/1 → 6/30
- At least 2-3 strategies producing 12-point card scores supporting promotion
- No new contamination introduced during the window (operator discipline)

If any of these is missing, push the decision another 30 days.
