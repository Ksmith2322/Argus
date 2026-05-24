# xs_momentum factor decomposition

Generated: 2026-05-24T05:01:55.177745+00:00

ETF-proxy Fama-French + Momentum regression (SPY / IWM / IWD / IWF / MTUM). Caveats in helio/factor_decomposition.py module docstring.

## Backtest summary

- **trades**: 48
- **win_rate**: 0.625
- **profit_factor**: 3.79
- **cagr_pct**: 18.04
- **max_drawdown_pct**: 22.39
- **portfolio_growth_pct**: 336.34
- **first_entry**: 2017-07-03T00:00:00+00:00
- **last_exit**: 2026-05-22T00:00:00+00:00

## Decomposition result

```
=== Factor decomposition: forge_xs_momentum (broad-8, 252-21, top-2) ===
period: 2017-07 - 2026-05  (106 months)

alpha (monthly):     +0.000225
alpha (annualized):  +0.270%  (t=+0.08, p=0.9384)
R²:                  0.549   adj R²: 0.531
Resid std (monthly): 2.96%
Information Ratio:   +0.03 (annualized)

factor         beta   std_err       t         p
--------------------------------------------------
MKT_RF       +0.691    0.0782   +8.84    0.0000
SMB          -0.097    0.0902   -1.07    0.2829
HML          +0.024    0.1127   +0.21    0.8347
MOM          +0.280    0.0972   +2.88    0.0040

INTERPRETATION:
  • ALPHA ZERO: +0.27%/yr after factor controls (p=0.938). Strategy returns are FULLY EXPLAINED by factor exposure — there is no manager skill component. The backtest return is beta, not alpha.
  • PASSIVE REPLICATION: 54.9% of returns explained by factors. The same exposure can be approximated with a static portfolio of +0.69*MKT_RF, +0.28*MOM. Execution costs + complexity of the active strategy may not be justified.
  • MOM beta = +0.28 (significant, p=0.004). The strategy has meaningful exposure to the momentum factor. A passive MTUM holding captures part of the same return cheaply.
  • R² = 0.549 (factors explain 54.9% of return variance).
  • Annualized Information Ratio (alpha / residual vol) = 0.03.
```