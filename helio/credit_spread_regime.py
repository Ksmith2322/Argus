"""Credit-spread regime: long-SPY gated on HYG/LQD ratio above its 50d SMA.

Thesis (per Gilchrist-Zakrajsek "excess bond premium" literature, replicated
through 2024): widening HY-vs-IG credit spreads lead equity drawdowns by
2-6 weeks. A simple long-SPY position gated on HYG/LQD > 50d-MA flips
OFF precisely before equity stress. Distinct from xs_momentum because
the signal is rates-side dispersion, not equity-side rank.

Rule (Strategy agent's spec, docs/AUDIT_2026_05_25_PART2/STRATEGY.md #3):
  - ENTRY: long SPY at next open when (HYG/LQD ratio > 50d SMA of ratio)
           AND VIX < 22
  - EXIT: at next open when (HYG/LQD ratio crosses below 50d SMA)
  - Sizing: 1.0× anchor on engaged days; flat otherwise
  - Expected ~60-70% time-in-market

Pre-backtest expectation (Strategy agent): PF 1.5-2.0 at 10bps, CAGR +7-9%,
max DD ~12%, n=25-40 round-trips over 20y.

Failure mode (Strategy agent): whipsaw in credit-equity decorrelation regimes
(rare, ~2015-16); n=25 means bootstrap CI will be wide. Optional 5-day
confirmation filter halves the whipsaw rate at the cost of later entry.

Data: HYG (April 2007 inception, the binding constraint), LQD (2002),
SPY (1993), ^VIX (1990). Effective backtest window: ~19 years.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from helio.bootstrap_stats import (
    block_bootstrap_profit_factor,
)
from helio.yfinance_cache import download_cached


DEFAULT_SMA_WINDOW = 50
DEFAULT_VIX_CAP = 22.0
DEFAULT_SLIPPAGE_BPS_RT = 10.0
DEFAULT_PF_FLOOR = 1.20
DEFAULT_BOOTSTRAP_RESAMPLES = 5000


@dataclass(frozen=True)
class CreditSpreadTrade:
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_px: float
    exit_px: float
    holding_days: int
    gross_ret_pct: float
    net_ret_pct: float       # after slippage_bps_rt


@dataclass
class CreditSpreadBacktest:
    n_trades: int = 0
    trades: list[CreditSpreadTrade] = field(default_factory=list)
    period_label: str = ""
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT
    sma_window: int = DEFAULT_SMA_WINDOW
    vix_cap: float = DEFAULT_VIX_CAP
    confirm_days: int = 0       # 0 = strategy agent's vanilla; >0 adds N-day confirmation
    error: Optional[str] = None

    # Aggregate (filled by compute_stats)
    win_rate: float = 0.0
    pf: float = 0.0
    avg_ret_pct: float = 0.0
    total_ret_pct_compounded: float = 0.0
    max_dd_pct: float = 0.0
    pf_ci_lower: float = 0.0
    pf_ci_upper: float = 0.0
    avg_holding_days: float = 0.0
    time_in_market_pct: float = 0.0
    n_days_total: int = 0
    n_days_engaged: int = 0

    def pnls(self) -> list[float]:
        return [t.net_ret_pct for t in self.trades]


# ── Data alignment ────────────────────────────────────────────────────

def _fetch_one(ticker: str, period: str) -> pd.DataFrame:
    df = download_cached(
        ticker, period=period, interval="1d",
        fresh_seconds=0, allow_stale_fallback=True, auto_adjust=False,
    )
    if df is None or df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def _close(df: pd.DataFrame) -> pd.Series:
    cols = {c.lower(): c for c in df.columns}
    return df[cols.get("close") or cols.get("adj close")].astype(float)


def _open(df: pd.DataFrame) -> pd.Series:
    cols = {c.lower(): c for c in df.columns}
    return df[cols.get("open")].astype(float)


def _align(*series: pd.Series) -> pd.DataFrame:
    """Align by the intersection of dates. Drops any row with a missing val."""
    df = pd.concat(series, axis=1).dropna()
    return df


# ── Backtest ─────────────────────────────────────────────────────────

def backtest_credit_spread(
    *,
    period: str = "20y",
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT,
    sma_window: int = DEFAULT_SMA_WINDOW,
    vix_cap: float = DEFAULT_VIX_CAP,
    confirm_days: int = 0,
) -> CreditSpreadBacktest:
    """Run the credit-spread-regime backtest.

    Returns a CreditSpreadBacktest with .trades + compute_stats-d fields.
    """
    out = CreditSpreadBacktest(
        period_label=period,
        slippage_bps_rt=slippage_bps_rt,
        sma_window=sma_window,
        vix_cap=vix_cap,
        confirm_days=confirm_days,
    )

    try:
        hyg = _fetch_one("HYG", period)
        lqd = _fetch_one("LQD", period)
        spy = _fetch_one("SPY", period)
        vix = _fetch_one("^VIX", period)
    except Exception as exc:
        out.error = f"data fetch failed: {exc!r}"
        return out

    if any(df is None or df.empty for df in (hyg, lqd, spy, vix)):
        out.error = "one or more data series empty"
        return out

    # Align all four on the intersection of dates
    df = _align(
        _close(hyg).rename("hyg_close"),
        _close(lqd).rename("lqd_close"),
        _close(spy).rename("spy_close"),
        _open(spy).rename("spy_open"),
        _close(vix).rename("vix_close"),
    )
    if df.empty:
        out.error = "alignment produced empty frame"
        return out
    out.n_days_total = len(df)

    # Compute HYG/LQD ratio + 50d SMA
    df["ratio"] = df["hyg_close"] / df["lqd_close"]
    df["ratio_sma"] = df["ratio"].rolling(sma_window).mean()
    df = df.dropna()
    if len(df) < sma_window * 2:
        out.error = "insufficient post-SMA history"
        return out

    # Entry condition (per-bar): ratio > sma AND vix < cap
    df["entry_ok"] = (df["ratio"] > df["ratio_sma"]) & (df["vix_close"] < vix_cap)

    # Optional confirmation: require N consecutive entry_ok days before
    # actually entering. Reduces whipsaw at the cost of slower entry.
    # When confirm_days=0 the entry fires on the first ok day after any
    # not-ok day; the walk loop's in_position check prevents stacking.
    if confirm_days > 0:
        rolling = df["entry_ok"].rolling(confirm_days).sum()
        # Fire when the rolling-sum first reaches confirm_days (so the
        # day-before sum was strictly less). Walk-loop ignores while
        # already in position.
        df["enter_signal"] = (rolling == confirm_days) & (
            rolling.shift(1, fill_value=0) < confirm_days
        )
    else:
        df["enter_signal"] = df["entry_ok"] & ~df["entry_ok"].shift(1, fill_value=False)

    # Exit condition: ratio crosses BELOW sma (regardless of VIX)
    df["below_sma"] = df["ratio"] < df["ratio_sma"]
    df["exit_signal"] = df["below_sma"] & ~df["below_sma"].shift(1, fill_value=False)

    # Walk the bars; track open position
    drag = slippage_bps_rt / 100.0
    in_position = False
    entry_idx = -1
    entry_px = 0.0
    entry_date = None
    days_engaged = 0

    bars = df.reset_index().rename(columns={"index": "date", df.index.name or "Date": "date"})
    if "date" not in bars.columns:
        # Index has no name — set after reset
        bars = df.copy()
        bars.insert(0, "date", df.index)
        bars = bars.reset_index(drop=True)

    for i, row in bars.iterrows():
        if in_position:
            days_engaged += 1
            # Exit: at next open if exit_signal fires today (we use today's
            # signal to enter the NEXT open; standard MOO execution).
            if row["exit_signal"] and i + 1 < len(bars):
                exit_row = bars.iloc[i + 1]
                exit_px = float(exit_row["spy_open"])
                gross = (exit_px / entry_px - 1.0) * 100.0
                net = gross - drag
                holding = int(i + 1 - entry_idx)
                out.trades.append(CreditSpreadTrade(
                    entry_date=pd.Timestamp(entry_date),
                    exit_date=pd.Timestamp(exit_row["date"]),
                    entry_px=entry_px,
                    exit_px=exit_px,
                    holding_days=holding,
                    gross_ret_pct=gross,
                    net_ret_pct=net,
                ))
                in_position = False
                entry_idx = -1
        else:
            if row["enter_signal"] and i + 1 < len(bars):
                next_row = bars.iloc[i + 1]
                entry_px = float(next_row["spy_open"])
                entry_date = next_row["date"]
                entry_idx = i + 1
                in_position = True

    # Close any open position at the last bar (mark-to-market style)
    if in_position and entry_idx >= 0:
        last_row = bars.iloc[-1]
        exit_px = float(last_row["spy_close"])
        gross = (exit_px / entry_px - 1.0) * 100.0
        net = gross - drag
        out.trades.append(CreditSpreadTrade(
            entry_date=pd.Timestamp(entry_date),
            exit_date=pd.Timestamp(last_row["date"]),
            entry_px=entry_px,
            exit_px=exit_px,
            holding_days=int(len(bars) - entry_idx),
            gross_ret_pct=gross,
            net_ret_pct=net,
        ))
        days_engaged += int(len(bars) - entry_idx)

    out.n_trades = len(out.trades)
    out.n_days_engaged = days_engaged
    if out.n_days_total > 0:
        out.time_in_market_pct = 100.0 * days_engaged / out.n_days_total

    compute_stats(out)
    return out


def compute_stats(b: CreditSpreadBacktest, *, n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES) -> None:
    pnls = b.pnls()
    if not pnls:
        return
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    b.win_rate = wins / n
    b.pf = _pf(pnls)
    b.avg_ret_pct = sum(pnls) / n
    if b.trades:
        b.avg_holding_days = sum(t.holding_days for t in b.trades) / len(b.trades)
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
        boot = block_bootstrap_profit_factor(pnls, block_size=3, n_resamples=n_resamples)
        b.pf_ci_lower = boot.ci_lower
        b.pf_ci_upper = boot.ci_upper
    except Exception:
        pass


def _pf(pnls: list[float]) -> float:
    w = sum(p for p in pnls if p > 0)
    l = abs(sum(p for p in pnls if p < 0))
    return (w / l) if l > 0 else (float("inf") if w > 0 else 0.0)


# ── Walk-forward H1/H2 ───────────────────────────────────────────────

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
    b: CreditSpreadBacktest,
    *,
    pf_floor: float = DEFAULT_PF_FLOOR,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
) -> tuple[HalfStats, HalfStats]:
    trades = sorted(b.trades, key=lambda t: t.entry_date)
    mid = len(trades) // 2
    h1 = trades[:mid]
    h2 = trades[mid:]

    def _half(label: str, lst: list[CreditSpreadTrade]) -> HalfStats:
        if not lst:
            return HalfStats(label, 0, 0.0, 0.0, 0.0, 0.0, False)
        pnls = [t.net_ret_pct for t in lst]
        pf = _pf(pnls)
        boot = block_bootstrap_profit_factor(pnls, block_size=3, n_resamples=n_resamples)
        avg = sum(pnls) / len(pnls)
        return HalfStats(
            label=label, n=len(pnls), pf=pf,
            pf_ci_lower=boot.ci_lower, pf_ci_upper=boot.ci_upper,
            avg_ret_pct=avg,
            pass_floor=(boot.ci_lower >= pf_floor),
        )

    return _half("H1", h1), _half("H2", h2)
