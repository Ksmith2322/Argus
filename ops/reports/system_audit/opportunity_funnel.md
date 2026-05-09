# Opportunity Funnel

Increase throughput only by finding more valid opportunities, not by degrading filters.

- `argus_cadjpy`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `argus_gbpusd`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `argus_usdjpy`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `cadjpy_mtf`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_atlas`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_aud_asian_breakout`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_cuebanks`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_fomc_drift`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_gdx_gld`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_gld_pm_long`: constraint `sample_accumulation`. Continue paper/shadow until evidence gate clears.
- `forge_jpy_pm_short`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_mamba`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_multi_orb`: constraint `killed_do_not_increase`. No live-throughput action.
- `forge_nq_london_close`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `forge_nq_overnight`: constraint `sample_accumulation`. Continue paper/shadow until evidence gate clears.
- `forge_rebalance`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_spy_mean_rev`: constraint `killed_do_not_increase`. No live-throughput action.
- `forge_spy_trend_follower`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_themis`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_tom_international`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_tori`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_vix_intraday`: constraint `sample_accumulation`. Continue paper/shadow until evidence gate clears.
- `forge_vix_revert`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `forge_wick_gbpusd`: constraint `filters_or_risk_gate`. Trace blocked signals and compare MTF/pass thresholds to backtest expected rate; do not loosen blindly.
- `gbpusd_range`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `signals`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
- `usdjpy_mtf`: constraint `idle_or_uninstrumented`. Verify launch flags, heartbeat truth, and signal logging before judging edge.
