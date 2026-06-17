"""Overnight equity-risk-premium backtest for SPY/QQQ/IWM.

Thesis: US equity indices earn essentially all their long-run
risk-adjusted return between the prior 4pm close and the next 9:30
open (Kelly/Clark 2011, Lou/Polk/Skouras 2019, replicated annually).
Intraday returns are roughly zero-mean once overnight is stripped.

Trade definition:
  - BUY at the close of day t (proxy: today's `Close` field from yfinance)
  - SELL at the open of day t+1 (proxy: tomorrow's `Open` field)
  - Per-trade return = (open[t+1] / close[t] - 1) - round_trip_slippage

Per the disciplined gate, slippage is the deciding factor. 10 bps is
the gate's default (5 bps in + 5 bps out). At a 9:30 MOO + 15:55 MOC
pair on liquid US index ETFs (SPY/QQQ/IWM all >$1bn/day), realistic
fills are tighter — but we test at 10 bps to be honest.

Regime gate (optional): only enter the leg whose trailing 60-day
overnight Sharpe is > 0. This handles 2017-style ultra-low-vol regimes
where the premium temporarily compresses.

Walk-forward H1/H2 split (used by the disciplined gate CLI): chronologically
split the trade list into halves; each half must independently clear the
PF CI lower ≥ 1.20 floor for the strategy to be deployable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from helio.bootstrap_stats import (
    BootstrapResult,
    bootstrap_profit_factor,
    block_bootstrap_profit_factor,
)
from helio.yfinance_cache import download_cached


# Defaults aligned with ops/audit/run_walk_forward_oos.py.
DEFAULT_SLIPPAGE_BPS_RT = 10.0      # round-trip, matches xs_momentum gate
DEFAULT_REGIME_LOOKBACK = 60        # trading days for regime Sharpe
DEFAULT_PF_FLOOR = 1.20             # bootstrap CI lower bound to pass
DEFAULT_BOOTSTRAP_RESAMPLES = 5000


@dataclass(frozen=True)
class OvernightTrade:
    date: pd.Timestamp     # the entry date (day t close)
    ticker: str
    close_t: float
    open_tp1: float
    gross_ret_pct: float   # (open_tp1 / close_t - 1) * 100
    net_ret_pct: float     # after slippage_bps_rt


@dataclass
class LegBacktest:
    """One-leg overnight-drift backtest result."""
    ticker: str
    n_trades: int
    trades: list[OvernightTrade] = field(default_factory=list)
    period_label: str = ""
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT
    regime_gated: bool = False
    error: Optional[str] = None

    # Aggregate stats (filled by `compute_stats`)
    win_rate: float = 0.0
    pf: float = 0.0
    avg_ret_pct: float = 0.0
    total_ret_pct_compounded: float = 0.0
    max_dd_pct: float = 0.0
    sharpe_ann: float = 0.0
    pf_ci_lower: float = 0.0
    pf_ci_upper: float = 0.0

    def pnls(self) -> list[float]:
        return [t.net_ret_pct for t in self.trades]


# ── Data fetch ───────────────────────────────────────────────────────

def _fetch_daily(ticker: str, period: str) -> pd.DataFrame:
    """Daily bars with Open + Close fields. Uses helio.yfinance_cache
    (fresh_seconds=0 so the disciplined-gate runs always pull current
    data; cache still serves on yf rate-limit blip)."""
    df = download_cached(
        ticker, period=period, interval="1d",
        fresh_seconds=0, allow_stale_fallback=True, auto_adjust=False,
    )
    if df is None or df.empty:
        return df
    cols_lower = {c.lower(): c for c in df.columns}
    if "open" not in cols_lower or "close" not in cols_lower:
        # Some yfinance return shapes wrap columns; flatten
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            cols_lower = {c.lower(): c for c in df.columns}
    return df


# ── Per-leg backtest ─────────────────────────────────────────────────

def backtest_leg(
    ticker: str,
    *,
    period: str = "20y",
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT,
    regime_gated: bool = False,
    regime_lookback: int = DEFAULT_REGIME_LOOKBACK,
) -> LegBacktest:
    """Backtest the overnight-drift strategy on a single ticker.

    Returns a LegBacktest with .trades + aggregate .compute_stats()-d fields.
    """
    out = LegBacktest(
        ticker=ticker, n_trades=0, period_label=period,
        slippage_bps_rt=slippage_bps_rt, regime_gated=regime_gated,
    )
    try:
        df = _fetch_daily(ticker, period)
    except Exception as exc:
        out.error = f"fetch failed: {exc!r}"
        return out
    if df is None or df.empty:
        out.error = "empty data frame"
        return out

    # Standardize column access (yfinance sometimes returns lowercase)
    cols_lower = {c.lower(): c for c in df.columns}
    close_col = cols_lower.get("close") or cols_lower.get("adj close")
    open_col = cols_lower.get("open")
    if close_col is None or open_col is None:
        out.error = f"missing Open/Close columns; have {list(df.columns)}"
        return out

    closes = df[close_col].astype(float).values
    opens = df[open_col].astype(float).values
    dates = df.index

    drag = slippage_bps_rt / 100.0  # bps → percentage points

    # Build candidate trade list (one per day, exiting next-day open)
    candidate_rets: list[float] = []  # gross overnight returns for regime calc
    candidate_dates = []
    candidate_closes = []
    candidate_opens = []
    for i in range(len(closes) - 1):
        c_t = closes[i]
        o_tp1 = opens[i + 1]
        if not (math.isfinite(c_t) and math.isfinite(o_tp1) and c_t > 0):
            continue
        gross = (o_tp1 / c_t - 1.0) * 100.0
        candidate_rets.append(gross)
        candidate_dates.append(dates[i])
        candidate_closes.append(c_t)
        candidate_opens.append(o_tp1)

    if not candidate_rets:
        out.error = "no valid overnight returns"
        return out

    # Regime gate (optional): require trailing N-day overnight Sharpe > 0
    # to enter today's trade. Sharpe = mean / std on the trailing window.
    if regime_gated and regime_lookback > 0:
        take_mask = [False] * len(candidate_rets)
        for i in range(regime_lookback, len(candidate_rets)):
            window = candidate_rets[i - regime_lookback:i]
            mean = sum(window) / len(window)
            var = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            take_mask[i] = (sd > 0 and mean / sd > 0)
    else:
        take_mask = [True] * len(candidate_rets)

    for i, take in enumerate(take_mask):
        if not take:
            continue
        gross = candidate_rets[i]
        net = gross - drag
        out.trades.append(OvernightTrade(
            date=pd.Timestamp(candidate_dates[i]),
            ticker=ticker,
            close_t=candidate_closes[i],
            open_tp1=candidate_opens[i],
            gross_ret_pct=gross,
            net_ret_pct=net,
        ))
    out.n_trades = len(out.trades)
    compute_stats(out)
    return out


# ── Statistics ───────────────────────────────────────────────────────

def compute_stats(leg: LegBacktest, *, n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES) -> None:
    """Fill the aggregate stat fields on `leg` in place."""
    pnls = leg.pnls()
    if not pnls:
        return
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    leg.win_rate = wins / n
    leg.pf = _profit_factor(pnls)
    leg.avg_ret_pct = sum(pnls) / n
    # Compounded total
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    for p in pnls:
        eq *= (1.0 + p / 100.0)
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    leg.total_ret_pct_compounded = (eq - 1.0) * 100.0
    leg.max_dd_pct = max_dd * 100.0
    # Annualized Sharpe assuming ~252 trades/year (one per trading day)
    mean = leg.avg_ret_pct
    var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var) if var > 0 else 0.0
    leg.sharpe_ann = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0
    # Bootstrap PF CI (use moving-block since overnight returns autocorrelate
    # at the daily-news cluster scale).
    try:
        boot = block_bootstrap_profit_factor(
            pnls, block_size=5, n_resamples=n_resamples,
        )
        leg.pf_ci_lower = boot.ci_lower
        leg.pf_ci_upper = boot.ci_upper
    except Exception:
        leg.pf_ci_lower = 0.0
        leg.pf_ci_upper = 0.0


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = abs(sum(p for p in pnls if p < 0))
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


# ── Walk-forward H1/H2 split ─────────────────────────────────────────

@dataclass(frozen=True)
class HalfStats:
    label: str
    n_trades: int
    pf: float
    pf_ci_lower: float
    pf_ci_upper: float
    sharpe_ann: float
    avg_ret_bps: float
    pass_floor: bool


def split_h1_h2(
    leg: LegBacktest,
    *,
    pf_floor: float = DEFAULT_PF_FLOOR,
    n_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
) -> tuple[HalfStats, HalfStats]:
    """Chronologically split a leg's trades into halves; bootstrap each."""
    trades = sorted(leg.trades, key=lambda t: t.date)
    mid = len(trades) // 2
    h1 = trades[:mid]
    h2 = trades[mid:]

    def _stats(label: str, t_list: list[OvernightTrade]) -> HalfStats:
        if not t_list:
            return HalfStats(label, 0, 0.0, 0.0, 0.0, 0.0, 0.0, False)
        pnls = [t.net_ret_pct for t in t_list]
        pf = _profit_factor(pnls)
        boot = block_bootstrap_profit_factor(pnls, block_size=5, n_resamples=n_resamples)
        n = len(pnls)
        mean = sum(pnls) / n
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1) if n > 1 else 0.0
        sd = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0
        return HalfStats(
            label=label,
            n_trades=n,
            pf=pf,
            pf_ci_lower=boot.ci_lower,
            pf_ci_upper=boot.ci_upper,
            sharpe_ann=sharpe,
            avg_ret_bps=mean * 100.0,
            pass_floor=(boot.ci_lower >= pf_floor),
        )

    return _stats("H1", h1), _stats("H2", h2)
