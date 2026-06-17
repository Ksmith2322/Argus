---
name: 2026-05-19 new-edge sprint — PEAD + XS-momentum both pass backtest, FX investigated, sunset list shipped
description: Two new strategies built and BACKTEST-VALIDATED in one session. forge_pead PF=2.04 CAGR=7.5% (curated universe caveat). forge_xs_momentum PF=2.05 CAGR=16% over 9 years. Plus sunset doc for the 5/31 freeze (archive 16, keep 4-6). FX sizing 'bug' diagnosed as risk-vs-notional conflict on strategies slated for archive; skipped.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Real signal session. Two backtests passed where vix_carry yesterday failed —
indicating the bottleneck is strategy SELECTION, not the backtest harness.

## What shipped

### 1. forge_pead — POST-EARNINGS DRIFT — PASSED gate

- `helio/pead_signal.py` — pure decision logic (entry gates + exit logic)
- `forge/pead/runner.py` — runner with --backtest, --check, --evaluate, --loop
- `argus_flow/tests/test_pead.py` — 16 tests (all entry/skip paths + exit logic + universe load)
- Universe: Apollo's 16-name PEAD watchlist (apollo/data/core_watchlist.json)

**5-year backtest results (2021-2026, Apollo 16-name universe):**
- n=69 trades, WR=37.7%, PF=2.04
- avg_win +17.3%, avg_loss -5.1% (R/R 3.4×)
- Scaled equity growth +38.5%, CAGR +7.47%, max DD 11.5%
- 11 of 16 names produced ≥3 trades; top PLTR/REGN/MU/GOOGL/TSM
- Exits 61% atr_stop, 39% hold_period_end (healthy mix)

**Honest caveat (recorded in __init__.py + allocation_factors kill_log):**
Apollo's universe was curated BECAUSE these names show PEAD drift, so this
validates the implementation, not the factor in general. Activate at 0.5×
post-reset; generalization test on non-curated universe is the next step.

### 2. forge_xs_momentum — CROSS-SECTIONAL 12-1 MOMENTUM — PASSED gate

- `helio/xs_momentum.py` — pure decision logic (compute_momentum_score, rank, select_top_quintile)
- `forge/xs_momentum/runner.py` — runner with --backtest, --check, --evaluate
- `argus_flow/tests/test_xs_momentum.py` — 13 tests (including 12-1 lookback correctness test)
- Universe: 15 ETFs (10 US sectors + 5 country)

**9-year backtest results (2017-2026):**
- n=79 trades, WR=51.9%, PF=2.05
- avg_win +11.5%, avg_loss -6.1%
- Portfolio growth +279.83%, CAGR +16.22%, max DD 37.35%
- All 15 tickers contributed (range 2-10 trades each)

**Honest caveat:** 37% DD is the documented "momentum crashes" feature
at regime turns (Mar-2020, late-2018, 2022). Capital ladder -10%/-20%
kill rules would pause well before that depth. Activate at 0.5× until
12+ months clean post-reset trades establish real-money DD profile.

### 3. Sunset decision document

`project_2026_05_31_sunset_decisions.md` — committed to memory.
Compresses 22+2=24 strategies into a defensible **post-reset roster of
4-6 actually-traded strategies**:

**KEEP (4 active):**
- forge_gld_pm_long (proven, n=15 PF=2.73 real)
- forge_nq_overnight (proven but in 74% drawdown, on probation)
- forge_pead (NEW, backtest-validated, post-reset activation)
- forge_spy_trend_follower (passive beta sleeve / benchmark)

**SHADOW (2 logging-only):**
- forge_gdx_gld (cointegrated metals pair)
- forge_vix_carry (backtest failed, template kept)

**ARCHIVE (16):**
- 3 argus FX pairs (silent + sizing-vs-cap conflict)
- 3 YM trader replicas (cuebanks/mamba/tori — same factor)
- 4 other dark forge strategies (aud/wick/jpy/vix_revert)
- forge_fomc_drift, forge_tom_international (event-driven, 0 fires)
- forge_rebalance (annual event, dormant)
- 6 Greek scanners (apollo/hermes/titan/ares/atlas/themis — apollo's universe is now in PEAD; rest are unused)

**NEW slots planned post-7/1:** csp_quality, coint_pairs, russell_recon
(only after one of pead/xs_momentum survives 60+ clean post-reset trades).

### 4. argus FX sizing — diagnosed, not fixed

The cull audit's "25-35× over fleet cap warnings every cycle" is NOT a bug.
It's risk-based sizing (0.5% of equity = $150 risk) conflicting with the
2.0× notional cap. With a 5-pip stop on GBPUSD, the math correctly demands
299K units = $380K notional = 6.3× the $60K cap — and the system correctly
rejects. The symptom is log noise from rejected entries.

**Decision:** SKIPPED the fix. These 3 strategies are slated for ARCHIVE
in the sunset doc. Fixing the math to enable more trades on strategies
the audit recommends sunsetting is bad ROI. If a serious FX strategy is
ever resurrected, the right approach is widening stops or clamping size
to fit notional, not raising the cap.

## Today's gate scoreboard

| Strategy | Verdict | PF | CAGR | Notes |
|---|---|---|---|---|
| forge_vix_carry (yesterday) | FAILED | 1.01 | -3.07% | Code retained as template |
| forge_pead | **PASSED** | 2.04 | +7.47% | Caveat: curated universe |
| forge_xs_momentum | **PASSED** | 2.05 | +16.22% | 9-year history through multiple regimes |

**2 of 3 candidates cleared the gate.** That's the real signal — when the
universe + thesis are right (apollo's PEAD watchlist; AQR's 12-1 momentum),
the strategies work. The dominant lesson from this 2-day sprint:
**building scaffolding is cheap; finding edge is what's hard, and the
backtest-first discipline is what distinguishes hope from validated alpha.**

## Test scoreboard

93/93 green across all new + key existing tests today:
- test_pead.py: 16/16
- test_xs_momentum.py: 13/13
- test_vix_carry.py: 17/17
- test_entry_write_smoke.py: 2/2
- test_lineage_id.py: 9/9
- test_killed_strategy_invariant.py: 4/4
- test_capacity_stress.py: 10/10
- test_canonical_fills.py: 22/22

## Open items going into next session

1. **Operator action:** restart the fleet so the 5/18 ENTRY-write + lineage_id
   fixes take effect in production.
2. **5/31 reset prep:** execute the sunset doc — set `no_restart=True` on
   the 16 archived strategies in `helio/fleet_monitor.SYSTEMS`, update
   cohort startup scripts.
3. **Post-reset paper allocation:** assign initial factors for pead (0.5×) and
   xs_momentum (0.5×). Activate after the reset on 6/1.
4. **Generalization test for PEAD:** retest on a non-curated universe (e.g.,
   top-30 SPX by liquidity) to see if edge depends on Apollo's curation.
5. **Real-money decision delayed to 2026-10-01+** stands. The next 5 months
   are about accumulating ≥60 clean trades on pead + xs_momentum +
   gld_pm_long + nq_overnight.

## What I'd do differently if this were day-1

Build the vectorized engine the Architect described BEFORE running 22
strategies as daemons. The next 3-4 candidates (csp_quality, coint_pairs,
russell_recon, and the eventual generalization tests) all need backtests
first — that's where time should go, not per-strategy daemon plumbing.

But the existing daemon architecture is fine for what we need now: 4-6
surviving strategies running paper, periodic re-evaluation, no new builds
until at least one proves itself with real post-reset evidence.
