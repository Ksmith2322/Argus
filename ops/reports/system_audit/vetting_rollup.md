# Weekly vetting rollup -- live PF vs backtest CI

Generated: `2026-05-26T22:33:51.937398+00:00`
Epoch: `post_reset_20260522` (started 2026-05-22T23:14:53.156216+00:00)
Total EXIT fills in epoch: **3**
Promotion PF floor: 1.2

## Summary: 0 FAIL / 0 UNDER / 0 TRACKING / 0 OVER / 4 TOO_FEW

## Fleet-combined CAGR vs SPY-net-of-tax bar

- Allocation sum (deployed risk): **3.50x**
- Allocation-weighted pre-tax CAGR: **13.74%**
- After-tax (short-term @ 39%): **8.38%**
- SPY long-term baseline after-tax: 8.0%
- Delta vs SPY net: **+0.38pp**  -->  **PASSES** (mandatory floor pre-tax CAGR >= 13%)

| Strategy | Status | n_live | PF live | PF backtest | CI live | CI backtest | $ PnL | CAGR (pre-tax) | vs SPY net | Family |
|---|---|---:|---:|---:|---|---|---:|---:|---|---|
| `forge_tail_hedge` | **TOO_FEW_FILLS** | 0 | -- | 2.91 | -- | [1.40, 5.85] | +0.00 | 5.0% | -5.0pp ✗ | tail_hedge_regime |
| `forge_gld_pm_long` | **TOO_FEW_FILLS** | 0 | -- | 1.08 | -- | [0.90, 1.30] | +0.00 | 5.0% | -5.0pp ✗ | pm_pattern_intraday |
| `forge_uso_pm_long` | **TOO_FEW_FILLS** | 0 | -- | 1.40 | -- | [1.20, 1.75] | +0.00 | 12.0% | -0.7pp ✗ | pm_pattern_intraday |
| `[SLEEVE] xs_momentum_sleeve` | **TOO_FEW_FILLS** | 0 | -- | 2.43 | -- | [1.21, 5.40] | +0.00 | 14.5% | +0.8pp ✓ | xs_momentum_combined |

## Action items (highest priority first)

_None._ All strategies with sufficient fills are tracking or outperforming their backtest CI.

## Cadence check (strategies that should have fired but haven't)

- `forge_gld_pm_long`: expected ~1.6 fills by now, has 0 live. Check runner health + heartbeat.
- `forge_uso_pm_long`: expected ~2.8 fills by now, has 0 live. Check runner health + heartbeat.