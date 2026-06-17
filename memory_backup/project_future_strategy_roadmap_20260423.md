---
name: future strategy roadmap — valuation / penny / dividend (post-5/1)
description: Three future strategy directions user outlined 2026-04-23. All deferred until after 5/1 review. Discovery-first framing: find the universe, then build execution.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Three future strategy directions

User's framing: **find the universe first, then decide when and how to trade it.** Don't build the executor before the discovery/ranker is proven.

### 1. Valuation + performance-based mean reversion (NEXT up after current monitoring)

**Goal:** Find under-valued, over-valued, under-performing, and over-performing stocks — look for corrections.

**Discovery phase first:**
- Screener that surfaces stocks by valuation percentile (P/E, P/B, EV/EBITDA) and performance percentile (1m/3m/6m returns vs peers/sector)
- Labeled universe: "over-valued + over-performing" (mean-revert SHORT candidates), "under-valued + under-performing" (mean-revert LONG candidates), etc.
- Historical validation: does membership in these buckets predict corrections?

**Execution phase (only after discovery works):**
- Entry/exit rules TBD
- Likely fundamental data needed (financials), which we don't currently pull

### 2. Penny stocks via Tim Sykes method (after #1)

**Goal:** Penny stock trading using Tim Sykes' published methodology.

**Typical Sykes patterns** (for memory — to cross-check if user describes one):
- First green day (bottomed stock has first up-close after multi-day drop)
- Morning panic dip-buy (oversold pre-market crash + reversal)
- Shorting pump-and-dumps (afternoon fade of promoted small-caps)
- Breakout over key resistance on volume

**Likely challenges to flag when we get there:**
- Penny-stock borrow availability (hard-to-borrow for shorts)
- IBKR data coverage for sub-$5 tickers
- Thin liquidity → slippage may eat the edge
- Will need custom universe (probably biggest 3-day % gainers filtered by market cap < $500M)

**2026-04-24 user addition:** "we need to add to look for large cap capital with little debt"
This is interesting because it cuts AGAINST the typical Sykes universe (which is heavy on
diluted, debt-laden small-caps). Two interpretations to resolve when we start building:
- **Interpretation A (separate strategy):** add a side-screener for large-cap + low-debt
  quality names, running alongside the Sykes penny pattern. Different universe entirely.
- **Interpretation B (quality filter on pennies):** keep the Sykes universe but FILTER to
  small-caps with strong balance sheets (low debt-to-cap, high cash reserves, possibly
  spinoffs of large-caps). Skip the pump-and-dump trash; trade only "real" small companies
  with capital strength.

User probably means (B) given the framing ("for the penny stock idea, we need to add..."),
but verify before building. (B) substantially changes the strategy — it'd be more of a
quality/value small-cap play than a momentum/pump-fade play.

**Data needed either way:** balance-sheet data (debt, cash, total capital) per ticker.
Not currently in our feeds — would be a data-layer project first (yfinance has some of
this; SEC EDGAR for fuller data).

### 3. Dividend edge (last, long horizon)

**Goal:** Find highest-paying, most reliable dividend stocks.

**Discovery axes:**
- Yield (forward dividend / price)
- Payout sustainability (payout ratio, FCF coverage, years of continuous payment)
- Growth (dividend CAGR, forward estimate)
- Reliability proxy: Dividend Aristocrats (25+ yr consecutive raises), Kings (50+)

## When to use this memory

**Why:** User explicitly said "all of this is later one [later on]" — they want this queued, not started. Current focus remains monitoring the 22-strategy fleet + 5/1 review.

**How to apply:**
- If user returns and says "let's start that next strategy" or similar, check this memory first to confirm WHICH of the three they mean (they likely mean #1, valuation)
- Do not volunteer to start any of these without explicit green light
- When #1 is greenlit, the first deliverable is a discovery script that ranks stocks — not a runner. Build the universe before the executor.
- If user starts describing a Sykes pattern out of context, match against the list above rather than asking them to re-explain
- Dividend work requires different data (fundamentals, payout history) not currently in our feeds — will be a data-layer project first
