---
name: 90/100 Confidence Roadmap
description: Agent debate results — prioritized list of everything needed to get Argus to 9/10 across strategy, risk, operations
type: project
---

## Context
Agent-to-agent debate (2026-03-25) produced 20-item prioritized roadmap. Current confidence: 25/100 (infra 90, strategy 10).

## Implemented 2026-03-25
- Regime classifier (TRENDING/RANGING/CHOPPY) via Kaufman efficiency ratio + linear regression slope
- Regime gating in LOG_ONLY mode (stamps all signals, config-switchable to GATE via `"regime_gate": "GATE"`)
- Asset fitness scorer (`python -m argus_flow.ops.asset_fitness`)
- 15-instrument 24hr coverage (8 FX + 7 futures, two runners on separate clientIds)
- Futures switched from vol_burst to range_accel strategy
- Wrap-around session support (22-08 UTC for Asia pairs)
- Pool-based position sizing in generate_live_config.py

## Priority Queue (next sessions)
1. **Backtest pipeline for argus_flow** (16-20hr, +25 confidence) — BIGGEST UNLOCK. Download IBKR historical, replay through exact feature/trigger logic
2. **Correlation guard** (6hr, +10) — max 3 positions same-currency-side. BLOCKER FOR LIVE.
3. **ATR-scaled stops/targets** (4hr, +7) — replace fixed pip stops
4. **Drawdown circuit breaker** (3hr, +5) — pause all entries at -3% equity. BLOCKER FOR LIVE.
5. **Parameter sweep** (8hr, +10) — grid search per instrument via backtest. BLOCKED BY #1.
6. **Fix directional logic** (4hr, +5) — dist_from_low mean-reversion contradicts range_accel breakout. BLOCKED BY #1.
7. **Strategy diversity** (16hr, +8) — wire FVG/momentum. Regime selects strategy.

## Future Strategy Families (post-FX mastery)
8. **Trendline cascade strategy — Gold & Crude** (+10) — Multi-TF price action method (Tori's approach): monthly→weekly→daily trendline fan, cascade point A/B anchors, trade wedge breakouts at convergence zones. Instruments: MGC (micro gold, COMEX), MCL (micro crude, NYMEX). Requires: swing-point hierarchy builder, trendline validity scoring (touches, age, non-intersection), breakout/retest entry logic. This is trend-following with multi-day holds — different family from current mean-reversion/burst strategies. GATED BY: proving current FX fleet profitable first.

## Key Debate Findings
- 86% timeout rate = strategy isn't finding decisive entries. Root: untested params + no regime filter + directional contradiction
- 8 FX pairs can stack 5-6 positions same side of USD — catastrophic if dollar moves against
- Fixed pip stops wrong across instruments — EURUSD (8-pip range) vs GBPJPY (40-pip range) need different stops
- Honest timeline to 90/100: 3-4 weeks (1 week build, 2-3 weeks data validation)

**Why:** User target is 90/100 in 1 week. Framework can be built in a week; statistical validation needs trade data accumulation.
**How to apply:** Start each session by checking this list. Build backtest pipeline first — it unlocks items 5, 6, 7.
