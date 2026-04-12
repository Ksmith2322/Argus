"""
FOMC Pre-Announcement Drift Backtest
=====================================
Edge: Buy SPY at prior close, exit at 2pm ET on FOMC announcement day.
Mechanism: Risk-premium accumulation into uncertainty resolution.
Reference: Lucca & Moench (NY Fed 2015) — ~80% of SPX equity premium
           accrues in the 24h pre-FOMC window.

Usage:
    python -m forge.fomc_drift
    python -m forge.fomc_drift --start 2010 --end 2026
    python -m forge.fomc_drift --plot
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("Required: pip install yfinance pandas numpy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# FOMC announcement dates 2000-2026
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
# Each date is the ANNOUNCEMENT day (statement released ~2pm ET).
# Multi-day meetings: we list the final (announcement) day only.
# ---------------------------------------------------------------------------
FOMC_DATES = [
    # 2000
    "2000-02-02", "2000-03-21", "2000-05-16", "2000-06-28",
    "2000-08-22", "2000-10-03", "2000-11-15", "2000-12-19",
    # 2001
    "2001-01-03", "2001-01-31", "2001-03-20", "2001-04-18",
    "2001-05-15", "2001-06-27", "2001-08-21", "2001-09-17",
    "2001-10-02", "2001-11-06", "2001-12-11",
    # 2002
    "2002-01-30", "2002-03-19", "2002-05-07", "2002-06-26",
    "2002-08-13", "2002-09-24", "2002-11-06", "2002-12-10",
    # 2003
    "2003-01-29", "2003-03-18", "2003-05-06", "2003-06-25",
    "2003-08-12", "2003-09-16", "2003-10-28", "2003-12-09",
    # 2004
    "2004-01-28", "2004-03-16", "2004-05-04", "2004-06-30",
    "2004-08-10", "2004-09-21", "2004-11-10", "2004-12-14",
    # 2005
    "2005-02-02", "2005-03-22", "2005-05-03", "2005-06-30",
    "2005-08-09", "2005-09-20", "2005-11-01", "2005-12-13",
    # 2006
    "2006-01-31", "2006-03-28", "2006-05-10", "2006-06-29",
    "2006-08-08", "2006-09-20", "2006-10-25", "2006-12-12",
    # 2007
    "2007-01-31", "2007-03-21", "2007-05-09", "2007-06-28",
    "2007-08-07", "2007-09-18", "2007-10-31", "2007-12-11",
    # 2008
    "2008-01-22", "2008-01-30", "2008-03-18", "2008-04-30",
    "2008-06-25", "2008-08-05", "2008-09-16", "2008-10-08",
    "2008-10-29", "2008-12-16",
    # 2009
    "2009-01-28", "2009-03-18", "2009-04-29", "2009-06-24",
    "2009-08-12", "2009-09-23", "2009-11-04", "2009-12-16",
    # 2010
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23",
    "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    # 2011
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22",
    "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    # 2012
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20",
    "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    # 2013
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19",
    "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    # 2014
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18",
    "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    # 2015
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17",
    "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    # 2016
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15",
    "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    # 2017
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14",
    "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    # 2018
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13",
    "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    # 2019
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19",
    "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    # 2020
    "2020-01-29", "2020-03-03", "2020-03-15", "2020-04-29",
    "2020-06-10", "2020-07-29", "2020-09-16", "2020-11-05",
    "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-17",
    # 2026 (scheduled)
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-16",
]


def get_spy_data(start_year: int = 2000, end_year: int = 2026) -> pd.DataFrame:
    """Download SPY daily OHLCV from yfinance."""
    start = f"{start_year}-01-01"
    end = f"{end_year}-12-31"
    print(f"Downloading SPY {start} -> {end} ...")
    df = yf.download("SPY", start=start, end=end, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    print(f"  {len(df)} bars loaded ({df.index[0].date()} to {df.index[-1].date()})")
    return df


def find_prior_close(df: pd.DataFrame, target_date: datetime) -> tuple:
    """Find the trading day before target_date and return (date, close price).
    Returns (None, None) if not found."""
    prior_dates = df.index[df.index < pd.Timestamp(target_date)]
    if len(prior_dates) == 0:
        return None, None
    prior_day = prior_dates[-1]
    return prior_day, float(df.loc[prior_day, "Close"])


def find_announcement_day_price(df: pd.DataFrame, target_date: datetime) -> tuple:
    """Get announcement day open and close.
    Since we exit at 2pm ET and daily bars don't have intraday,
    we approximate exit as the announcement day close (conservative —
    the 2pm exit typically captures most of the move).
    Returns (open, close) or (None, None)."""
    ts = pd.Timestamp(target_date)
    if ts in df.index:
        return float(df.loc[ts, "Open"]), float(df.loc[ts, "Close"])
    return None, None


def run_backtest(start_year: int = 2000, end_year: int = 2026) -> pd.DataFrame:
    """Run the FOMC drift backtest."""
    df = get_spy_data(start_year, end_year)

    trades = []
    for date_str in FOMC_DATES:
        fomc_date = datetime.strptime(date_str, "%Y-%m-%d")
        if fomc_date.year < start_year or fomc_date.year > end_year:
            continue

        prior_day, entry_price = find_prior_close(df, fomc_date)
        if entry_price is None:
            continue

        ann_open, ann_close = find_announcement_day_price(df, fomc_date)
        if ann_close is None:
            # FOMC on non-trading day (e.g. emergency Sunday meeting 2020-03-15)
            continue

        exit_price = ann_close  # proxy for 2pm ET exit
        ret_pct = (exit_price - entry_price) / entry_price * 100
        ret_bps = ret_pct * 100

        trades.append({
            "fomc_date": date_str,
            "entry_date": prior_day.strftime("%Y-%m-%d"),
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "return_pct": round(ret_pct, 4),
            "return_bps": round(ret_bps, 2),
            "win": 1 if ret_pct > 0 else 0,
            "year": fomc_date.year,
        })

    return pd.DataFrame(trades)


def print_results(trades: pd.DataFrame):
    """Print comprehensive backtest summary."""
    if trades.empty:
        print("No trades found.")
        return

    wins = trades[trades["return_pct"] > 0]
    losses = trades[trades["return_pct"] <= 0]

    gross_wins = wins["return_pct"].sum() if len(wins) > 0 else 0
    gross_losses = abs(losses["return_pct"].sum()) if len(losses) > 0 else 0.001
    profit_factor = gross_wins / gross_losses

    total_ret = trades["return_pct"].sum()
    years = trades["year"].nunique()
    annual_ret = total_ret / years if years > 0 else 0

    # Cumulative equity curve for max drawdown
    cum_ret = (1 + trades["return_pct"] / 100).cumprod()
    rolling_max = cum_ret.cummax()
    drawdowns = (cum_ret - rolling_max) / rolling_max * 100
    max_dd = drawdowns.min()

    print("\n" + "=" * 70)
    print("FOMC PRE-ANNOUNCEMENT DRIFT — BACKTEST RESULTS")
    print("=" * 70)
    print(f"Period:          {trades['fomc_date'].iloc[0]} to {trades['fomc_date'].iloc[-1]}")
    print(f"Total trades:    {len(trades)}")
    print(f"Years:           {years}")
    print(f"Trades/year:     {len(trades) / years:.1f}")
    print()
    print(f"Win rate:        {len(wins) / len(trades) * 100:.1f}% ({len(wins)}W / {len(losses)}L)")
    print(f"Profit factor:   {profit_factor:.2f}")
    print()
    print(f"Total return:    {total_ret:.2f}%")
    print(f"Annual return:   {annual_ret:.2f}%")
    print(f"Avg trade:       {trades['return_pct'].mean():.3f}% ({trades['return_bps'].mean():.1f} bps)")
    print(f"Median trade:    {trades['return_pct'].median():.3f}% ({trades['return_bps'].median():.1f} bps)")
    print(f"Std dev:         {trades['return_pct'].std():.3f}%")
    print()
    print(f"Best trade:      {trades['return_pct'].max():.3f}% ({trades.loc[trades['return_pct'].idxmax(), 'fomc_date']})")
    print(f"Worst trade:     {trades['return_pct'].min():.3f}% ({trades.loc[trades['return_pct'].idxmin(), 'fomc_date']})")
    print(f"Max drawdown:    {max_dd:.2f}%")
    print()

    avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
    avg_loss = losses["return_pct"].mean() if len(losses) > 0 else 0
    print(f"Avg win:         {avg_win:.3f}%")
    print(f"Avg loss:        {avg_loss:.3f}%")
    print(f"Win/loss ratio:  {abs(avg_win / avg_loss):.2f}" if avg_loss != 0 else "Win/loss ratio:  inf")
    print()

    # Sharpe-like ratio (per-trade, not annualized)
    if trades["return_pct"].std() > 0:
        sharpe_per_trade = trades["return_pct"].mean() / trades["return_pct"].std()
        # Annualize: ~8 trades/year
        sharpe_annual = sharpe_per_trade * np.sqrt(8)
        print(f"Sharpe (ann.):   {sharpe_annual:.2f} (assuming 8 trades/yr)")
    print()

    # By-decade breakdown
    print("-" * 70)
    print("BY DECADE")
    print("-" * 70)
    for decade_start in range(2000, 2030, 10):
        decade_end = decade_start + 9
        mask = (trades["year"] >= decade_start) & (trades["year"] <= decade_end)
        dec = trades[mask]
        if len(dec) == 0:
            continue
        dec_wins = dec[dec["return_pct"] > 0]
        dec_gross_w = dec_wins["return_pct"].sum() if len(dec_wins) > 0 else 0
        dec_gross_l = abs(dec[dec["return_pct"] <= 0]["return_pct"].sum()) or 0.001
        print(f"  {decade_start}s: {len(dec)} trades | "
              f"WR {len(dec_wins)/len(dec)*100:.0f}% | "
              f"PF {dec_gross_w/dec_gross_l:.2f} | "
              f"Avg {dec['return_pct'].mean():.3f}% | "
              f"Total {dec['return_pct'].sum():.2f}%")

    # Year-by-year
    print()
    print("-" * 70)
    print("BY YEAR")
    print("-" * 70)
    yearly = trades.groupby("year").agg(
        trades_count=("return_pct", "count"),
        total_ret=("return_pct", "sum"),
        avg_ret=("return_pct", "mean"),
        win_rate=("win", "mean"),
    )
    for year, row in yearly.iterrows():
        bar = "+" * int(max(0, row["total_ret"] * 5)) + "-" * int(max(0, -row["total_ret"] * 5))
        print(f"  {year}: {row['trades_count']:2.0f} trades | "
              f"WR {row['win_rate']*100:4.0f}% | "
              f"Total {row['total_ret']:+6.2f}% | {bar}")

    print()
    print("=" * 70)

    # Kill/keep verdict
    if profit_factor >= 1.5 and len(trades) >= 50:
        print("VERDICT: PASS (PF >= 1.5 on 50+ trades)")
        print("  -> Candidate for paper deployment after burn-in completes")
    elif profit_factor >= 1.2:
        print("VERDICT: MARGINAL (PF 1.2-1.5)")
        print("  -> Needs deeper analysis — check if edge is decaying")
    else:
        print("VERDICT: FAIL (PF < 1.2)")
        print("  -> Kill this strategy, move to GDX/GLD pairs")
    print("=" * 70)


def save_trades(trades: pd.DataFrame):
    """Save trade log to forge/fomc_drift_trades.csv"""
    out_path = Path(__file__).parent / "fomc_drift_trades.csv"
    trades.to_csv(out_path, index=False)
    print(f"\nTrade log saved: {out_path}")


def plot_equity(trades: pd.DataFrame):
    """Optional equity curve plot."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — skipping plot")
        return

    cum_ret = (1 + trades["return_pct"] / 100).cumprod()
    dates = pd.to_datetime(trades["fomc_date"])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(dates, cum_ret, "b-", linewidth=1.5)
    ax1.set_title("FOMC Pre-Announcement Drift — Equity Curve (SPY)", fontsize=14)
    ax1.set_ylabel("Cumulative Return (1.0 = starting capital)")
    ax1.grid(True, alpha=0.3)
    ax1.axhline(y=1.0, color="gray", linestyle="--", alpha=0.5)

    colors = ["green" if r > 0 else "red" for r in trades["return_pct"]]
    ax2.bar(dates, trades["return_pct"], color=colors, width=20, alpha=0.7)
    ax2.set_ylabel("Trade Return %")
    ax2.set_xlabel("Date")
    ax2.grid(True, alpha=0.3)
    ax2.axhline(y=0, color="gray", linestyle="--", alpha=0.5)

    plt.tight_layout()
    plot_path = Path(__file__).parent / "fomc_drift_equity.png"
    plt.savefig(plot_path, dpi=150)
    print(f"Equity curve saved: {plot_path}")
    plt.show()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FOMC Pre-Announcement Drift Backtest")
    parser.add_argument("--start", type=int, default=2000, help="Start year")
    parser.add_argument("--end", type=int, default=2026, help="End year")
    parser.add_argument("--plot", action="store_true", help="Show equity curve plot")
    args = parser.parse_args()

    trades = run_backtest(args.start, args.end)
    print_results(trades)
    save_trades(trades)

    if args.plot:
        plot_equity(trades)
