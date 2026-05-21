---
name: 2026-05-07 week-of audit + roadmap (4-agent post-mortem)
description: Comprehensive post-mortem of 4/30-5/7 fleet performance, incidents, sizing bugs, margin crisis. Includes synthesized roadmap and 5/15 kill/keep decisions. Reference for next deep-work session.
type: project
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
# Week-of audit — 2026-04-30 to 2026-05-07

Synthesized from a 4-agent post-mortem on 2026-05-07: Performance Auditor, Bug Archaeologist, Risk Auditor, Strategy Effectiveness. Where agents disagreed, this doc resolves with the data.

## The honest one-line

**The fleet is not really making money.** Reported +$7,696 weekly PnL is mostly phantom trades; real PnL is ~+$477 (+1.5%) vs SPY's +1.26% — within noise, no meaningful alpha. Of 22 active strategies, 1 has confirmed working improvement (vix_intraday post-blackout), 1 actively regressed (multi_orb post-window-change), and ~17 are dead, dark, or too-small-to-judge.

## Phantom trades — the data quality problem

Five trades in the week's data are paper-side calculations that didn't fill at broker:
- **nq_london_close 2026-05-05 16:15:** 417 NQ contracts, $11.7M notional, +$8,324 PnL recorded → broker rejected
- **multi_orb 2026-05-05 four trades:** 681-1419 QQQ shares, $500K-$965K notional each → broker rejected

**These contaminated reported PnL by +$7,125.** Real fleet PnL excluding phantoms: **+$476.52**.

Root cause identified (Risk Auditor): `forge/multi_orb/runner.py:209` formula
```python
shares = max(1, int(risk_budget / max(stop_dollars, 0.01)))
```
On 5/5 multi_orb trades, stop_dollars was $0.21 (tight stop on $680 stock). `int($300 / $0.21) = 1,428 shares`. Same root in `forge/nq_london_close/runner.py:221`.

Fix shipped this week was a CIRCUIT BREAKER (50% NetLiq cap rejecting at submit_bracket time). The underlying math is still broken — it just can't reach the broker now. Real fix needs to use `base_stop_distance = max(stop_atr_mult × atr, floor)` instead of the chosen stop.

## Per-strategy verdict (real data, phantoms excluded)

| Strategy | n | PnL | PF | WR | Status | 5/15 action |
|---|---:|---:|---:|---:|---|---|
| **vix_intraday** | 21 | +$84 | 1.08 | 52% | **EDGE_PROBABLE** — 17 UTC blackout worked (PF +23% vs pre) | Promote 1.0x→1.5x if n≥50 + PF≥1.10 |
| **gld_pm_long** | 4 | +$179 | 7.88 | 75% | EDGE_PROBABLE — small n but clean WR | Hold; promote at n≥10 if PF≥1.50 |
| **nq_overnight** | 2 | +$332 | ∞ | 100% | TOO_SMALL but tracking — both wins clean | Hold; primary RM candidate at n≥10 + PF≥2.0 |
| **multi_orb** | 45 | -$22 (real) | 0.96 | 35% | **STRUCTURAL_LOSER** — window 24→32 made it worse (-64% PF) | **REVERT window to 24** OR escalate to KILL |
| **spy_trend_follower** | 1 open | unrealized +$141 | — | — | TOO_SMALL — first cycle since 5/4 launch | Hold; need ≥5 closed trades |
| **nq_london_close** | 0 real (1 phantom) | $0 (excl. phantom) | — | — | DARK — never fires | 60d backtest with 15:00-17:00 UTC window |
| **argus pairs (cadjpy, gbpusd, usdjpy)** | 0 real fills | $0 | — | — | **DEAD** — 3,800+ signals, 0 broker fills despite config fixes | Diagnose MTF gate or KILL |
| **cuebanks, tori, mamba** | 0 | $0 | — | — | **DEAD** — bug fixes deployed but still 0 signals | Verify launch flags; KILL if still dark by 5/15 |
| **spy_mean_rev** | 0 (KILL'd) | $0 | — | — | KILL'd correctly via factor=0.0x | Stay KILL'd |
| **apollo, hermes, titan, forge_rebalance** | 0 | $0 | — | — | KILL'd 4/30 | Stay KILL'd |
| **vix_revert, wick_gbpusd, fomc_drift** | 0 | $0 | — | — | Environmentally silent (correct) | Continue as designed |

## The 5/1 ceremony scorecard — net change negative

| Tweak | Predicted | Actual | Verdict |
|---|---|---|---|
| multi_orb window 24→32 | PF improvement | PF 0.78→0.28 (-64%) | **FAILED — revert** |
| vix_intraday 17 UTC blackout | Stabilize | PF 0.88→1.08 (+23%) | **WON** |
| spy_mean_rev factor 0.0x KILL | Remove drag | Drag removed | CORRECT |
| cuebanks/tori/mamba launch flags | Unlock signals | Still 0 signals | **NOT EFFECTIVE** |
| Argus pairs config + integer fix | Unlock fills | Still 0 fills | **NOT EFFECTIVE** |

**Net: 1 win + 1 correct kill + 3 no-ops/failures.** The largest tunable signal in the fleet (multi_orb V2 9152-sample evidence) didn't pan out in live data.

## Incident timeline

13 distinct incidents this week. Severity and recurrence:

| # | Date | Incident | Severity | Status |
|---|---|---|---|---|
| 1 | 4/30 | Cuebanks `score_confluence` missing `sd_zones` param | strategy-blocking | FIXED `175aec7` |
| 2 | 5/1 02:00 | Argus DD_RAMP stuck at 92.7% from sticky peak_pnl | strategy-blocking | FIXED (state file edit) |
| 3 | 5/1 14:26 | FX cluster cap 0.6× treating GBPUSD as stock | execution-blocker | FIXED `7daa29c` |
| 4 | 5/1 18:22 | IBKR Error 10318 fractional FX qty | execution-blocker | FIXED `aa8c1c3` |
| 5 | 5/4 08:55 | Argus orphan FX positions (startup-time) | data-loss-risk | BAND-AIDED `1613d62` |
| 6 | 5/5 morning | Same orphan bug at runtime | data-loss-risk | BAND-AIDED `779556e` |
| 7 | 5/5 daytime | Phantom oversized trades (1419 QQQ, 417 NQ) | margin-risk | HARD-CAPPED `b58308f` |
| 8 | 5/6 22:00 | gdx_gld nightly silent-death | strategy-blocking | UNRESOLVED |
| 9 | 5/7 morning | TWS overnight reset (failure mode #10) | infrastructure | KNOWN MODE |
| 10 | 5/7 afternoon | Margin cushion crashed 8.8%→1.57% (IBKR alert) | margin-crisis | EMERGENCY CLOSE |
| 11 | 5/7 multiple | broker_truth refresh occasionally returns $0 | infrastructure | UNRESOLVED |
| 12 | 5/7 ongoing | RECON_DRIFT recurring (CADJPY adopted but new drift) | data-loss-risk | RECURRING |

**Recurring bugs:** Orphans (4 occurrences this week despite fixes), TWS reset (3 times), gdx_gld silent-death (every night).

## Three structural bugs still unaddressed

### 1. Lost fill confirmation across session boundary
- **Symptom:** Filled orders disappear from ib_insync history when session reconnects. Creates orphans.
- **Root cause:** `execDetailsEvent` only fires for orders in current TCP session.
- **Current "fix":** Adoption layer (detection + recovery). Band-aid, not root.
- **Real fix:** Persistent `pending_orders.jsonl` queue. Write before submit; on startup, poll broker for executions matching pending entries.

### 2. Sizing formula `shares = risk_budget / stop_distance`
- **Symptom:** Phantom 1,419-share QQQ orders, 417-contract NQ orders.
- **Root cause:** Tight stops cause division to explode share count.
- **Current "fix":** Hard 50% NetLiq cap rejects oversized orders. Circuit breaker, not root.
- **Real fix:** Use `base_stop_distance = max(stop_atr_mult × atr, floor)` for sizing math. Decouple sizing from execution stop.

### 3. gdx_gld nightly silent-death (~22:00 UTC)
- **Symptom:** Process dies 8-16min after launch with no error in runner.log.
- **Root cause:** UNKNOWN. Possibly data farm timeout, lock collision, or silent exception.
- **Current "fix":** Daily restart. Pure operational.
- **Real fix:** Add explicit stderr capture, process monitoring wrapper, or detailed logging during the 22:00 window.

## Risk control gaps still present

Per Risk Auditor:

1. **Notional caps don't approximate margin.** Current 1.5× NetLiq notional cap doesn't account for instrument-specific maintenance margin (futures use ~10% margin/notional vs stocks 25%). Could still allow over-leverage.
2. **No pre-entry orphan check.** Reconciliation is post-hoc. Should query broker position before submitting any new entry.
3. **No sizing-floor on stop distance.** The phantom-trade root.
4. **Circuit breaker oscillates** (11 transitions in 6 hours on 5/7). Needs hysteresis: hold FLATTEN ≥4 hours before demoting.
5. **No per-strategy notional cap.** One strategy can saturate the fleet cap.

## Roadmap — concrete actions ranked by ROI

### Priority 1 — This week (5/8-5/14)
| # | Action | File:Line | Effort | Impact |
|---|---|---|---|---|
| 1 | **REVERT multi_orb window 32→24** | `forge/multi_orb/runner.py:71` | 5 min | Stop the -$1,145/week leak |
| 2 | **Fix sizing formula** in multi_orb + nq_london_close | `forge/multi_orb/runner.py:209`, `forge/nq_london_close/runner.py:221` | 30 min | Eliminates phantom-trade root cause |
| 3 | **Verify cuebanks/tori/mamba launch flags** in `ops/start_all_runners.ps1` | check `--live` not `--loop` | 15 min | Unlock 3 dead strategies if mis-launched |
| 4 | **Diagnose argus pairs zero-fills** | trace MTF gate logic vs config | 1-2h | Either fix or KILL |
| 5 | **Add minimum stop-distance floor to all strategies** | `helio/strategy_common.py` (new helper) | 1h | Universal protection vs sizing-formula bug |

### Priority 2 — Next 2 weeks (5/15 mid-cycle review)
| # | Action | Effort | Impact |
|---|---|---|---|
| 6 | **Persistent fill queue** (`pending_orders.jsonl`) | medium (4-6h) | Root fix for orphans — eliminates the recurring bug |
| 7 | **Margin-aware cluster cap** (use IBKR maint-margin not notional) | medium (4-6h) | Prevent another margin crisis |
| 8 | **Pre-entry broker reconciliation** (query position before submit) | low (2h) | Catches orphans before doubling positions |
| 9 | **Circuit breaker hysteresis** | low (1h) | Prevent thrashing oscillation |
| 10 | **gdx_gld silent-death investigation** with explicit stderr capture | medium (debug-heavy) | Eliminate daily restart |

### Priority 3 — Post-5/31 freeze
| # | Action | Effort | Impact |
|---|---|---|---|
| 11 | **Audit all 22 strategies for sizing-formula bugs** (multi_orb is template) | high | Prevent future phantoms across fleet |
| 12 | **Correlation-aware cluster caps** (SPY+UVXY treated as one cluster, not two) | medium | Better real risk model |
| 13 | **Per-strategy notional caps** in cluster_exposure.py | low | Prevent one strategy saturating fleet |
| 14 | **Broker-cap enforcement in paper execution** | medium | Stop phantoms from ever being logged |

## 5/15 mid-cycle decisions (concrete)

**Promote (paper factor 1.0x → 1.5x):**
- vix_intraday — IF n≥35 and PF≥1.05 by then

**Hold paper (continue accumulating):**
- gld_pm_long, nq_overnight, spy_trend_follower

**KILL (stop runner):**
- multi_orb (if window-revert hasn't recovered PF≥1.0 by 5/15)
- cadjpy, gbpusd (if still 0 fills)
- cuebanks, tori, mamba (if still 0 signals after launch flag fix)

**Investigate before deciding:**
- nq_london_close (window expansion test)
- usdjpy (gate diagnosis)

## Lessons learned (specific, not generic)

1. **Every order-submit path must survive a restart-during-pending-fill.** Adoption is detection; persistent queue is prevention.
2. **Sizing calculations must use a stop-distance FLOOR**, not just the chosen stop. Tight stops cause division explosion.
3. **Session boundaries (socket drops, TWS reset, restarts) need explicit logging** with `[SESSION_EVENT]` markers — recurring failures lack diagnostic context.
4. **Margin cushion alerts must trigger automated size-reduction**, not just notifications. Manual operator intervention is too slow at <5% cushion.
5. **Data freshness must be a universal pre-trade check** — only CADJPY currently has staleness blocking. Others may trade on stale data.
6. **A "fix" that's a circuit breaker is a band-aid.** Track each fix by category (root-cause vs band-aid) so band-aids accumulate visibility for future deep work.

## Bottom-line

Real-money launch is **NOT** ready. With phantoms excluded, the fleet has no statistically valid edge yet. Best case for 5/31:
- **vix_intraday at n=50+ with PF≥1.10** → real-money 0.5x candidate
- **nq_overnight + gld_pm_long at n≥10** → secondary candidates
- **Everything else** → KILL'd or held paper through summer

By 5/31 freeze, expect a fleet of **2-3 real-money candidates** at most. The other 17-20 strategies stay paper, get reworked, or are archived.

The 5/15 mid-cycle is the next decision gate. Goal: cut multi_orb's bleeding (revert window), unblock cuebanks/tori/mamba (verify launch flags), fix sizing root cause, and confirm vix_intraday's improvement holds.
