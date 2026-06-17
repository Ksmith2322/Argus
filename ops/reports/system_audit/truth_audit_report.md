# Truth Audit Report

Critical truth failures are separated from dashboard/cosmetic issues.

- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (``). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (`forge/logs/atlas/heartbeat.json`). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING PRE_RESET_DATA_CONTAMINATION**: Strategy has both pre-reset and post-reset trade rows (`forge/logs/gld_pm_long/trades.csv`). Fix: Keep pre-reset diagnostic only; exclude from promotion score.
- **WARNING PRE_RESET_DATA_CONTAMINATION**: Strategy has both pre-reset and post-reset trade rows (`forge/logs/multi_orb/trades.csv`). Fix: Keep pre-reset diagnostic only; exclude from promotion score.
- **WARNING DASHBOARD_TRUTH_MISMATCH**: Killed strategy still has PnL-bearing artifacts (`forge/logs/multi_orb/trades.csv`). Fix: Dashboard must label killed/legacy series, never render as active flat equity.
- **WARNING PHANTOM_TRADE_EXCLUDED**: Post-reset phantom-sized trades excluded from scorecard via sidecar annotations (`forge/logs/multi_orb/trades.csv`). Fix: Use phantom_trade_annotations.csv for promotion, PnL, and ROI decisions; keep raw artifacts immutable.
- **WARNING PHANTOM_TRADE_EXCLUDED**: Post-reset phantom-sized trades excluded from scorecard via sidecar annotations (`forge/logs/nq_london_close/trades.csv`). Fix: Use phantom_trade_annotations.csv for promotion, PnL, and ROI decisions; keep raw artifacts immutable.
- **WARNING PRE_RESET_DATA_CONTAMINATION**: Strategy has both pre-reset and post-reset trade rows (`forge/logs/nq_overnight/trades.csv`). Fix: Keep pre-reset diagnostic only; exclude from promotion score.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (`forge/logs/rebalance/heartbeat.json`). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (`forge/logs/themis/heartbeat.json`). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (`forge/logs/tori/heartbeat.json`). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING PRE_RESET_DATA_CONTAMINATION**: Strategy has both pre-reset and post-reset trade rows (`forge/logs/vix_intraday/trades.csv`). Fix: Keep pre-reset diagnostic only; exclude from promotion score.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (`forge/logs/vix_revert/heartbeat.json`). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (``). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (``). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **WARNING STALE_STATE**: Ghost strategy has no trades and no signal evidence (``). Fix: Classify as BLOCKED/IDLE/LEGACY; do not score as stable performer.
- **INFO HALT_TRUTH_RECONCILED**: Execution halt checks use reconciled halt truth (`helio/halt_state.py`). Fix: Keep dashboard/API/runtime readers pointed at the same halt-state module.
