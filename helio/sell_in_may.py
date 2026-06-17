"""Halloween effect / Sell-in-May modulated SPY↔SHY rotation backtest.

Thesis (Bouman-Jacobsen 2002, Andrade-Chhaochharia-Fuerst 2013 across
109 markets): May-Oct equity returns underperform Nov-Apr. The most
replicated calendar anomaly in equity finance, though weakened post-2010.

Rule (Strategy agent #7 from docs/AUDIT_2026_05_25_PART2/STRATEGY.md):
  - ENTRY/SWITCH: at MOC of last trading day of April → SPY → SHY
  - ENTRY/SWITCH: at MOC of last trading day of October → SHY → SPY
  - SIZING: 1.0x anchor always-invested
  - n = ~20 round-trips per 10y (one switch per April + one per Oct)

This is fundamentally different from tom_spy / nov_spy (which are
flat-most-of-the-year + take occasional 5-30 day positions). The
sell-in-May strategy is ALWAYS in one ETF. So:
  - PF is a less-meaningful stat (~20 trades of 6-month duration each)
  - The right comparison is BENCHMARK: total compounded return + Sharpe
    + max DD vs buy-and-hold SPY (the natural alternative)

A PASS verdict here means:
  - Total compounded ≥ buy-and-hold SPY (positive expectation)
  - Max DD < buy-and-hold SPY's max DD (defensive value)
  - Sharpe ≥ buy-and-hold SPY (risk-adjusted positive)
  - Walk-forward H1/H2: BOTH halves keep the same outperformance sign
    vs SPY (no era flip)

Pre-backtest expectation (Strategy agent): PF 1.6-2.0, CAGR +7-9%
vs SPY +9%, max DD 18-22%, Sharpe +0.2 vs SPY.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from helio.bootstrap_stats import block_bootstrap_profit_factor
from helio.yfinance_cache import download_cached


DEFAULT_SLIPPAGE_BPS_RT = 10.0
DEFAULT_PF_FLOOR = 1.20


@dataclass(frozen=True)
class RotationLeg:
    """One half of a year: either SPY-on or SHY-on."""
    start_date: pd.Timestamp
    end_date: pd.Timestamp
    ticker: str
    entry_px: float
    exit_px: float
    holding_days: int
    gross_ret_pct: float
    net_ret_pct: float


@dataclass
class SellInMayBacktest:
    n_legs: int = 0
    legs: list[RotationLeg] = field(default_factory=list)
    period_label: str = ""
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT
    error: Optional[str] = None

    # Strategy stats (filled by compute_stats)
    pf: float = 0.0
    pf_ci_lower: float = 0.0
    pf_ci_upper: float = 0.0
    avg_ret_pct: float = 0.0
    total_ret_pct_compounded: float = 0.0
    max_dd_pct: float = 0.0
    n_days_total: int = 0
    sharpe_ann: float = 0.0
    cagr_pct: float = 0.0
    avg_holding_days: float = 0.0

    # Benchmark (buy-and-hold SPY over same period)
    bench_total_ret_pct: float = 0.0
    bench_max_dd_pct: float = 0.0
    bench_sharpe_ann: float = 0.0
    bench_cagr_pct: float = 0.0


# ── Data + calendar ─────────────────────────────────────────────────

def _fetch_close(ticker: str, period: str) -> pd.Series:
    df = download_cached(
        ticker, period=period, interval="1d",
        fresh_seconds=0, allow_stale_fallback=True, auto_adjust=False,
    )
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    cols = {c.lower(): c for c in df.columns}
    s = df[cols.get("close") or cols.get("adj close")].astype(float)
    return s


def _last_trading_day_of_month(dates: pd.DatetimeIndex, month: int) -> list[pd.Timestamp]:
    """Return the last trading day of each occurrence of `month` in dates."""
    df = dates.to_series()
    grouped = df[df.dt.month == month].groupby([df.dt.year])
    return [g.iloc[-1] for _, g in grouped]


# ── Backtest ─────────────────────────────────────────────────────────

def backtest_sell_in_may(
    *,
    period: str = "20y",
    slippage_bps_rt: float = DEFAULT_SLIPPAGE_BPS_RT,
    risk_off_ticker: str = "SHY",
    risk_on_ticker: str = "SPY",
) -> SellInMayBacktest:
    out = SellInMayBacktest(
        period_label=period,
        slippage_bps_rt=slippage_bps_rt,
    )

    try:
        spy = _fetch_close(risk_on_ticker, period)
        shy = _fetch_close(risk_off_ticker, period)
    except Exception as exc:
        out.error = f"fetch failed: {exc!r}"
        return out
    if spy is None or shy is None or spy.empty or shy.empty:
        out.error = f"data empty for {risk_on_ticker} or {risk_off_ticker}"
        return out

    df = pd.concat([spy.rename("spy"), shy.rename("shy")], axis=1).dropna()
    if df.empty:
        out.error = "alignment empty"
        return out
    out.n_days_total = len(df)
    dates = df.index

    # Switch dates: last trading day of April → SPY→SHY; last trading day
    # of Oct → SHY→SPY.
    apr_ends = _last_trading_day_of_month(dates, 4)
    oct_ends = _last_trading_day_of_month(dates, 10)

    # Build the switch timeline + figure out the starting position
    # (whichever side of the calendar we're on at backtest start)
    switches = []
    for d in apr_ends:
        switches.append((d, "shy"))  # switch INTO shy at end of April
    for d in oct_ends:
        switches.append((d, "spy"))  # switch INTO spy at end of October
    switches.sort()

    if not switches:
        out.error = "no switch dates found"
        return out

    # Starting position: opposite of the first switch
    start_dt = dates[0]
    first_switch_dt, first_switch_to = switches[0]
    current_ticker = "spy" if first_switch_to == "shy" else "shy"
    current_start = start_dt
    current_entry_px = float(df.iloc[0][current_ticker])

    drag = slippage_bps_rt / 100.0

    for switch_dt, switch_to in switches:
        # Close the current leg at the switch date's close
        if switch_dt not in df.index:
            # Shouldn't happen (we got switch_dt FROM the index) but be safe
            continue
        exit_px = float(df.loc[switch_dt, current_ticker])
        gross = (exit_px / current_entry_px - 1.0) * 100.0
        # Pay slippage half on entry, half on exit; since each leg sees
        # one entry + one exit pay the full round-trip drag on each leg
        net = gross - drag
        # Holding days = days between leg start and switch_dt (in trading days)
        try:
            start_idx = df.index.get_loc(current_start)
            switch_idx = df.index.get_loc(switch_dt)
            holding = max(1, switch_idx - start_idx)
        except KeyError:
            holding = 0
        out.legs.append(RotationLeg(
            start_date=pd.Timestamp(current_start),
            end_date=pd.Timestamp(switch_dt),
            ticker=current_ticker.upper(),
            entry_px=current_entry_px,
            exit_px=exit_px,
            holding_days=holding,
            gross_ret_pct=gross,
            net_ret_pct=net,
        ))
        # Open the new leg at the next available bar's close
        try:
            switch_idx = df.index.get_loc(switch_dt)
            next_idx = switch_idx + 1
        except KeyError:
            continue
        if next_idx >= len(df):
            break
        current_ticker = switch_to
        current_start = df.index[next_idx]
        current_entry_px = float(df.iloc[next_idx][current_ticker])

    # Close the final open leg at last bar
    last_dt = dates[-1]
    if current_start != last_dt:
        exit_px = float(df.iloc[-1][current_ticker])
        gross = (exit_px / current_entry_px - 1.0) * 100.0
        net = gross - drag
        try:
            start_idx = df.index.get_loc(current_start)
            holding = max(1, len(df) - 1 - start_idx)
        except KeyError:
            holding = 0
        out.legs.append(RotationLeg(
            start_date=pd.Timestamp(current_start),
            end_date=pd.Timestamp(last_dt),
            ticker=current_ticker.upper(),
            entry_px=current_entry_px,
            exit_px=exit_px,
            holding_days=holding,
            gross_ret_pct=gross,
            net_ret_pct=net,
        ))

    out.n_legs = len(out.legs)

    compute_stats(out, df)
    return out


def compute_stats(b: SellInMayBacktest, df: pd.DataFrame) -> None:
    if not b.legs:
        return
    pnls = [leg.net_ret_pct for leg in b.legs]
    n = len(pnls)
    b.pf = _pf(pnls)
    b.avg_ret_pct = sum(pnls) / n
    b.avg_holding_days = sum(leg.holding_days for leg in b.legs) / n
    # Strategy equity curve: compound the leg returns
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

    # CAGR
    years = b.n_days_total / 252.0 if b.n_days_total else 0.0
    b.cagr_pct = ((eq ** (1.0 / years) - 1.0) * 100.0) if years > 0 else 0.0

    # Sharpe: daily-return series proxy. Compute daily strategy returns
    # by walking the legs and aligning each day to its current ticker.
    daily_ret = []
    leg_idx = 0
    cur_leg = b.legs[leg_idx]
    for i in range(1, len(df)):
        d = df.index[i]
        while leg_idx + 1 < len(b.legs) and d > b.legs[leg_idx].end_date:
            leg_idx += 1
            cur_leg = b.legs[leg_idx]
        ticker_lower = cur_leg.ticker.lower()
        prev = float(df.iloc[i - 1][ticker_lower])
        curr = float(df.iloc[i][ticker_lower])
        if prev > 0:
            daily_ret.append((curr / prev - 1.0))
    if daily_ret:
        mean = sum(daily_ret) / len(daily_ret)
        var = sum((r - mean) ** 2 for r in daily_ret) / (len(daily_ret) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        b.sharpe_ann = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0

    # Bootstrap PF on leg-level returns
    try:
        boot = block_bootstrap_profit_factor(pnls, block_size=2, n_resamples=5000)
        b.pf_ci_lower = boot.ci_lower
        b.pf_ci_upper = boot.ci_upper
    except Exception:
        pass

    # Benchmark: buy-and-hold SPY over same period
    spy = df["spy"]
    spy_eq = 1.0
    spy_peak = 1.0
    spy_max_dd = 0.0
    spy_daily = []
    for i in range(1, len(spy)):
        ret = spy.iloc[i] / spy.iloc[i - 1] - 1.0
        spy_eq *= (1.0 + ret)
        spy_peak = max(spy_peak, spy_eq)
        dd = (spy_peak - spy_eq) / spy_peak if spy_peak > 0 else 0.0
        spy_max_dd = max(spy_max_dd, dd)
        spy_daily.append(ret)
    b.bench_total_ret_pct = (spy_eq - 1.0) * 100.0
    b.bench_max_dd_pct = spy_max_dd * 100.0
    if years > 0:
        b.bench_cagr_pct = ((spy_eq ** (1.0 / years) - 1.0) * 100.0)
    if spy_daily:
        mean = sum(spy_daily) / len(spy_daily)
        var = sum((r - mean) ** 2 for r in spy_daily) / (len(spy_daily) - 1)
        sd = math.sqrt(var) if var > 0 else 0.0
        b.bench_sharpe_ann = (mean / sd) * math.sqrt(252) if sd > 0 else 0.0


def _pf(pnls: list[float]) -> float:
    w = sum(p for p in pnls if p > 0)
    l = abs(sum(p for p in pnls if p < 0))
    return (w / l) if l > 0 else (float("inf") if w > 0 else 0.0)


@dataclass(frozen=True)
class HalfStats:
    label: str
    n: int
    leg_pf: float
    leg_pf_ci_lower: float
    total_ret_pct: float
    bench_total_pct: float
    outperformance_pp: float
    max_dd_pct: float


def split_h1_h2(b: SellInMayBacktest, df: pd.DataFrame) -> tuple[HalfStats, HalfStats]:
    if not b.legs:
        return HalfStats("H1", 0, 0, 0, 0, 0, 0, 0), HalfStats("H2", 0, 0, 0, 0, 0, 0, 0)
    legs_sorted = sorted(b.legs, key=lambda x: x.start_date)
    mid = len(legs_sorted) // 2
    h1 = legs_sorted[:mid]
    h2 = legs_sorted[mid:]

    def _stats(label: str, leg_list: list[RotationLeg]) -> HalfStats:
        if not leg_list:
            return HalfStats(label, 0, 0, 0, 0, 0, 0, 0)
        pnls = [l.net_ret_pct for l in leg_list]
        pf = _pf(pnls)
        try:
            boot = block_bootstrap_profit_factor(pnls, block_size=2, n_resamples=2000)
            ci_lo = boot.ci_lower
        except Exception:
            ci_lo = 0.0
        # Strategy total return over this half
        eq = 1.0
        peak = 1.0
        max_dd = 0.0
        for p in pnls:
            eq *= (1.0 + p / 100.0)
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        strat_total = (eq - 1.0) * 100.0
        # SPY benchmark over same time
        start_d = leg_list[0].start_date
        end_d = leg_list[-1].end_date
        try:
            slc = df.loc[start_d:end_d, "spy"]
            spy_total = ((slc.iloc[-1] / slc.iloc[0]) - 1.0) * 100.0
        except (KeyError, IndexError):
            spy_total = 0.0
        return HalfStats(
            label=label, n=len(leg_list), leg_pf=pf, leg_pf_ci_lower=ci_lo,
            total_ret_pct=strat_total, bench_total_pct=spy_total,
            outperformance_pp=strat_total - spy_total,
            max_dd_pct=max_dd * 100.0,
        )

    return _stats("H1", h1), _stats("H2", h2)
