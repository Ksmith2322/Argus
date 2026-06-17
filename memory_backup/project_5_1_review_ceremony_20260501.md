---
name: 5/1 Review Ceremony — verdict template per strategy
description: Formal review process for 2026-05-01. Run once. Produces a decision record per strategy: WINNER (scale up), REWORK (fix one thing), KILL (shelve). Output saved to verdict_20260501.json.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## Run this on 2026-05-01

Markets close 4PM ET. After close, run the ceremony. Should take 60-90 min.

## Pre-ceremony data gathering (15 min)

```bash
cd c:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m ops.full_audit --diff
```

Plus pull these into one place:
- `argus_flow/logs/canonical_fills.jsonl` — last 30 days of fills
- `argus_flow/logs/operational_maturity_report.json` — current verdicts
- `argus_flow/logs/fleet_perf_summary.json` — per-strategy rolling stats
- `argus_flow/logs/promotion_readiness_report.json` — promotion-gate status
- Per-strategy equity curves dashboard panel (live UI, screenshot for record)

## Verdict labels — 7-tier system (replaces old WINNER/REWORK/KILL trio)

The word "WINNER" was overloaded. Silence-without-audit was being treated like failed experiment. Both fixes below.

| Label | Meaning |
|---|---|
| **BLOCKED** | Strategy has not produced valid trades and operational vetting has not been completed. Cannot judge edge yet — strategy may not be operational. Must be diagnosed before any kill decision. |
| **OBSERVE** | Strategy IS operational (fires correctly, logs correctly, reconciles), just thin sample. |
| **KEEP-PAPER** | Valid behavior + meaningful sample, not capital-ready yet. |
| **WINNER-CANDIDATE** | Promising paper evidence; closer review. |
| **REAL-CANDIDATE** | Eligible for real-money gate review. |
| **REWORK** | Concept may be valid, named structural fix required. |
| **QUARANTINE** | Concept may be valid, but implementation/data/execution untrusted. |
| **KILL** | Valid experiment failed OR backtest/math disproves edge. Stop the strategy. |

**Critical distinction added 2026-04-27:** A strategy with zero trades cannot be killed for "no edge" without first being audited as operational. Silence + bad backtest = KILL is fine. Silence + good/unknown backtest + no operational audit = BLOCKED, not KILL.

A strategy can be worth keeping (KEEP-PAPER) without being real-money worthy. A strategy can be silent (BLOCKED) without being dead.

## Verdict template per strategy

```json
{
  "strategy": "forge_vix_intraday",
  "verdict": "WINNER-CANDIDATE",
  "live_trades_30d": 18,
  "live_pf_30d": 1.45,
  "operational_maturity": "HEALTHY",
  "evidence_class": "Paper-Live Valid",
  "reasoning": "n=18 over 30d, PF 1.45, expectancy positive, no DEGRADED days. Cleanest signal in fleet but n still below 30 — needs more proof before REAL-CANDIDATE eligibility.",
  "action": "Continue paper, no tier bump until n≥30. Re-review at 5/15.",
  "owner": "user",
  "review_date": "2026-05-01",
  "next_review": "2026-05-15"
}
```

## Verdict criteria

### REAL-CANDIDATE (eligible for real-money gate)
ALL of:
- ≥30 valid trades in last 60d
- live PF ≥ 1.30 (higher bar than WINNER-CANDIDATE)
- operational_maturity HEALTHY for last 21d (no DEGRADED days)
- no execution-path bugs in last 30d
- expectancy > 0 with `p_expectancy_positive` ≥ 0.75
- evidence_class = Paper-Live Valid (per `project_evidence_promotion_ladder.md`)

**Action:** flag for real-money gate review per `project_real_money_readiness_gate_20260531.md`. Does NOT mean "deploy real money" — means "considered for the pilot."

### WINNER-CANDIDATE (promising, more proof needed)
ALL of:
- ≥15 valid trades in last 30d
- live PF ≥ 1.20 over 30d window
- operational_maturity not DEGRADED for last 14d
- no critical bugs in execution path

**Action:** continue paper, optional tier bump within paper risk_pct ladder. Re-review at next milestone. NOT eligible for real-money gate yet.

### KEEP-PAPER (valid behavior, not promotion-ready)
- 5-15 trades in 30d
- live PF between 0.95 and 1.20 (modestly positive or near break-even)
- operational_maturity HEALTHY
- behavior matches design intent (entries/exits where expected)

**Action:** keep running on paper, no risk changes. Collecting more sample.

### REWORK (one specific named fix)
At least 2 of:
- known structural issue (gate too tight / data path / sizing bug)
- live PF in 0.8-1.0 range with the structural issue identified
- operational_maturity flicker
- execution path bug that's diagnosable

**Action:** name the ONE thing to fix. Don't bundle. Re-review 2-3 weeks. If fix doesn't move needle by 5/31, auto-converts to KILL.

### QUARANTINE (concept valid, implementation untrusted)
- Strategy logic seems sound but execution/data/runtime is unreliable
- Examples: gdx_gld with chronic socket disconnects, tori_paper with shape bugs
- Don't kill the concept, but don't let it trade live until the implementation is fixed

**Action:** stop the runner OR mark RESEARCH_ONLY=True. Document the implementation issue. Re-evaluate after the fix lands.

### OBSERVE (insufficient volume)
- < 5 trades in 30d AND no clear gate issue (just low signal frequency by design)
- Examples: fomc_drift, wick_gbpusd, rebalance — event-driven, low expected fire rate
- Set explicit next-review date based on expected fire rate
- If still OBSERVE at 5/31 with no fires: convert to KILL by default

**Action:** keep running, no changes. Track expected vs actual fire rate.

### KILL (stop, no ceremony)
ANY of:
- 0 trades in 60+ days despite gate-loosening attempts
- live PF < 1.0 over n ≥ 30
- operational_maturity DEGRADED for 21+ consecutive days
- duplicates an existing strategy with worse performance
- structural rebuild required and not in scope

**Action:** stop the runner, set disposition.status = "killed", document reason, archive logs. Do NOT delete code.

## Two-track 5/1 review (added 2026-04-27)

The review runs on TWO tracks because silent and trading strategies need different evaluations.

### Track 1 — Performance Cull (for strategies that DID trade in 90d)

For each: did it trade correctly? clean execution? meaningful sample? positive expectancy? beat benchmark? named structural issue?

Possible verdicts: KEEP-PAPER / WINNER-CANDIDATE / REAL-CANDIDATE / REWORK / QUARANTINE / KILL.

This is where the actual cull happens. `forge_multi_orb` (n=48, -$69, no fix) → KILL. `forge_spy_mean_rev` (n=29, -$72, v2 fix in hand) → REWORK.

### Track 2 — Operational Vetting (for strategies with ZERO trades in 90d)

For each: run the **Operational Vetting Checklist** below. ONLY after the checklist passes can you ask "is there edge?"

Possible verdicts: BLOCKED (default if checklist incomplete) / LOW_FREQUENCY_OBSERVE (if expected to be quiet) / QUARANTINE (if known broken) / KILL (only if backtest/math disproves edge independent of silence).

### Operational Vetting Checklist (15 items, per strategy with zero trades)

1. Runner exists and starts cleanly
2. Scheduled task exists and `last_result=0` (if it has one)
3. Correct account mode: paper
4. Correct IBKR client_id (no collision)
5. Correct symbol/contract mapping
6. Market data available
7. Timezone/session logic correct
8. RESEARCH_ONLY flag is correct (False if intended to trade)
9. Entry gates produce diagnostic logs (signals.csv populated even when NO_TRIGGER)
10. Blocked signals are logged with reasons
11. Strategy can generate at least one synthetic/test signal
12. Orders are blocked only for valid reasons
13. No stale state preventing entry
14. No weekend/holiday guard blocking incorrectly
15. Output files (heartbeat, state, signals) update as expected

If a strategy fails any of items 1-15: that's the explanation for silence. Fix that BEFORE judging edge.

If all 15 pass and the strategy still doesn't trade: ask whether market conditions for its setup occurred during the window. Some strategies are correctly silent (vix_revert needs VIX>25, fomc_drift needs FOMC days, etc.). Those go to LOW_FREQUENCY_OBSERVE.

If all 15 pass AND market conditions DID occur AND it still didn't fire: gates are too tight. → REWORK with named gate adjustment.

## Forced paper-validation window (5/1 → 5/31)

For strategies labeled BLOCKED on 5/1:

- **5/1 → 5/15 (repair window):** fix the operational issue. Strategy must produce diagnostic output (even if no trades). Re-audit on 5/15.
- **5/15 → 5/31 (evidence window):** repaired strategies must produce one of: (a) valid live paper trades, (b) blocked-signal logs proving gates evaluated, or (c) proof that market condition didn't occur.

At 5/31: a strategy still BLOCKED with no diagnostic output, no signal logs, and no condition-occurrence proof = KILL or ARCHIVE. By then we've earned the right to call it dead.

## Acceptance criteria for the ceremony itself

For the 5/1 review to count as "done":
- Every strategy has a verdict from the 6-tier set (no "skipped for now")
- Numbers should fall roughly:
  - **REAL-CANDIDATE: 0-1** (extremely rare at this sample size; only possible if a strategy already had n≥30 pre-conversion)
  - **WINNER-CANDIDATE: 1-3** (vix_intraday is the most likely)
  - **KEEP-PAPER: 4-8** (the bulk of viable strategies)
  - **REWORK: ≤5** (more than 5 = cull isn't decisive enough)
  - **QUARANTINE: 0-3** (gdx_gld likely candidate)
  - **OBSERVE: 4-8** (event-driven + just-restarted strategies)
  - **KILL: 2-6** (the deserved cull)

If numbers fall outside these ranges, surface the pattern — fleet either too immature or too cluttered. Adjust the 5-week roadmap.

**Note:** "8-12 WINNERs by 5/31" was the old framing. Drop it. Real target by 5/31 is **1-3 REAL-CANDIDATEs, 4-6 WINNER-CANDIDATEs / KEEP-PAPER, rest culled**.

## 5/1 is volume-graded, not PnL-graded

The success metric for 5/1 is **TRADE COUNT**, not P&L. Sample size at 5/1 is too small to draw P&L conclusions. What matters:

- **How many strategies fired live trades?** (target: ≥15 of 22 with at least 1 trade in 30d window)
- **Did the silent-15 wake-up work?** (target: ≥8 of 15 produced live data after gate-loosening / RESEARCH_ONLY removal)
- **Are gates calibrated reasonably?** (no strategy should be at 0 trades AND a known-too-strict gate)
- **Is execution working?** (no chronic REAL_ENTRY FAILED patterns)

PnL stories ("vix_intraday is up $97!") are noise at this sample size. Don't promote based on small-sample PnL — that's the classic overfit trap. Promote based on:
- Did the strategy fire when it should?
- Did the execution path work?
- Are the trades in the expected R-distribution range?

Save the actual edge evaluation for the 5/31 review where samples are larger.

If these aren't met: the review surface a pattern — fleet either too immature (extend monitoring) or too cluttered (more aggressive cull). Document and adjust the 5-week roadmap accordingly.

## Output

Save the verdict record:
```bash
# Manual edit or via script
echo "<verdicts json>" > argus_flow/logs/verdict_20260501.json
git add argus_flow/logs/verdict_20260501.json
git commit -m "review: 5/1 verdict record — N winners, M rework, K kill"
```

This becomes the source of truth for the cull execution in Week 2.

## Anti-patterns to avoid

- **"It might fire next week"** — that's INSUFFICIENT_DATA, not WINNER. Don't be optimistic with verdicts.
- **"It almost works"** — that's REWORK only if there's a specific named fix. Otherwise KILL.
- **"We've spent so much time on it"** — sunk cost. If the data says KILL, kill it.
- **"It's research_only so we can keep it"** — research_only without a research outcome is just a runner consuming attention.

## How to apply this memory

**Why:** the 5/1 ceremony is the inflection point. Done well, it's a real cull. Done badly, it's another observation that changes nothing.

**How to apply:**
- Don't skip the ceremony. Run it 5/1 even if you "don't have time."
- Use the template strictly. Free-form verdicts let bias creep in.
- The verdict record is the contract for Week 2 work — execute exactly what was decided, not what feels right after the fact.
