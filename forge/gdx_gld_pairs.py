"""
GDX/GLD Cointegration Pairs Trading Backtest
=============================================
Edge: GDX has embedded operational leverage on gold. The GDX/GLD ratio
overshoots on sentiment and mean-reverts on 10-30 day windows.

Entry:  z-score of spread hits +/-2.0 -> mean-reversion trade
Exit:   z-score crosses 0 (mean)
Stop:   z-score hits +/-3.0 (cointegration breakdown)

Tests two spread methods:
  1. Engle-Granger OLS residual with rolling hedge ratio
  2. Log-ratio: log(GDX/GLD) -- simpler, may be more stable

Usage:
    python -m forge.gdx_gld_pairs
"""

import sys
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant
    from statsmodels.tsa.stattools import adfuller
except ImportError:
    print("Required: pip install yfinance pandas numpy statsmodels")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ZSCORE_ENTRY = 2.0
ZSCORE_EXIT = 0.0
ZSCORE_STOP = 3.0
COINT_WINDOW = 252        # rolling OLS window for hedge ratio
ZSCORE_LOOKBACK = 60      # rolling z-score lookback
START_DATE = "2006-05-22"  # GDX inception
END_DATE = "2026-04-10"
COST_PER_SIDE_BPS = 5      # 5 bps slippage + commission per side


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    entry_date: str
    exit_date: Optional[str] = None
    direction: str = ""        # "long_spread" or "short_spread"
    entry_zscore: float = 0.0
    exit_zscore: float = 0.0
    entry_spread: float = 0.0
    exit_spread: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""


def download_data() -> pd.DataFrame:
    """Download GDX and GLD daily close prices."""
    print("Downloading GDX and GLD data from yfinance...")
    gdx = yf.download("GDX", start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
    gld = yf.download("GLD", start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)

    # Handle MultiIndex columns from yfinance
    if isinstance(gdx.columns, pd.MultiIndex):
        gdx.columns = gdx.columns.get_level_values(0)
    if isinstance(gld.columns, pd.MultiIndex):
        gld.columns = gld.columns.get_level_values(0)

    df = pd.DataFrame({
        "GDX": gdx["Close"],
        "GLD": gld["Close"],
    }).dropna()

    print(f"  Data: {df.index[0].strftime('%Y-%m-%d')} to {df.index[-1].strftime('%Y-%m-%d')} "
          f"({len(df)} trading days)")
    return df


# ---------------------------------------------------------------------------
# Spread computation methods
# ---------------------------------------------------------------------------

def compute_eg_spread(df: pd.DataFrame) -> pd.DataFrame:
    """
    Engle-Granger spread with rolling OLS hedge ratio.
    spread = GDX - hedge_ratio * GLD
    """
    n = len(df)
    hedge_ratio = pd.Series(np.nan, index=df.index)
    spread = pd.Series(np.nan, index=df.index)

    for i in range(COINT_WINDOW, n):
        window = df.iloc[i - COINT_WINDOW:i]
        X = add_constant(window["GLD"].values)
        y = window["GDX"].values
        model = OLS(y, X).fit()
        hr = model.params[1]
        hedge_ratio.iloc[i] = hr
        spread.iloc[i] = df["GDX"].iloc[i] - hr * df["GLD"].iloc[i]

    result = pd.DataFrame({
        "spread": spread,
        "hedge_ratio": hedge_ratio,
    }, index=df.index)
    return result


def compute_log_ratio_spread(df: pd.DataFrame) -> pd.DataFrame:
    """
    Simple log-ratio spread: log(GDX / GLD).
    No rolling OLS needed.
    """
    spread = np.log(df["GDX"] / df["GLD"])
    result = pd.DataFrame({
        "spread": spread,
        "hedge_ratio": pd.Series(np.nan, index=df.index),  # not applicable
    }, index=df.index)
    return result


def compute_zscore(spread: pd.Series, lookback: int = ZSCORE_LOOKBACK) -> pd.Series:
    """Rolling z-score of the spread."""
    mean = spread.rolling(lookback).mean()
    std = spread.rolling(lookback).std()
    return (spread - mean) / std


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(df: pd.DataFrame, zscore: pd.Series, spread: pd.Series,
                 label: str) -> List[Trade]:
    """
    Run the pairs backtest on a z-score series.
    Returns list of completed trades.
    """
    trades: List[Trade] = []
    position: Optional[Trade] = None

    valid = zscore.dropna()
    if len(valid) == 0:
        return trades

    start_idx = valid.index[0]
    mask = df.index >= start_idx

    dates = df.index[mask]
    z_vals = zscore[mask]
    s_vals = spread[mask]

    for i in range(1, len(dates)):
        date = dates[i]
        z = z_vals.iloc[i]
        z_prev = z_vals.iloc[i - 1]
        s = s_vals.iloc[i]

        if pd.isna(z) or pd.isna(z_prev) or pd.isna(s):
            continue

        # --- Check exits first ---
        if position is not None:
            close = False
            reason = ""

            # Mean reversion exit: z crosses 0
            if position.direction == "long_spread" and z_prev < ZSCORE_EXIT <= z:
                close, reason = True, "mean_revert"
            elif position.direction == "long_spread" and z >= ZSCORE_EXIT and z_prev < ZSCORE_EXIT:
                close, reason = True, "mean_revert"
            elif position.direction == "short_spread" and z_prev > ZSCORE_EXIT >= z:
                close, reason = True, "mean_revert"
            elif position.direction == "short_spread" and z <= ZSCORE_EXIT and z_prev > ZSCORE_EXIT:
                close, reason = True, "mean_revert"

            # Stop: z hits +/-3
            if position.direction == "long_spread" and z <= -ZSCORE_STOP:
                close, reason = True, "stop"
            elif position.direction == "short_spread" and z >= ZSCORE_STOP:
                close, reason = True, "stop"

            if close:
                position.exit_date = date.strftime("%Y-%m-%d")
                position.exit_zscore = z
                position.exit_spread = s

                # P&L: for long_spread, profit when spread rises
                if position.direction == "long_spread":
                    raw_pnl = (s - position.entry_spread)
                    # Normalize by entry spread magnitude for percentage
                    denom = abs(position.entry_spread) if abs(position.entry_spread) > 0.01 else 1.0
                    position.pnl_pct = (raw_pnl / denom) * 100.0
                else:  # short_spread
                    raw_pnl = (position.entry_spread - s)
                    denom = abs(position.entry_spread) if abs(position.entry_spread) > 0.01 else 1.0
                    position.pnl_pct = (raw_pnl / denom) * 100.0

                # Subtract costs (round-trip = 2 * COST_PER_SIDE_BPS / 100)
                position.pnl_pct -= 2 * COST_PER_SIDE_BPS / 100.0
                position.exit_reason = reason
                trades.append(position)
                position = None

        # --- Check entries ---
        if position is None:
            # z crosses below -2: spread is cheap -> long spread
            if z_prev > -ZSCORE_ENTRY and z <= -ZSCORE_ENTRY:
                position = Trade(
                    entry_date=date.strftime("%Y-%m-%d"),
                    direction="long_spread",
                    entry_zscore=z,
                    entry_spread=s,
                )
            # z crosses above +2: spread is expensive -> short spread
            elif z_prev < ZSCORE_ENTRY and z >= ZSCORE_ENTRY:
                position = Trade(
                    entry_date=date.strftime("%Y-%m-%d"),
                    direction="short_spread",
                    entry_zscore=z,
                    entry_spread=s,
                )

    # Close any open position at end
    if position is not None:
        position.exit_date = dates[-1].strftime("%Y-%m-%d")
        position.exit_zscore = z_vals.iloc[-1]
        position.exit_spread = s_vals.iloc[-1]
        raw_pnl = ((s_vals.iloc[-1] - position.entry_spread) if position.direction == "long_spread"
                    else (position.entry_spread - s_vals.iloc[-1]))
        denom = abs(position.entry_spread) if abs(position.entry_spread) > 0.01 else 1.0
        position.pnl_pct = (raw_pnl / denom) * 100.0 - 2 * COST_PER_SIDE_BPS / 100.0
        position.exit_reason = "end_of_data"
        trades.append(position)

    return trades


# ---------------------------------------------------------------------------
# Cointegration diagnostics
# ---------------------------------------------------------------------------

def coint_diagnostics(spread: pd.Series, label: str):
    """Run ADF test on the spread to check stationarity."""
    clean = spread.dropna()
    if len(clean) < 100:
        print(f"  [{label}] Not enough data for ADF test")
        return
    result = adfuller(clean, maxlag=20, regression="c")
    print(f"  [{label}] ADF statistic: {result[0]:.4f}  p-value: {result[1]:.4f}")
    print(f"  [{label}] Critical values: 1%={result[4]['1%']:.3f}  "
          f"5%={result[4]['5%']:.3f}  10%={result[4]['10%']:.3f}")
    if result[1] < 0.05:
        print(f"  [{label}] Spread IS stationary (p < 0.05) -- cointegration supported")
    else:
        print(f"  [{label}] Spread is NOT stationary (p >= 0.05) -- cointegration questionable")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(trades: List[Trade], label: str, df: pd.DataFrame):
    """Print comprehensive backtest results."""
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    if not trades:
        print("  No trades generated.")
        return

    n = len(trades)
    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct <= 0]
    pnls = [t.pnl_pct for t in trades]

    win_rate = len(wins) / n * 100
    avg_win = np.mean([t.pnl_pct for t in wins]) if wins else 0.0
    avg_loss = np.mean([t.pnl_pct for t in losses]) if losses else 0.0
    avg_trade = np.mean(pnls)
    total_pnl = np.sum(pnls)

    gross_profit = sum(t.pnl_pct for t in wins) if wins else 0.0
    gross_loss = abs(sum(t.pnl_pct for t in losses)) if losses else 0.001
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Sharpe (annualized, assuming ~10 trades/year avg)
    if len(pnls) > 1:
        trades_per_year = n / max(1, (pd.Timestamp(trades[-1].exit_date) -
                                       pd.Timestamp(trades[0].entry_date)).days / 365.25)
        sharpe = (np.mean(pnls) / np.std(pnls, ddof=1)) * np.sqrt(max(trades_per_year, 1))
    else:
        sharpe = 0.0
        trades_per_year = 0.0

    # Max drawdown on cumulative P&L
    cum_pnl = np.cumsum(pnls)
    running_max = np.maximum.accumulate(cum_pnl)
    drawdowns = cum_pnl - running_max
    max_dd = np.min(drawdowns) if len(drawdowns) > 0 else 0.0

    # Stops vs mean-revert exits
    n_stops = sum(1 for t in trades if t.exit_reason == "stop")
    n_mean_revert = sum(1 for t in trades if t.exit_reason == "mean_revert")
    n_eod = sum(1 for t in trades if t.exit_reason == "end_of_data")
    n_long = sum(1 for t in trades if t.direction == "long_spread")
    n_short = sum(1 for t in trades if t.direction == "short_spread")

    # Years spanned
    first_year = int(trades[0].entry_date[:4])
    last_year = int(trades[-1].exit_date[:4])
    years = last_year - first_year + 1

    print(f"\n  Trades:          {n} ({trades_per_year:.1f}/year)")
    print(f"  Direction:       {n_long} long spread / {n_short} short spread")
    print(f"  Win rate:        {win_rate:.1f}%")
    print(f"  Avg winner:      {avg_win:+.2f}%")
    print(f"  Avg loser:       {avg_loss:+.2f}%")
    print(f"  Avg trade:       {avg_trade:+.2f}%")
    print(f"  Total P&L:       {total_pnl:+.2f}%")
    print(f"  Profit factor:   {profit_factor:.2f}")
    print(f"  Sharpe (ann):    {sharpe:.2f}")
    print(f"  Max drawdown:    {max_dd:.2f}%")
    print(f"  Exits:           {n_mean_revert} mean-revert / {n_stops} stops / {n_eod} end-of-data")

    # --- By-decade breakdown ---
    print(f"\n  {'Decade':<12} {'Trades':>7} {'WinRate':>8} {'AvgTrade':>10} {'PF':>7} {'TotalPnL':>10}")
    print(f"  {'-'*55}")

    decades = {}
    for t in trades:
        yr = int(t.entry_date[:4])
        decade = (yr // 10) * 10
        key = f"{decade}s"
        if key not in decades:
            decades[key] = []
        decades[key].append(t)

    for decade in sorted(decades.keys()):
        dt = decades[decade]
        dn = len(dt)
        dw = sum(1 for t in dt if t.pnl_pct > 0) / dn * 100
        da = np.mean([t.pnl_pct for t in dt])
        dgp = sum(t.pnl_pct for t in dt if t.pnl_pct > 0) or 0.0
        dgl = abs(sum(t.pnl_pct for t in dt if t.pnl_pct <= 0)) or 0.001
        dpf = dgp / dgl if dgl > 0 else float("inf")
        dtp = sum(t.pnl_pct for t in dt)
        print(f"  {decade:<12} {dn:>7} {dw:>7.1f}% {da:>+9.2f}% {dpf:>7.2f} {dtp:>+9.2f}%")

    # --- Sample trades ---
    print(f"\n  Sample trades (first 10):")
    print(f"  {'Entry':<12} {'Exit':<12} {'Dir':<14} {'Z_in':>6} {'Z_out':>6} {'PnL%':>8} {'Reason':<12}")
    print(f"  {'-'*72}")
    for t in trades[:10]:
        print(f"  {t.entry_date:<12} {t.exit_date:<12} {t.direction:<14} "
              f"{t.entry_zscore:>+5.2f} {t.exit_zscore:>+5.2f} {t.pnl_pct:>+7.2f}% {t.exit_reason:<12}")

    # --- Kill/Keep verdict ---
    print(f"\n  VERDICT: ", end="")
    if n >= 30 and profit_factor >= 1.3:
        print(f"KEEP -- {n} trades, PF {profit_factor:.2f} >= 1.30")
    elif n < 30:
        print(f"INSUFFICIENT DATA -- only {n} trades (need 30+)")
    else:
        print(f"KILL -- PF {profit_factor:.2f} < 1.30 on {n} trades")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 70)
    print("  GDX/GLD Cointegration Pairs Trading Backtest")
    print("  Edge: GDX operational leverage overshoots -> mean reversion")
    print("=" * 70)

    df = download_data()

    # --- Method 1: Engle-Granger rolling OLS ---
    print("\n[1/2] Computing Engle-Granger rolling spread (this takes a minute)...")
    eg = compute_eg_spread(df)
    eg_spread = eg["spread"]
    eg_zscore = compute_zscore(eg_spread)

    coint_diagnostics(eg_spread, "Engle-Granger")

    eg_trades = run_backtest(df, eg_zscore, eg_spread, "Engle-Granger")
    report(eg_trades, "Method 1: Engle-Granger OLS Spread", df)

    # --- Method 2: Log ratio ---
    print("\n[2/2] Computing log-ratio spread...")
    lr = compute_log_ratio_spread(df)
    lr_spread = lr["spread"]
    lr_zscore = compute_zscore(lr_spread)

    coint_diagnostics(lr_spread, "Log-Ratio")

    lr_trades = run_backtest(df, lr_zscore, lr_spread, "Log-Ratio")
    report(lr_trades, "Method 2: Log(GDX/GLD) Ratio", df)

    # --- Summary comparison ---
    print(f"\n{'='*70}")
    print(f"  COMPARISON SUMMARY")
    print(f"{'='*70}")
    for label, trades in [("Engle-Granger", eg_trades), ("Log-Ratio", lr_trades)]:
        n = len(trades)
        if n == 0:
            print(f"  {label:<20} No trades")
            continue
        wr = sum(1 for t in trades if t.pnl_pct > 0) / n * 100
        pnls = [t.pnl_pct for t in trades]
        gp = sum(p for p in pnls if p > 0) or 0.0
        gl = abs(sum(p for p in pnls if p <= 0)) or 0.001
        pf = gp / gl
        tp = sum(pnls)
        print(f"  {label:<20} Trades: {n:>4}  WR: {wr:>5.1f}%  PF: {pf:>5.2f}  Total: {tp:>+8.2f}%")

    print("\nDone.")


if __name__ == "__main__":
    main()
