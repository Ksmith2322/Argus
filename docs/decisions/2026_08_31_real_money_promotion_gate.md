# Real-Money Promotion Gate — 2026-08-31

**Status**: COMMITTED (operator-authorized 2026-05-26)
**Supersedes**: `2026_06_30_argus_deployment.md` (date pushed; criteria sharpened)

## The gate, stated precisely

By **2026-08-31** at 23:59 UTC, if EITHER of the following two strategies meets ALL three conditions below, promote that strategy to a **$5,000 real-money smoke deployment** at **0.5% per-trade risk** on a NEW real-money IBKR account (not the paper account):

**Eligible strategies for the first real-money deployment**:
1. `forge_uso_pm_long` (intraday PM-window, ~22 fills/month projected)
2. `xs_momentum_sleeve` (combined 6 variants treated as one strategy)

**Four conditions** (ALL four must hold):

1. **Edge floor — two-tier**:
   - **MANDATORY** (fleet-combined): allocation-weighted fleet pre-tax CAGR must be ≥ **13.0%**. This is the floor the whole portfolio must clear. A strategy with sub-13% CAGR may stay in the fleet if its contribution to risk-adjusted *fleet* return is positive (e.g., `tail_hedge` reducing drawdown).
   - **ASPIRATIONAL** (per-strategy): each individual strategy ideally beats 13% pre-tax CAGR on its own. Strategies that fail this bar but pass the disciplined gate (PF + CI) are kept as kill-candidates flagged in every weekly vetting rollup. Kill no later than the next 30-day review unless they demonstrably lift fleet Sharpe / lower fleet drawdown enough to justify staying.

   Derivation of 13%: at the operator's ~37-39% short-term capital-gains marginal rate, a trading strategy needs pre-tax CAGR ≥ 13% just to match buying and holding SPY at the long-term capital-gains rate (SPY ~10% × (1 − 0.20 LTCG) = 8.0% net; required pre-tax = 8.0% / (1 − 0.39) ≈ 13.1%). Strategies below this floor lose to a brain-dead SPY hold once tax-adjusted, and don't earn their operator-attention cost.
2. **Cadence**: ≥30 live post-epoch (post-2026-05-22) EXIT fills accumulated in `argus_flow/logs/canonical_fills.jsonl` for that strategy / sleeve.
3. **Live-vs-backtest non-contradiction**: live PF bootstrap CI lower bound overlaps the backtest CI range from the BACKTEST_BASELINE table in `ops/audit/run_vetting_rollup.py`. The live PF doesn't need to be inside the CI — it just needs to not contradict it (live CI upper ≥ backtest CI lower).
4. **Operational integrity**: zero unexplained reconciliation breaks, zero phantom fills (single-trade PnL > $1,000 with no broker confirmation), zero ≥4-hour silent-failure incidents in the 30 calendar days preceding the promotion decision.

## What this gate is testing

This is **NOT an edge-validation gate**. The edge was already validated by 20-year backtests with walk-forward H1/H2 + bootstrap CI lower ≥ 1.20 at honest slippage. Mathematically, 30 live fills cannot statistically confirm an edge — at typical strategy Sharpe ratios you need years of live data for that, not weeks.

This **IS a plumbing-validation gate**. It tests:
- Orders route, fill, log, and reconcile without invention.
- Broker equity cache stays fresh through normal incidents.
- Sizing layer correctly refuses entries when broker is unreachable.
- Live slippage doesn't egregiously contradict the 5bps assumption.
- The operator can run the system for 6+ weeks without breaking discipline.

April-May 2026 produced 4+ distinct silent-failure incidents (phantom NQ fills, CBOT routing failure, EXIT FAILED cascades, broker-equity timeouts). 30 clean days post-cull are exactly what's needed to trust the infrastructure with real money.

## Per-strategy CAGR vs the 13% floor (as of 2026-05-26)

Backtest CAGRs translated against the after-tax SPY benchmark:

| Strategy | Backtest CAGR | Net after-tax | vs SPY (8% net) | Clears 13% floor? |
|---|---:|---:|---|:---:|
| `xs_momentum_style_top3` | ~22% | ~13% | beats by ~5pp | YES |
| `xs_momentum_style` | ~15-18% | ~9-11% | beats by ~1-3pp | YES |
| `xs_momentum_sleeve` (weighted) | ~14-16% | ~9-10% | beats marginally | YES (borderline) |
| `uso_pm_long` | ~10-15% | ~6-9% | ties / loses | BORDERLINE |
| `xs_momentum` baseline | ~10% | ~6% | loses by 2pp | NO |
| `xs_momentum sectors / legacy15 / global47` | ~10-12% | ~6-7% | loses or ties | NO |
| `tom_spy` | ~3% | ~2% | loses badly | NO |
| `gld_pm_long` | ~5% | ~3% | loses badly | NO |
| `tail_hedge` (regime-engaged) | ~5% | ~1-2% | loses (defensive role) | NO |

**The honest read**: by the after-tax SPY bar, only the xs_momentum sleeve (anchored by style + style_top3) and possibly uso_pm_long pass on backtest. The sleeve aggregate is what we'll measure live; individual variants will likely show similar ranking. Strategies that fail the 13% bar but stay in the fleet need an explicit justification beyond "passed PF/CI gate." For the v31 fleet:

- `tom_spy`: kill candidate at end-of-June review. Backtest CAGR ~3% can't justify operator attention.
- `gld_pm_long`: already at 0.1× post-cull, only justified by plumbing-validation role for the PM-intraday architecture. Auto-kill if live PF stays below 1.0 at end-of-June.
- `tail_hedge`: kept as hedge role (not measured on CAGR; measured on left-tail behavior).

## Why these two strategies first

`forge_uso_pm_long` is the only strategy that comfortably clears the 30-trades/month cadence floor (projected ~22/month per backtest = ~30-40 actual depending on signal hours engaged). It hits 30 cumulative live fills in roughly 7-10 weeks of continuous operation, putting the first eligibility date in early-to-mid July — comfortably before the 8/31 gate.

`xs_momentum_sleeve` fires only ~6/month combined, so cumulative 30 fills takes ~5 months. **It will most likely NOT qualify by 8/31** — that's expected. The sleeve becomes the second promotion candidate at the ~December 2026 review. We list it here so the gate is unambiguous about which strategies are in scope.

## What happens if neither qualifies by 8/31

Extend the gate to **2026-09-30** automatically. If neither qualifies by 9/30, halt the real-money initiative pending either:
- A new validated edge that passes the disciplined gate AND meets the cadence floor, OR
- An operator-authored memo explaining why the current strategies failed to qualify and what changes (if any) would restore confidence.

## What is NOT in scope for the 8/31 decision

- Adding new strategies to the active fleet. (No new shipping until 8/31 except research-track backtests.)
- Increasing existing allocations above their 2026-05-26 v31 values.
- Promoting the calendar plays parked in v31 cadence cull (nov_spy, ewz_breakout, ief_jul_hold, gld_jan_hold, uso_jun_hold, hyg_apr_hold). They stay killed for promotion purposes regardless of live performance.

## What the $5K real-money deployment specifically does NOT prove

- Whether the strategy compounds to a meaningful capital base.
- Whether 30%/year is achievable.
- Whether the operator should leave one of the W2 jobs.

It proves only: **live execution slippage matches paper assumption, infrastructure can hold real money safely**. That's the value of the smoke. Everything else is a separate decision at a later date with a different gate.

## Scaling ladder (post-smoke, illustrative only)

If the $5K smoke runs cleanly for 60 calendar days post-promotion, ladder up to:
- $10K (90 days clean)
- $25K (180 days clean)
- $100K (360 days clean + Sharpe live ≥ 0.7 confirmed)

Each ladder rung requires a fresh operator authorization. None are automatic.

## Operator commitments

- Run the vetting rollup weekly. Don't skip weeks.
- If cadence-check section flags 0 fills when expected, investigate within 24 hours.
- If FAIL_VS_FLOOR appears in summary, pause that strategy's allocation within 48 hours.
- Don't promote anything ad hoc. The gate is the gate. If criteria need changing, write a new dated decision doc; don't bend this one.

---

**Date authored**: 2026-05-26
**Author**: Argus operator + Claude session
**Next review**: 2026-08-31 (or earlier if uso_pm_long hits cadence first)
