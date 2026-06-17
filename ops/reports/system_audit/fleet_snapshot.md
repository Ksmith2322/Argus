# Argus fleet snapshot

Generated: 2026-05-24T18:00:18.966203+00:00

## Roster classification

  ACTIVE=2  PENDING_OPT_IN=2  KILLED=28  LIMBO=0  ABANDONED=0

| Strategy | Verdict | Alloc | Runner | Kill date |
|---|---|---|---|---|
| apollo | KILLED | 0.0 | no | 2026-05-20 |
| argus_cadjpy | KILLED | 0.0 | no | 2026-05-20 |
| argus_gbpusd | KILLED | 0.0 | no | 2026-05-20 |
| argus_usdjpy | KILLED | 0.0 | no | 2026-05-20 |
| forge_atlas | KILLED | 0.0 | yes | 2026-05-20 |
| forge_aud_asian_breakout | KILLED | 0.0 | yes | 2026-05-20 |
| forge_coint_pairs | KILLED | 0.0 | yes | 2026-05-20 |
| forge_cuebanks | KILLED | 0.0 | yes | 2026-05-20 |
| forge_fomc_drift | KILLED | 0.0 | yes | 2026-05-20 |
| forge_gdx_gld | KILLED | 0.0 | no | 2026-05-20 |
| forge_gld_pm_long | ACTIVE | 0.5 | yes |  |
| forge_jpy_pm_short | KILLED | 0.0 | yes | 2026-05-20 |
| forge_mamba | KILLED | 0.0 | yes | 2026-05-20 |
| forge_multi_orb | KILLED | 0.0 | yes | 2026-05-07 |
| forge_nov_spy | PENDING_OPT_IN | 0.0 | yes |  |
| forge_nq_london_close | KILLED | 0.0 | yes | 2026-05-13 |
| forge_nq_overnight | KILLED | 0.0 | yes | 2026-05-23 |
| forge_pead | KILLED | 0.0 | yes | 2026-05-23 |
| forge_rebalance | KILLED | 0.0 | no | 2026-05-20 |
| forge_spy_mean_rev | KILLED | 0.0 | yes | 2026-04-30 |
| forge_spy_trend_follower | KILLED | 0.0 | yes | 2026-05-23 |
| forge_themis | KILLED | 0.0 | yes | 2026-05-20 |
| forge_tom_international | KILLED | 0.0 | yes | 2026-05-20 |
| forge_tom_spy | PENDING_OPT_IN | 0.0 | yes |  |
| forge_tori | KILLED | 0.0 | yes | 2026-05-20 |
| forge_vix_carry | KILLED | 0.0 | yes | 2026-05-19 |
| forge_vix_intraday | KILLED | 0.0 | yes | 2026-05-12 |
| forge_vix_revert | KILLED | 0.0 | no | 2026-05-20 |
| forge_wick_gbpusd | KILLED | 0.0 | yes | 2026-05-20 |
| forge_xs_momentum | ACTIVE | 1.0 | yes |  |
| hermes | KILLED | 0.0 | no | 2026-05-20 |
| titan | KILLED | 0.0 | no | 2026-05-20 |

## Allocation factors

Version: 2026-05-24.v14_formalize_kill_registry
Last updated: 2026-05-24T17:30:00.000000+00:00

Recent kill_log entries:
- 2026-05-19: forge_xs_momentum -> 0.0 through 5/31 reset (PAPER_CANDIDATE post-reset). Built today as Architect audit #3 (cross-sectional 12-1 momentum, long top-quintile of 15-ETF universe = 10 US sec...
- 2026-05-20: forge_coint_pairs -> 0.0 (RESEARCH_ONLY birth-state). Architect #5 candidate. 5y backtest with 8 default pairs (KO/PEP, XOM/CVX, HD/LOW, GLD/SLV, EWZ/EWW, TLT/IEF, V/MA, MSFT/GOOGL): n=184...
- 2026-05-20: SUNSET BATCH — 16 strategies set to 0.0 per project_2026_05_31_sunset_decisions.md. (a) 3 argus FX pairs (gbpusd/usdjpy/cadjpy) — silent + risk-vs-notional cap conflict. (b) 3 YM YouTube r...
- 2026-05-23: forge_pead 0.5 -> 0.0 AND forge_nq_overnight 0.5 -> 0.0. Slippage-recalibration sprint (ops/audit/run_slippage_recalibration.py 2026-05-23) confirmed both fail the disciplined gate at real...
- 2026-05-24: FORMALIZED 18 LIMBO STRATEGIES INTO KILL REGISTRY (per operator instruction — 'make sure poor performers are killed; archive in a month if no revival'). All were already at allocation=0.0 ...

## Capacity stress (max_safe_multiplier per strategy)

Anchor: $250,000  Computed: 2026-05-24T15:42:44.875902+00:00

| Strategy | current $ | max_safe_mult |
|---|---|---|
| forge_nq_overnight | $29,529 | 5.0x |
| forge_spy_mean_rev | $718 | 10.0x |
| forge_xs_momentum | $100,000 | 1.0x |
| forge_tom_spy | $100,000 | 1.0x |
| forge_gld_pm_long | $100,000 | 1.0x |

## Heartbeats

| Strategy | Age (hours) | Heartbeat ts |
|---|---|---|
| forge_atlas | 42.82 | 2026-05-22T23:10:53.015149+00:00 |
| forge_aud_asian_breakout | 42.92 | 2026-05-22T23:05:00.313126+00:00 |
| forge_coint_pairs | MISSING ⚠️ | MISSING |
| forge_cuebanks | 42.82 | 2026-05-22T23:11:06.790622+00:00 |
| forge_fomc_drift | 43.11 | 2026-05-22T22:53:35.366859+00:00 |
| forge_gdx_gld | 0.01 | 2026-05-24T17:59:43.977378+00:00 |
| forge_gld_pm_long | 0.92 | 2026-05-24T17:05:00.549495+00:00 |
| forge_jpy_pm_short | 43.0 | 2026-05-22T23:00:30.535885+00:00 |
| forge_mamba | 42.81 | 2026-05-22T23:11:49.524294+00:00 |
| forge_multi_orb | 396.99 ⚠️ | 2026-05-08T05:00:50.351384+00:00 |
| forge_nov_spy | MISSING ⚠️ | MISSING |
| forge_nq_london_close | 42.84 | 2026-05-22T23:10:05.350337+00:00 |
| forge_nq_overnight | 1.0 | 2026-05-24T17:00:30.357032+00:00 |
| forge_pead | 4.42 | 2026-05-24T13:35:04.274494+00:00 |
| forge_rebalance | 42.82 | 2026-05-22T23:11:05.223795+00:00 |
| forge_spy_mean_rev | 565.5 ⚠️ | 2026-05-01T04:30:10.483066+00:00 |
| forge_spy_trend_follower | 1.56 | 2026-05-24T16:26:26.851668+00:00 |
| forge_themis | 45.64 | 2026-05-22T20:22:03.461354+00:00 |
| forge_tom_international | 43.11 | 2026-05-22T22:53:39.311495+00:00 |
| forge_tom_spy | MISSING ⚠️ | MISSING |
| forge_tori | 43.69 | 2026-05-22T22:19:08.303650+00:00 |
| forge_vix_carry | MISSING ⚠️ | MISSING |
| forge_vix_intraday | 292.75 ⚠️ | 2026-05-12T13:15:15.356977+00:00 |
| forge_vix_revert | 43.48 | 2026-05-22T22:31:45.497250+00:00 |
| forge_wick_gbpusd | 42.92 | 2026-05-22T23:05:19.638870+00:00 |
| forge_xs_momentum | 0.09 | 2026-05-24T17:54:59.334360+00:00 |

## Canonical fills since epoch

Epoch: ?
Total fills: 0

## xs_momentum current top picks

As of: ?

Picks (would hold if rebalance fired today):

Full ranking:

## Real-money preflight (active strategies)

- **forge_gld_pm_long**: BLOCKED  (GREEN=6 YELLOW=2 RED=4)
- **forge_xs_momentum**: BLOCKED  (GREEN=7 YELLOW=2 RED=3)