---
name: Phase 2 — instrument compatibility sweep on surviving strategies
description: Post-5/31 plan. Once the fleet is culled to confirmed winners, run each pair-agnostic survivor against a universe of comparable instruments and add the top-ranked compatible names. Distinct from the strategy freeze.
type: project
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## When this kicks in

After 2026-05-31 strategy freeze. Phase ordering:

- **Phase 1 (now → 5/31):** observe + cull. Verdict per strategy: WINNER / REWORK / KILL.
- **Phase 2 (post-5/31):** for each WINNER, sweep instrument compatibility.
- **Phase 3 (later):** real money, scaled per tier.

Phase 2 is **NOT** new strategy work. The strategy freeze rules out NEW strategies. Expanding instruments on existing winners is "scaling what works" — different operation, allowed.

## Concept

Most forge runners hardcode their instrument list. That choice was usually intuition + a backtest on the original target. After we know a strategy has edge, the question becomes: what *other* liquid instruments respond profitably to this same signal logic?

Pattern: sweep the strategy's signal/entry logic against a curated universe of comparable instruments, rank by PF (or by P(expectancy>0) since the project already prefers that), keep the top N that meet a liquidity floor and aren't redundantly correlated.

## Which strategies are even sweepable

**Pair-agnostic (sweep candidates):**
- `forge_vix_intraday` (UVXY) → sweep VXX, SVXY, VIXY, TVIX-equivalents
- `forge_multi_orb` (SPY/QQQ/IWM/GLD) → sweep DIA, EEM, IWM-already-in, sector ETFs
- `forge_spy_mean_rev` (SPY) → sweep QQQ, IWM, SOXX, XLK, etc.
- `forge_wick_gbpusd` (GBPUSD) → sweep major FX pairs
- `forge_nq_overnight` (MNQ) → sweep MES, MYM, M2K
- `forge_aud_asian_breakout` (AUDUSD) → sweep NZDUSD, AUDJPY

**Pair / relationship strategies (NOT sweepable):**
- `forge_gdx_gld` — inherently a pair, signal IS the spread
- `forge_jpy_pm_short` — already multi-pair (USDJPY+CADJPY)

**Event-driven (NOT sweepable, expand events instead):**
- `forge_fomc_drift` — sweep doesn't apply (event = FOMC)
- `forge_tom_international` — already expanded 3→6 ETFs (4/26)
- `forge_rebalance` — could expand to S&P deletions or Russell adds

**Strategy-specific scanners (different question):**
- `apollo` (earnings catalyst) — universe is already broad (113 tickers); the question for apollo is gate threshold, not universe expansion
- `hermes` (gap fill) — same; universe is already the whole liquid stock list
- `titan` (trend) — same

## Prerequisites before sweeping

1. **Liquidity filter:** drop any instrument below a minimum ADV ($X/day) or bid-ask spread above Y%. Paper results on illiquid instruments produce false confidence — IBKR fills won't match in real markets.
2. **Correlation prune:** if instrument B has > 0.85 correlation to A on the 90d window, adding B doesn't add edge — it concentrates risk. Keep one of the two.
3. **Out-of-sample discipline:** sweep on a training window (2026-01 to 2026-04), freeze top N, evaluate on 2026-05+ as held-out. Skipping this = data-mining noise into "discoveries."
4. **Risk-budget rebalance:** if `vix_intraday` extends from 1 instrument to 4, total notional exposure can balloon unless `NOTIONAL_FRACTION_MULTIPLIER` is rescaled (we did this for tom_international 3→6 with multiplier 100→50 on 4/26).

## Existing infrastructure to build on

- `argus_flow/configs/discovery_fx_universe.json` — discovery-mode FX universe stub. Pattern of running a strategy against a universe likely partially exists. Audit before building new infra.
- `argus_flow/ops/weekly_pair_onboarding.py` — already has the pattern of evaluating a candidate pair, freezing if it passes thresholds.
- `helio/promotion_readiness.py` — promotion gate checks that should also gate "new instrument added to existing winner."

## How to apply this memory

**Why:** when 5/31 hits and we shift from explore-mode to scale-mode, the natural question is "what else does X work on?" This is the playbook for that.

**How to apply:**
- Don't start phase 2 work before 5/31 cull is complete. The cull tells us which strategies are even worth expanding.
- For each WINNER from the 5/31 review, classify as sweepable / non-sweepable per the lists above.
- Don't sweep a strategy that needs a structural fix (REWORK verdict) first; fix the strategy, then sweep.
- Cap phase 2 at top 3-5 instruments per winner. The point is concentration on what works, not breadth for breadth's sake.
- Re-audit fleet_sizing.json risk budgets after expansion — total exposure can creep up silently when N instruments doubles per strategy.
