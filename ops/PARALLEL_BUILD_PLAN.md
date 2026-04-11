# Parallel Build Plan — Burn-In Weeks

**Created:** 2026-04-11
**Context:** 5 systems in paper-trading burn-in. User wants to use the 4-week wait productively without disrupting live systems.

## TL;DR

After consulting 3 perspectives (skeptic, creative strategist, ops risk analyst), the consensus is:

- **Building during burn-in IS safe with strict guardrails** — primarily isolation from live infrastructure
- **The original Phase 5 list (Trendline Cascade, Options Wheel, Bond Rotation) is mediocre** — there are better, more measurable edges available
- **The biggest risk isn't broken code, it's broken focus** — burn-in review is sacred

## What All 3 Agents Agreed On

1. **Friday burn-in reviews are non-negotiable**. No building activity Thursday night or Friday until review is complete.
2. **New code must NOT touch shared infrastructure** (`helio/`, `fleet_risk`, `io_logs`, IBKR client IDs)
3. **Cognitive load is the silent killer**. Reduce build hours dramatically.
4. **The original Phase 5 list has weak edges** — Trendline Cascade is pattern matching, Options Wheel is "long stock with extra steps," Bond Rotation needs a defined signal
5. **Side income automation has the lowest contamination risk** because it shares zero infrastructure with trading

## Where They Disagreed

| Question | Skeptic | Creative | Ops |
|----------|---------|----------|-----|
| Build at all? | NO — build the Validation Charter review template instead | YES — 4 specific edges to build | YES with hard guardrails |
| Trendline Cascade? | REJECT — same family as killed Argus Cascade | REJECT — trendlines aren't measurable edge | DEFER — too much overlap with live code |
| Options Wheel? | REJECT — IV percentile is a screen, not edge | REJECT — paying theta buyers for gap risk | LAST priority if at all |
| Bond Rotation? | REJECT — "simple addition" is the scary phrase | DEFER — needs mechanical signal first | DEFER |

## The Final Plan (Synthesis)

### What I'm Killing From the Original Plan

**1. Trendline Cascade** — KILLED FOR NOW
- Two agents independently flagged it: "If you can't define trendline algorithmically, that's your answer"
- We already closed the original Argus Cascade as a structural kill
- Multi-timeframe trendlines on free data are notoriously curve-fit
- **Verdict:** Don't build the same family on different underlyings without a new hypothesis

**2. Options Wheel** — KILLED
- Skeptic: "post-2020 vol premium is fairly priced, will produce 6-8% annualized with fat left tail, not differentiated from owning SPY"
- Creative: "you're paying theta buyers to take gap risk you didn't price"
- Ops: "highest contamination risk — shares symbols with live Apollo"
- **Verdict:** The "edge" was a narrative, not a measurement

**3. Bond Rotation (TLT)** — DEFERRED
- All 3 agreed: needs a mechanical signal first, not a vibe
- "Simple addition" is exactly the scary phrase that misses bugs

### What I'm Building Instead (New Plan)

The creative agent surfaced 4 ideas that have **measurable, published edges** with proper academic/empirical backing. These are substantively better than the original list.

#### Build #1 — FOMC Drift (Week 1, ~2 days)
- **What:** Buy SPY at close day before FOMC, exit at 2pm ET on announcement day
- **Edge mechanism:** Risk-premium accumulation into uncertainty resolution. Lucca/Moench (NY Fed 2015) documented ~80% of SPX equity premium accrues in 24h pre-FOMC window
- **Frequency:** 8 trades/year
- **Expected:** 25-35 bps avg, ~60% hit rate, ~3-5% annual on deployed capital
- **Why this first:** Cheapest build, real published mechanism, zero correlation to existing fleet, zero infrastructure overlap
- **Build:** Backtest with yfinance + hardcoded FOMC dates, single Python file
- **Risk:** Tail event on announcement day (2008, March 2020)

#### Build #2 — GDX/GLD Pairs Trade (Week 1-2, ~2 days)
- **What:** Cointegration-based pairs trade between gold miners (GDX) and gold (GLD). Statistical z-score entry, mean revert
- **Edge mechanism:** GDX has embedded operational leverage on gold, ratio overshoots sentiment, mean-reverts on 10-30 day windows
- **Frequency:** 8-12 trades/year
- **Expected:** 6-10% annual on deployed capital, Sharpe ~1.0
- **Why this beats trendline cascade:** Same gold exposure, but **statistical, measurable, backtestable** instead of vibes-based
- **Build:** Engle-Granger cointegration test + rolling z-score + 2σ entry / 0 exit / 3σ stop
- **Risk:** Cointegration breaks (gold/miner divergence regime), need 2σ filter to confirm

#### Build #3 — Form 4 Cluster Buy Detection (Week 2-3, ~3-5 days)
- **What:** When 3+ insiders at the same company buy shares within 10 trading days, follow them long for 6 months
- **Edge mechanism:** Cohen/Malloy/Pomorski (2012) documented 10-15% alpha annualized on cluster buys (vs 1-3% for single insider buys = noise)
- **Frequency:** 15-25 signals/year across Russell 3000
- **Expected:** 8-12% alpha annualized on signal subset
- **Build:** SEC EDGAR Form 4 XML parser + cluster detection logic
- **Risk:** Overlaps with stock swing — should be a FILTER for Titan, not a parallel system
- **Note:** Could become a Titan enhancement instead of a standalone system

#### Build #4 — Turn-of-Month Effect on International ETFs (Week 3, ~half day)
- **What:** Buy EEM/EWJ/VGK in last-2/first-3 trading days of each month, exit
- **Edge mechanism:** Pension flow timing across jurisdictions hasn't equalized — US TOM is arbed out, international isn't yet
- **Frequency:** 12 trades/year per ETF
- **Expected:** 3-5% annual, low Sharpe but very low drawdown
- **Why:** Easy ship, totally uncorrelated, asymmetric to existing book
- **Build:** Calendar-based scheduling + simple buy/sell logic

### Optional — Side Income (No Contamination Risk)

If parallel trading builds get exhausted or burnout hits, switch lanes to non-trading automation:

- **Sports betting edge detection** — Same problem (mispriced probability), different domain. Free odds API, model-based picks on NHL totals or NBA props
- **eBay/Amazon arb scanner** — Lower margin but zero correlation to anything financial
- **Discord bot SaaS** — Trading signal bots, $20-50/mo recurring

These are listed but **not committed to**. They exist as backup tracks if you need a complete mental break from finance code.

## Build Order (Final)

**Week 1 of burn-in (now):**
- Day 1-2: FOMC Drift backtest + skeleton code
- Day 3-4: GDX/GLD Pairs cointegration test
- Day 5: REST. Don't touch anything Friday before review.
- Friday: Run burn-in review (4 hours, blocking)

**Week 2:**
- Day 1-2: Form 4 Cluster Buy parser (SEC EDGAR)
- Day 3-4: TOM Effect International ETFs
- Day 5: REST.
- Friday: Burn-in review.

**Week 3:**
- Day 1-3: Pick whichever Week 1-2 build looked most promising and harden it (add tests, document, prepare for paper deployment)
- Day 4-5: Side income exploration (optional)
- Friday: Burn-in review.

**Week 4:**
- All 4 new strategies fully backtested and reviewed
- Ready to deploy on burn-in completion (Week 5)
- Friday: FINAL burn-in review + decisions on which originals to keep/kill + deploy decision on new strategies

## The Hard Guardrails (Non-Negotiable)

These come from the ops risk analyst. We keep all of them.

### 1. Time Boxing
- **Max 10 hours/week** on new builds
- **Zero build hours** on Mondays (review the weekend's data) and Fridays (burn-in review)
- **No building past midnight** ever

### 2. Code Isolation
- **New strategies in new directories**: `forge/` for the new builds (FOMC, pairs, Form 4, TOM)
- **Zero imports** from `helio/`, `fleet_risk`, `io_logs`, `argus_flow/`
- **Copy-fork** any utility you need rather than import (reconcile after burn-in)
- **Separate git branch**: `forge/build` — never merge to `phase6-hardening` during burn-in
- **No edits to `.env` or shared config** during burn-in

### 3. IBKR Isolation
- **Reserved client ID range: 100-199** for all new builds
- All backtests use **historical data only** (no IBKR connection)
- If a new build needs paper testing, it gets a **separate paper account** (not DUP472829)

### 4. Stop Conditions (Pause Building Immediately)
- Any live system throws an unhandled exception
- Heartbeat staleness > 10 min on any system
- Fleet drawdown > 3%
- You catch yourself skipping a daily review
- Friday review reveals any Validation Charter red flag
- IBKR reconnect storm or client ID collision
- You're coding past midnight 2 nights in a row

### 5. Friday Review is Sacred
- No building activity Thursday night or Friday morning
- Run the full Validation Charter (19 questions) on the 5 live systems
- Sign off in writing before resuming any new build work
- If review reveals a problem, **PAUSE ALL NEW BUILDS** until it's diagnosed

## What I'm NOT Doing

- Not building 8 strategies in parallel (creative agent's 4 + original 3 + side income)
- Not touching the running 5 systems for any reason
- Not promoting any new strategy to live during burn-in
- Not making strategic decisions on <30 trades of evidence per system

## The Honest Tradeoffs

The skeptic was right that the most disciplined move is to build NOTHING and just stare at the data. I'm not going to do that because:

1. You explicitly want to keep building
2. The 4 new strategies have measurable mechanisms (not vibes like the original Phase 5 list)
3. The isolation guardrails make contamination unlikely
4. Friday review being blocking + Mon/Fri being build-free protects review quality

But the skeptic's warning is real: **the failure mode here is not building bad code, it's rushing the burn-in review.** The guardrail is "Friday review is blocking and signed off in writing." If you ever skip that, the whole plan blows up.

## Decision Point

When burn-in completes (Week 5), we have:
- **5 original systems** with 4 weeks of paper trading data — keep, kill, or tune each
- **4 new strategies** fully backtested in `forge/` — deploy whichever validated best in their backtests
- **Total fleet potential**: up to 9 strategies, but realistically 4-6 after kills

That's the real prize. Not "we built 8 things in 4 weeks." It's "we have 4 weeks of validation data on the originals AND 4 backtested candidates ready to deploy, all without disrupting the live fleet."

## Tomorrow's First Build: FOMC Drift

Cheapest, fastest, highest-confidence edge. Real published mechanism. Builds in ~2 hours. Backtest 20+ years of data. Zero infrastructure contamination.

If FOMC Drift validates in backtest at PF >1.5, we ship it after burn-in completes. If not, we kill it and move on. Either way, week 1 of build time is well spent.
