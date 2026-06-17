---
name: Sliding window position sizing idea
description: User wants adaptive position sizing based on which strategy/timeframe is performing better — confidence-weighted allocation
type: project
---

User proposed a sliding window position size approach: whichever strategy or timeframe is more successful gets a larger position allocation. Essentially a confidence-weighted sizing system.

**Why:** Not all signals are equal. If 4hr trendline entries are outperforming 1m confluence entries in recent history, they should get more capital.

**How to apply:** Implement after filter optimization (MIN_SCORE + regime gate) is validated. Could track rolling WR/PF per signal source over last N trades and scale position size proportionally. Phase 19 Priority 4 or later.
