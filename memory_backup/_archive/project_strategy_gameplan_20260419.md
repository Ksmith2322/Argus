---
name: Strategy tuning game plan (2026-04-19)
description: Per-strategy next steps and evidence bars derived from 2026-04-19 review of the fleet
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
Evidence bar for every strategy: 10 trades = sanity, 30 = review, 60 = promotion. Do not tune until enough live/shadow trades exist to distinguish signal from noise.

**forge_gld_pm_long** — best near-term strategy. One live/paper win so far. Keep running, add per-hour live-vs-replay monitoring, judge only at 10/30/60.

**argus_gbpusd** — confidence is not low because it loses; it has no valid live evidence. Drift report shows ~65 triggers but 0 entries, mostly HOUR_FILTERED. First test: replay the exact live window with the current hour filter, then run an hour-filter ablation. If edge lives outside allowed hours, realign hours or demote.

**argus_usdjpy** — confidence low/medium but unstable. Only 2 valid promotion-gate trades, PF below gate, signal-frequency ratio ~0.14, only one regime observed. Run blocker attribution on MTF S/R and RSI gates. Shadow-test one relaxed variant against the frozen production config. Do not touch live gates until the shadow variant beats current config chronologically.

**argus_cadjpy** — be ruthless. Weak old-config evidence; current promotion evidence is absent. Keep paper-only, run MTF/AI-overlay blocker ablation, require a convincing walk-forward improvement before more risk.

**forge_jpy_pm_short** — research edge may be real, but live evidence is 1 losing trade. Exposure control first: separate USDJPY/CADJPY legs, cap correlated JPY shorts, evaluate by pair/session. Tune only after ≥20 live/shadow trades or a targeted replay of the current calendar window.

**forge_wick_gbpusd** — fix the stale heartbeat first. Too sparse to optimize quickly. Do not loosen filters to generate trades. Research sibling labels on lower timeframes or adjacent GBP pairs as separate strategies, each with walk-forward gates.

**forge_gdx_gld** — historical PnL attractive; live confidence low until fill-truth is clean. Canonical contamination fix landed 2026-04-19 (emit_canonical default False in backtest). Next: paper fill journal capturing both legs, slippage, borrow availability, hedge ratio, and cointegration stability. Historical backfill is not live promotion evidence.

**apollo** — research-only. Counterfactual labeling change is good. Wait for 100+ forward-return records, then score-band by 75-85 / 85-95 / 95+ and compare T+1/T+3/T+5. No execution path until T+3/T+5 survives that review.

**Why:** This plan was set after a 2026-04-19 code+state review. The bot is broker-connected, no killswitch or reset flags active, but the surface is not clean (stale forge_wick_gbpusd/ares/oracle, Argus canonical drift).

**How to apply:** Use this as the default tuning order. Do not skip the evidence bar. If user asks "what should I tune next," default to the highest-cadence strategy with clean evidence (currently gld_pm_long), not the one with the most alluring backtest PnL (gdx_gld).
