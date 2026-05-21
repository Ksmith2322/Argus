---
name: 20-test confidence framework + sprint plan (2026-04-19)
description: Testing doctrine — 90% confidence in positive expectancy (not win rate), 20 high-value tests, per-strategy moves, custom strategy shortlist
type: project
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
**Target redefined.** Not "90% win rate" — **90% confidence that expectancy stays positive after costs, slippage, execution mistakes, and regime changes**. A 35-45% WR system with strong R:R can be excellent; a 90% WR system with fat-tailed losses can blow up.

**Honesty check (2026-04-19):** Mamba (~29 trades, ~24% WR, PF ~0.88) and Tori (~65 trades, ~29% WR, PF ~0.61) are **not** ready to be treated as high-confidence. The ideas may be fine; the current encoded versions don't capture the discretionary chart edge yet.

**Regulatory frame:** CFTC Rule 4.41 — simulated results don't represent actual trading. Investor.gov — backtests are hypothetical. Bailey/Lopez de Prado Deflated Sharpe — correct for selection bias when many variants are tested.

## The 20 Tests (order implies value, not sequence)
1. Live-Window Replay Parity — fix data timing / spread filters / hour filters / blockers before touching rules.
2. Signal Funnel — candidates → triggers → blocked → entries → valid → exits. Tune the stage bleeding the most edge.
3. Score Calibration — deciles must have monotonic forward expectancy, or the scorer is broken.
4. Parameter Stability Heatmap — edge must survive a plateau around current values, not one cliff.
5. Walk-Forward Frozen Params — promote only variants with positive expectancy across folds.
6. Purged/Embargoed CV — critical for Tori/Mamba/Apollo/Wick to catch leakage.
7. Multiple-Testing Penalty / Deflated Sharpe — more variants tried → higher raw PF required.
8. Monte Carlo Trade-Order Shuffle — 10k shuffles for drawdown/ruin risk.
9. Slippage + Commission Stress — 1x/2x/3x costs; if PF dies at 2x, rework execution or reject.
10. Regime Split — vol/trend/chop/VIX/rates/CPI/Fed/earnings/session. Regime-router out the negative cells.
11. Entry Ablation — remove one condition at a time.
12. Exit-Only Optimization — freeze entries, tune stops/targets/time/trail/partials (huge for Mamba/Tori).
13. Time-of-Day / Day-of-Week — whitelist positive windows only.
14. Forward Paper Shadow — would-have-filled vs actual-fillable; if paper beats reality, lower confidence.
15. Chart-to-Code Label Test — 200 human labels vs code; disagreements are new feature candidates.
16. Mamba 1-Minute Truth — stop synthesizing from 5m; use real 1m NQ/MNQ.
17. Tori Setup-Type Split — break vs bounce vs break_retest, A vs A+, by instrument + slope/spacing.
18. Portfolio Correlation — simultaneous exposure across systems expressing the same risk.
19. Monthly Trade-Frequency Capacity — don't force sparse strategies to trade more; build a basket to reach 60+/month.
20. Kill/Promote Simulation — replay historical through governance rules.

## Per-strategy moves (extends project_strategy_gameplan_20260419.md)
- **GLD PM Long** — add per-hour + per-regime PF. Narrow to working hours. Consider GLD/SLV/GDX siblings.
- **GDX/GLD** — execution truth first: hedge ratio, borrow availability, one-leg fill risk, cointegration stability.
- **Argus GBPUSD** — hour-filter + live/replay parity. Signals exist; entries eaten. No more risk until funnel solved.
- **Argus USDJPY** — MTF strictness, S/R proximity, session concentration. Prefer slower cadence over relaxed gates.
- **CADJPY** — research-only until fresh walk-forward + blocker ablation.
- **JPY PM Short** — pair-by-pair; USDJPY and CADJPY may be one JPY macro bet in disguise.
- **Wick GBPUSD** — don't loosen; clone to EURUSD/AUDUSD/GBPJPY/4H as **separate** candidates.
- **Apollo** — keep collecting forward returns; at 100+ records decide by score bucket and horizon (T+3 likely first candidate).
- **Oracle (Polymarket) — PAUSED 2026-04-19.** Polymarket is geoblocked for US users, no execution path. Removed from `run_cohort_report.ps1` (commented, reversible) and from `helio/fleet_monitor.py` SYSTEMS dict. Do not resurrect as a Polymarket trader. If user brings it up again, the only viable path is a Kalshi port (CFTC-regulated, US-legal). Code kept in `oracle/` for that scenario.
- **Mamba** — rebuild around real 1m, ORB, VWAP, sweep/reclaim, structure confirm, time stop. Fewer, cleaner sniper trades.
- **Tori** — split by setup/grade; likely keeps only A+ retests or A+ breaks with strong safety-line geometry.

## Custom candidates (prioritized for 60+/month basket)
1. NQ/MNQ ORB + VWAP Reclaim (9:30–10:30 ET, real 1m, strict stop)
2. Liquidity Sweep Reversal (prior H/L sweep + reclaim + volume + structure)
3. VWAP Trend-Day Pullback (price holds VWAP, HL/LH confirms)
4. FX London/NY Session Breakout (EURUSD/GBPUSD/USDJPY box breakout/retest)
5. GLD Late-Day Continuation Family (extend to SLV/GDX/GC/MGC)
6. NQ Overnight Mean Reversion / Gap Fill (VIX/regime filtered)
7. Pair Mean Reversion Expansion (SLV/GLD, QQQ/SPY, XLE/USO)

## Recommended next sprint
1. **Build strategy confidence scoreboard** — per strategy: live/paper trades, PF, expectancy, P(expectancy>0), signal freq, drift, regime coverage, blocker funnel.
2. **Run 5 tests first** — #1 Replay Parity, #2 Signal Funnel, #3 Score Calibration, #12 Exit-Only, #15 Chart-to-Code.
3. **Rebuild Mamba/Tori as label-first** — Mamba = "NQ liquidity sweep / VWAP / ORB"; Tori = "A+ trendline retest with safety-line geometry".

**Why:** Framework set on 2026-04-19 after cleanup sprint landed. Regulator-aware (CFTC 4.41, Investor.gov, Deflated Sharpe) and anti-overfit by design.

**How to apply:** Before any strategy change, ask "which of the 20 tests would catch whether this is real edge or noise?" Don't ship changes that haven't passed at least the parity + funnel + calibration trio. The sprint's confidence scoreboard is the default build target unless user redirects.
