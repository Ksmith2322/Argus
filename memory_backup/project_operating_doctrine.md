---
name: Operating Doctrine — 6 rules, 6 builds, 5 rights
description: The operational translation of "less sprawl / more selection / cleaner execution / harder kills / better attribution / capital only to proven edges" into rules, mechanisms, and build order. Captures 2026-04-26 evening session direction.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## The 5 rights (simplest version)

```
1. A strategy must earn the right to stay alive.
2. A strategy must earn the right to be promoted.
3. A strategy must earn the right to receive capital.
4. A strategy must earn the right to scale.
5. A strategy can lose those rights immediately.
```

That's the entire system. Every other rule below is a mechanism that enforces these.

## The 6 operating rules → mechanisms

| Principle | Rule | Mechanism |
|---|---|---|
| Less sprawl | No new strategy unless one existing strategy is killed/quarantined/archived | `argus_flow/state/strategy_registry.json` enforces 1-in/1-out |
| More selection | Every strategy gets weekly score and ranked action | Scorecard generator produces `strategy_scorecard_<date>.csv` |
| Cleaner execution | Every order produces an execution-quality record | `argus_flow/logs/execution_quality.jsonl` per fill |
| Harder kills | Predefined pause/kill thresholds enforced automatically | Kill/pause engine code at trade/strategy/instrument/cluster/fleet layers |
| Better attribution | Every strategy must beat a dumb benchmark | Alpha attribution report compares strategy vs passive baseline |
| Capital only to proven edges | No real money without minimum sample + positive edge + clean execution + clean recon + benchmark outperformance + manual approval | Capital allocator policy with hard real-candidate gate |

Cross-references:
- Selection / scoring details → `project_strategy_scorecard.md`
- Allocator tiers + ladder → `project_capital_allocator_policy.md`
- Cluster caps → `project_cluster_exposure_model.md`
- Kill/pause layers → `project_kill_pause_engine.md`
- Real-money boundary → `project_real_money_boundary.md`
- Evidence stages → `project_evidence_promotion_ladder.md`
- Capital decision audit → `project_capital_promotion_ledger.md`

## The 6-build order (Week 3+ code work)

Build in this order. Each later build depends on earlier builds. Do NOT build out of order.

```
1. Strategy Registry (data file, foundational)
   → argus_flow/state/strategy_registry.json
   → seeded with 22 active + 3 incoming strategies, each tagged status/tier/instrument

2. Scorecard Generator (reads registry + canonical_fills + per-strategy CSVs)
   → tools/build_strategy_scorecard.py
   → outputs argus_flow/reports/strategy_scorecard_<date>.csv

3. Kill/Pause Engine (reads registry + scorecard + drawdown state)
   → argus_flow/risk/kill_pause_engine.py
   → writes argus_flow/state/strategy_controls.json
   → 5-layer: trade/strategy/instrument/cluster/fleet

4. Execution Quality Logger (writes per fill, feeds scorecard)
   → patches into helio/ibkr_execution.py + ibkr_executor.py + signal_executor.py
   → appends to argus_flow/logs/execution_quality.jsonl

5. Alpha Attribution Report (reads canonical_fills + benchmark data)
   → tools/build_alpha_attribution.py
   → outputs argus_flow/reports/alpha_attribution_<date>.csv

6. Capital Allocator Recommendations (reads everything above)
   → argus_flow/risk/capital_allocator.py
   → outputs argus_flow/reports/capital_recommendations_<date>.json
   → recommends only — does NOT auto-size
```

## What is NOT in this build plan

- Aggressive sleeve code (post 30 days clean real-money operation)
- Auto-rotation (manual approval until 90 days real evidence)
- LLM overlay (post-5/31 freeze)
- New strategies beyond the 3 already approved (no sprawl)
- Parameter optimization or strategy tuning (freeze applies)

## Decisions effectively committed by 2026-04-26

By engaging with the full session and the layered analyses, the following are now binding:

1. **Option A** — survival-first, retire 2-3x as a target, focus on the $10K fund-up goal
2. **Account boundary** — paper and real are SEPARATE IBKR accounts (mechanical boundary)
3. **Real-account panic** — -10% real account triggers HARD STOP + manual reassessment
4. **Concentration caps** — 25% normal max per strategy, 50% offensive max (post-90-day proof), never above 50%
5. **Capital ladder** — $50 (current) → $500-1K pilot → $2K → $5K → $10K → $25K → $100K (each rung = explicit decision)
6. **Short-vol** — 90+ days paper minimum, no overnight, separate cap, hard VIX>30 force-close
7. **Auto-rotation** — manual until 90 days real evidence, never fully auto under current scope
8. **Daily ops** — design for 30 min/day, not 2 hours
9. **Failover** — manual runbook required pre-real-money, hot standby later
10. **Walk-forward + experiment_valid** — must be scripted/standardized before 5/31 verdicts depend on them
11. **Strategy freeze 5/31** — covers no new strategies, no new instruments, no new overlays, no clever exceptions, no parameter hunting
12. **The 6-rule doctrine** above is the operating system going forward

## Silent Strategy Review Rule (added 2026-04-27)

**A strategy with zero live trades cannot be auto-killed for silence alone.** Two completely different reasons cause silence; they require opposite responses:

1. **Strategy is broken / blocked / misconfigured** — must be repaired and forced to produce valid evidence.
2. **Strategy is correctly implemented but has no edge** — kill.

Silence alone doesn't tell you which one applies. The Operational Vetting Checklist (in `project_5_1_review_ceremony_20260501.md`) determines which.

**The doctrine:**

```
A strategy with zero trades enters BLOCKED until operational audit passes.

It can move to KILL only if:
1. Backtest/math is already negative (apollo: PF 0.69, P(exp>0)=0.012), OR
2. Thesis is invalid, OR
3. It has been previously repaired and STILL cannot produce diagnostic output, OR
4. It is redundant with a stronger strategy.

Otherwise: BLOCKED → diagnose → repair → forced paper-validation window → re-classify.
```

A good backtest earns a strategy a proper paper trial. It does NOT earn permanent survival. Live paper is the interview, real money is the job. Backtest is just admission.

## Strategy exchange table (1-in/1-out enforcement)

The 3 Week-2 builds need explicit kill exchanges. With softening per the silent-strategy review rule:

| New strategy | Replacement candidate | Condition for the exchange |
|---|---|---|
| `forge_zn_momentum` | `forge_multi_orb` | Kill multi_orb at 5/1 unless v2 redesign is named (it currently isn't). |
| `forge_spy_tlt_pair` | `apollo` | Kill apollo at 5/1 — backtest math (PF 0.69, P(exp>0)=0.012) disproves edge independent of any operational issue. |
| `forge_vix_short` | `forge_mamba` (or DEFER) | Mamba is BLOCKED, not yet KILL — RESEARCH_ONLY removed 4/26, hasn't had a chance to trade. Defer this exchange until mamba either trades and fails OR fails the operational vetting checklist. If neither has happened by 5/31, defer the vix_short build entirely rather than force a premature kill of mamba.|

The principle: **kill strategies with bad evidence, broken thesis, or no repair path. Do not kill blocked-but-promising strategies just to make room for new ones.**

## The "sniper" framing (added 2026-04-27)

Argus is not trying to be an index alternative (boring 15%). It is not trying to be a 200-300% growth bot (reckless). It's a market sniper:

- Many setups are watched
- Few setups are fired
- Bad setups are cut quickly
- Good setups are scaled only after live proof
- Capital concentrates only after live evidence supports concentration

If the system only produces 15% gross with high complexity and maintenance load, it's not worth building. The justification is one of:

1. Higher return than passive indexing (after costs, taxes, attention)
2. Better drawdown-adjusted return than passive
3. A scalable capital-allocation engine that compounds
4. A repeatable research machine that finds new edges
5. A path to asymmetric upside over time

Without one of those, the project is hobby-grade. With them, it's a real instrument.

## Experiment validity (added 2026-04-27)

The unifying principle. "Did it trade?" is the wrong question. The right question is **"Was the experiment valid?"**

A trade — or a non-trade — has three possible classifications:

| Validity | Meaning | Counts toward promotion? |
|---|---|---|
| **VALID** | Strategy operational, signal evaluated correctly, fill clean, reconciled | YES |
| **DIRTY** | Signal correct but execution layer was bad (slippage spike, partial fill, broker disconnect during fill) | NO — diagnostic only |
| **INVALID** | Strategy bug, wrong instrument, stale data, recon drift, RESEARCH_ONLY mismatch, account boundary breach | NO — diagnostic only |

Promotion uses only VALID. Bug fixes use INVALID. Execution improvements use DIRTY.

Same logic for non-trades: a NO_TRIGGER event is VALID if the strategy actually evaluated the signal and the gate correctly blocked it; INVALID if the runner wasn't operational.

## Reset ledger (added 2026-04-27)

When a strategy is reset (state cleared, gate change, runner refactor, RESEARCH_ONLY flip, etc.), record it in `argus_flow/logs/strategy_reset_ledger.jsonl`:

```json
{
  "date": "2026-04-26",
  "strategy": "forge_spy_mean_rev",
  "reset_type": "CONFIG_CHANGE",
  "reason": "v2 trend filter deployed; v1 results no longer comparable",
  "old_sample_n": 29,
  "old_pnl_usd": -72.76,
  "old_status": "REWORK",
  "new_baseline_start_ts": "2026-04-26T00:00:00Z",
  "evidence_policy": "pre_reset_informational_only",
  "promotion_n_required_after_reset": 30
}
```

**Rule:** old data is diagnostic. New data is promotion evidence. After reset, the strategy starts a fresh count — it doesn't get a "clean historical pardon" or carry forward old credit.

## Blocked-signal logging standard (added 2026-04-27)

Every runner cycle must produce a record EVEN WHEN it didn't trade. Not just `signals.csv` — a structured event with reason. Format:

```json
{
  "ts": "2026-05-01T14:30:00Z",
  "strategy": "forge_aud_asian_breakout",
  "event": "SIGNAL_BLOCKED",
  "reason": "range_pips_below_min",
  "value": 6,
  "required": 8,
  "market_data_ok": true,
  "runner_ok": true,
  "account_mode": "paper"
}
```

Without these logs, "0 trades" is uninformative. With them, the operational vetting checklist becomes mechanical: did the runner evaluate? did the gate fire? what threshold blocked it? Each silent strategy has its silence explained.

## The fair-trial doctrine (consolidated)

```
A strategy cannot be killed until it has received a valid paper experiment.

A strategy cannot be promoted until it has produced clean post-reset evidence.

A backtest earns a trial, not capital.

A reset creates a new promotion baseline, not a clean historical pardon.

A silent strategy must explain its silence (blocked-signal logs).

A winner must beat its benchmark (alpha attribution).

A real-money candidate must be clean operationally, statistically, AND behaviorally.
```

## When to apply this memory

**Why:** at the end of an extended design session, this is the clean summary. Future session that asks "what's the operating doctrine?" reads this first.

**How to apply:**
- Before any code build: confirm which build number you're on, confirm earlier builds exist
- Before adding a new strategy idea: cite the 1-in/1-out rule, ask which existing strategy gets removed
- Before promoting a strategy: cite the 5 rights and walk through which right is being earned
- If the doctrine drifts: amend this memo deliberately, don't bend rules case-by-case
