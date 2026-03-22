# Argus Project Status Report — 2026-03-22

## Executive Summary

After extensive research, backtesting, and two complete strategy kill cycles, we have:
- **One viable strategy** (EUR/USD via IBKR) in paper trading
- **One validated-but-dormant system** (Argus Spot) protecting capital correctly
- **Two cleanly killed strategies** (Cascade, BTC spot precursors) with documented learnings
- **Full infrastructure** for rapid strategy development and testing

---

## System Status

### 1. Argus Spot (Crypto — Coinbase)
**Status:** Running passively. No trades. Governor correctly blocking.

**What we learned (41 backtests, all failed):**
- Current strategy is not profitable at Coinbase's 60bps fee tier
- 0/30 configs profitable across TP 6-12%, SL 1-2%, hold 2-8h
- Walk-forward: 0/8 windows profitable (PF 0.02-0.16)
- OOS validation: FAIL (12% WR, PF 0.08)
- Trendlines hurt performance (PF 0.05 vs 0.14 without)
- Governor is protective — missed signals would have lost money
- Would need 25bps fees ($50K+/month volume) to approach breakeven

**Decision:** Keep running for data collection only. No config changes. Not a viable profit center at current fee levels.

### 2. Cascade / BTC Spot Research
**Status:** CLOSED — Two clean kills.

**Kill 1 — Cascade (breakout + flow continuation):**
- Signal real (58% WR, directional bias confirmed)
- Payoff non-existent: only 4.4% of trades reach 30bps target
- 72.8% of trades timeout with no meaningful move
- Every stop/target combination deeply negative
- Structural: BTC spot doesn't move enough to cover fees

**Kill 2 — Displacement precursors (payoff-first):**
- 195 large displacement events found (75bps+ in 10min)
- 7/8 precursor features show WEAK separation from background
- Large BTC moves are essentially unpredictable from microstructure
- Conclusion: Kraken BTC spot is a bad research substrate

**Key learning:** Edge ≠ tradability. Directional bias ≠ monetizable edge.

### 3. EUR/USD FX Strategy (IBKR) — FIRST VIABLE CANDIDATE
**Status:** Paper trading runner live. Awaiting FX market open (Sunday 5pm ET).

**Strategy (T4 Full Stack):**
- Trigger: range_pct >= 0.0012 AND range_accel > 0 AND vol_z > 0 AND session 08-19 UTC
- Direction: long near session low, short near session high
- Stop: 20 pips | Target: 40 pips | Timeout: 60 minutes
- Frequency: ~8 signals/day

**Backtest results (33 days, 264 signals):**
- Win rate: 54.2%
- Expectancy: +0.98 pips/trade
- Total: +259 pips
- 15/54 configs viable (positive expectancy)
- Annual projection: ~1,983 pips (~$19,829 at mini lot)

**Stress test results (7/7 pass):**
| Test | Result |
|---|---|
| Entry delay (0-3 bars) | PASS — survives with 10.6% decay |
| Session breakdown | PASS — profitable in London, NY, US evening |
| Fee sensitivity | PASS — viable up to 1.0bps (IBKR is ~0.2bps) |
| Direction split | PASS — both long (+0.57p) and short (+1.66p) |
| Temporal stability | CAUTION — first half weak, second half strong |
| Parameter sensitivity | PASS — no cliff collapse |
| Drawdown | ACCEPTABLE — 8 max consec losses, -235p max DD |

**Improvement analysis (V2 candidate for later):**
- Session narrowing to 13-17 UTC: +1.47pip/trade (vs +0.98 base)
- Tighter range_pct (0.0015): +1.46pip/trade
- But fewer trades → lower total annual pips
- Decision: run V1 first for data, refine to V2 after 30+ live trades

---

## Infrastructure Built

### Reusable Research Pipeline
- Displacement event finder (any market/timeframe)
- Precursor extraction and contrast analysis (event vs background)
- Monotonic relationship checker
- Payoff test framework (stop/target/timeout simulation)
- 7-test stress suite (delay, session, fees, direction, temporal, parameters, drawdown)
- Edge classifier (DEAD / ILLUSION / WEAK_BUT_REAL / PROMISING)
- 9-sweep parameter grid runner

### Execution Infrastructure
- Kraken API client (connected, $50 balance)
- IBKR TWS Gateway adapter (connected, $1M paper account)
- EUR/USD paper trading runner with crash recovery
- Argus multi-coin runners (ETH, BTC on Coinbase)
- Dashboard with decision trace, penalty attribution

### Analysis Tools
- Penalty attribution analysis (Argus Spot)
- MFE/MAE analysis for missed signals
- Adversarial test suite (40+ tests designed, Tier 1 ready to build)

---

## Key Learnings

1. **Fee structure determines viability more than signal quality.** Coinbase 60bps kills everything. IBKR <1bps makes the same edge tradable.

2. **Payoff-first > signal-first.** Cascade spent weeks building signals that couldn't pay. EUR/USD started from "where do big moves happen?" and found a viable strategy in hours.

3. **Kill fast, kill clean.** Two strategies killed with full documentation. No ambiguity, no "one more tweak."

4. **The "payoff test" is the ultimate filter:**
   - Target reach rate must be meaningful (not 4%)
   - Timeout must not dominate (not 72%)
   - Losses must not arrive faster than gains
   - Distribution must have real tails

5. **Precursors matter more in structured markets.** BTC spot had zero useful precursors. EUR/USD had two STRONG and three MEDIUM — structured session behavior makes prediction possible.

---

## Next Step Options

### Option A — Monitor EUR/USD paper trading (LOW EFFORT)
Let the runner collect 30+ trades over the next 1-2 weeks. Validate that live results match backtest. This is the minimum — must happen regardless of what else we do.

### Option B — Add parallel strategies (MEDIUM EFFORT)
Multiple EUR/USD strategies running simultaneously on different configs:
- V1 (current): broad session, base filters
- V2: peak hours only (13-17 UTC), tighter range filter
- Short-only variant
- Different pair (GBP/USD, USD/JPY) with same logic

Each runs its own runner instance with separate clientId, logs, and state. Same IBKR gateway. Could deploy all Sunday evening.

### Option C — Additional FX pairs research (MEDIUM EFFORT)
Run the same payoff-first analysis on:
- GBP/USD (more volatile, wider spreads)
- USD/JPY (different session dynamics, yen-specific catalysts)
- EUR/GBP (pure European play, tight spreads)

Same pipeline — just swap the data. Could have results in a few hours per pair.

### Option D — Adversarial test suite (MEDIUM EFFORT)
Build Tier 1 of the 40+ adversarial tests:
- Flash crash injection
- Feed gaps
- Zero/extreme values
- Kill mid-trade recovery
- Volume anomalies

Hardens every system for production readiness.

### Option E — Argus Spot fee reduction path (LOW-MEDIUM EFFORT)
Investigate:
- Coinbase Pro/Advanced fee tiers with higher volume
- Alternative crypto exchanges with lower fees
- Whether $2,500 capital changes the math enough

### Option F — IBKR equities / TQQQ prep (FUTURE)
- Needs $25K for PDT (not available yet)
- Swing trading approach (1-3 day holds) avoids PDT
- Same payoff-first framework applicable

### Option G — Build dual-model collaboration (Phase 22C) (FUTURE)
- Streamlit app with Claude + OpenAI parallel analysis
- For difficult strategy decisions
- Not urgent until multiple live strategies running

---

## Recommended Priority

1. **Option A** (mandatory) — Let EUR/USD paper trade, collect data
2. **Option C** — Research additional FX pairs (highest ROI while waiting)
3. **Option B** — Deploy V2 in parallel if Option C finds more viable pairs
4. **Option D** — Adversarial testing when ready for production hardening

---

## Accounts & Access

| Platform | Account | Status | Capital |
|---|---|---|---|
| Coinbase | Active | Running (paper) | $500 per coin |
| Kraken | Active | Connected, research closed | $50 |
| IBKR | DUP472829 | Paper trading active | $1M paper |
| Polymarket | Waitlisted (#1M+) | Weeks/months | — |

## Active Runners

| Runner | Market | Status | Action |
|---|---|---|---|
| Argus ETH | Coinbase spot | Running, all WATCH_GATED | Passive data collection |
| Argus BTC | Coinbase spot | Running, all WATCH_GATED | Passive data collection |
| EUR/USD | IBKR paper | Running, awaiting market open | Active paper trading |