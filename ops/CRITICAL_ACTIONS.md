# Critical Actions — What Actually Moves Returns

**Date:** 2026-04-15
**Source:** Deep portfolio-level analysis of the full repo
**Core insight:** The biggest return limiter is NOT missing stocks or strategies. It's under-deployment of proven edges and structural long-only bias.

## The Three Things That Actually Move Returns

### 1. PROMOTE PROVEN EDGES (highest impact)
Stop leaving Tier 1 strategies in research mode.

| Strategy | PF | Status Now | Action |
|----------|-----|-----------|--------|
| VIX Revert | 2.40 | Built, not deployed | PROMOTE to paper execution |
| Index Rebalance | 6.65 | Built, not deployed | PROMOTE to paper execution |
| Atlas Event Executor | Data-validated | Built, not deployed | PROMOTE cascade trades |
| Ares | 1.35 | Reporting-only | PROMOTE to small live allocation |
| Themis Cluster | Tracking | Signal-only | PROMOTE top signals to execution |

### 2. ADD DOWNSIDE SLEEVE (biggest structural gap)
The fleet is structurally long-only:
- Titan: `long_only = True`
- Ares: long-or-cash, never short
- No system profits from falling markets

**Build:** A dedicated short/downside momentum system that activates when Atlas regime = RISK_OFF/CRISIS. Short breakdowns, not just stop trading.

### 3. ACTIVATE CONVICTION SIZING (already built, not used)
The conviction scorer exists and works (tested: MSFT 1.5x HIGH) but every runner still treats it as LOG_ONLY. The actual sizing multiplier is never applied to trade size.

**Risk:** If conviction model is wrong, you size up bad trades. Only activate after validation.
**Action:** Run conviction in shadow mode for 2 weeks, compare "would-have" sizing vs actual results. If it improves Sharpe, flip to live.

---

## What NOT To Do

1. **Don't add more stock symbols.** Universe breadth is no longer the bottleneck.
2. **Don't loosen Apollo.** Pre-earnings, run-up, and small-gap variants are negative EV per position_rules.py.
3. **Don't deploy Tier 3 at size.** Mamba (PF 0.68), Tori (PF 0.61), Cue Banks (PF 0.89) stay in signal-only until they prove PF > 1.2.
4. **Don't spread capital evenly.** Concentrate on the 4-5 proven edges, not all 13.
5. **Don't turn on conviction sizing before validating it.**

---

## The 4 Core Alpha Types We Need

| Type | Current Coverage | Gap |
|------|-----------------|-----|
| Event-driven | Apollo (earnings drift) | Covered |
| Trend/momentum | Titan (long-only swing) + Argus FX | Missing: short side |
| Crisis/downside | NOTHING | **Build a short equity sleeve** |
| Relative-value/hedge | GDX/GLD + VIX + Index Rebalance + Sector Rotation | Covered BUT not deployed |

---

## Friday Action Plan (Revised Priority)

### HIGH IMPACT (do first)
1. Promote VIX Revert to paper execution (1 day)
2. Promote Index Rebalance to paper execution (1 day)
3. Wire Atlas Event Executor to paper execution on cascade trades
4. Build downside/short equity sleeve (2-3 days)
5. Strip PDT constraints from Titan/Apollo/Hermes

### MEDIUM IMPACT (do second)
6. Wire Titan earnings/news filters into actual entry path
7. Activate conviction sizing in shadow mode (track what it WOULD do)
8. Make Argus pair-specific (timeout, side bias per CRITICAL_REVIEW.md)
9. Promote Ares to small live allocation
10. Set up IBC for auto-login

### INFRASTRUCTURE (do third)
11. Stamp signal price on every strategy for real slippage tracking
12. Unify drawdown recovery across fleet
13. Make position aging enforce capital recycling
14. Build auto-pruning (weak regimes/hours auto-throttle)
15. Optimize exits by instrument (MFE/MAE-driven)

---

## The Honest Truth

> "If I were building this to compete with top performers, I would build a portfolio machine, not a collection of smart bots."

The fleet has **too many ideas at low allocation** instead of **fewer ideas at high conviction**. The path to real returns:

1. Concentrate capital on the 4-5 strategies with PF > 1.3
2. Add the one missing regime sleeve (downside/short)
3. Size based on conviction (once validated)
4. Let everything else stay in research until it earns promotion

**The allocator is the brain, not the signal generators.**
