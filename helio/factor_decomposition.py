"""Factor decomposition — OLS regression of strategy returns against
Fama-French 3-factor + Momentum proxies.

The headline question every concentrated equity-like strategy must answer
is "am I generating real alpha, or am I just buying factor beta?". For a
cross-sectional momentum strategy in particular, the natural baseline is
the momentum factor — if our 1.0× holding has zero alpha after controlling
for MOM, the same exposure can be bought from MTUM at 0.15% expense ratio.

This module uses ETF PROXIES for the four factors rather than Ken French's
published series. The trade-off:
  - Pros: zero external data dependency (yfinance already in venv);
    works in offline sessions; no zip-download fragility.
  - Cons: proxies are imperfect — IWM-SPY isn't quite SMB, IWD-IWF isn't
    quite HML, MTUM-SPY captures roughly 60% of true MOM's variance.
    The alpha estimate is approximately right but should be confirmed
    against the true F-F factors before any real-money decision.

PROXIES:
  - Mkt-RF: SPY excess return over 3mo T-bill (assume 4.5% annual = 0.000174/day)
  - SMB    (Small Minus Big):    IWM - SPY
  - HML    (High Minus Low B/M): IWD - IWF
  - MOM    (Momentum factor):    MTUM - SPY

Caller passes a `monthly_returns` dict (YYYY-MM → float fraction); the
module fetches the same months from the factor proxies, aligns, runs OLS,
and reports alpha (annualized) + betas + t-stats + R² + Newey-West
adjusted standard errors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd


DEFAULT_RF_ANNUAL = 0.045  # 3mo T-bill ~ 4.5% as of 2026
FACTOR_TICKERS = {
    "MKT": "SPY",      # broad market proxy
    "SMB_LO": "IWM",   # Russell 2000 (small)
    "SMB_HI": "SPY",   # large
    "HML_LO": "IWD",   # Russell 1000 Value
    "HML_HI": "IWF",   # Russell 1000 Growth
    "MOM": "MTUM",     # iShares Momentum Factor
    # Cross-asset factors (added 2026-05-23 — required for strategies
    # that trade outside US equities)
    "DUR": "TLT",      # long Treasury (duration / safe-haven)
    "GOLD": "GLD",     # gold
    "INTL_DEV": "EFA", # international developed
    "INTL_EM": "EEM",  # emerging markets
}


@dataclass
class FactorBeta:
    name: str
    coef: float
    t_stat: float
    p_value: float
    std_err: float


@dataclass
class FactorDecompositionResult:
    label: str
    n_months: int
    period_start: str
    period_end: str
    alpha_monthly: float           # intercept, monthly fraction
    alpha_annualized: float        # alpha_monthly * 12 (linear, conservative)
    alpha_t_stat: float
    alpha_p_value: float
    r_squared: float
    r_squared_adj: float
    betas: list[FactorBeta] = field(default_factory=list)
    residual_std_monthly: float = 0.0
    information_ratio: float = 0.0   # annualized alpha / annualized resid std
    interpretation: str = ""
    raw_panel_csv: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "n_months": self.n_months,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "alpha_monthly": round(self.alpha_monthly, 6),
            "alpha_annualized_pct": round(self.alpha_annualized * 100, 3),
            "alpha_t_stat": round(self.alpha_t_stat, 3),
            "alpha_p_value": round(self.alpha_p_value, 5),
            "r_squared": round(self.r_squared, 4),
            "r_squared_adj": round(self.r_squared_adj, 4),
            "betas": [
                {"name": b.name, "coef": round(b.coef, 4),
                 "t_stat": round(b.t_stat, 3), "p_value": round(b.p_value, 5),
                 "std_err": round(b.std_err, 4)}
                for b in self.betas
            ],
            "residual_std_monthly_pct": round(self.residual_std_monthly * 100, 3),
            "information_ratio": round(self.information_ratio, 3),
            "interpretation": self.interpretation,
        }


# ─── factor proxy assembly ─────────────────────────────────────────────

def _fetch_monthly_closes(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Fetch daily closes via yfinance, resample to month-end. auto_adjust=True
    because factor returns must include dividends (which IS the SMB/HML signal
    on TLT etc.). See helio/yfinance_data.py convention block."""
    import yfinance as yf
    df = yf.download(tickers, start=start, end=end, interval="1d",
                       auto_adjust=True, progress=False,
                       group_by="ticker", threads=False)
    out: dict[str, pd.Series] = {}
    for t in tickers:
        if (t, "Close") in df.columns:
            out[t] = df[(t, "Close")]
        elif t in df.columns:
            out[t] = df[t]
    closes = pd.DataFrame(out).dropna(how="all")
    closes.index = pd.to_datetime(closes.index, utc=True).tz_convert(None)
    # Month-end last close
    monthly = closes.resample("ME").last()
    return monthly


def build_factor_panel(start: str, end: str,
                        rf_annual: float = DEFAULT_RF_ANNUAL,
                        include_cross_asset: bool = False,
                        source: str = "etf_proxy") -> pd.DataFrame:
    """Returns a DataFrame indexed by month-end with columns:
       MKT_RF, SMB, HML, MOM (and DUR, GOLD, INTL_DEV, INTL_EM if
       include_cross_asset=True). All as monthly fractions.

    For US-equity-only strategies, the 4-factor model is sufficient.
    For cross-asset strategies (xs_momentum on broad-8 universe etc.),
    pass include_cross_asset=True to add bond/gold/international
    factors — otherwise the regression will mistake cross-asset
    rotation for unexplained alpha.

    source="etf_proxy" (default) builds from yfinance ETFs (SPY/IWM/
    IWD/IWF/MTUM/TLT/GLD/EFA/EEM). Convenient but imperfect.

    source="kf" uses Ken French's published daily factors (MKT_RF/SMB/
    HML/MOM/RF), compounded to monthly. More accurate; required for
    load-bearing decisions. include_cross_asset is only meaningful
    when source="etf_proxy" — Ken French doesn't publish DUR/GOLD/INTL
    factors, so when source="kf" the cross-asset extras come from
    yfinance regardless (the KF 4-factor model is supplemented with
    the same ETF-proxy cross-asset terms)."""
    if source == "kf":
        return _build_factor_panel_kf(
            start, end, rf_annual=rf_annual,
            include_cross_asset=include_cross_asset,
        )
    if include_cross_asset:
        tickers = sorted({v for v in FACTOR_TICKERS.values()})
    else:
        # Drop the cross-asset tickers to match the classic 4-factor model
        skip = {"TLT", "GLD", "EFA", "EEM"}
        tickers = sorted({v for v in FACTOR_TICKERS.values() if v not in skip})
    monthly = _fetch_monthly_closes(tickers, start, end)
    rets = monthly.pct_change().dropna()
    rf_monthly = (1.0 + rf_annual) ** (1 / 12) - 1.0
    panel = pd.DataFrame(index=rets.index)
    panel["MKT_RF"] = rets["SPY"] - rf_monthly
    panel["SMB"] = rets["IWM"] - rets["SPY"]
    panel["HML"] = rets["IWD"] - rets["IWF"]
    panel["MOM"] = rets["MTUM"] - rets["SPY"]
    if include_cross_asset:
        panel["DUR"] = rets["TLT"] - rets["SPY"]
        panel["GOLD"] = rets["GLD"] - rets["SPY"]
        panel["INTL_DEV"] = rets["EFA"] - rets["SPY"]
        panel["INTL_EM"] = rets["EEM"] - rets["SPY"]
    return panel.dropna()


def _build_factor_panel_kf(start: str, end: str,
                            *,
                            rf_annual: float = DEFAULT_RF_ANNUAL,
                            include_cross_asset: bool = False) -> pd.DataFrame:
    """Build the factor panel from Ken French's published monthly factors.
    Cross-asset extras (DUR/GOLD/INTL_DEV/INTL_EM) still come from
    yfinance ETF proxies since French doesn't publish them."""
    from helio.fama_french_data import load_factors_monthly
    kf = load_factors_monthly()
    # Filter to window
    start_dt = pd.Timestamp(start).normalize()
    end_dt = pd.Timestamp(end).normalize()
    kf = kf[(kf.index >= start_dt) & (kf.index <= end_dt)]
    if kf.empty:
        raise ValueError(
            f"Ken French data has no rows in window {start} - {end}. "
            f"Check that the cached files cover this date range."
        )
    # rf_annual parameter is ignored when source='kf' — we use French's
    # actual RF (the 1-month T-bill rate column from his data).
    panel = pd.DataFrame(index=kf.index)
    panel["MKT_RF"] = kf["MKT_RF"]
    panel["SMB"] = kf["SMB"]
    panel["HML"] = kf["HML"]
    panel["MOM"] = kf["MOM"]
    panel["_RF"] = kf["RF"]  # carry through so the regressor can subtract correctly

    if include_cross_asset:
        skip = {"SPY"}  # SPY already implicit via MKT_RF; need other cross-asset
        ca_tickers = sorted({v for v in FACTOR_TICKERS.values()
                              if v not in skip and v in {"TLT", "GLD", "EFA",
                                                         "EEM", "IWM"}})
        # Always include SPY too as the subtraction reference
        ca_tickers = sorted(set(ca_tickers) | {"SPY"})
        monthly_close = _fetch_monthly_closes(ca_tickers, start, end)
        rets = monthly_close.pct_change().dropna()
        # Reindex to KF panel
        rets.index = rets.index.normalize()
        rets = rets.reindex(panel.index, method="nearest", tolerance=pd.Timedelta("5D"))
        panel["DUR"] = rets["TLT"] - rets["SPY"]
        panel["GOLD"] = rets["GLD"] - rets["SPY"]
        panel["INTL_DEV"] = rets["EFA"] - rets["SPY"]
        panel["INTL_EM"] = rets["EEM"] - rets["SPY"]
    return panel.dropna()


# ─── regression ────────────────────────────────────────────────────────

def regress_returns_against_factors(
    portfolio_monthly: dict[str, float],
    *,
    label: str = "",
    rf_annual: float = DEFAULT_RF_ANNUAL,
    use_newey_west: bool = True,
    include_cross_asset: bool = False,
    source: str = "etf_proxy",
) -> FactorDecompositionResult:
    """Run OLS portfolio_excess ~ MKT_RF + SMB + HML + MOM.

    portfolio_monthly: {"YYYY-MM": return_fraction, ...}
    Returns alpha (intercept) + factor betas with t-stats. If
    use_newey_west, applies Newey-West HAC standard errors with lag=3
    to handle residual serial correlation common in monthly strategy
    returns.
    """
    import statsmodels.api as sm

    if not portfolio_monthly:
        raise ValueError("portfolio_monthly is empty")

    months_sorted = sorted(portfolio_monthly)
    start = months_sorted[0] + "-01"
    # add 1 month buffer at end so resample sees the full final month
    end_year, end_month = months_sorted[-1].split("-")
    end_month_int = int(end_month) + 1
    end_year_int = int(end_year)
    if end_month_int > 12:
        end_month_int = 1
        end_year_int += 1
    end = f"{end_year_int:04d}-{end_month_int:02d}-01"

    panel = build_factor_panel(start=start, end=end, rf_annual=rf_annual,
                                  include_cross_asset=include_cross_asset,
                                  source=source)
    # Use KF's per-month RF when available (more accurate than a static
    # rf_annual); otherwise fall back to the rf_annual approximation.
    has_kf_rf = "_RF" in panel.columns
    # Index by YYYY-MM string for easy join
    panel.index = panel.index.strftime("%Y-%m")
    rf_monthly_static = (1.0 + rf_annual) ** (1 / 12) - 1.0

    factor_names = ["MKT_RF", "SMB", "HML", "MOM"]
    if include_cross_asset:
        factor_names = factor_names + ["DUR", "GOLD", "INTL_DEV", "INTL_EM"]

    rows = []
    for m in months_sorted:
        if m not in panel.index:
            continue
        rf_for_month = (float(panel.loc[m, "_RF"]) if has_kf_rf
                          else rf_monthly_static)
        port_excess = float(portfolio_monthly[m]) - rf_for_month
        row = {"month": m, "port_excess": port_excess}
        for fn in factor_names:
            row[fn] = panel.loc[m, fn]
        rows.append(row)
    if len(rows) < 12:
        raise ValueError(
            f"only {len(rows)} aligned months — need >=12 for meaningful regression. "
            f"Check portfolio_monthly keys (must be YYYY-MM) and date range."
        )
    df = pd.DataFrame(rows).set_index("month")
    y = df["port_excess"].values
    X = df[factor_names].values
    X = sm.add_constant(X)  # adds intercept column

    if use_newey_west:
        model = sm.OLS(y, X).fit(cov_type="HAC", cov_kwds={"maxlags": 3})
    else:
        model = sm.OLS(y, X).fit()

    params = model.params
    se = model.bse
    t_stats = model.tvalues
    p_values = model.pvalues
    alpha = float(params[0])
    alpha_t = float(t_stats[0])
    alpha_p = float(p_values[0])
    betas: list[FactorBeta] = []
    for i, name in enumerate(factor_names, start=1):
        betas.append(FactorBeta(
            name=name,
            coef=float(params[i]),
            t_stat=float(t_stats[i]),
            p_value=float(p_values[i]),
            std_err=float(se[i]),
        ))

    residuals = model.resid
    resid_std = float(np.std(residuals, ddof=len(factor_names) + 1))
    alpha_ann = alpha * 12.0
    resid_std_ann = resid_std * np.sqrt(12)
    ir = alpha_ann / resid_std_ann if resid_std_ann > 0 else 0.0

    interpretation = _interpret(alpha_ann, alpha_p, betas, model.rsquared, ir)

    return FactorDecompositionResult(
        label=label or "(unlabeled)",
        n_months=len(rows),
        period_start=months_sorted[0],
        period_end=months_sorted[-1],
        alpha_monthly=alpha,
        alpha_annualized=alpha_ann,
        alpha_t_stat=alpha_t,
        alpha_p_value=alpha_p,
        r_squared=float(model.rsquared),
        r_squared_adj=float(model.rsquared_adj),
        betas=betas,
        residual_std_monthly=resid_std,
        information_ratio=ir,
        interpretation=interpretation,
    )


def _interpret(alpha_ann: float, alpha_p: float,
                betas: list[FactorBeta], r2: float, ir: float) -> str:
    """Plain-English interpretation an operator can paste into the next
    weekly review."""
    bits: list[str] = []
    mom = next((b for b in betas if b.name == "MOM"), None)
    mkt = next((b for b in betas if b.name == "MKT_RF"), None)

    # Alpha interpretation
    if abs(alpha_ann) < 0.005 and alpha_p > 0.30:
        bits.append(
            f"ALPHA ZERO: {alpha_ann*100:+.2f}%/yr after factor controls "
            f"(p={alpha_p:.3f}). Strategy returns are FULLY EXPLAINED by "
            f"factor exposure — there is no manager skill component. The "
            f"backtest return is beta, not alpha."
        )
    elif alpha_ann > 0.03 and alpha_p < 0.05:
        bits.append(
            f"ALPHA POSITIVE: {alpha_ann*100:+.2f}%/yr after factor controls, "
            f"statistically significant (p={alpha_p:.3f}). Edge survives the "
            f"factor decomposition."
        )
    elif alpha_ann > 0 and alpha_p < 0.10:
        bits.append(
            f"ALPHA MARGINAL: {alpha_ann*100:+.2f}%/yr (p={alpha_p:.3f}). "
            f"Edge exists but is not statistically robust at the 5% level — "
            f"could be noise."
        )
    elif alpha_ann > 0:
        bits.append(
            f"ALPHA POSITIVE BUT NOISY: {alpha_ann*100:+.2f}%/yr (p={alpha_p:.3f} >= 0.10). "
            f"Cannot reject 'this strategy has zero alpha after factor exposure'."
        )
    else:
        bits.append(
            f"ALPHA NEGATIVE: {alpha_ann*100:+.2f}%/yr (p={alpha_p:.3f}). "
            f"Strategy underperforms a passive factor-replicating portfolio."
        )

    # Replicability assessment — if R² > 0.40 and alpha is zero, the
    # strategy is essentially a factor-mimicking portfolio.
    if r2 > 0.40 and abs(alpha_ann) < 0.02:
        # Find the dominant factors
        dominant = sorted(
            [b for b in betas if b.p_value < 0.05 and abs(b.coef) > 0.1],
            key=lambda b: abs(b.coef), reverse=True,
        )
        if dominant:
            recipe = ", ".join(f"{b.coef:+.2f}*{b.name}" for b in dominant)
            bits.append(
                f"PASSIVE REPLICATION: {r2*100:.1f}% of returns explained "
                f"by factors. The same exposure can be approximated with a "
                f"static portfolio of {recipe}. Execution costs + complexity "
                f"of the active strategy may not be justified."
            )

    if mom and abs(mom.coef) > 0.2 and mom.p_value < 0.05:
        bits.append(
            f"MOM beta = {mom.coef:+.2f} (significant, p={mom.p_value:.3f}). "
            f"The strategy has meaningful exposure to the momentum factor. "
            f"A passive MTUM holding captures part of the same return cheaply."
        )
    elif mom and abs(mom.coef) > 0.3:
        bits.append(
            f"MOM beta = {mom.coef:+.2f} (large but not stat-sig, "
            f"p={mom.p_value:.3f}). Behavior is momentum-like but the "
            f"relationship is noisy."
        )
    elif mom:
        bits.append(
            f"MOM beta = {mom.coef:+.2f} (small, p={mom.p_value:.3f}). "
            f"Cross-asset rotation behaves DIFFERENTLY from single-asset "
            f"momentum."
        )

    if mkt and mkt.coef > 0.7:
        bits.append(
            f"MKT beta = {mkt.coef:+.2f} — high market exposure. The strategy "
            f"is mostly a long-equity position with factor tilts."
        )

    bits.append(f"R² = {r2:.3f} (factors explain {r2*100:.1f}% of return variance).")
    bits.append(f"Annualized Information Ratio (alpha / residual vol) = {ir:.2f}.")

    return "  ".join(bits)


def render_report(result: FactorDecompositionResult) -> str:
    """Pretty-print for stdout/markdown."""
    d = result.to_dict()
    lines = []
    lines.append(f"=== Factor decomposition: {d['label']} ===")
    lines.append(f"period: {d['period_start']} - {d['period_end']}  ({d['n_months']} months)")
    lines.append("")
    lines.append(f"alpha (monthly):     {d['alpha_monthly']:+.6f}")
    lines.append(f"alpha (annualized):  {d['alpha_annualized_pct']:+.3f}%  "
                  f"(t={d['alpha_t_stat']:+.2f}, p={d['alpha_p_value']:.4f})")
    lines.append(f"R²:                  {d['r_squared']:.3f}   adj R²: {d['r_squared_adj']:.3f}")
    lines.append(f"Resid std (monthly): {d['residual_std_monthly_pct']:.2f}%")
    lines.append(f"Information Ratio:   {d['information_ratio']:+.2f} (annualized)")
    lines.append("")
    lines.append(f"{'factor':<10} {'beta':>8} {'std_err':>9} {'t':>7} {'p':>9}")
    lines.append("-" * 50)
    for b in d["betas"]:
        lines.append(
            f"{b['name']:<10} {b['coef']:+8.3f} {b['std_err']:9.4f} "
            f"{b['t_stat']:+7.2f} {b['p_value']:9.4f}"
        )
    lines.append("")
    lines.append("INTERPRETATION:")
    for chunk in d["interpretation"].split("  "):
        if chunk.strip():
            lines.append(f"  • {chunk.strip()}")
    return "\n".join(lines)
