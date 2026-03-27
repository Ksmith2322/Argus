# EUR/USD Live Validation Gate
# Status: ACTIVE — collecting live validation trades
# Config: S35/T30/TO75, session 8-14 UTC, NY blocked
# Created: 2026-03-26
# Research: CLOSED — 150/150 neighbor plateau confirmed

## Frozen Baseline Config

- Strategy: T4_full_stack
- Stop: 35 pips
- Target: 30 pips
- Timeout: 75 minutes
- Session: 8-14 UTC (NY blocked)
- Filters: range_pct_min=0.0012, range_accel_min=0.0, vol_z_min=0.0
- Direction: dist_long < 0.4 = long, dist_short > 0.6 = short
- Regime gate: LOG_ONLY (no hard gate)

## DO NOT MODIFY until live evidence gives a reason.

No stop changes. No timeout changes. No filter changes. No session changes.
The only allowed modifications during validation are:
- Bug fixes that don't change trade logic
- Infrastructure improvements (monitoring, logging)
- Risk guard calibration (portfolio-level, not strategy-level)

## 30-Trade Review (Triage Gate)

At 30 clean live trades, evaluate:

| Metric | Expected | Caution | Hard Stop |
|--------|----------|---------|-----------|
| P(reach +10) | >= 60% | < 50% | < 40% |
| Give-back +10 to <=0 | < 25% | > 35% | > 45% |
| Realized/MFE capture | > 0.30 | < 0.25 | < 0.18 |
| Timeout rate | ~65-75% | > 80% | > 85% |
| Short expectancy | > +3 pip | < +1.5 pip | <= 0 |
| Core session (8-14) exp | > +2 pip | < +1 pip | <= 0 |

### 30-trade decision:
- All green: CONTINUE to 60-trade gate
- Any caution: CONTINUE but flag for review
- Any hard stop: PAUSE and diagnose

## 60-Trade Promotion Gate (Funding Decision)

At 60 clean live trades, evaluate:

| Metric | Required for $10K funding |
|--------|--------------------------|
| PF | >= 1.30 |
| Friction-adjusted expectancy | >= +1.0 pip/trade |
| Max drawdown | <= 8% |
| Give-back after +10 | <= 35% |
| Realized/MFE capture | >= 0.25 |
| No single trade > 25% of net PnL | Yes |
| Rolling last-20 expectancy | Not deeply negative |
| Runtime integrity failures | Zero |
| At least 1 additional pair validated | Yes |

### 60-trade decision:
- All pass: PROMOTE to $10K funding (0.5% risk/trade)
- Any fail: HOLD at paper, diagnose specific failure

## Primary Early Warning

**Give-back rate after +10 pips**

This is the single most important live metric because:
- It directly tests the drift monetization mechanism
- It detects follow-through decay before aggregate PF collapses
- It is mechanistic, not just outcome-based

Historical baseline: ~20% give-back after +10 (from backtest)

## Research Evidence Summary (CLOSED)

- 150/150 neighborhood configs profitable (broad plateau)
- Walk-forward: positive 5/6 test periods
- Drift curve: classic real-edge shape, peak at 45-60 min
- Bootstrap: 89% P(expectancy > 0)
- Ablation: filters improve edge 2x (range_pct is key)
- Friction: survives up to 3 pips of drag
- Shorts robust across all conditions
- Longs viable only in morning+choppy
- NY session dead at all friction levels
- No single day/trade carries the result
- Parameters on broad plateau (PF 1.03-2.00 across all neighbors)
