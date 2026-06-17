"""Turn-of-quarter calendar effect backtest for SPY.

Thesis (Etula-Rinne-Suominen-Vaittinen 2020): pension fund + 401k
contributions land at quarter-start; index funds must put cash to
work in the first 3 trading days. Predictable buy-pressure on SPY,
distinct from monthly TOM effect (forge_tom_spy).

Rule (Strategy agent #2 per docs/AUDIT_2026_05_25_PART2/STRATEGY.md):
  - ENTRY: at close of LAST trading day of each quarter (Mar/Jun/Sep/Dec)
  - EXIT:  at close of 3rd trading day of new quarter
  - 4 round-trips per year × 20y = ~80 trades
  - Hold time: 3-4 trading days

Pre-backtest expectation: PF 1.8-2.5 at 5bps, n=80, CAGR contribution
+1.5-2.5%, max DD 8-12%.

Data: yfinance SPY daily Close. 20y backtest 2005-2026.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from helio.bootstrap_stats import block_bootstrap_profit_factor
from helio.yfinance_cache import download_cached


DEFAULT_SLIPPAGE_BPS_RT = 10.0
DEFAULT_EXIT_OFFSET = 3      # 3rd trading day of new quarter
DEFAULT_PF_FLOOR = 1.20

QUARTER_END_MONTHS = (3, 6, 9, 12)


@dataclass(frozen=True)
class TOQTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_px: float
    exit_px: float
    holding_days: int
    gross_ret_pct: float
    net_ret_pct: float


@dataclass
class TOQBacktest:
    ticker: str = "SPY"
    n_trades: int = 0
    trades: list[TOQTrade] = field(default_factory=list)
    period_label: str = ""
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT
    exit_offset: int = DEFAULT_EXIT_OFFSET
    error: Optional[str] = None
    win_rate: float = 0.0
    pf: float = 0.0
    avg_ret_pct: float = 0.0
    total_ret_pct_compounded: float = 0.0
    max_dd_pct: float = 0.0
    pf_ci_lower: float = 0.0
    pf_ci_upper: float = 0.0
    avg_holding_days: float = 0.0

    def pnls(self) -> list[float]:
        return [t.net_ret_pct for t in self.trades]


def _close_series(ticker: str, period: str) -> pd.Series:
    df = download_cached(
        ticker, period=period, interval="1d",
        fresh_seconds=0, allow_stale_fallback=True, auto_adjust=False,
    )
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    cols = {c.lower(): c for c in df.columns}
    return df[cols.get("close") or cols.get("adj close")].astype(float)


def _last_trading_day_of(year: int, month: int, dates: pd.DatetimeIndex) -> pd.Timestamp | None:
    """Last trading day of (year, month) present in dates."""
    series = pd.Series(dates)
    mask = (series.dt.year == year) & (series.dt.month == month)
    if not mask.any():
        return None
    return series[mask].iloc[-1]


def backtest_turn_of_quarter(
    *,
    ticker: str = "SPY",
    period: str = "20y",
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT,
    exit_offset: int = DEFAULT_EXIT_OFFSET,
) -> TOQBacktest:
    out = TOQBacktest(
        ticker=ticker, period_label=period,
        slippage_bps_rt=slippage_bps_rt, exit_offset=exit_offset,
    )
    try:
        s = _close_series(ticker, period)
    except Exception as exc:
        out.error = f"fetch failed: {exc!r}"
        return out
    if s is None or s.empty:
        out.error = "empty data"
        return out

    s = s.dropna()
    dates = s.index
    drag = slippage_bps_rt / 100.0

    # For each quarter in the data, find entry date (last trading day of
    # quarter-end month) and exit date (Nth trading day of next month).
    seen_years = sorted(set(d.year for d in dates))
    for year in seen_years:
        for q_month in QUARTER_END_MONTHS:
            entry_dt = _last_trading_day_of(year, q_month, dates)
            if entry_dt is None:
                continue
            # Exit: Nth trading day starting from the day AFTER entry_dt
            try:
                entry_idx = dates.get_loc(entry_dt)
            except KeyError:
                continue
            exit_idx = entry_idx + exit_offset
            if exit_idx >= len(dates):
                continue
            exit_dt = dates[exit_idx]
            entry_px = float(s.iloc[entry_idx])
            exit_px = float(s.iloc[exit_idx])
            gross = (exit_px / entry_px - 1.0) * 100.0
            net = gross - drag
            out.trades.append(TOQTrade(
                entry_date=pd.Timestamp(entry_dt),
                exit_date=pd.Timestamp(exit_dt),
                entry_px=entry_px,
                exit_px=exit_px,
                holding_days=exit_offset,
                gross_ret_pct=gross,
                net_ret_pct=net,
            ))

    out.n_trades = len(out.trades)
    compute_stats(out)
    return out


def compute_stats(b: TOQBacktest, *, n_resamples: int = 5000) -> None:
    pnls = b.pnls()
    if not pnls:
        return
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    b.win_rate = wins / n
    b.pf = _pf(pnls)
    b.avg_ret_pct = sum(pnls) / n
    b.avg_holding_days = sum(t.holding_days for t in b.trades) / n
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    b.total_ret_pct_compounded = (eq - 1.0) * 100.0
    b.max_dd_pct = max_dd * 100.0
    try:
        boot = block_bootstrap_profit_factor(pnls, block_size=2, n_resamples=n_resamples)
        b.pf_ci_lower = boot.ci_lower
        b.pf_ci_upper = boot.ci_upper
    except Exception:
        pass


def _pf(pnls: list[float]) -> float:
    w = sum(p for p in pnls if p > 0)
    l = abs(sum(p for p in pnls if p < 0))
    return (w / l) if l > 0 else (float("inf") if w > 0 else 0.0)


@dataclass(frozen=True)
class HalfStats:
    label: str
    n: int
    pf: float
    pf_ci_lower: float
    pf_ci_upper: float
    avg_ret_pct: float
    pass_floor: bool


def split_h1_h2(
    b: TOQBacktest, *,
    pf_floor: float = DEFAULT_PF_FLOOR,
    n_resamples: int = 3000,
) -> tuple[HalfStats, HalfStats]:
    trades = sorted(b.trades, key=lambda t: t.entry_date)
    mid = len(trades) // 2
    h1 = trades[:mid]
    h2 = trades[mid:]

    def _half(label: str, lst: list[TOQTrade]) -> HalfStats:
        if not lst:
            return HalfStats(label, 0, 0.0, 0.0, 0.0, 0.0, False)
        pnls = [t.net_ret_pct for t in lst]
        pf = _pf(pnls)
        boot = block_bootstrap_profit_factor(pnls, block_size=2, n_resamples=n_resamples)
        avg = sum(pnls) / len(pnls)
        return HalfStats(
            label=label, n=len(pnls), pf=pf,
            pf_ci_lower=boot.ci_lower, pf_ci_upper=boot.ci_upper,
            avg_ret_pct=avg,
            pass_floor=(boot.ci_lower >= pf_floor),
        )

    return _half("H1", h1), _half("H2", h2)
