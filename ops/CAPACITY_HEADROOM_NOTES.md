# Capacity headroom — operator decision needed

Last reviewed: 2026-05-24

## Current state

`ops/audit/run_capacity_stress.py` reports `max_safe_multiplier = 1.0`
for every ACTIVE strategy — i.e. **at current per-strategy sizing,
none has 2× headroom**. The binding caps are:

| Layer | Setting | File |
|---|---|---|
| Per-strategy notional cap | `PER_STRATEGY_NOTIONAL_CAP_X` | `helio/cluster_exposure.py` |
| Per-cluster cap | `CLUSTER_CAPS` (e.g. `METALS=0.9`, `EQUITY_BETA=2.5`) | `helio/cluster_exposure.py` |
| Single-instrument cap | `SINGLE_INSTRUMENT_CAP_X = 0.6` | `helio/cluster_exposure.py` |
| Fleet total notional | `TOTAL_NOTIONAL_CAP_X = 1.5` | `helio/cluster_exposure.py` |
| Asset-class default | `notional_caps_by_asset_class.etf = 0.3` | `argus_flow/configs/fleet_sizing.json` |

For the post-reset roster the binding layer per strategy is:
- `forge_xs_momentum`: defaults to `PER_STRATEGY_DEFAULT_CAP = 0.4`
  (not explicitly listed). With 1.0× allocation_factor on a 2-pick
  basket, this is non-binding at $250K anchor.
- `forge_gld_pm_long`: `PER_STRATEGY_NOTIONAL_CAP_X = 0.4` (binding).
- `forge_tom_spy`, `forge_nov_spy`: PENDING_OPT_IN — would default to
  `0.4` once activated.

## Why this matters

When `max_safe_multiplier = 1.0`, the strategy *can* trade at its
configured size but **cannot scale**. Doubling the allocation factor
(or doubling the anchor through real-money deployment) would clip
against the per-strategy / cluster cap before reaching the intended
notional, producing silent under-sizing.

## Three options

1. **Stay at 1× (recommended for paper phase)**. The disciplined gate
   was scored at the realistic slippage of the *currently sized*
   positions. Raising the cap changes the slippage profile and would
   need a fresh gate-pass at the new size before being trusted.

2. **Raise `PER_STRATEGY_NOTIONAL_CAP_X` per-strategy after 30 days of
   clean live evidence**. Specifically once `live_pf_30trades` is in
   the disciplined-gate CI band AND `n_live_trades >= 30`, we have
   statistical grounds to claim the strategy works at current size
   and can take more.

3. **Raise `TOTAL_NOTIONAL_CAP_X = 1.5 -> 2.0`** to give the fleet
   1.5× → 2× leverage headroom. ONLY do this if (a) IBKR maintenance
   margin cushion is well above the 5/7 incident threshold (current
   `MAX_FLEET_MAINT_MARGIN_PCT = 0.5` keeps cushion comfortable) AND
   (b) the additional headroom is needed for a concrete strategy
   that has earned it.

## Decision log

- 2026-05-22: post-reset paper anchor $250K, all caps unchanged.
- 2026-05-24: capacity stress confirms 1× binding across all
  survivors. **No change recommended until 6/30 honest-path
  evidence review** (per
  `project_revised_2026_07_01_real_money_timeline.md`).

The capacity cap is **NOT a bug**; it is the survival-first sizing
discipline that protected the fleet on 5/7. Earn the higher cap
through clean live evidence, don't infer it from backtest stats.
