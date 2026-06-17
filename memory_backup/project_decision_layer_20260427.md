---
name: 2026-04-27 Decision Layer — pivot from observability to decision compression
description: Built the full metrics→scoring→decision→allocation stack on the dashboard. Introduces /api/strategy_actions as the decision engine. First time the system can answer "what do I do with capital tomorrow?" instead of just "what's happening?"
type: project
originSessionId: 0256622d-fcf2-4b57-9505-2a4e805eef61
---
## Why this exists

User reviewer pushed back on the dashboard improvements 2026-04-27 evening: tonight's
work (market clock widget, kill-switch banner, cluster exposure endpoint, FLEET TOTAL
alignment, blocked-entries widget) was *all observability*. None of it answered the
operational question that matters: **"where should capital go next, and what should
be killed?"**

Critique paraphrase: "You've built better diagnostics, not a decision engine. Your
brain is still the compute layer. That's not compression."

Pivoted same session to build the missing layers.

## Architecture (the stack the system now implements)

```
Layer 1 — Metrics
    /api/strategy_efficiency      time-in-market, PnL/min, eff score
    /api/strategy_drift            recent vs prior 30 trades expectancy
    /api/opportunity_vs_taken      signals fired vs taken + block reasons
    /api/capital_deployment_timeline  hourly % deployed (7d window)
    /api/target_capture            realized/planned move ratio per trade

Layer 2 — Scoring
    confidence weighting (built into Layer 3): n>=30 -> 100%, n>=10
    proportional, n<10 -> 10%. Down-weights low-sample noise.

Layer 3 — Decision
    /api/strategy_actions          decision engine
    Combines metrics + confidence + explicit rules -> ACTION per strategy.
    Output: SCALE_UP / HOLD / REDUCE / KILL / IGNORE
    Rules are in code (not in head), versioned: rules_version=v1_2026-04-27

Layer 4 — Allocation hint
    Each ACTION maps to a suggested capital % (SCALE_UP -> 20-30%, etc).
    Hint only — actual allocator engine is downstream/separate.
```

## Decision rules (v1)

```
n < 10                          -> IGNORE   (insufficient sample)
drift=DECLINING, pnl < 0        -> KILL     (losing + degrading)
drift=DECLINING, pnl >= 0       -> REDUCE   (profitable but degrading)
drift=RISING, eff_score > 5     -> SCALE_UP (improving + meaningful)
drift=RISING, eff_score <= 5    -> HOLD     (improving, small magnitude)
drift=FLAT/STABILIZING, pnl > 0 -> HOLD     (working, keep going)
drift=FLAT, pnl < 0             -> REDUCE   (consistently negative)
drift=EARLY                     -> HOLD     (have data, not enough for trend)
otherwise                       -> HOLD
```

To change a decision: rewrite a rule. Discretionary override is not a decision —
it's drift back into the failure mode the engine exists to prevent.

## First-run findings (5/1 preview)

Decision engine output as of 2026-04-27 22:00 UTC:
- **REDUCE × 1**: forge_multi_orb (n=48, pnl=-$70, drift=STABILIZING) — losing
  consistently with no positive trajectory. Throttle.
- **HOLD × 2**: forge_vix_intraday (n=18, +$97), forge_spy_mean_rev (n=29, -$73).
  Have data, not enough for trend judgment yet.
- **IGNORE × 5**: nq_overnight (n=3), gld_pm_long (n=1), argus_usdjpy (n=2),
  nq_london_close (n=2), jpy_pm_short (n=3) — all under sample threshold.
- **SCALE_UP × 0**, **KILL × 0**: nothing meets the criteria yet.

Target capture findings (MFE proxy):
- spy_mean_rev: mean capture +0.01, 52% full target hit, **45% stopped** —
  textbook "win small, lose full" — confirms reviewer's "death by weak
  follow-through" hypothesis.
- vix_intraday: mean +0.08, 44% stopped — similar pattern.
- gld_pm_long: mean +1.00 (1 trade, full target hit) — n too low to trust.

## What this DOESN'T do (deferred)

- **Full MFE with per-trade bar lookback**: target_capture is a proxy from
  existing target_px/exit_px data. True MFE (max favorable excursion within
  hold window) requires fetching minute bars per trade, which is a 1-2h
  build. Deferred to next session.
- **Auto-allocator**: allocation_hint is a string suggestion. There's no
  engine that takes the SCALE_UP/REDUCE actions and adjusts position sizing
  per-strategy. That's the next layer (Layer 4 enforcement, not just hint).
- **Correlation/redundancy map**: reviewer flagged this — multi_orb on SPY +
  spy_mean_rev + vix_intraday all overlap in EQUITY_BETA cluster. Cluster
  cap (built earlier today) prevents stacking, but doesn't show the user
  WHICH pairs are redundant. Future work.
- **Per-trade rolling expectancy chart**: rolling expectancy is in
  /api/strategy_drift (recent vs prior 30) but not visualized as a sparkline
  per strategy. Could be added.

## How to apply this memory

**Why:** the dashboard transitioned from "show me what's happening" to "tell
me what to do" in this session. The decision engine is now the primary view.
Future dashboard work should ADD to this architecture (more refined rules,
better confidence weighting, allocator enforcement) rather than build more
observability widgets.

**How to apply:**
- 5/1 review: USE the decision engine output as the starting point for
  verdicts. Don't override without rewriting a rule.
- When proposing dashboard work: ask "does this enhance Layer 1-4 of the
  decision stack, or is it more observability?" Observability isn't bad,
  but should be subordinate to decision quality.
- When sample sizes grow (>30 trades per strategy at 5/15-5/31), the drift
  window comparison will start producing actual RISING/DECLINING verdicts
  instead of mostly STABILIZING. Re-tune rule thresholds then if needed.
- The `efficiency_score` formula is `pnl / (max(time_in_pct, 0.01) *
  max(avg_notional, 1)) * 1_000_000`. The constants are arbitrary
  scaling — what matters is RANK, not absolute value. Don't read the
  number as "dollars per anything"; read it as a sort key.
