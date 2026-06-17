# Per-Strategy Extended Audit v2 — 2026-05-13

**Adds 27 questions to the v1 framework**: Edge Quality & Decay (E), Execution Quality (F), Capacity & Scalability (G), Robustness (H), Risk-Adjusted Metrics (I).

This document is the "surgical by end of month" foundation. Each strategy treated as its own cash vehicle with specific blockers, levers, and risks identified.

## Major findings the extended audit added (vs v1)

### 1. **forge_gld_pm_long sizing constraint is a real SMOKE_5K blocker**

Position size capped at ~21 shares (~$9K notional). The strategy WANTS to size larger per its risk budget but `helio/strategy_common.safe_position_size` hits a `max_notional_usd` floor. At hours 18/19/20 today, 3 of 5 signal hours rejected with `NOTIONAL_CAP` + `BROKER_HAS_POSITION`. The "winner candidate" can't actually scale to SMOKE_5K's $5K target without resolving this. **This is the #1 surgical action.**

### 2. **forge_nq_london_close: definite KILL with quantified reason**

Real PF 0.90 (excluding phantom 5/5 trade) = negative edge. The 417-contract phantom at 5/5 16:15 UTC at 25× anchor leverage. ROI proof CSV contaminated. Two real trades net +$16 over 20 days. KILL.

### 3. **Concentration risk: cuebanks + mamba + tori ALL trade YM/MYM**

Three strategies, one instrument cluster. Not actual diversification — three independent attempts at the same edge. Any YM gap or flash crash hits all three simultaneously. Fleet correlation math overstates independence. **Flagged for fleet-level discussion.**

### 4. **forge_mamba's wick-rejection edge is mathematically invalid**

Backtest used synthetic 1m bars (resampled from 5m, not real ticks). The strategy's entire edge thesis is wick-rejection patterns at the 1m level — which CANNOT be measured on synthetic 1m. PF 1.98 backtest is unreliable. Real 1m data pipeline (Polygon/Databento) is missing. Strategy should not be promoted until real 1m data exists.

### 5. **forge_cuebanks edge has a single-factor dependency**

`SCOPE_SD_SUPPLY_ZONE_ONLY=True` gate is **80% of the edge** (PF 3.65 with it on, PF 0.89 with it off). The entire strategy hinges on supply/demand zone detection working correctly. Curve-fit risk: was this discovered via post-hoc filtering or pre-specified?

### 6. **forge_wick_gbpusd has 0 signals in 25+ days**

Filter says nothing has triggered since the 4/24 parameter freeze. Either the filter is overfit (likely) or the market regime genuinely doesn't produce these setups currently. Backtest claims PF 1.8 on 5 years; live says zero. That's a meaningful divergence.

### 7. **Exit-price logging bug affects multiple event-driven strategies**

`fomc_drift` (n=1, exit_px=None), `tom_international` (audit said n=6, canonical says 0). The `close_position_market` path doesn't capture fill price in some code paths. **Affects edge measurement on all event-driven strategies.**

### 8. **Dual-write gap: trades.csv vs canonical_fills divergence**

Multiple strategies report local trades (in trades.csv) that don't appear in canonical_fills.jsonl. wick_gbpusd, tom_international cited. This is the canonical truth-layer mismatch — strategies are succeeding/failing in ways the central evaluator doesn't see.

### 9. **Live vs backtest divergence flagged on argus_cadjpy**

Backtest PF 1.01 (already thin). 1 live trade in 20 days, -$0.30 loss. The edge may not be real — backtest is so thin that random noise washes it out. **KILL candidate.**

### 10. **risk_oversight broker_truth=0.0 is breaking sizing logic across strategies**

Multiple strategies' sizing pulls from `get_sizing_anchor_usd()` which reads `risk_oversight_report.json`. Currently shows `account_equity_usd: 0.0`. Affects gld_pm_long, jpy_pm_short, others. **Fleet-wide silent degradation** while dashboard shows different equity.

---

## Updated ranked dispositions

| # | Strategy | v2 Verdict | Δ from v1 | Single biggest lever |
|---|---|---|---|---|
| 1 | **forge_gld_pm_long** | WINNER but BLOCKED on sizing | Same | **Fix `max_notional_usd` cap / `safe_position_size` logic** |
| 2 | **forge_nq_overnight** | KEEP-PAPER → WINNER-CANDIDATE | UP | Gate on bull regime; concentration-fragile |
| 3 | **forge_wick_gbpusd** | DIAGNOSE | DOWN (was KEEP) | 0 signals in 25 days — overfit filter |
| 4 | **forge_aud_asian_breakout** | DIAGNOSE | DOWN | Run 180d backtest; sizing policy oversized |
| 5 | **forge_vix_revert** | KEEP-PAPER (dormant) | Same | Correct dormancy; wait for VIX>30 spike |
| 6 | **forge_jpy_pm_short** | OPS-BLOCKED | Same | Manual flatten CADJPY 42610 + cluster cap analysis |
| 7 | **forge_spy_trend_follower** | DIAGNOSE | Same | Position size dropped 26→1 without logged exit |
| 8 | **forge_tom_international** | DIAGNOSE | Same | Fix exit_px capture before 5/28 event |
| 9 | **forge_fomc_drift** | DIAGNOSE | Same | Fix exit_px capture before 6/17 FOMC |
| 10 | **forge_rebalance** | OBSERVE (waiting) | Same | Run backtest on 2023-2025 S&P additions |
| 11 | **argus_usdjpy** | OPS-DEGRADED | Same | Restore heartbeat freshness |
| 12 | **argus_gbpusd** | DIAGNOSE | Same | hour_filter killing 99.95% — re-validate hours |
| 13 | **argus_cadjpy** | **KILL CANDIDATE** | DOWN (was DIAGNOSE) | Thin edge (PF 1.01) + 1 fill in 20 days |
| 14 | **forge_cuebanks** | DIAGNOSE | Same | Single-factor dependency on S/D zones; verify edge isn't curve-fit |
| 15 | **forge_mamba** | DIAGNOSE-DEEP | Same | Need real 1m data — synthetic invalidates wick edge |
| 16 | **forge_tori** | REWORK CRITICAL | Same | Fix `_find_opposing_safety` returning flat instead of angled trendline |
| 17 | **forge_gdx_gld** | OPS-BLOCKED | Same | TWS WinError 1225 — connection issue |
| 18 | **forge_nq_london_close** | **KILL** | Confirmed | PF 0.90 + phantom contamination — kill today |

## Cross-strategy systemic issues — extended list

**From v1 (S1-S7):** Argus heartbeat staleness, exit_px capture bug, stale jpy_pm_short position, TWS WinError 1225, hour_filter disabled, NQ phantom trade, tori exit logic.

**New from v2:**

**S8 — Concentration on YM/MYM:** cuebanks, mamba, tori, nq_overnight, nq_london_close all trade Dow or Nasdaq micro futures. 5 of 18 strategies on a single asset class.

**S9 — Synthetic 1m data invalidates mamba:** wick-rejection requires real tick data.

**S10 — Curve-fit risk on cuebanks:** 80% of edge depends on one factor (S/D zones). Was that pre-specified or post-hoc?

**S11 — Dual-write breakage:** trades.csv and canonical_fills diverge for wick_gbpusd, tom_international, gld_pm_long.

**S12 — broker_truth=$0.0 cascading:** Multiple strategies pulling stale equity, affecting sizing fleet-wide.

**S13 — Edge crowdedness:** Cue Banks (671K), MambaFX (~500K), Tori (~200K) all teach their strategies publicly. Edge erosion likely.

**S14 — Insufficient sample for risk metrics:** Most strategies have n<10 fills, making Sharpe/Calmar/VaR all INSUFFICIENT_DATA.

**S15 — Calendar event blind spots:** No strategy has explicit Fed/NFP/CPI gating beyond fomc_drift itself. Other strategies may underperform on event days but don't filter.

---

## What you should actually do, in priority order

### Today (operational triage)
1. **Investigate broker_truth=$0.0 cascading** (S12) — affects sizing everywhere
2. **Investigate argus heartbeat staleness** (S1) — 3 strategies affected
3. **Manual flatten jpy_pm_short stale CADJPY position** (S3)
4. **Diagnose TWS WinError 1225 affecting gdx_gld** (S4)

### This week (correctness)
5. **Fix gld_pm_long `safe_position_size` cap** — unblocks the winner candidate
6. **Fix tori `_find_opposing_safety`** — strategy mathematically losing until fixed
7. **Fix exit_px capture in close_position_market** — affects fomc + tom_international + possibly others
8. **KILL forge_nq_london_close** — confirmed negative edge + phantom contamination
9. **KILL or REWORK forge_argus_cadjpy** — thin edge + thin sample

### This month (data-driven decisions)
10. **Run backtests post-2026-05-12 fixes** on ALL active strategies (data is stale)
11. **Build real-1m data pipeline for mamba** — strategy is invalid without it
12. **Verify cuebanks S/D zone gate isn't curve-fit** — single-factor dependency is concerning
13. **Audit hour_filter for argus_gbpusd** — current config kills 99.95% of signals
14. **Promote forge_gld_pm_long to SMOKE_5K** (only after items 1, 5, ladder ops clears)

### Strategic / fleet-level
15. **Address YM/MYM concentration** (S8) — 5 strategies on one asset class
16. **Define rework criteria per strategy** — currently only kill/keep is mechanical
17. **Fix dual-write gaps** (S11) — multiple strategies' truth layers diverge from canonical

### Don't do
- Add new strategies before 5/31 freeze
- Re-introduce MAX_TRADES_PER_DAY caps anywhere
- Promote any strategy past SMOKE_5K without addressing its identified blocker

---

## What each strategy needs to be a "true cash vehicle" (per operator's framing)

**forge_gld_pm_long:** Fix sizing cap. Scale to $25K notional. Verify hour-20 edge holds at scale. Watch overnight stop-bias.

**forge_nq_overnight:** Gate on bull regime via atlas. Watch concentration risk (PF 8+ on tiny sample). Run on a separate cluster from cuebanks/mamba/tori.

**forge_wick_gbpusd:** Re-run backtest. Loosen filter quantiles. If still 0 signals, kill.

**forge_aud_asian_breakout:** Run 180d backtest. Fix sizing policy (current notional is 20× fleet cap). Validate range_pips threshold.

**forge_vix_revert:** Wait. Add time-stop to exit. Verify IBKR execution on next spike.

**forge_jpy_pm_short:** Manual flatten orphan position. Reduce per-trade sizing (currently 5× cluster cap).

**forge_spy_trend_follower:** Reconcile broker position (26→1 today). Add explicit position-sync check. Restore 30% notional target.

**forge_tom_international:** Fix exit_px capture. Run backtest with new 6-instrument basket (only old 3-instrument backtested). Validate before 5/28.

**forge_fomc_drift:** Fix exit_px capture. Single-source-of-truth on entry/exit dates. Verify Fed June 17 setup.

**forge_rebalance:** Wait. Run backtest on 2023-2025 history before next event.

**argus_usdjpy:** Restore heartbeat freshness. Run 50-trade validation post-restart.

**argus_gbpusd:** Re-validate hour_filter via backtest. Currently 99.95% of signals killed.

**argus_cadjpy:** KILL or run 50-trade live validation with loosened thresholds. PF 1.01 is too thin.

**forge_cuebanks:** Verify S/D zone gate isn't curve-fit. Address cluster TOTAL_NOTIONAL cap. Need real fills (currently 0).

**forge_mamba:** Build real-1m data pipeline. Strategy is hypothesis-only without it.

**forge_tori:** Fix exit trail logic before any other work. Strategy is losing money by design until fixed.

**forge_gdx_gld:** Restore TWS connectivity. Once connected, run --signal-only first to validate logic.

**forge_nq_london_close:** Kill.

---

## What's still missing from this audit

Even with 38 questions, gaps remain that would need additional work:

1. **Live fill data for risk metrics** — most strategies have n<10 fills, so Sharpe/Calmar/VaR can't be computed honestly. Need 30+ fills per strategy.
2. **Pairwise correlation matrix** — can compute from canonical_fills but requires more daily-PnL data than we have.
3. **Capacity stress testing** — testing what happens at 10× position size needs simulation, not just opinion.
4. **Regime-segmented backtests** — most strategies backtested on a single window; bull/bear/chop segmentation pending.
5. **OOS validation post-fixes** — none of the strategies have been re-backtested since the 2026-05-12 silent-gate fixes; all live data is mixed pre/post-fix.

These are "after surgical decisions" work. Don't block on them.

End of v2 audit.
