# Argus Bot — Comprehensive Audit (2026-05-25)

This is the final audit before the 7/1 real-money decision. Three
parallel agent reviews (skeptic / code-reviewer / inventory) +
live audit-suite snapshots. The findings here are deliberately
brutal — the operator asked for "everything and how to be better
in every way."

## Executive Summary

**The bot is an excellent _system_ wrapped around a strategy with no
demonstrable _edge_.** 51,819 LOC, 177 test files, 2286 passing tests,
50 audit CLIs, 125 dashboard endpoints, 41 panels, 27 documented
kills. The operational discipline is institutional-grade. The
disciplined gate has been honestly applied — even to the operator's
preferred strategies (PEAD, NQ overnight shelved).

But the trading IP is thin:

- Factor decomposition (5/19) says `forge_xs_momentum` has **zero
  alpha** (p=0.94 vs MKT+SMB+HML+MOM)
- Tonight's short-side sweep on the same universes: **0/4 survive**
  (PFs 0.47-0.86). Confirms the long-only "edge" is captured factor
  beta, not skill
- Cohort Sharpe 1.19 is **in-sample** to backtest construction
- Variant overlap is real: 5 of 6 active strategies ARE the
  xs_momentum engine on different universes; 3 of them currently
  hold the exact same picks (XLE, XLK, ±EEM)
- True independent engines: **2** (gld_pm_long + xs_momentum)

**Honest grade**: B+ for engineering, C for trading. Real money
should wait until live evidence (not backtest) shows IR > 0.3 vs
MTUM+SPY benchmark.

## Inventory Snapshot

| Metric | Count |
|---|---|
| Strategies in allocation_factors.json | 37 |
| **Active** (allocation > 0) | **7** (6 xs_momentum variants + gld_pm_long) |
| Killed (KILLED_STRATEGY_CUTOFFS) | 27 |
| Audit CLIs (ops/audit/*.py) | 50 |
| Dashboard endpoints | ~125 |
| Dashboard panels | 41 across 6 views |
| Test files | 177 |
| Tests passing | 2286 |
| Total LOC | 51,819 |
| Root-level docs | 18 |
| Commits in last 48h | 61 |

## Honest Grades by Area

| Area | Grade | Reason |
|---|---|---|
| Operational hardening | **B+** | PID locks, killed-strategy runtime invariant, fail-closed broker, IBC, watchdogs — but live runs keep finding races unit tests didn't catch |
| Test discipline | **B** | 2286 green, but heavy mock-bias; live entry-timeout race + EXIT cascade weren't pinned until they fired |
| Audit suite | **A-** | 50 CLIs covering every quantitative dimension; replay-harness shipped tonight closes the last big gap |
| Dashboard visibility | **A** | 125 endpoints, 41 panels, 6 role-based views; over-built relative to fleet size |
| Strategy diversity | **D** | "7 active" is misleading — 6 are xs_momentum variants. 3 hold identical picks today. True engines: 2 |
| Edge quality | **C+** | Disciplined gate honestly applied; surviving edges are real but only xs_momentum CI lower clears 1.20 with margin |
| Documentation | **A-** | 18 root MDs + 70+ memory entries form a forensic trail; volume risks outrunning reader bandwidth |
| Real-money readiness | **C** | All structural blockers shipped; live n still 0; 7/1+ go-live decision is defensible only if expectations are explicitly factor-beta |

## What the Skeptic Says (verbatim findings)

### Where is the alpha?

There isn't any verifiable alpha. The 5/19 factor decomposition was
explicit: `alpha ≈ 0, p = 0.94`. The 5/25 short-side sweep produces
**PF 0.47-0.86** with drawdowns 79-98% across every tested universe —
the signature of a long-only beta harvester, not skill.

The style-factor variant's CAGR is highest precisely because
MTUM/QUAL/VLUE *are* the factors the regression decomposes into.
**You are buying the momentum factor by selecting the MTUM ETF.**

The 1.19 cohort Sharpe is in-sample to backtest construction:
universes were selected for surviving the disciplined gate, then
pooled Sharpe computed across the survivors. No walk-forward
out-of-sample test exists.

### Variant correlation: not what it looks like

Variant returns correlate 0.07-0.62 *in normal months*. But in
correlated drawdowns (when SPY drops below 200dma — 53 of 228
months historically), every universe's DD explodes 25-35% → 50-66%
in the no-gate baseline. **All 6 momentum variants sell the same
names into the same losing factor at the same monthly close.** The
1.19 Sharpe assumes Gaussian-ish independence that vanishes
precisely when it matters.

Right now, **50% of fleet notional ($150K of $300K) is in just
XLE + XLK** across 3 "different" strategies that happen to pick the
same things from overlapping universes:

| Variant | XLE? | XLK? | Notes |
|---|---|---|---|
| sectors | yes | yes | SPDR sectors universe |
| legacy15 | yes | yes | 15-ticker mix includes sectors |
| legacy15_regime | yes | yes | same universe + regime gate |

### Capacity ceiling

`capacity_stress.max_safe_multiplier = 1.0` for every active strategy.
At $250K paper anchor, the fleet uses ~$300K notional (1.2× leverage).
Scaling to $1M real-money would breach cluster caps unless cluster_caps
also scale, but **slippage on $200K trade size** in MTUM/XLE/XLK is
15-30 bps — pushing 4 of 5 variants below their disciplined-gate CI
lower bounds. The strategy **does not have capacity for $1M**.

### Realistic real-money return expectations

Assuming factor beta survives and alpha = 0 (the honest base case):

- **$10K**: tracks US large-cap momentum factor ~+3% to -10% over 12 months
- **$100K**: same return path, ~0.7-0.9% commission drag
- **$1M**: breaches capacity; cannot deploy without raising caps + invalidating the disciplined gate scores

### The skeptic's bottom line

> "You've built world-class plumbing for a sink with no water."

## Code-Review Findings — Production Bugs

The code-reviewer agent inspected the active strategies and found
**3 CRITICAL + 4 HIGH + 3 MED + 1 LOW** real-money-readiness issues.

### CRITICAL — fix before real money

**C1: Regime gate fails OPEN on missing data**
- File: `forge/xs_momentum/runner.py:849-851`
- When SPY/VIX fetch fails, the runner logs "fail-OPEN" and proceeds.
  For real money this must fail CLOSED.
- **Fix**: refuse rebalance + record blocker on missing regime data.

**C2: State race between fill + state save**
- File: `forge/xs_momentum/runner.py:998-1138`
- If process crashes between `_submit_market_order()` returning a fill
  and `_save_state(state)` later in the function, the fill is in
  trades.csv but never recorded in state. Next cycle sees ghost
  position.
- **Fix**: dual-write to canonical_fills + state synchronously after
  every fill.

**C3: Regime gate data staleness on holidays**
- Files: `forge/xs_momentum/runner.py:845`, `forge/gld_pm_long/runner.py:221`
- yfinance can be 1+ day stale during holidays. Regime gate uses
  latest available, not "must be today."
- **Fix**: validate `(now - df.index[-1]) < 1_business_day`; refuse
  trade if stale.

### HIGH — fix before scaling

**H1: Variant config drift at runtime** — `runner.py:164-182`
configure_variant mutates module-level PARAMS. Process restart preserves
drift.

**H2: Order-of-operations** — `runner.py:814-832` allocation check
happens BEFORE regime gate; deallocated strategy can still skip exit on
bearish regime.

**H3: Missing _save_state in self-heal** — `gld_pm_long:248-266` self-heal
clears phantom but doesn't durably persist; loop forever on transient
errors.

**H4: GLD time-stop unguarded** — `gld_pm_long:292-317` if position held
>60d (against PARAMS contract), entry_idx falls before df range and
bars_held inflates incorrectly.

### MEDIUM (workable but flagged)

- M1: Regime-skip exit doesn't always save state
- M2: ATR computed on potentially stale yfinance data during TWS
  disconnect
- M3: Variant `top_pick_fraction` not logged at evaluate_once entry

### LOW

- L1: No file lock on state writes (concurrent runner protected by
  PID lock, but not the file itself)

## Operational State (live snapshot)

- 7 venv runners alive, all on commit `b520e4d`
- All data feeds GREEN
- Roster: 7 ACTIVE / 28 KILLED / 2 PENDING_OPT_IN
- Variant exposure: GREEN (no >30% single-ticker concentration)
- canonical_fills since 5/22 reset: **0**
- Order lifecycle: 0 lineages (no fills yet)
- Replay audit: empty (waiting for fills)
- Capacity headroom: 1.0× on every active strategy

## The Honest Verdict on 7/1 Real-Money Decision

**No, not yet.** Three reasons, prioritized:

1. **No measurable alpha.** Factor decomposition + short-side
   sweep both say so. You can get the same factor exposures from
   MTUM, VTV, USMV at 15bp expense ratios with zero infrastructure
   risk.

2. **Zero live evidence.** 0 canonical fills since reset. 30 days
   from now you'll have 20-40 trades from the monthly variants —
   statistically meaningless. Disciplined gate requires n=20 just
   to compute live PF.

3. **Operational fragility.** 4 CRITICAL/HIGH bugs in active
   strategies. Capacity stress = no headroom. Fail-OPEN regime
   gate is a real money disaster waiting.

### Defensible alternate path

- Continue paper-trading through 90 days post-reset (target: 8/20)
- Fix all CRITICAL + HIGH bugs from code-reviewer findings (~2-3 hours)
- Re-decompose alpha on **live** fills, not backtest
- Require live IR vs MTUM+SPY benchmark > 0.3 with t-stat > 2
- If hit, start $5-10K. If not, the strategy is honestly a factor
  harvester and real money doesn't add value

## How to be Better — Ranked Roadmap

Order by **impact ÷ effort**. Most items have already been built
out tonight; what remains is the strategic next moves.

### Tier 1 — Required before real money (~3 hours)

1. **Fix C1+C2+C3 from code review** (~2h). Real money cannot start
   with fail-OPEN regime gates, state races, or stale-data
   non-validation.
2. **Add benchmark tracking panel** (~1h). Dashboard should show
   live cumulative PnL vs SPY and MTUM. Current PnL attribution
   panel shows absolute; benchmark-relative is the honest metric.

### Tier 2 — Required to claim real edge (1-2 weeks)

3. **Tick-data ingestion pipeline** (1-2 weeks). Without intraday
   data we can't test microstructure strategies that might have
   actual alpha (order-flow, opening-range, volume-anomaly).
4. **Big-move dataset build** (3-5 days). Tag every >5% single-day
   move 2020-2026 with cause type. Foundation for event-driven
   strategies.
5. **Walk-forward out-of-sample testing** (1 day). Take all
   surviving universes, split 2005-2018 (train) and 2018-2026
   (test). Refuse to deploy any strategy whose OOS Sharpe <
   in-sample Sharpe × 0.5.

### Tier 3 — Lower-priority but improve quality (½ day each)

6. **Vol-targeted sizing** across cohort
7. **Auto-promotion pipeline** (gate pass → allocation flip with
   operator confirm)
8. **Tail-hedge sleeve** (small VIX-call / put-buy allocation —
   currently the HEDGE role is defined but empty)
9. **Cross-PC redundancy** (only if real-money fleet > $100K)

### Tier 4 — Operational polish

10. PnL attribution by sleeve TYPE (factor exposure attribution)
11. Live-vs-backtest tracking sparkline per strategy
12. Per-variant kill-switch in dashboard (operator can pause one
    variant without code change)

## What Would Make This Bot Impressive (Honest Answer)

Right now it's impressive at **process** and average at **trading**.
To flip that:

1. **One demonstrably skill-driven strategy** — not a factor harvester.
   Means: a strategy whose factor decomposition shows alpha p < 0.05
   AND whose short-side counterpart also makes money (asymmetric edge).
2. **Live evidence ≥ 6 months** showing live IR matching backtest
   IR within ±30%.
3. **A genuinely novel data input** — tick data, news sentiment,
   options skew, etc. — that's not in standard yfinance EOD.
4. **An open-source contribution** — the audit suite is genuinely
   reusable infrastructure. Releasing helio/promotion_panel or the
   replay harness as an open-source library would be substantive.

## Closing Note

The operator built world-class plumbing. The infrastructure is the
asset. The strategies will be replaceable; the system that gates
them honestly will not. **Continue building the funnel; refuse to
fall in love with any single strategy until it has independently
verifiable skill over a meaningful sample.**

The honest move from here is **defer the 7/1 real-money decision
to a date predicated on live evidence, not calendar**. If by 8/20
the live IR vs benchmark beats 0.3 with t-stat > 2, deploy $5K.
If not, the strategies are honest factor harvesters and real money
doesn't add value over buying the ETFs directly.

Either outcome is fine. The bot is in good enough shape to wait.
