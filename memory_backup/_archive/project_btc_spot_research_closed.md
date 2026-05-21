---
name: BTC Spot Research — CLOSED (2026-03-22)
description: Two clean negative results on Kraken BTC spot. Cascade (signal real, payoff non-existent) + Displacement precursors (payoff exists, precursors don't). No further iteration.
type: project
---

## Status: CLOSED — No further research on Kraken BTC spot

### Negative Result 1: Cascade (breakout + flow continuation)
- Signal real (58% WR, directional bias confirmed)
- Payoff non-existent (4.4% target reach, 72.8% timeout)
- No filter combination rescues it
- Structural kill — instrument doesn't move enough

### Negative Result 2: Displacement precursors (payoff-first)
- 195 large displacement events found (75bps+ in 10min, ~14/day)
- Precursor contrast: 7/8 features WEAK separation from background
- Only range_pct has MEDIUM signal (basically volatility clustering)
- No monotonic precursor→magnitude relationship except range_pct
- Large BTC moves are not forecastable from tested microstructure features

### Conclusion
Kraken BTC spot is a bad research substrate for:
- Short-horizon continuation strategies
- Microstructure-based precursor prediction

### What NOT to do
- Do not reopen with tweaked features
- Do not add external data sources as a rescue attempt
- Do not assume perps fixes the precursor problem

### Infrastructure preserved
All code in `argus_flow/` reusable for FX research:
- Displacement event finder
- Precursor extraction
- Contrast analysis (event vs background)
- Monotonic checker
- Full pipeline orchestrator
