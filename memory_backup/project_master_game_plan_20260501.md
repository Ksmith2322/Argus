---
name: Master per-strategy game plan 5/1 → 5/31
description: Per-strategy action plan synthesized from a 3-agent debate (Performance Auditor, Resurrection Specialist, Devil's Advocate) on 2026-04-30. Use as the runbook for the 30 days before strategy freeze.
type: project
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
# Master game plan — 5/1 → 5/31 strategy decisions

Synthesized from 3-agent debate on 2026-04-30. The Devil's Advocate argued KILL on 20/23 strategies based on entry rate alone. The Resurrection Specialist surfaced backtest evidence on subsets the Devil missed. The Performance Auditor focused on data-rich strategies. This document resolves the disagreements and gives you a per-strategy plan.

## The honest baseline (going into May)

- **3 strategies have meaningful sample (n≥30)**: all three are losing in-window. Multi_orb has gates that V2 says are *costing* alpha (n=9152 evidence — strongest tunable in the fleet).
- **6 strategies have small samples (n=1-4)**: too few to judge.
- **14 strategies are dark or fully-blocked**: 7 zero-signals, 5 fully-blocked, 2 special-case.
- **Backtest PF on validated subsets exists for several "dead" strategies**: cuebanks 3.63, tori 3.65, mamba 1.98, gdx_gld 1.88. The Devil's Advocate missed this; the Resurrection Specialist surfaced it.

## Resolved fleet decision (with reasoning)

### Tier 1 — Confirmed KILL (5 strategies, all agents agree or one with backtest support)

| Strategy | Reason | Action |
|---|---|---|
| forge_spy_mean_rev | n=38, PF=0.63, WR=52.6%, -$2.24/trade. Wins are too small to overcome -2.4% avg loss. Already at factor 0.0x via 4/30 deploy. | Stop runner. Update factor to 0.0 (already done). Update verdict file disposition: KILLED. |
| apollo | Earnings calendar scanner; data feed broken Q2 2026 + no automated execution path. No edge thesis backtested live. | Set disposition=research_only in fleet_sizing. Stop runner. Revisit Q3 if data feed restored. |
| hermes | Pre-market gap fade scanner. Backtest PF=0.88 on 20-trade history (NEGATIVE expectancy). Currently DRY-RUN, IBKR disconnected. | Stop runner. Disposition=archived. Do NOT enable execution — backtest says it'd lose money. |
| titan | Momentum/breadth scanner. No backtest. No clear edge thesis. Stale state ("open_positions: 1" while DRY-RUN). | Stop runner. Disposition=archived. |
| forge_rebalance | S&P index rebalance event scanner. ~4 events/year, none pending in May. Even validated PF 1.31 backtest, but execution friction (open-day slippage) often eats it. | Pause runner (it's correctly silent). Decide post-5/31 if Q3 events appear. |

### Tier 2 — REWORK with specific code changes (4 strategies, agent-disagreement resolved by backtest evidence)

| Strategy | Backtest evidence | Action | File:line |
|---|---|---|---|
| forge_multi_orb | V2 n=9152 says `breakout_window_expired` is COSTING alpha (mean +0.38% LONG, only 22% saves). Largest tunable signal in fleet. | Lift `breakout_window_bars` from 24→32 | `forge/multi_orb/runner.py:71` |
| forge_cuebanks | Validated subset (S/D supply zones only) PF 3.63 on n=30 backtest. Currently in research_only mode. | Verify launch removes `--signal-only`; confirm RESEARCH_ONLY=False at runtime | `forge/cuebanks/runner.py:63` + launch script |
| forge_tori | Validated subset (Dow LONG only) PF 3.65 on n=78 backtest. Currently silent due to research_only flag. | Same — confirm RESEARCH_ONLY=False at runtime; verify trendline_scanner break detection | `forge/tori/runner.py:47` + scanner module |
| forge_mamba | Validated subset (YM=F only) PF 1.98 backtest. 13,828 signal CSV rows have empty action field — log-format issue, not edge issue. | Fix signal output to write ENTRY_LONG/SHORT explicitly | `forge/mamba/runner.py:~400` |

**Critical:** these REWORK strategies have backtest-validated edges. The Devil wanted to kill them; the Resurrection Specialist correctly objected. Do the small fix first, then judge.

### Tier 3 — Diagnose-then-decide (3 strategies)

| Strategy | What needs diagnosis | Action |
|---|---|---|
| forge_gdx_gld | Backtest PF 1.88 (validated). Currently DOWN (process_alive=False per dashboard). Likely stuck in --signal-only mode or socket dropped. | Run `python -m forge.gdx_gld_runner --status`. Restart in `--live` mode. Tonight's TWS reconnect-with-backoff will keep it alive. |
| argus_cadjpy / argus_gbpusd | 3,772 + 3,844 signals, ~2-26 ENTRY signals each, **0 fills**. Either MTF gate too tight OR fills failing at broker. | Check `argus_flow/logs/<pair>/state.json` for `drawdown_pause_until` timestamp. Inspect MTF gate threshold in `runner_unified.py`. Decide on tighten vs loosen with broker fills as ground truth. |
| forge_fomc_drift | 1 entry + 1 exit fired (4/28-4/29) but trades.csv has no closed-trade row. Reconciliation issue. | Read IBKR broker statement for 4/29 fill. Manually write the closed trade to trades.csv. |

### Tier 4 — Continue paper accumulation, no code changes (8 strategies, n<30 + signs of edge)

These need data, not tweaks. Re-evaluate at 5/15 and again at 5/30:

| Strategy | n | Note | Trigger to upgrade decision |
|---|---:|---|---|
| forge_vix_intraday | 40 | V2 says all 4 gates SAVING ALPHA at 96%+ save rate. Edge is real; April was just a vol-whipsaw drawdown. | n≥50 + cumulative PF≥1.10 by 5/15 → KEEP-PAPER 1.0x. PF<1.00 → KILL. |
| forge_nq_overnight | 4 | PF 5.11 on 4 trades is an outlier but the wins are clean (3 winners +95/+80/+107pts). Minimal ops overhead. | n≥10 + PF≥2.0 → real-money primary candidate at 0.5x sizing. |
| forge_gld_pm_long | 4 | PF 3.14 on 4 trades. Sound thesis (PM gold + safe-haven flows). | n≥10 + PF≥1.50 → 0.5x go-live. Watch correlation with nq_overnight (devil flagged). |
| forge_jpy_pm_short | 4 | PF 1.73, both winners cleanly hit target. | n≥10 + PF≥1.40 → 1.0x go-live. |
| forge_nq_london_close | 2 | V2 gates SAVING ALPHA at 67-75% save rate. Gates are sound; problem is firing rate (2 in 30d). | Test broader window 15:00-17:00 UTC on 60d backtest. If PF≥1.20, roll change in by 5/20. |
| forge_aud_asian_breakout | 1 | n=1 means nothing. V2 verdicts are NEUTRAL — no clear gate edge yet. | n≥10 by late May → 0.5x marginal go-live. Otherwise WAIT. |
| argus_usdjpy | 2 | Has MTF + AI overlay infrastructure. Tune at config level only. | n≥20 by late May. Don't touch code. |
| forge_tom_international | 0 closed | 6 entries open from 4/29; T+3 exits hit 5/2-5/3. Re-evaluate then. | After 5/3, n=6 closed. Backtest PF 1.31 on 1,249 trades supports the strategy. |

### Tier 5 — Environmentally silent, KEEP RUNNING (2 strategies)

| Strategy | Why silent is correct |
|---|---|
| forge_vix_revert | VIX < 30 for the entire window. The strategy is correctly waiting for a panic regime. Don't lower threshold. |
| forge_wick_gbpusd | Wick reversal needs choppy/compression regime. April was trending USD, naturally suppresses entries. Don't tighten/loosen until regime shifts. |

## Per-strategy specific actions, ranked by expected ROI

### Highest-ROI batch (this weekend, before next week)

1. **multi_orb window extension** (REWORK, ~30 min): change `breakout_window_bars` 24→32 at `forge/multi_orb/runner.py:71`. 9152 V2 samples support this. Highest-confidence change in the fleet.
2. **gdx_gld diagnose + restart** (~15 min): the runner is DOWN; backtest PF 1.88 says it's worth running. With tonight's TWS reconnect-with-backoff, it'll auto-recover from socket drops. Just start it in `--live` mode.
3. **Verify cuebanks/tori/mamba launch flags** (~30 min): check the actual launch commands in process manager / start_all_runners.ps1. If any are launched with `--signal-only`, remove that flag. These have backtest PF 2-3.6 on validated subsets — operational fix unlocks real edge.

### Medium-ROI batch (this week)

4. **Investigate argus_cadjpy / argus_gbpusd zero-fills** (~1-2h): 5,800+ signals with 28 ENTRY signals total but 0 broker fills. Either MTF gate problem OR fills failing. Check state.json for drawdown_pause; check broker reconciliation logs.
5. **Reconcile fomc_drift 4/28-4/29 trade** (~15 min): IBKR broker statement → manual write to trades.csv. Then strategy will have its first datapoint.
6. **Stop and archive Tier 1 KILL strategies** (~30 min): spy_mean_rev (already 0.0x), apollo, hermes, titan, rebalance. Update fleet_sizing.json dispositions. Removes 5 zombie processes from the fleet inventory.

### Larger batches (weekends, before 5/15)

7. **nq_london_close window expansion test** (~2h): backtest 15:00-17:00 UTC window on 60d historical, compare PF. If still ≥1.20, roll into runner by 5/20. Could 3x the entry frequency.
8. **vix_intraday 90d backtest comparison** (~2h): is April's drawdown a regime artifact or persistent edge issue? Run backtest on 90d historical to ground-truth.
9. **V2 SYMBOL_MAP expansion** (~1h): add NQ=F, USDJPY=X, CADJPY=X, GBPUSD=X to V2's SYMBOL_MAP. Currently V2 only covers 4 symbols. Expanding to ~10 unlocks counterfactual analysis on argus pairs + mamba + nq strategies.

## Fleet projection at 5/31

**Realistic survivor count: 1-3 strategies into real-money consideration.**

- **Primary real-money candidate**: `forge_nq_overnight` if it reaches n≥10 with PF≥2.0 (currently n=4, PF=5.11). Likely yes.
- **Secondary real-money candidate**: `forge_vix_intraday` if n≥50 with PF≥1.10 (currently n=40, PF=0.88). Coin flip.
- **Conditional 0.5x candidates** (3 possibles): `nq_london_close`, `gld_pm_long`, `jpy_pm_short` — depend on n≥10 by 5/31. Each contributes ~5-15% probability.
- **REWORK lottery tickets** (3 possibles): cuebanks/tori/mamba if launch fixes work AND backtest subsets validate live. Each ~25% probability.
- **Continuing paper through summer**: ~10-15 strategies that stay at OBSERVE pending more data.
- **Confirmed dead**: 5 strategies (Tier 1 above).

The honest 5/31 outcome is **a much smaller real-money fleet than the 22-strategy paper inventory**. Per `feedback_master_before_build`: master what we have. Most of the fleet will graduate to "either prove it or kill it" by mid-summer, not by 5/31.

## What we have NOT done yet (gaps remaining)

1. **No multi-instrument risk-on/risk-off correlation analysis**. If nq_overnight (QQQ-LONG) and gld_pm_long (gold-LONG) end up in the live fleet together, are they orthogonal or correlated? Devil flagged this; we haven't done the math.
2. **No per-strategy sample-size projection model**. We're eyeballing "will it hit n≥10 by 5/31?" — should be a deterministic projection from current entry-rate and remaining trading days.
3. **No backtest re-run for any 2026 data**. Every strategy's backtest PF is from historical (2020-2024 typical). The 2026 regime may differ. Critical for vix_intraday especially.
4. **No drill record for KILL_SWITCH / circuit breaker**. Items 11/12/14 in readiness gate. Operator hasn't done these yet.
5. **V2 SYMBOL_MAP is incomplete** — no counterfactual data for argus pairs, mamba, gdx_gld, fomc_drift, tom_international.
6. **No stop-loss-effectiveness analysis** for spy_mean_rev / multi_orb / vix_intraday — are stops being hit at random spots or at predictable structural levels? Could change which gates we tune.

## Update 2026-04-30 evening — verified findings + applied changes

After verifying the agent claims with file:line reads, here's what was applied in code (all changes are inert until runner restart per CLAUDE.md rule #1):

### Bug fixes applied
- **forge_cuebanks**: `score_confluence()` was missing `sd_zones` parameter at `forge/cuebanks/runner.py:843-852` (loop) and `:976-980` (live). Backtest mode at line 287-310 had it; loop+live didn't. Without `sd_zones`, the SCOPE_SD_SUPPLY_ZONE_ONLY filter could never match. **Fixed**: added `find_supply_demand_zones(df_h4, lookback_bars=60)` call before `score_confluence()` in both paths. This is the root cause of cuebanks producing zero signals despite RESEARCH_ONLY=False.

### Config changes applied
- **argus_cadjpy** stage `watcher` → `paper` in `argus_flow/configs/cadjpy_mtf_paper_v1.json` (was deployed in OBSERVE-only mode; now will execute paper trades).
- **argus_gbpusd** added `hour_filter: { enabled: false }` in `argus_flow/configs/gbpusd_range_paper_v1.json` (default profitable_hours [0,2,5,10,12,20,21,22,23] only overlapped session [7-20] at 10/12/20, killing 99.95% of signals).

### Launch script changes applied (`ops/start_all_runners.ps1`)
- `forge.tori.runner` `--loop` → `--live` (was signal-only; couldn't submit orders despite RESEARCH_ONLY=False)
- `forge.mamba.runner` `--loop` → `--live` (same reason)
- Removed `apollo.runner`, `hermes.runner`, `titan.runner`, `forge.rebalance_runner` from auto-launch (Tier 1 KILL)

### Strategy parameter changes applied
- **forge_multi_orb**: `breakout_window_bars` 24 → 32 in `forge/multi_orb/runner.py:71`. Version bumped `v2_qqq_only` → `v3_qqq_window32`. V2 9152-sample evidence: window expiry was costing alpha (+0.38% LONG counterfactual, only 22% saves).
- **forge_vix_intraday**: added `blackout_hours_utc: [17]` to PARAMS + gate check in `signal_check()`. Stop-loss analysis showed 80% stop rate on n=10 entries at 17:00 UTC.

### Verdict file updated
- apollo, hermes, titan, forge_rebalance: OBSERVE → KILL with reasoning logged in `_2026_04_30_amendment` field of `argus_flow/logs/verdict_20260501.json`.

### Analysis tooling expanded
- **`ops/sample_size_projection.py`** — deterministic per-strategy n forecast at 5/31. Output: only 3 strategies project n≥30 (multi_orb, vix_intraday, spy_mean_rev — but two of those are KILL or REWORK). 13 strategies project DEAD (no rate). Effectively 1 strategy (vix_intraday) at full statistical maturity at freeze.
- **`ops/stop_loss_effectiveness.py`** — diagnoses bad stops vs bad entries. Findings: multi_orb 83% / spy_mean_rev 94% of stops fire in first 25% of intended hold = BAD ENTRIES, not bad stops. 17:00 UTC unusable (77-80% stop rate) for spy_mean_rev and vix_intraday.
- **`ops/instrument_correlation.py`** — daily-return correlation between RM-candidate instruments. nq_overnight vs vix_intraday at -0.77 (inverse twins, must treat as ONE cluster). 4-strategy fleet has N_eff = 2.23 of 4 (only 55.7% truly independent). tom_international's 6 ETFs are 0.57-0.99 pairwise (one bet, not six).
- **`ops/block_outcome_v2.py`** SYMBOL_MAP expanded to 18 strategies; reads `argus_flow/logs/` and `forge/logs/` paths. Note: argus pairs (cadjpy/gbpusd/usdjpy), mamba, and gdx_gld emit signals.csv schemas without an `action` column, so V2 can't analyze them until runner CSV emit format updates.

### Restart helper
- **`ops/apply_game_plan_changes.ps1`** — review-only by default. Run with `-Execute` to actually stop Tier 1 KILL processes, restart affected runners, and refresh dashboard. Does NOT auto-apply.

## Decision discipline reminder

Per `feedback_iteration_style`: honest math, no hedging. The Devil's Advocate was right that most of the fleet won't make it to real money in May. The Resurrection Specialist was right that backtest evidence saves a few from premature death. The Performance Auditor was right that V2 evidence on multi_orb is the highest-confidence change we can make.

**By 5/15**: aim to have all Tier 1 KILLs stopped, all Tier 2 REWORKs deployed, and all Tier 3 diagnoses resolved.
**By 5/22**: have n-projection for every Tier 4 strategy.
**By 5/30**: final go/no-go for each survivor; close ceremony 5/31.
