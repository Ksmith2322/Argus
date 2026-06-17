# xs_momentum factor decomposition

Generated: 2026-05-24T12:59:05.110955+00:00

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
=== Factor decomposition: forge_xs_momentum vs US 4-factor (MKT/SMB/HML/MOM) ===
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

=== Factor decomposition: forge_xs_momentum vs 8-factor (+DUR/GOLD/INTL_DEV/INTL_EM) ===
period: 2017-07 - 2026-05  (106 months)

alpha (monthly):     -0.001502
alpha (annualized):  -1.802%  (t=-0.70, p=0.4809)
R²:                  0.719   adj R²: 0.695
Resid std (monthly): 2.39%
Information Ratio:   -0.22 (annualized)

factor         beta   std_err       t         p
--------------------------------------------------
MKT_RF       +1.074    0.0963  +11.15    0.0000
SMB          -0.003    0.0744   -0.04    0.9721
HML          -0.042    0.0639   -0.66    0.5064
MOM          +0.279    0.0865   +3.23    0.0013
DUR          +0.102    0.0740   +1.38    0.1682
GOLD         +0.348    0.0929   +3.74    0.0002
INTL_DEV     -0.027    0.1167   -0.23    0.8171
INTL_EM      +0.096    0.0997   +0.96    0.3352

INTERPRETATION:
  • ALPHA NEGATIVE: -1.80%/yr (p=0.481). Strategy underperforms a passive factor-replicating portfolio.
  • PASSIVE REPLICATION: 71.9% of returns explained by factors. The same exposure can be approximated with a static portfolio of +1.07*MKT_RF, +0.35*GOLD, +0.28*MOM. Execution costs + complexity of the active strategy may not be justified.
  • MOM beta = +0.28 (significant, p=0.001). The strategy has meaningful exposure to the momentum factor. A passive MTUM holding captures part of the same return cheaply.
  • MKT beta = +1.07 — high market exposure. The strategy is mostly a long-equity position with factor tilts.
  • R² = 0.719 (factors explain 71.9% of return variance).
  • Annualized Information Ratio (alpha / residual vol) = -0.22.

=== Factor decomposition: forge_xs_momentum vs Ken French 4-factor ===
period: 2017-07 - 2026-05  (105 months)

alpha (monthly):     -0.000894
alpha (annualized):  -1.073%  (t=-0.35, p=0.7251)
R²:                  0.620   adj R²: 0.605
Resid std (monthly): 2.72%
Information Ratio:   -0.11 (annualized)

factor         beta   std_err       t         p
--------------------------------------------------
MKT_RF       +0.812    0.0634  +12.80    0.0000
SMB          -0.093    0.0929   -1.00    0.3164
HML          -0.038    0.0710   -0.54    0.5915
MOM          +0.367    0.0922   +3.98    0.0001

INTERPRETATION:
  • ALPHA NEGATIVE: -1.07%/yr (p=0.725). Strategy underperforms a passive factor-replicating portfolio.
  • PASSIVE REPLICATION: 62.0% of returns explained by factors. The same exposure can be approximated with a static portfolio of +0.81*MKT_RF, +0.37*MOM. Execution costs + complexity of the active strategy may not be justified.
  • MOM beta = +0.37 (significant, p=0.000). The strategy has meaningful exposure to the momentum factor. A passive MTUM holding captures part of the same return cheaply.
  • MKT beta = +0.81 — high market exposure. The strategy is mostly a long-equity position with factor tilts.
  • R² = 0.620 (factors explain 62.0% of return variance).
  • Annualized Information Ratio (alpha / residual vol) = -0.11.

=== Factor decomposition: forge_xs_momentum vs KF 4-factor + ETF cross-asset ===
period: 2017-07 - 2026-05  (104 months)

alpha (monthly):     -0.002403
alpha (annualized):  -2.884%  (t=-1.23, p=0.2195)
R²:                  0.764   adj R²: 0.744
Resid std (monthly): 2.19%
Information Ratio:   -0.38 (annualized)

factor         beta   std_err       t         p
--------------------------------------------------
MKT_RF       +1.125    0.0835  +13.47    0.0000
SMB          -0.069    0.0716   -0.96    0.3360
HML          -0.051    0.0583   -0.87    0.3821
MOM          +0.361    0.0731   +4.94    0.0000
DUR          +0.065    0.0659   +0.99    0.3229
GOLD         +0.260    0.0809   +3.21    0.0013
INTL_DEV     +0.072    0.1118   +0.64    0.5213
INTL_EM      +0.158    0.0923   +1.71    0.0880

INTERPRETATION:
  • ALPHA NEGATIVE: -2.88%/yr (p=0.220). Strategy underperforms a passive factor-replicating portfolio.
  • MOM beta = +0.36 (significant, p=0.000). The strategy has meaningful exposure to the momentum factor. A passive MTUM holding captures part of the same return cheaply.
  • MKT beta = +1.13 — high market exposure. The strategy is mostly a long-equity position with factor tilts.
  • R² = 0.764 (factors explain 76.4% of return variance).
  • Annualized Information Ratio (alpha / residual vol) = -0.38.
```