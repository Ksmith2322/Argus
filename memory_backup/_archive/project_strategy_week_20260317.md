---
name: Strategy completion week plan
description: Week of 2026-03-17 strategy finalization plan - massive backtesting, feature additions, coin rotation system
type: project
---

Strategy completion week starting 2026-03-17.

**Phase A (overnight 3/17):** 688 backtests across 2 machines — 23 coins, param sweeps, combo grids, exit models, confluence weights, vol sizing. Results by morning.

**Phase B (3/18):** Assess results → identify PF ceiling of current strategy → add features that raise that ceiling → retest.

**Phase C (3/18-19):** 45-day recent-data batches for validation. Finalize tradeable coin list (TRADE / WATCH / SKIP tiers).

**Phase D (3/19-20):** Coin health-check system via Task Scheduler — auto-rotate coins in/out based on rolling PF. Proves coins earn their slot.

**Phase E (later):** Automatic scanning for new crypto opportunities. Watchlist pipeline.

**Why:** User wants strategy completed this week. 2-3K backtests/day throughput is the edge — iterate fast on features + params. Speed of testing > perfection of any single test.

**How to apply:** Bias toward shipping features fast and testing them, not overthinking. Let data decide. Build coin rotation infra early so strategy self-corrects over time.
