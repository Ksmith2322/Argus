---
name: Hard Truths from Deep Analysis (2026-03-26)
description: Existential questions answered — edge source unknown, $100K target unrealistic with current approach, strategy may be simpler than labeled
type: project
---

## The $500 to $100K Reality
- Requires 2.12% compounded EVERY trading day for a year
- Current economics: ~0.1%/day at best = ~28.6% annualized
- Off by an order of magnitude from the target
- NOT achievable with current signal economics without destructive leverage
- Requires either: (a) much larger capital, (b) fundamentally stronger signal, (c) higher risk tolerance approaching ruin

## Strategy Identity Crisis
range_accel may not be a real signal. With accel_min=0 and vol_z_min=0/null, the actual strategy is:
- Session hours + range_pct > 0.0012 + dist_from_low direction
- Must run ablation test: does removing range_pct change results?
- If yes: range_pct is the signal. If no: session+location is the signal.
- Either way, rename honestly.

## Diversification Illusion
15 instruments but 1-2 strategy families = maybe 3 effective independent bets
- In stress, all collapse to same macro factor
- Fleet "diversification" is cross-sectional deployment, not independent alpha
- First principal component likely explains most bad-day variance

## Edge Source Unknown
No proven counterparty. Possible candidates:
- Stops/forced exits after range expansion
- Dealer inventory rebalancing
- Sessional order-flow imbalance
- Noise/reactive breakout traders entering late
Currently: "may be pattern-matching noise with a plausible story"

## Data Insufficiency
- 14 days EUR/USD = one market mood, not regime diversity
- MIDPOINT data removes spread = systematically upward bias for tiny edges
- FX and futures from different periods = cross-instrument comparison invalid
- Minimum for belief: 3-6 months. Current data: exploratory grade only.

## Immediate Action Items (from this analysis)
1. Run ablation test: session+dist_from_low vs full trigger chain
2. Measure fleet PnL correlation (daily + trade-level + bad-day-conditioned)
3. Recalibrate capital expectations with user — $100K target incompatible with current edge
4. Collect 3+ months of data before making capital decisions
5. Build drift-decay curve as soon as 75+ trades available

**Why:** Forces honest confrontation with whether signal exists, rather than optimizing infrastructure around an unproven hypothesis.
**How to apply:** When tempted to add more features/strategies/instruments, ask first: "Do we have evidence the existing signal is real?"
