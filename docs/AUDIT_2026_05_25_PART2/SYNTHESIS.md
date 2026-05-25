# Synthesis — Trade-Velocity & Learning-Rate Audit (2026-05-25 Part 2)

Reframing the 4-agent audit through the operator's actual goals:
**more trades, parallel info-gathering, faster learning, more overall.**
This document re-reads GREEDY.md / OUT_OF_BOX.md / CREATIVE.md /
STRATEGY.md against those four lenses and ranks the moves.

The original briefs were framed as "shorten timeline to real money."
That's a *related but secondary* question. The bottleneck is **evidence
velocity**, not calendar wait — once evidence accumulates the deployment
decision is mechanical.

## Headline ranking — the 5 moves with the most leverage per day of work

1. **Build `forge_overnight_drift_qqq`** (Strategy #1) — **single biggest
   trade-count multiplier**. ~250 trades/year per leg × 3 legs
   (SPY/QQQ/IWM) = ~750 trades/year. Gets to live n=20 in ~10 trading
   days instead of 2 years (monthly xs_momentum baseline). Ships in 3
   days, factor-orthogonal, uses data we already have.
   *Hits goals 1, 3, 4.*

2. **Opt in `forge_tom_spy` (0.3×) + `forge_nov_spy` (0.2×) tomorrow**
   (GREEDY #6). Both PENDING_OPT_IN, both gate-passed at 8-9/9 layers,
   *zero new code to write*. tom_spy fires monthly (~12 trades/yr at
   correlation 0.03 to xs_momentum — the cleanest diversifier on the
   bench). Doubles independent edges from 2 to 4 with one allocation
   edit. **Highest-ROI move on the table.**
   *Hits goals 1, 2, 4.*

3. **Pre-Trade Replay Bridge** (CREATIVE #1). Every live order gets
   replayed through `helio/replay_harness.py` before submission; mismatch
   blocks the order. Effectively **2× learning per fill** — each live
   trade simultaneously validates the strategy code AND surfaces
   live-vs-backtest drift in real time, not post-mortem.
   *Hits goals 2, 3.*

4. **Variant Consensus Sleeve** (CREATIVE #2). Build
   `forge_xs_momentum_consensus` that takes positions only when ≥3 of
   the 5 xs_momentum variants vote the same ticker. Zero new IBKR
   orders during paper validation; the existing 5 variants stay as
   "scouts." Naturally throttles concentration when agreement is thin
   — exactly the regime where correlated DDs hit. **Creates a 9th
   strategy from existing parts in <1 day.**
   *Hits goals 1, 4.*

5. **Two-Track Real Money** (OUT_OF_BOX #1). Operator buys MTUM + GLD
   + TLT at xs_momentum's intended weights in a personal brokerage
   account; bot keeps paper-trading. The daily delta is the bot's
   alpha contribution — settles the "is there real alpha?" question
   in 30 days with real money, no bot deployment risk.
   *Hits goals 2, 3.*

## Cross-cutting analysis by goal

### Goal 1 — More trades

Ranked by trades/year added at zero/low new code:

| Move | Trades/yr | Days to ship | Source |
|---|---|---|---|
| `forge_overnight_drift_qqq` (3 legs) | ~750 | 3 | Strategy #1 |
| `forge_xs_momentum_consensus` (meta) | ~12 monthly | 1 | Creative #2 |
| Opt-in tom_spy + nov_spy | ~13 | 0 | Greedy #6 |
| `forge_etf_pair_meanrev_xlk_xlf` | ~5 | 2 | Strategy #6 |
| `forge_turn_of_quarter` | ~4 | 1 | Strategy #2 |
| `forge_credit_spread_regime` | ~3 | 3 | Strategy #3 |
| `forge_sell_in_may_modulated` | ~2 | 1 | Strategy #7 |

**Aggregate if all shipped:** ~800 trades/year vs current ~50/year for the
8-strategy fleet. **16× trade-count multiplier in ≤10 days of work.**

### Goal 2 — Parallel info-gathering

Each move below runs *alongside* existing strategies without consuming
new capital, so they ADD evidence streams in parallel:

- **Pre-Trade Replay Bridge** (Creative #1) — paper + live + replay,
  three readings per fill
- **Pure-Alpha Pairs Sleeve** (Creative #5) — directly tests the
  zero-alpha hypothesis in paper without risking capital
- **Two-Track Real Money** (Out-of-Box #1) — real-ETF account runs in
  parallel to paper-Helio, generating direct alpha-contribution data
- **Subscribe-and-Execute** (Out-of-Box #4) — paper-trade an externally
  validated strategy (e.g. AllocateSmartly BAA) alongside the Helio
  fleet; compares the bot's strategies against a verified benchmark
- **`forge_overnight_drift_qqq` 3-leg structure** — SPY/QQQ/IWM each
  run independently, 3 parallel statistical samples on the same
  underlying premium

### Goal 3 — Speed up learning

The learning loop has three sub-loops; each move below tightens one:

| Sub-loop | Move | Acceleration |
|---|---|---|
| Strategy hypothesis → live n=20 | `forge_overnight_drift_qqq` | 24 months → 1 month |
| Live drift → operator action | Live-vs-Backtest Auto-Pause (Creative #7) | post-mortem → 5 trades |
| Code bug → real-money safe | Pre-Trade Replay Bridge (Creative #1) | post-incident → pre-fill |
| Hypothesis → falsified | Pure-Alpha Pairs Sleeve (Creative #5) | wait 90 days → 60 days w/ explicit answer |
| Edge discovery → deployed | Auto-Promotion Cooling-Off Queue (Creative #8) | manual → 24h |
| External edge → in fleet | Subscribe-and-Execute (Out-of-Box #4) | re-derive from scratch → import + paper |

### Goal 4 — Get more overall

Compounding from goals 1-3. The "more" comes in three dimensions:

- **More strategies live:** +6 (overnight_drift, consensus, tom_spy,
  nov_spy, TOQ, sell-in-May) in 1 week → 14 active vs 8 today
- **More independent edges:** today's audit says 2 true engines. After
  this sprint: 5+ (xs_momentum, gld_pm, overnight_drift, calendar
  cohort, credit-spread regime)
- **More data per trade:** replay-bridge + auto-pause turn each fill
  into 3 readings instead of 1

## Sequencing — what to actually ship, in what order

**Sprint 1 (days 1-3, max-leverage / zero capital risk):**

1. **Day 1 (now):** Opt in `forge_tom_spy` + `forge_nov_spy`. One edit
   to `allocation_factors.json` + one bump to v21. ~30 min.
2. **Day 1:** Ship `forge_xs_momentum_consensus` runner (no IBKR
   orders, just a virtual ledger). Validates the 6→consensus idea on
   paper before any capital. ~3 hours.
3. **Day 2-3:** Build + backtest `forge_overnight_drift_qqq`. Bootstrap
   CI + walk-forward H1/H2. If passes, ship at 0.25× paper allocation
   for the 30-day evidence window. ~2 days.

**Sprint 2 (days 4-7, infrastructure leverage):**

4. Pre-Trade Replay Bridge — wire `helio/replay_harness.py` into
   `submit_bracket`. ~4 hours + tests.
5. Live-vs-Backtest Auto-Pause cron — 5-min poll, 25bps drift threshold.
   ~3 hours + replay validation against the 5/19 CADJPY incident.
6. Auto-Promotion Cooling-Off Queue — wire the dormant
   `helio/auto_promotion.py` to a 24h queue + dashboard approve/reject.
   ~4 hours.

**Sprint 3 (days 8-14, second strategy wave):**

7. `forge_turn_of_quarter` (1 day — fork of tom_spy)
8. `forge_etf_pair_meanrev_xlk_xlf` (2 days)
9. `forge_credit_spread_regime` (3 days — new `helio/credit_regime.py`
   module + runner)

**Hold for later (after Sprint 2 produces evidence):**

- Pure-Alpha Pairs Sleeve (Creative #5) — high-information, but the
  short-MTUM leg has slippage cost that needs the replay bridge live
  first
- Subscribe-and-Execute (Out-of-Box #4) — only valuable as a
  benchmark; defer until at least one Sprint-1 strategy has 30 days
  of evidence to compare against
- PEAD-Filtered Momentum (Creative #3) — failed gate at 40 bps
  standalone; filtering shrinks n; lower priority
- Two-Track Real Money (Out-of-Box #1) — operator-side decision, not
  a code task

## What this synthesis explicitly REJECTS

- **Productize the audit suite** (Out-of-Box #3, #5, #7) — orthogonal to
  the trade-velocity goal. Worth considering as a separate business
  decision, but doesn't put more trades or more evidence into the bot.
- **Prop-funded eval** (Out-of-Box #2) — same. Could happen in parallel
  but isn't a trade-velocity move.
- **NQ-Overnight as GLD Co-Pilot Hedge** (Creative #4) — the rule is
  conditioned on rare events (SPY <−0.5% AND GLD position open), so n
  per year is tiny. Low evidence yield.
- **`forge_xs_low_vol_factor`** (Strategy #4) — monthly cadence, factor
  overlap with USMV ETF. Adds n=12/yr at high overlap cost.

## Real-money implication (secondary, not the goal)

If Sprint 1 + 2 ship as planned, by **day 30** the fleet has:
- 11+ active strategies
- 5+ independent edges with live evidence n≥10 each
- Real-time drift detection
- Pre-trade replay safety
- 24h cooling-off promotion queue

That state makes a defensible $5-10K real-money decision possible by
**June 25** — 8 weeks earlier than the comprehensive audit's recommended
8/20 conservative timeline. Not because we cheated the gate, but because
we accelerated the evidence by 16× via Sprint 1.

But the goal is the **evidence velocity**, not the calendar move. Even
if real money waits until 8/20 for psychological reasons, the fleet
will be a materially better research platform by then.
