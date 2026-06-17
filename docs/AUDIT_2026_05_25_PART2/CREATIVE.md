# Creative Synthesis — Argus Audit 2026-05-25 (Part 2)

**Highest-conviction synthesis (one sentence):** Wire the **replay harness** into the live signal path as a *pre-trade shadow* so every order, in paper and real, must agree with its own backtest before submission — converting the strategy fleet from "trust we coded it right" to "ledger and replay must match every fill, every day, fail-closed." This is the cheapest path to the live IR≥0.3 evidence the disciplined gate demands.

The eight syntheses below combine pieces that *already exist in the repo* (replay harness, role registry, auto_promotion engine, tail_hedge sleeve, dispersion-broadened universes, decision/attribution panels, PEAD watchlist, canonical_fills ledger). Each names the components, the *new* behaviour that emerges from the combination, and a validation step that is cheap and falsifiable.

---

## 1. Pre-Trade Replay Bridge — the paper-to-real safety net

- **Components:** `helio/replay_harness.py` + `helio/ibkr_execution.submit_bracket()` + canonical_fills ledger + the existing fail-closed real-money guard.
- **New behaviour:** Every order, before placement, asks "would the backtest of this exact strategy on the bars I have right now, ending one bar ago, have produced this order?" If REPLAY_ONLY or TICKER_DIVERGENT > 0 over the trailing 5 trades, `submit_bracket` blocks the order with `replay_mismatch` and writes an incident. The mismatch threshold tightens automatically as we approach real money.
- **Validation:** Run nightly against the last 5 days of paper fills; the gate should reject ~0 historically clean trades. Any rejection is a real C2/C1-class bug surfaced before capital is at risk.

## 2. Variant Consensus Sleeve — voting layer over the 6 xs_momentum runners

- **Components:** the 6 live xs_momentum variants + `helio/auto_promotion.compute_recommendations` + a new `forge_xs_momentum_consensus` runner that issues *zero* IBKR orders of its own.
- **New behaviour:** The consensus runner reads each variant's intended-picks snapshot at the rebalance bar. It writes a virtual ledger taking a position only when **≥3 of 5** variants vote the same ticker. Capital allocation re-tilts: variants stay at 0.25× as scouts; the consensus sleeve gets a 0.5–1.0× weight. Because variants currently cluster on XLE/XLK, the consensus naturally throttles concentration when agreement is thin (the regime when correlated DDs hit).
- **Validation:** Replay the consensus vote over the 20y disciplined-gate window; required: PF CI lower ≥1.20 AND max DD < worst single-variant DD. If the consensus DD isn't materially lower, the variants aren't independent and we should formally collapse the fleet to one engine.

## 3. PEAD-Filtered Momentum — revive the shelved Apollo watchlist as a sub-universe

- **Components:** Apollo's 16-name curated PEAD list (already living under `helio/data/pead/earnings/`) + the xs_momentum engine + the universe registry.
- **New behaviour:** Add a new universe `PEAD_FILTERED_16` to `xs_momentum_universes.py`. The xs_momentum engine ranks *only those tickers within the 5 trading days following each name's earnings*. This converts the dead PEAD strategy from a price-drift bet (failed slippage gate at 40 bps) into a *catalyst-conditioned* momentum filter, which has different statistics: fewer trades but each one factor-conditioned on an event the rest of the fleet ignores.
- **Validation:** Disciplined-gate sweep at 10bps and 40bps slippage on 20y data. Must beat plain xs_momentum on the same equity sub-universe by ≥0.2 Sharpe to justify the operational complexity.

## 4. NQ-Overnight as a GLD Co-Pilot Hedge

- **Components:** the shelved `forge_nq_overnight` runner + `forge_gld_pm_long` (the only DEFENSE-role winner) + the role registry.
- **New behaviour:** Re-classify `forge_nq_overnight` from OFFENSE to HEDGE and re-wire it: only enter NQ overnight short when `forge_gld_pm_long` holds an open position AND the prior-day SPY return < −0.5%. Sizes to ~0.3× of GLD notional so its expected loss in a risk-on tape is rounding error, but its expected gain in a risk-off overnight protects the daytime GLD entry. The 0.526 IR-lower failure was as a standalone alpha bet; as a conditioned hedge the bar is the 0.80 HEDGE floor and negative correlation, not 1.20.
- **Validation:** Backtest the conditional rule on 20y data measuring *combined* GLD+NQ-short PnL vs. GLD alone. Pass = combined Sharpe ≥ GLD-only Sharpe AND max combined DD reduced ≥15%.

## 5. Pure-Alpha Pairs Sleeve — long bot picks, short MTUM

- **Components:** the 5/19 factor decomposition finding (alpha p=0.94 vs MTUM) + the xs_momentum live picks + an MTUM short leg in the cluster-exposure module.
- **New behaviour:** A new `forge_xs_momentum_pairs` runner mirrors the picks of `forge_xs_momentum_style` and simultaneously shorts the equivalent dollar of MTUM. The PnL of this pair IS the residual alpha the regression said is zero. If the pair makes money in paper, that result is mechanically impossible under "alpha = 0"; if it loses, the audit is vindicated and the fleet should down-weight further.
- **Validation:** Three-month live paper sample of the pair sleeve. Pair-PnL t-statistic > 2 → real alpha and the operator has the first defensible argument for real money. Pair-PnL drifting toward zero → factor harvester confirmed; rotate capital to MTUM ETF directly.

## 6. Composite Defense Sleeve — vol-target + tail hedge + regime gate as one knob

- **Components:** `forge_tail_hedge` (GLD+TLT when SPY<200dma) + the regime-gate logic in `xs_momentum_legacy15_regime` + the vol-targeted sizing slated for Tier 3 in the comprehensive audit.
- **New behaviour:** A single `helio/composite_defense.py` module that exposes one function `defense_state(now) -> {risk_multiplier, hedge_weight}`. When SPY > 200dma AND realized 20d vol < 12%: risk_mult=1.0, hedge=0. When SPY < 200dma: risk_mult=0.5, hedge_weight=0.20 (engage tail sleeve). When SPY > 200dma but vol > 18%: risk_mult=0.7, hedge_weight=0.10. The OFFENSE runners read `risk_multiplier`; the HEDGE runner reads `hedge_weight`. The three knobs become one regime-aware dial rather than three independent strategies that all might or might not engage in the same month.
- **Validation:** 20y backtest comparing (a) status quo and (b) composite. Required: combined Sharpe ≥ status quo AND max DD reduced ≥30%. Smoke-test: in the 53 SPY-below-200dma months historically, composite should be hedged in ≥90% of them.

## 7. Live-vs-Backtest Real-Time Auto-Pause

- **Components:** the PnL-attribution dashboard endpoint + `helio/auto_pause.py` (mirror of auto_promotion) + canonical_fills.
- **New behaviour:** Cron every 5 minutes computes cumulative bps drift between live-realized PnL and what the same-period replay says the strategy *should* have made. If drift exceeds 25 bps over 5 consecutive trades, the strategy's allocation auto-flips to 0.0 with a `live_vs_replay_drift` incident and operator notification. Effectively makes the dashboard's existing attribution view the *trigger* for risk action instead of a passive read.
- **Validation:** Replay the rule over the 5/22-onwards canonical_fills and incident log. Required: would have fired on 2026-05-19 CADJPY cascade (yes — drift was catastrophic) and would NOT have fired on the GLD self-heal day (no real PnL divergence). If it false-alarms on noise, tighten the bps band before live use.

## 8. Auto-Promotion Engine Wired to a 24h Cooling-Off Queue

- **Components:** `helio/auto_promotion.compute_recommendations` (exists, runs, but no operator wiring) + canonical_fills `n_trades` counter + the existing dashboard.
- **New behaviour:** A nightly job appends qualifying recommendations to a `pending_promotions.jsonl` queue with a `not_before_ts = now + 24h`. The dashboard renders the queue; the operator can one-click APPROVE/REJECT. Approved promotions write to `allocation_factors.json`. The 24h delay enforces a sleep-on-it discipline and lets a sudden adverse signal (e.g. SPY −2% overnight) auto-cancel the pending promotion. This converts the dormant auto_promotion module from "we built it but never use it" to the actual capital-allocation loop the operator runs at his morning coffee.
- **Validation:** Backfill the queue against the last 14 days of allocation_factors.json edits. Required: the engine's recommendations match what the operator actually did ≥70% of the time, or the rules are wrong and need re-tuning before automating.

---

## Most likely to fail, and why

- **#5 (pure-alpha pairs)** is the most likely to fail because the 5/19 factor decomposition was explicit: alpha p = 0.94. Going long picks and short MTUM in paper will probably trend slowly negative due to slippage on both legs (~40 bps roundtrip × 2) with zero residual alpha to cover the bleed. The *value* is that it converts the abstract decomposition into a falsifiable live experiment with a hard answer in 60-90 days — losing $300-800 of paper to settle the alpha-or-not debate is cheap. Build it; expect it to lose; act on the result.
- **#3 (PEAD-filtered momentum)** is the next most likely to fail. PEAD already failed the disciplined gate at 40 bps slippage (CI lower 1.025). Filtering an already-borderline universe by an event window will shrink n further and may not lift PF enough to clear the 1.20 floor. Worth the half-day to test, but I would not bank capital on it surviving the same gate that killed the standalone version.
