# Argus Fleet Doctrine
# Created: 2026-03-27
# Authority: This document governs pair status, promotion, and kill decisions.

## Pair Status Labels (exactly three)

### ANCHOR
- Research-closed, frozen config, validated across full battery
- Counts toward fleet return targets and portfolio heat planning
- Still subject to live drift monitoring — NOT immune to audit
- Current: EUR/USD (S35/T30/TO75, 8-14 UTC)

### PROBATIONARY
- Promising but not yet fully proven in live
- Counts toward fleet return targets at REDUCED weight
- Frozen config, explicit cohort tag, fixed risk cap
- No tuning during observation window
- Current: GBP/USD (S30/T50/TO90, 8-20 UTC) — neighborhood confirmed 125/125 profitable, mean PF 2.09

### QUARANTINED
- Known fragility, insufficient robustness, or unproven
- Does NOT count toward fleet return targets
- Does NOT influence sizing logic or confidence scoring
- Shadow/paper only unless explicitly collecting behavioral evidence
- Current: USD/JPY (concentration fragility — 3 trades carry result)

## Pairs NOT in any status are UNLABELED INVENTORY
- Not future contributors until they earn a label
- EURJPY, GBPJPY, CADJPY, AUDJPY, AUDUSD = unlabeled
- All futures (MES, MNQ, etc.) = unlabeled

## Promotion Gates (Quarantined → Probationary)

ALL must pass:
- Full 10-test battery completed
- Neighborhood shows broad plateau (>80% neighbors profitable)
- Post-friction expectancy positive
- Remove-best-3 still positive (concentration test)
- Walk-forward positive in majority of periods
- No single regime/session carrying >70% of PnL without design intent

## Promotion Gates (Probationary → Anchor)

ALL must pass:
- 30+ clean live trades
- Live PF >= 1.20
- Live expectancy positive after friction
- Live give-back rate after +10 <= 40%
- No live concentration issues
- Runtime integrity clean
- Passes all anchor funding gates from EURUSD_LIVE_VALIDATION_GATE.md

## Kill / Demotion Rules

### Kill (remove from fleet entirely):
- Negative expectancy after full battery AND narrow neighborhood
- Consistently negative across all walk-forward periods
- No identifiable conditional sub-edge worth restricting to

### Demote (Probationary → Quarantined):
- Live expectancy persistently negative over 20+ trades
- Concentration stress fails (remove-best-3 negative)
- Single regime/session carrying result without design intent

### Constrain (keep but restrict):
- Edge exists but only in specific regime/session/side
- Deploy ONLY in the constrained context
- Document constraint explicitly

## Fleet Accounting Rules

- Only ANCHOR + PROBATIONARY pairs count toward fleet return targets
- Only ANCHOR + PROBATIONARY pairs count toward portfolio heat planning
- QUARANTINED pairs are excluded from all fleet metrics
- Sizing roadmap assumptions use only promoted pairs
