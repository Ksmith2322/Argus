---
name: Decision-engine vocabulary — OBSERVE not IGNORE
description: When a strategy has insufficient sample (n<10), label the action OBSERVE, not IGNORE. Honest framing in the decision panel.
type: feedback
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
When the decision engine emits an action for a strategy with insufficient sample (n<10), label it **OBSERVE**, not IGNORE.

**Why:** "IGNORE" connotes "this strategy is irrelevant / not worth your attention." The actual meaning is "we don't have enough trades yet to make a decision." Those are different states with different implications. A strategy in OBSERVE may turn into a winner once it accumulates sample; a strategy mislabeled as IGNORE gets mentally written off. User flagged this 2026-04-28 in the dashboard review — the rename was applied across `ops/dashboard.py` (decision engine, action colors, summary counts) and `ops/auto_allocator.py` (factor map). Action vocabulary is now: SCALE_UP / HOLD / REDUCE / KILL / OBSERVE.

**How to apply:** Any time the decision-engine action set is extended or referenced in code, docs, dashboards, or memos, use OBSERVE for the "insufficient sample" bucket. Don't reintroduce IGNORE. If a future state is needed for "deliberately set aside / archived but not killed," use a new label (QUARANTINE, ARCHIVED) — don't recycle IGNORE.
