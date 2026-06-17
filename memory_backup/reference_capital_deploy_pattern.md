---
name: Pre-ceremony capital deployment pattern (factor-floor approach)
description: How to act on data-supported KILL/REDUCE signals before formal ceremony day without compromising the verdict. Uses allocation_factors as the "floor of all possible verdicts."
type: reference
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
## The pattern

When the dashboard's recommendations have been stable for >24 hours and the data is unambiguous, you don't have to wait for ceremony day to start protecting capital. But you also shouldn't pre-judge the verdict.

The reconciliation: **deploy the FLOOR of all plausible verdicts via allocation_factors**.

Allocation factor mapping (from `argus_flow/configs/allocation_factors.json`):
```
SCALE_UP    → 1.5x
HOLD        → 1.0x  (default)
REDUCE      → 0.5x
QUARANTINE  → 0.0x  (no new sizing)
KILL        → 0.0x  (no new sizing)
SCOPE_DOWN  → 0.5x  (size-down while logic gets fixed)
```

If the strategy is somewhere in {KILL, REWORK, SCOPE_DOWN, REDUCE} per the engine, **0.5x is the floor**. If it's in {KILL, QUARANTINE} only, **0.0x is the floor**.

Apply the floor pre-ceremony. The actual verdict label gets recorded on ceremony day. The strategy doesn't bleed in the meantime.

## When to apply

Apply when:
- Engine has been recommending the same direction for >24 hours
- Triple-confirmed: decision engine + drilldown + benchmark alpha
- Currently no open position OR position is bracket-protected at IBKR
- Reversible (allocation_factors are read on each cycle, not at boot)

Don't apply when:
- Verdict is actively shifting (e.g. PF compressed 1.34 → 1.06 in 24h)
- Strategy is just at threshold and one more session could flip it
- Logic change is needed (SCOPE_DOWN to a subset) — that's weekend work

## How to apply

```bash
# Effective KILL via sizing (most conservative)
curl -s -X POST http://localhost:8080/api/allocation_factors \
  -H "Content-Type: application/json" \
  -d '{"strategy": "forge_<NAME>", "factor": 0.0}'

# Effective REDUCE / SCOPE_DOWN floor
curl -s -X POST http://localhost:8080/api/allocation_factors \
  -H "Content-Type: application/json" \
  -d '{"strategy": "forge_<NAME>", "factor": 0.5}'
```

Reverse:
```bash
curl -s -X POST http://localhost:8080/api/allocation_factors \
  -d '{"strategy": "forge_<NAME>", "factor": 1.0}'
```

## What this is NOT

- **NOT a runner stop.** Process stays alive, heartbeats continue, operational vetting still applies. Just zero-sized entries.
- **NOT a ceremony verdict.** The formal verdict on 5/1 / 5/15 / 5/31 still happens. This is the conservative FLOOR while waiting for the formal record.
- **NOT terminal.** Single POST flips back. Audit trail in `argus_flow/logs/_risk/allocator_audit.jsonl`.

## Examples from 4/29-4/30 deploys

```
2026-04-30 03:03 UTC  forge_spy_mean_rev: 1.0 → 0.5  (44h consistent REDUCE recommendation)
2026-04-30 03:03 UTC  forge_multi_orb:    1.0 → 0.5  (cumulative -$73, REDUCE rec)
2026-04-30 14:30 UTC  forge_spy_mean_rev: 0.5 → 0.0  (44h+ stable; floor of {KILL})
```

Held back at 1.0x: forge_vix_intraday (verdict shifting, alpha lead +17.83pp).

## How to apply this memory

**Why:** waiting for ceremony day with the engine screaming for 44 hours straight = real capital bleed. The factor-floor pattern lets you act on data-supported signals without skipping the formal verdict process.

**How to apply:**
- When user asks "can we deploy this early?" — check if it's stable for >24 hours and triple-confirmed. If yes, deploy the floor; defer the formal verdict.
- If only single-source confirmation OR verdict is moving, hold at current factor.
- Don't apply factor changes to strategies whose verdict is shifting in the last 24-48h. The data is telling you to wait.
