---
name: Decisive Test Protocol (ChatGPT synthesis 2026-03-26)
description: Statistical framework for kill/hold/promote decisions — minimum sample sizes, drift curve analysis, walk-forward validation, overfitting correction
type: project
---

## Core Finding
30 trades is a continuation gate, not a promotion gate. Need 75-100 for serious answer, 150-300+ for conviction on small-edge FX intraday.

## Strategy Identity
EUR/USD range_accel is NOT a target-hitting breakout system. It is a **bounded-horizon drift capture process**.
- 86% timeout rate IS the strategy
- Target is decorative tail capture (1.5% hit rate)
- The real edge (if any) is in the timeout exit distribution being net positive

## Drift Decay Curve Test
For each trade, compute PnL at 5/10/15/30/45/60/90 minutes after entry.
- Real edge: positive slope early, concave rise, plateau, then decay
- Fake edge: flat/noisy, no early separation, endpoint-dependent
- Dangerous shape: slow rise only becoming positive late = inventory bleed disguised as alpha

## Sample Size Requirements (n ≈ (1.96 * σ / μ)²)
- If mean edge = 0.5 pip, σ = 6 pips → need ~553 trades
- If mean edge = 1.0 pip, σ = 6 pips → need ~138 trades
- 30 trades: WR CI is 36%-72% (useless for conviction)

## Overfitting Risk
- 80-combo sweep: P(at least one false positive) ≈ 98.3%
- Best config is NOT evidence of stable optimum — it's a candidate for adversarial validation
- Must do walk-forward: 4-6 rolling folds, train/validate split, check parameter stability

## Why 1:1 Beat 1:2
NOT "1:1 is better than 1:2." Actually: wider stop (30 vs 20) + closer target (30 vs 40) + longer hold (90 vs 60) = the strategy needs breathing room and time, not asymmetric payoff.

## Regime Gate
Keep LOG_ONLY. GATE hurt because:
- Classifier may lag the trade horizon
- Strategy may already embed regime info via triggers
- Binary gating too blunt — prefer penalty/size reduction later
- Edge may live in regime transitions that get mislabeled

## Directional Logic
Not contradictory — but identity unclear. Is it:
- Expansion-exhaustion fade? (valid if dist_from_low predicts reversion)
- Expansion-continuation breakout? (valid if dist_from_low predicts momentum)
- Mixed? (need regime-conditional directional logic)
Test: conditional matrix of dist_from_low bins × regime × forward return

## Correlation Worst Case
3 USD-short + 3 JPY-short in flash crash:
- Clean stops: ~$60 loss
- Realistic slippage: $180-$370 (2.5-5% of fleet)
- Stop failure: $300-$600+ (4-8% of fleet)

## 7-Day Analysis Protocol
- Day 1: Truth-surface freeze, cohort sanitation
- Day 2: Path analysis, drift-decay curve, timeout anatomy
- Day 3: Parameter decomposition (local, not broad sweep)
- Day 4: Regime interaction study (continuous, not categorical)
- Day 5: Instrument triage (keep/hold/kill)
- Day 6: Execution reality stress test
- Day 7: Final decision memo per candidate

## The Honest Truth
"You do not need more systems to know whether EUR/USD range_accel works. You need a frozen config, a forward sample, conservative execution haircuts, and the discipline to kill it if the edge is not there."

**Why:** Forces evidence-based decisions instead of infrastructure-as-confidence-proxy.
**How to apply:** When user asks to add more features/infrastructure, check if the bottleneck is actually effect-size evidence, not engineering.
