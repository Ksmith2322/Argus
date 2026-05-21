---
name: $10K Funding Checklist — DEPRECATED 2026-04-20
description: Former funding-gate criteria. Superseded by paper-only focus per user_profile.md. Retained only for historical context — do NOT use these gates to drive promotion decisions.
type: project
originSessionId: 3a8e0a94-b591-4160-81af-20ab2d227878
---
## STATUS: DEPRECATED 2026-04-20

User explicitly dropped the $10K funding goal. Paper balance on DUP472829 is now the only success metric. Do NOT use any of the thresholds below to gate promotion, kill, or sizing decisions.

Reason: no external capital deployment is planned. Growing the paper anchor is the whole job.

What's still useful below: the **Strategy Lifecycle Response** order (observe → scale down → separate exit vs entry → lateral test → re-optimize under governance → family switch) is still sound guidance regardless of funding. Everything else is historical.

---

## [HISTORICAL — do not enforce] $10K Funding Gates

### Gate 1 — Runtime Integrity
- 0 unresolved reconciliation mismatches in last 30 live trades
- 0 duplicate fills/orders/ghost positions
- 0 feature-parity mismatches
- Kill/restart matrix passed, Reconnect matrix passed

### Gate 2 — Live Edge
- 60+ clean live trades
- PF >= 1.30
- Friction-adjusted expectancy >= +1.0 pip/trade
- No single trade > 25% of net PnL
- Rolling last-20 expectancy not deeply negative

### Gate 3 — Edge Mechanism
- P(reach +10) >= 50%
- Give-back after +10: caution >35%, hard stop >40%
- Realized PnL / MFE capture ratio: caution <0.25, hard stop <0.20

### Gate 4 — Drawdown
- Max closed-trade DD <= 8%
- Max peak-to-trough DD <= 10%
- No loss streak > 8 consecutive

### Gate 5 — Fleet
- EUR/USD fully validated (note: EUR/USD no longer in active cohort as of 2026-04-20)
- At least 1 additional pair validated
- Max 2 London-cluster pairs active
- Portfolio overlap controls active

## Strategy Lifecycle Response (still applicable)
1. Observe and classify degradation type
2. Scale down (preserve information)
3. Separate exit failure from entry failure
4. Look laterally (same edge, different instruments)
5. Re-optimize only under formal governance
6. Switch strategy family only if structural edge dies
