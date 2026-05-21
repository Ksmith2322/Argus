---
name: 2026-05-20 reset runbook + post-reset launcher + coint_pairs (FAILED backtest)
description: Three batch-3 items. 5/31 cutover runbook with 8-phase operator checklist. New post-reset launcher script that REFUSES to start if archived runners are still alive. forge_coint_pairs built end-to-end and BACKTEST FAILED across all parameter variants (PF=0.91, -40% over 5y). 94/94 tests green. Architect's batting average on new strategies: 2 of 4.
type: project
originSessionId: b0404a2b-d623-4c6e-b3e1-e95e059c7535
---
Tight 3-item batch focused on operational readiness for the 5/31 cutover.

## What shipped

### 1. 5/31 reset runbook
[project_2026_05_31_reset_runbook.md](project_2026_05_31_reset_runbook.md) — 8-phase
operator checklist for the cutover day:

  - **Phase 1**: stop all runners (with the exact PowerShell command)
  - **Phase 2**: snapshot canonical_fills + position_monitor + heartbeats
                 + run final pre-reset audits
  - **Phase 3**: dry-run + execute `epoch_reset.py --target 20260531`
  - **Phase 4**: advance `evidence_epoch.current_epoch_id` →
                 `post_reset_20260601` (is_clean=True)
  - **Phase 5**: edit allocation_factors.json — pead 0→0.5, xs_momentum 0→0.5
  - **Phase 6**: launch `start_post_reset_runners.ps1` (the new 5-strategy launcher)
  - **Phase 7**: dashboard smoke test
  - **Phase 8**: regression-test the invariants + log the cutover

Plus: post-cutover monitoring cadence (day 3 / 7 / 14 / 30 / 90 checks)
and rollback procedure.

### 2. start_post_reset_runners.ps1
New launcher at `ops/start_post_reset_runners.ps1` that:
  - Launches only the 5 surviving strategies (gld_pm_long, nq_overnight,
    pead, xs_momentum, spy_trend_follower) + argus_flow.runner_unified
    (dormant for paper-state reconciliation; allocation=0).
  - **REFUSES to launch if any archived runner is still alive** — checks
    against a list of 21 archived modules and exits with code 2 if any
    are running. Prevents the failure mode where the operator forgets
    Phase 1 of the runbook and ends up with a mixed fleet.
  - Keeps the same dry-run / restart-all / log structure as
    `start_all_runners.ps1` (which is retained for rollback).

### 3. forge_coint_pairs built end-to-end — BACKTEST FAILED

Architect's #5 candidate (cointegrated pairs trading). Built:
  - `helio/cointegration.py` — pure decision logic
    (compute_hedge_ratio, estimate_half_life, evaluate_pair_signal)
  - `forge/coint_pairs/runner.py` — backtest + check modes
  - `argus_flow/tests/test_cointegration.py` — 20 tests

8 default pairs: KO/PEP, XOM/CVX, HD/LOW, GLD/SLV, EWZ/EWW, TLT/IEF,
V/MA, MSFT/GOOGL.

**5-year backtest (2021-2026):**
  - n=184 trades, WR=62%, PF=0.91 (below 1.0)
  - avg_win +3.4% / avg_loss -6.2% (R:R inverted vs WR)
  - Total -40.9% unscaled, CAGR -1.51% scaled at 15%/pair
  - Max DD 13.0%

**Per-pair breakdown** (this is where the diagnosis lives):
  - WINNERS: V_MA +26%, EWZ_EWW +23%, TLT_IEF +7%, MSFT_GOOGL +2%
  - **LOSERS: GLD_SLV -42%, XOM_CVX -37%, KO_PEP -16%, HD_LOW -4%**

GLD/SLV decoupled fundamentally during 2021-2026 (gold as inflation
hedge held up; silver as industrial metal didn't) — the cointegration
broke. XOM/CVX had idiosyncratic 2022-2024 events. KO/PEP saw separate
COVID-recovery paths.

**Parameter sweep failed too** — tried entry_z {2.0, 2.5, 3.0} and
stop_z {3.0, 4.0}; NONE produced positive return. Tighter entry made
it WORSE: extreme z deviations CONTINUE trending rather than mean-revert.

**Diagnosis written into `forge/coint_pairs/__init__.py` STATUS block**
+ allocation_factors v9 _kill_log.

### Honest finding caught by the test suite

`test_half_life_on_random_walk_can_be_spurious` documents that the
OU half-life estimator gives a finite hl≈19 on a 200-step random walk
with seed=33 — meaning the half-life filter alone CAN admit
non-cointegrated pairs. This is a small-sample artifact of the simple
OU fit. Mitigation: combine with the z-score entry threshold (rare for
a random walk to sustain z>=2). Test exists to prevent silent regression.

## Architect scorecard (new strategies they recommended)

| # | Strategy | Backtest verdict | Allocation |
|---|---|---|---|
| 1 | forge_vix_carry | FAILED (PF=1.01, -3% CAGR) | 0.0 |
| 2 | forge_pead | PASSED with caveats (PF=2.04 / 1.61 non-curated; CI at floor) | 0.0 → 0.5 post-reset |
| 3 | forge_xs_momentum | PASSED (PF=2.05, CI +0.08 over floor) | 0.0 → 0.5 post-reset |
| 5 | forge_coint_pairs | FAILED (PF=0.91, -40% over 5y) | 0.0 |

**2 of 4 candidates worked.** Half the Architect's "untouched factor"
recommendations don't generalize to current regimes. This is a real
signal — backtest-before-ship caught both losers before they shipped.

## Test scoreboard

94/94 green:
- test_cointegration.py: 20/20 (including the random-walk spurious-detection finding)
- test_sunset_roster.py: 6/6
- test_bootstrap_stats.py: 18/18
- test_killed_strategy_invariant.py: 4/4
- test_pead.py: 16/16
- test_xs_momentum.py: 13/13
- test_vix_carry.py: 17/17

## State going into 5/31 cutover (11 days away)

**Operationally ready:**
- Reset runbook written
- Post-reset launcher script written + tested (refuses mixed fleet)
- Sunset mechanized (19 archived in allocation_factors v9, 15 with no_restart in fleet_monitor)
- Pre-freeze epoch flagged `is_clean=False`; post-reset epoch ready to advance
- evidence_epoch wired into ROI consumers

**Surviving 5-strategy roster post-reset:**
1. forge_gld_pm_long (live, proven, 0.4×)
2. forge_nq_overnight (live, probation 74% from peak, 0.5× brake-halved)
3. forge_pead (new, 0.5× activation post-reset)
4. forge_xs_momentum (new, 0.5× activation, only candidate with passing bootstrap CI)
5. forge_spy_trend_follower (passive beta benchmark)

**Operator actions outstanding:**
1. Restart the fleet to pick up the 5/18 ENTRY-write code + 5/19 lineage_id fix
   (or just wait for 5/31 cutover — the next launch will pick everything up)
2. On 5/31 evening: execute the runbook
3. 6/1: verify post-reset launcher came up, dashboard shows clean roster

## Open items going into next session

The fix-by-5/31 queue from Codex is essentially done. Remaining is post-reset work:
- 6/1 onwards: accumulate clean post-reset trades
- 9/1 review: re-run bootstrap CIs with the live + paper data
- 10/1+: real-money go/no-go (deferred from 7/1 per project_revised_2026_07_01_real_money_timeline.md)

The bot is in the cleanest state it's been in across the 5-day sprint.
4 honesty checks survived contact with data (vix_carry failed, coint_pairs
failed, pead generalizes with thin margin, xs_momentum the only one with
passing CI). The path forward is clear: ride the 5-strategy roster
through 6/1 → 9/1, gate real money on bootstrap CIs not point estimates.
