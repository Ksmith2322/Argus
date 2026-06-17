# Static portfolios vs Argus benchmark

Generated: `2026-05-26T04:09:49.019523+00:00`
Window: 2007-05-30 → 2026-05-22 (20y nominal)
Static slippage: 5.0 bps per rebalance
Risk-free (annual): 4.0%

## Static reference portfolios

| Portfolio | CAGR | MaxDD | Sharpe | Sortino | Calmar | Worst-12m | Corr→SPY | 2008 DD | 2020 DD | 2022 DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SPY_100 | 8.69% | -56.47% | 0.318 | 0.393 | 0.154 | -44.75% | 1.0 | -56.47% | -34.1% | -25.36% |
| static_60_30_5_5_SPY_TLT_GLD_BIL | 6.26% | -34.18% | 0.242 | 0.314 | 0.183 | -26.99% | 0.905 | -34.18% | -18.38% | -25.14% |
| static_70_20_5_5_SPY_TLT_GLD_BIL | 7.05% | -40.38% | 0.282 | 0.358 | 0.175 | -31.92% | 0.967 | -40.38% | -21.69% | -24.49% |
| risk_parity_SPY_TLT_GLD_10vol | 5.38% | -27.67% | 0.18 | 0.25 | 0.194 | -24.12% | 0.454 | -17.97% | -14.7% | -26.44% |

Risk-parity weights (10% vol target): SPY 0.289, TLT 0.408, GLD 0.153

## Argus (xs_momentum 1.0× baseline)

- CAGR: **9.96%** (calendar-walk via _calendar_monthly_returns (5/25 truth fix))
- MaxDD: 23.86%
- Sharpe: 0.447  |  Sortino: 0.756  |  Calmar: 0.417
- Trades: 99  |  PF: 3.3  |  WR: 0.636
- Months in monthly_returns: 227

## Honest verdict

**Methodology note (read this first)**: Sharpe / DD comparisons are sensitive
to weighting and risk-free convention. This script uses (a) sum-capped weights
(no leverage; risk-parity that demands >1.0 sum is scaled down with the
remainder implicit cash), (b) daily-return Sharpe with explicit risk-free
subtraction at 4.0% annual,
(c) `auto_adjust=False` (price-only, no dividends). A more aggressive
risk-parity (no cap, total return) can reverse the xs_momentum vs risk-parity
Sharpe comparison. Both methodologies are defensible. Don't treat either as
dispositive on its own — read the operator-time / dollar-edge math in
`docs/decisions/2026_06_30_argus_deployment.md` for the actual decision input.

- xs_momentum Sharpe 0.447 vs risk-parity Sharpe 0.18: **WINS**
- xs_momentum MaxDD 23.86% vs risk-parity MaxDD 27.67%: **WINS**
- xs_momentum Calmar 0.417 vs risk-parity Calmar 0.194: **WINS**

---
Re-run via: `python -m ops.audit.run_static_vs_argus_benchmark`