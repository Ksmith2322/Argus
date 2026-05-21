---
name: 5/31 freeze sunset decisions — which strategies survive
description: Decision document for the 5/31 strategy freeze. Compresses 22 strategies + 2 new candidates (vix_carry/pead) into a defensible post-reset roster of 4-6 actually-traded strategies. Based on Truth-Seeker ledger evidence + Skeptic n-analysis + capacity_stress headroom report + 5/19 PEAD backtest result.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
The 5/31 strategy freeze locks the roster. After 6/1, no new strategies
get added until the 7/1+ real-money decision. This document is the
honest list of who survives + why.

**Source evidence:**
- Truth-Seeker 2026-05-19: ledger shows 6 of 22 strategies traded;
  16 produced zero rows in 26 days post-reset.
- Capacity-stress 2026-05-19: 8 strategies fail at 1× their own
  configured cap (sizing math vs cluster math conflict).
- Cull audit 2026-05-18: 3 KEEPs, 9 OBSERVEs, 5 DEFERs.
- 2026-05-19 backtest gates: vix_carry FAILED (PF=1.01), pead PASSED (PF=2.04).

## Sunset decisions (default = ARCHIVE unless evidence justifies KEEP)

### KEEP (active in post-reset window) — 4 strategies

1. **forge_gld_pm_long** — KEEP at 0.4× cap.
   - Real-fill evidence: n=15, PF=2.73, +$361 / 26 days. Healthy peak-vs-current
     ratio (peak $463, current $361, dd_pct 22%). Not in brake-territory.
   - Cluster cap aligned (gld 0.4 in both fleet_sizing + cluster_exposure).
   - Hours/cap math checks out at 1× per capacity_stress.

2. **forge_nq_overnight** — KEEP but on probation.
   - Real-fill evidence: n=13, PF=1.21, +$182 net BUT **74% drawdown from
     $710 peak**. Drawdown brake correctly halving to 0.5%.
   - Decision: keep through the reset but flagged for re-evaluation after
     n=30 post-reset trades. If post-reset drawdown brake stays active, kill.

3. **forge_pead** (NEW, allocation 0.0 → 0.5× post-reset)
   - 5y backtest n=69, PF=2.04, CAGR=7.47%, maxDD=11.5%, diversified 11/16
     tickers contributing.
   - Caveat: curated-universe selection bias. Activate post-reset at half-size
     (0.5×); promote to 1.0× only after n=20 clean post-reset trades show
     PF holds.

4. **forge_spy_trend_follower** — KEEP at 1.0× per design.
   - No real-fill evidence (no closed trades — strategy is buy-and-hold
     50/200 regime). Currently long SPY 26sh.
   - Role: structural equity-beta sleeve. Not "alpha" — it's the SPY
     comparison line that other strategies have to beat.
   - Decision: keep as the benchmark in the fleet, NOT as an alpha source.
     If we go real-money on $10K, this slot is effectively "VTI proxy."

### OBSERVE-IN-SHADOW (no allocation, signal-only logging) — 2 strategies

5. **forge_gdx_gld** — pairs trade on cointegrated metals.
   - Real-fill evidence: n=0 in 26 days. Strategy depends on z-score
     thresholds rarely being hit.
   - Decision: shadow mode through 6/30. If a single trade fires post-reset,
     evaluate. If 0 fires by 6/30, ARCHIVE.

6. **forge_vix_carry** (NEW) — keep code, allocation 0.0.
   - 5y backtest FAILED across all parameter variants. Code retained as
     the reusable template for the next candidate; allocation locked at 0.0.
   - Decision: shadow only. Pre-2018 backtest extension is a separate
     research task (post-7/1 if pursued at all).

### ARCHIVE (sunset for the post-reset window) — 16 strategies

All ledger-silent strategies with no signal in 26 days OR strategies that
fail capacity stress at 1× their own cap. Code stays in repo but:
  - allocation_factor → 0.0
  - fleet_monitor → no_restart = True
  - Removed from cohort startup scripts
  - Removed from dashboard active view

#### FX trio — already killed strategies in 4-layer caps even if signals fire

7. **argus_gbpusd** — 0 fills post-reset. Capacity stress: fails at 1×.
   The sizing math is asking for 2.0× single-instrument cap = $60K notional
   on a $30K anchor. Real bug per cull audit.
8. **argus_usdjpy** — 2 fills total (both 4/23). 22 days of silence.
   Same 1× cap failure as gbpusd.
9. **argus_cadjpy** — 0 fills post-reset. Same.

Honest assessment: competing with banks + algo desks on the three most
liquid FX pairs on Earth was always optimistic. The sizing-math bug is a
symptom; even fixed, the edge thesis is weak per Skeptic.

#### YM/MYM YouTube trader replicas — redundant + curated universe of 3

10. **forge_mamba** — 0 fills post-reset. Single tier YouTube system.
11. **forge_tori** — 0 fills post-reset. Same factor as mamba.
12. **forge_cuebanks** — 0 fills post-reset. Same factor as mamba/tori.

All three trade YM/MYM intraday breakouts. They are **the same strategy**
wearing different names. The CBOT routing bug (5/16) finally unblocked
them but they've produced zero fills in 3 days post-fix. Architect's
"YouTube replicas decay" call.

Decision: archive ALL three. If we ever want a YM breakout strategy, we
write ONE — not three — with rigorous backtest first.

#### Other zero-fill strategies (12+ days silence in 26-day window)

13. **forge_aud_asian_breakout** — 2 fills, both negative. Capacity stress fails at 1×.
14. **forge_wick_gbpusd** — 0 fills post-reset.
15. **forge_jpy_pm_short** — n=4 (below 10-trade tier promotion floor).
16. **forge_fomc_drift** — event-driven, 0 fires per cull audit. The Architect
    noted the actual edge is IV-crush not directional drift — direction is
    the noisy edge. ARCHIVE; consider an options-based redesign post-7/1.
17. **forge_tom_international** — event-driven (turn-of-month), 0 fires.
18. **forge_vix_revert** — short-vol scalper. 0 fills. Already in the same
    archetype as the killed vix_intraday.
19. **forge_rebalance** — Russell-recon timing. Annual event. 0 fires per
    expected calendar (next window late June). KEEP CODE; not active runner.

#### Greek scanners (apollo/hermes/titan/ares/atlas/themis)

20-25. **All Greek family scanners** — ZERO canonical_fills rows.
- **apollo** — pre-event scanner. Its universe is now used by forge_pead;
  apollo itself doesn't need to be a separate runner. ARCHIVE the runner;
  KEEP the universe data file (apollo/data/core_watchlist.json).
- **hermes** — earnings scanner. Same role as apollo essentially. ARCHIVE.
- **titan** — sentiment / news. ARCHIVE.
- **ares** — sector rotation. ARCHIVE (or keep as monthly scheduled task
  if it produces an artifact someone reads).
- **atlas** — regime classifier. The Architect's view: atlas's keyword-rules
  approach isn't real regime detection. ARCHIVE the runner; KEEP the data
  files if anything else reads them.
- **themis** — Polymarket scanner. Per memory, already paused 4/19
  (geoblocked for US users). Already effectively archived.

### NEW strategy slots to build post-7/1 (NOT before)

After at least one of forge_pead or forge_xs_momentum has 60+ clean
post-reset trades, consider these as next builds:

A. **forge_xs_momentum** (Architect #3) — cross-sectional ETF momentum,
   monthly rebalance, 25-ETF basket. Building this today.
B. **forge_csp_quality** (Architect #4) — cash-secured puts on quality
   names. Requires options-enabled IBKR account; new infrastructure.
C. **forge_coint_pairs** (Architect #5) — 8 cointegrated pairs in one
   runner. Replaces gdx_gld with more diversification.
D. **forge_russell_recon** (Architect #6) — already partially scaffolded
   in forge_rebalance.

## Post-reset target fleet (4-6 strategies)

| Slot | Strategy | Role | Risk allocation |
|---|---|---|---|
| 1 | forge_gld_pm_long | Real proven edge, gold momentum | 0.4× |
| 2 | forge_nq_overnight | Real proven edge, on probation | 0.5× (brake-halved 1.0×) |
| 3 | forge_pead | New edge, backtest validated | 0.5× (post-reset) |
| 4 | forge_spy_trend_follower | Beta sleeve, benchmark | 1.0× (passive) |
| 5 | forge_xs_momentum (if backtest passes) | New edge candidate #2 | TBD |
| 6 | forge_vix_carry | Shadow / research | 0.0× |

If forge_xs_momentum backtest fails like vix_carry: 4 strategies total.

## What this saves

- **Operational time**: 22 strategies → 4-6 means fewer daemons, fewer
  client_ids, fewer heartbeats, fewer runner-died incidents.
- **Statistical interpretability**: per-strategy n grows faster when
  there are 4 strategies instead of 22 sharing the same calendar.
- **Decision velocity**: with 4 strategies, weekly review is one hour,
  not five. Real edges get more attention than redundant ORB clones.

## What this costs

- 16 strategies of accumulated paper-trading work goes to archive.
  This is the right call but it's a real concession that most of the
  fleet was redundant or untested.

## Operator action items before 5/31

1. Update `helio/fleet_monitor.SYSTEMS` to add `no_restart: True` for
   each of the 16 ARCHIVE entries (use the test_killed_strategy_invariant
   gate to verify).
2. Update `argus_flow/configs/allocation_factors.json` to set all
   archived strategies' factor=0.0 with `_kill_log` entries.
3. Stop the runner processes for archived strategies before 5/31 reset.
4. Update cohort startup scripts to launch only the surviving 4-6.
5. Update MEMORY.md SOURCE OF TRUTH to reflect the 4-6 surviving roster.
