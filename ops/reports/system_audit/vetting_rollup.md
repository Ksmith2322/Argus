# Weekly vetting rollup -- live PF vs backtest CI

Generated: `2026-05-26T19:04:16.142992+00:00`
Epoch: `post_reset_20260522` (started 2026-05-22T23:14:53.156216+00:00)
Total EXIT fills in epoch: **3**
Promotion PF floor: 1.2

## Summary: 0 FAIL / 0 UNDER / 0 TRACKING / 0 OVER / 15 TOO_FEW

| Strategy | Status | n_live | PF live | PF backtest | CI live | CI backtest | $ PnL | Family |
|---|---|---:|---:|---:|---|---|---:|---|
| `forge_xs_momentum` | **TOO_FEW_FILLS** | 0 | -- | 2.05 | -- | [1.43, 4.03] | +0.00 | xs_momentum_broad_8 |
| `forge_xs_momentum_sectors` | **TOO_FEW_FILLS** | 0 | -- | 2.50 | -- | [1.44, 4.55] | +0.00 | xs_momentum_spdr_11 |
| `forge_xs_momentum_style` | **TOO_FEW_FILLS** | 0 | -- | 3.78 | -- | [1.85, 7.40] | +0.00 | xs_momentum_style_8 |
| `forge_xs_momentum_legacy15` | **TOO_FEW_FILLS** | 0 | -- | 2.22 | -- | [1.38, 3.85] | +0.00 | xs_momentum_sectors_countries_15 |
| `forge_xs_momentum_style_top3` | **TOO_FEW_FILLS** | 0 | -- | 5.40 | -- | [2.70, 12.81] | +0.00 | xs_momentum_style_8_top3 |
| `forge_xs_momentum_legacy15_regime` | **TOO_FEW_FILLS** | 0 | -- | 2.22 | -- | [1.38, 3.85] | +0.00 | xs_momentum_sectors_countries_15_regime |
| `forge_xs_momentum_global47` | **TOO_FEW_FILLS** | 0 | -- | 2.33 | -- | [1.21, 4.10] | +0.00 | xs_momentum_global_47 |
| `forge_tail_hedge` | **TOO_FEW_FILLS** | 0 | -- | 2.91 | -- | [1.40, 5.85] | +0.00 | tail_hedge_regime |
| `forge_tom_spy` | **TOO_FEW_FILLS** | 0 | -- | 1.79 | -- | [1.28, 2.50] | +0.00 | tom_calendar |
| `forge_nov_spy` | **TOO_FEW_FILLS** | 0 | -- | 4.79 | -- | [1.46, 14.50] | +0.00 | month_holding_nov |
| `forge_gld_pm_long` | **TOO_FEW_FILLS** | 0 | -- | 1.08 | -- | [0.90, 1.30] | +0.00 | pm_pattern_intraday |
| `forge_uso_pm_long` | **TOO_FEW_FILLS** | 0 | -- | 1.40 | -- | [1.20, 1.75] | +0.00 | pm_pattern_intraday |
| `forge_ewz_breakout` | **TOO_FEW_FILLS** | 0 | -- | 1.73 | -- | [1.27, 2.45] | +0.00 | breakout_21d |
| `forge_ief_jul_hold` | **TOO_FEW_FILLS** | 0 | -- | 12.14 | -- | [4.21, 38.50] | +0.00 | month_holding_jul |
| `forge_gld_jan_hold` | **TOO_FEW_FILLS** | 0 | -- | 4.23 | -- | [1.46, 12.50] | +0.00 | month_holding_jan |

## Action items (highest priority first)

_None._ All strategies with sufficient fills are tracking or outperforming their backtest CI.

## Cadence check (strategies that should have fired but haven't)

- `forge_gld_pm_long`: expected ~1.6 fills by now, has 0 live. Check runner health + heartbeat.
- `forge_uso_pm_long`: expected ~2.7 fills by now, has 0 live. Check runner health + heartbeat.