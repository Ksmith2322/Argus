# Performance expectations — Argus paper fleet, May 26 → June 30 2026

Written 2026-05-24 after closing the 10 Codex gaps + Codex X7 lifecycle
audit + the runner-lock incident response. Bot is paper-only on the
post-reset epoch (`post_reset_20260522`, anchor $250K, clean=True).

## Big picture in one sentence

The fleet has **two strategies actively trading** and **two pending
opt-in**; we expect roughly **breakeven-plus-noise** at the strategy
level for the next 30 days, with most variance driven by SPY beta in
`xs_momentum`, not by edge.

## Per-strategy expectations

### `forge_xs_momentum` (OFFENSE, 1.0× allocation)

| Metric | Expectation | Honest qualifier |
|---|---|---|
| Trades in next 30d | 2-6 entries + 2-6 exits | Monthly rebalance fires once around June 1; intra-month positions held passively |
| Holding period | ~20 trading days per position | Top-2 of 8 ETFs at 12-1 momentum rank |
| Per-trade PnL | ±$300 to ±$3,000 | At $250K anchor with 2-pick basket, ~$25K notional per pick |
| Monthly net return | -2% to +6% on the sleeve | Annualized CAGR 16% in backtest, but huge monthly variance |
| Disciplined gate | live_pf within `[1.86, 6.30]` over a rolling window | Don't act on a single-month deviation — the gate window is 30 trades, not 30 days |
| Factor-decomposition reality | ~0 alpha vs MKT+SMB+HML+MOM | The CAGR is **passive factor beta**, not edge. Treat the strategy as tactical-asset-allocation beta, not a skill stream |

**Red flag if seen**: live PnL beyond ±$10K on a single position
(would imply sizing breach), OR live_pf below 1.20 over a rolling
10-trade window (would suggest regime drift, not just variance).

### `forge_gld_pm_long` (DEFENSE, 0.5× allocation)

| Metric | Expectation | Honest qualifier |
|---|---|---|
| Trades in next 30d | ~5-10 entries | Daily PM evaluation; signal frequency ~30-50% of weekdays |
| Holding period | 1-3 days per trade | Tight ATR-based stop + target |
| Per-trade PnL | ±$50 to ±$500 | $20-50K notional capped at $50K (0.4× × 0.5× of anchor) |
| Monthly net return | -1% to +3% on the sleeve | DEFENSE role — lower bar, role is risk-reduction not return-max |
| Disciplined gate | passes at DEFENSE floor 1.05 | Barely; CI lower 1.08 |
| Live PF expectation | Could legitimately register at 0.9-1.4 | n is small; treat anything between 0.9-1.6 as in-sample noise |

**Red flag if seen**: 3+ consecutive losers OR live PF below 0.7 over
a rolling 10-trade window. Either suggests regime change for gold.

### `forge_tom_spy` (PENDING_OPT_IN, 0.3× recommended)

Will not trade unless operator opts in via `allocation_factors.json`.

**First action window if activated**: May 26 (Tue, today) through
June 2 (next Mon). TOM strategies fire ~5 trading days/month.

| Metric | Expectation if activated |
|---|---|
| Trades in next 30d | 1-2 round-trips |
| Per-trade PnL | ±$200 to ±$2,000 |
| Monthly net return on the sleeve | -1% to +4% |
| Disciplined gate (CI lower) | 1.22 @ 10bp |
| Correlation vs xs_momentum | 0.03 (genuine diversification) |

**Decision required by Tuesday open**: opt in or stay PENDING.

### `forge_nov_spy` (PENDING_OPT_IN, 0.2× recommended)

Will not trade until **November 2 2026** regardless of opt-in
status — the strategy is November-only. Operator can opt in now
and the strategy will idle until November.

## Fleet-level expectations

### Paper account ($250K)

| Scenario | 30-day P&L band | Probability (rough) |
|---|---|---|
| Strategy + market both work | +$5,000 to +$15,000 | 25% |
| Quiet month, edge ~breakeven | -$3,000 to +$5,000 | 40% |
| Drawdown month (SPY -2 to -5%) | -$10,000 to -$3,000 | 25% |
| Tail event (correlation crisis) | -$20,000 to -$10,000 | 10% |

Expectations are **strategy-level**, not portfolio-of-account. The
account starts the month at $250K; raw account-level returns will
also include any cash-deposit/withdraw activity which is zero by
operator policy through 6/30.

### Honest baseline: SPY buy-and-hold over same window
Long-run SPY return is ~10% annualized = **~0.8% per 30 days** as
the comparison. If the fleet ends June 30 within ±$2,000 of a
$250K × 0.8% = $2,000 SPY benchmark, we have **no evidence of
edge**, just successful tracking of beta. Beating SPY by $3,000+
is the first signal that something more than beta is happening.

## What to look for (early-warning signals)

Order of priority — the higher the row, the faster you should act on
a red flag.

| Signal | Where to see it | RED reading |
|---|---|---|
| `daily_health_check` worst_status | `/api/daily_health_check` + dashboard banner | RED on any component *except* preflight (preflight RED is expected) |
| `pre_market_check` verdict | `/api/pre_market_check` + dashboard banner | `NOT_READY` |
| `data_feed_contract` verdicts | `/api/data_feed_contracts` + dashboard panel | RED that persists after refresh |
| `order_lifecycle` anomalies | `/api/order_lifecycle` | Any `ORPHAN_EXIT`, `DUPLICATE_ENTRY`, or `PARTIAL_EXIT` |
| `restart_status.n_need_restart` | dashboard | > 0 after a code push |
| `canonical_fills_since_epoch.total` | dashboard | Stays at 0 after Tuesday 5/26 close (means runners aren't writing) |
| `live_pf_30trades` vs CI band | dashboard `cohort-gate-panel` | Falls below CI lower for ≥10 trades |
| HALT.flag, FLATTEN_EOD.flag | dashboard banner | Present |
| Cluster exposure vs cap | dashboard `cluster-exposure-panel` | METALS approaches 0.9× cap (gld_pm_long + xs_momentum overlap) |

## Decision triggers per scenario

| Observation | What it means | Action |
|---|---|---|
| `canonical_fills` stays at 0 through Wed 5/27 | Runners aren't writing — likely IBKR connection or canonical-fill helper regression | Investigate before Friday close; restart runners if needed |
| `forge_xs_momentum` rebalance produces an unfamiliar pick mix on June 1 | Universe drift or data corruption | Compare with `xs_momentum_picks` from snapshot before vs after; if data_feed_contracts GREEN, accept the picks |
| `forge_gld_pm_long` fires 0 trades in 30 days | Regime is wrong for the strategy | Acceptable — DEFENSE role often quiet; only worry if 60 days |
| ORPHAN_EXIT shows up | Lost an ENTRY fill or broker filled unintended order | INVESTIGATE immediately; pause real-money consideration until resolved |
| Live PF falls below CI lower for ≥10 trades | Edge degradation | `auto_pause` recommendation; operator reviews + applies |
| All caps bind at 1× capacity_stress | Already true — known headroom limit | No action; see `ops/CAPACITY_HEADROOM_NOTES.md` |

## What success looks like by June 30

The bar is **NOT** "make money." The bar is **"produce clean evidence
that the bot operates correctly under post-reset code."** Specifically:

1. ≥30 closed trades in `canonical_fills` since `post_reset_20260522`
2. Zero `ORPHAN_EXIT` or `DUPLICATE_ENTRY` over the window
3. `daily_health_check` worst_status ≤ YELLOW on ≥27 of 30 days
4. Each ACTIVE strategy's `live_pf_30trades` lands within its CI band
   (or there's a clear regime explanation for deviation)
5. No silent runner regressions (the gap-#9 restart-verify catches them)
6. `auto_pause` recommendations applied within 24h of firing

If those six conditions hold, **and** the fleet has produced any
positive non-beta-attributable PnL, the 7/1+ real-money go/no-go
decision has the evidence base it needs.

If those conditions don't hold, real-money is deferred again. The
purpose of the 30-day clean window is to produce **evidence**, not
returns. Returns are a downstream artifact of operating correctly.
