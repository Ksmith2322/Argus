"""
Turn-of-Month (TOM) Effect — International ETF Backtest
========================================================
Edge hypothesis: Pension/fund flow timing across jurisdictions hasn't been
fully equalized. US TOM is arbed out; international markets may still show
a persistent turn-of-month premium (last 2 trading days + first 3 of next
month capture disproportionate returns).

Instruments:
    EEM  — iShares Emerging Markets (~2003)
    EWJ  — iShares Japan (~1996)
    VGK  — Vanguard FTSE Europe (~2005)
    SPY  — US benchmark / control

Usage:
    python -m forge.tom_international
"""

import sys
import os
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("Required: pip install yfinance pandas numpy")
    sys.exit(1)

# ── Config ──────────────────────────────────────────────────────────────────
ETFS = {
    "EEM": "2003-06-01",
    "EWJ": "1996-04-01",
    "VGK": "2005-04-01",
    "SPY": "1996-01-01",
}
TOM_ENTRY_DAYS_BEFORE_EOM = 2   # buy at close, 2nd-to-last trading day
TOM_EXIT_DAYS_AFTER_BOM = 3     # sell at close, 3rd trading day of new month
TRADE_LOG_PATH = Path(__file__).parent / "tom_international_trades.csv"


# ── Helpers ─────────────────────────────────────────────────────────────────
def download_data(ticker: str, start: str) -> pd.DataFrame:
    """Download daily OHLCV from yfinance."""
    df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
    # yfinance may return multi-level columns for single ticker
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.sort_index()
    return df


def identify_tom_windows(dates: pd.DatetimeIndex):
    """
    Return list of (entry_date, exit_date) pairs for each TOM window.
    Entry = close of T-2 (2nd to last trading day of month).
    Exit  = close of T+3 (3rd trading day of next month).
    """
    df_dates = pd.Series(dates, name="date").to_frame()
    df_dates["ym"] = df_dates["date"].dt.to_period("M")

    windows = []
    months = df_dates["ym"].unique()
    for i in range(len(months) - 1):
        this_month = months[i]
        next_month = months[i + 1]

        days_this = df_dates[df_dates["ym"] == this_month]["date"].values
        days_next = df_dates[df_dates["ym"] == next_month]["date"].values

        if len(days_this) < TOM_ENTRY_DAYS_BEFORE_EOM:
            continue
        if len(days_next) < TOM_EXIT_DAYS_AFTER_BOM:
            continue

        entry_date = days_this[-TOM_ENTRY_DAYS_BEFORE_EOM]
        exit_date = days_next[TOM_EXIT_DAYS_AFTER_BOM - 1]
        windows.append((pd.Timestamp(entry_date), pd.Timestamp(exit_date)))

    return windows


def backtest_etf(ticker: str, df: pd.DataFrame):
    """
    Run TOM backtest on a single ETF.
    Returns: trades DataFrame, rest-of-month returns Series.
    """
    close = df["Close"]
    windows = identify_tom_windows(close.index)

    trades = []
    rest_returns = []

    for i, (entry_dt, exit_dt) in enumerate(windows):
        if entry_dt not in close.index or exit_dt not in close.index:
            continue
        entry_px = close.loc[entry_dt]
        exit_px = close.loc[exit_dt]
        ret = (exit_px - entry_px) / entry_px

        trades.append({
            "ticker": ticker,
            "entry_date": entry_dt,
            "exit_date": exit_dt,
            "entry_px": round(float(entry_px), 4),
            "exit_px": round(float(exit_px), 4),
            "return": round(float(ret), 6),
            "win": int(ret > 0),
        })

        # Rest-of-month: from this exit to next entry
        if i + 1 < len(windows):
            next_entry = windows[i + 1][0]
            if next_entry in close.index and exit_dt in close.index:
                rest_ret = (close.loc[next_entry] - close.loc[exit_dt]) / close.loc[exit_dt]
                rest_returns.append(float(rest_ret))

    return pd.DataFrame(trades), rest_returns


def profit_factor(returns: pd.Series) -> float:
    gains = returns[returns > 0].sum()
    losses = abs(returns[returns < 0].sum())
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return round(gains / losses, 2)


def decade_breakdown(trades_df: pd.DataFrame):
    """Print by-decade stats."""
    trades_df = trades_df.copy()
    trades_df["decade"] = (trades_df["entry_date"].dt.year // 10) * 10
    decades = sorted(trades_df["decade"].unique())
    rows = []
    for d in decades:
        sub = trades_df[trades_df["decade"] == d]
        avg_ret = sub["return"].mean() * 100
        wr = sub["win"].mean() * 100
        n = len(sub)
        pf = profit_factor(sub["return"])
        rows.append(f"    {d}s:  n={n:>4}  avg={avg_ret:>+6.2f}%  WR={wr:>5.1f}%  PF={pf}")
    return "\n".join(rows)


def period_split(trades_df: pd.DataFrame, cutoff_year: int = 2015):
    """Compare pre/post cutoff."""
    pre = trades_df[trades_df["entry_date"].dt.year < cutoff_year]
    post = trades_df[trades_df["entry_date"].dt.year >= cutoff_year]
    results = {}
    for label, sub in [("pre", pre), ("post", post)]:
        if len(sub) == 0:
            results[label] = {"n": 0, "avg": 0, "wr": 0, "pf": 0}
            continue
        results[label] = {
            "n": len(sub),
            "avg": sub["return"].mean() * 100,
            "wr": sub["win"].mean() * 100,
            "pf": profit_factor(sub["return"]),
        }
    return results


def compute_portfolio_equity(all_trades: pd.DataFrame, etfs: list):
    """
    Equal-weight portfolio: invest 1/N in each ETF during TOM windows only.
    Returns equity curve, annual return, Sharpe, max drawdown.
    """
    # Gather all unique TOM trade dates and returns per ETF
    portfolio_returns = []
    # Group by entry_date — if multiple ETFs have same entry that's fine,
    # but TOM windows differ slightly per ETF. Align by exit_date month.
    for _, row in all_trades.iterrows():
        portfolio_returns.append({
            "date": row["exit_date"],
            "ticker": row["ticker"],
            "return": row["return"],
        })

    pr_df = pd.DataFrame(portfolio_returns)
    if pr_df.empty:
        return None

    # Average return across ETFs per exit month
    pr_df["ym"] = pr_df["date"].dt.to_period("M")
    monthly = pr_df.groupby("ym")["return"].mean()

    # Build equity curve
    equity = (1 + monthly).cumprod()
    total_years = len(monthly) / 12
    total_ret = equity.iloc[-1] - 1
    ann_ret = (equity.iloc[-1]) ** (1 / total_years) - 1 if total_years > 0 else 0

    # Sharpe (annualized, assume ~12 periods/year since we're monthly)
    sharpe = (monthly.mean() / monthly.std()) * np.sqrt(12) if monthly.std() > 0 else 0

    # Max drawdown
    eq_series = equity.values
    peak = np.maximum.accumulate(eq_series)
    dd = (eq_series - peak) / peak
    max_dd = dd.min()

    return {
        "total_return": total_ret,
        "annual_return": ann_ret,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "n_months": len(monthly),
        "years": total_years,
    }


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    print("=" * 72)
    print("  Turn-of-Month (TOM) International ETF Backtest")
    print("  Window: last 2 trading days + first 3 trading days of next month")
    print("=" * 72)

    all_trades = []
    etf_stats = {}

    for ticker, start in ETFS.items():
        print(f"\n{'─' * 72}")
        print(f"  {ticker} — downloading from {start}...")
        df = download_data(ticker, start)
        if df.empty:
            print(f"  WARNING: no data for {ticker}, skipping.")
            continue
        print(f"  {ticker}: {len(df)} bars, {df.index[0].date()} to {df.index[-1].date()}")

        trades_df, rest_returns = backtest_etf(ticker, df)
        if trades_df.empty:
            print(f"  WARNING: no trades for {ticker}")
            continue

        all_trades.append(trades_df)

        avg_tom = trades_df["return"].mean() * 100
        avg_rest = np.mean(rest_returns) * 100 if rest_returns else 0
        total_tom = ((1 + trades_df["return"]).prod() - 1) * 100
        total_rest = ((1 + pd.Series(rest_returns)).prod() - 1) * 100 if rest_returns else 0
        win_rate = trades_df["win"].mean() * 100
        pf = profit_factor(trades_df["return"])

        # TOM share of total returns
        total_combined = total_tom + total_rest
        tom_share = (total_tom / total_combined * 100) if total_combined != 0 else 0

        etf_stats[ticker] = {
            "n": len(trades_df),
            "wr": win_rate,
            "avg_tom": avg_tom,
            "avg_rest": avg_rest,
            "total_tom": total_tom,
            "total_rest": total_rest,
            "tom_share": tom_share,
            "pf": pf,
        }

        print(f"\n  {ticker} Results:")
        print(f"    Total TOM trades:       {len(trades_df)}")
        print(f"    Win rate:               {win_rate:.1f}%")
        print(f"    Avg TOM return:         {avg_tom:+.3f}%")
        print(f"    Avg rest-of-month ret:  {avg_rest:+.3f}%")
        print(f"    Total TOM return:       {total_tom:+.1f}%")
        print(f"    Total rest-of-month:    {total_rest:+.1f}%")
        print(f"    TOM share of returns:   {tom_share:.1f}%")
        print(f"    Profit factor:          {pf}")
        print(f"\n    By decade:")
        print(decade_breakdown(trades_df))

        ps = period_split(trades_df)
        print(f"\n    Decay check (pre/post 2015):")
        print(f"      Pre-2015:  n={ps['pre']['n']:>4}  avg={ps['pre']['avg']:>+6.3f}%  WR={ps['pre']['wr']:>5.1f}%  PF={ps['pre']['pf']}")
        print(f"      Post-2015: n={ps['post']['n']:>4}  avg={ps['post']['avg']:>+6.3f}%  WR={ps['post']['wr']:>5.1f}%  PF={ps['post']['pf']}")

    if not all_trades:
        print("\nNo trades generated. Exiting.")
        sys.exit(1)

    combined = pd.concat(all_trades, ignore_index=True)

    # Save trade log
    combined.to_csv(TRADE_LOG_PATH, index=False)
    print(f"\n  Trade log saved: {TRADE_LOG_PATH}")

    # ── Comparative summary ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  COMPARATIVE SUMMARY: TOM Share of Returns")
    print(f"{'=' * 72}")
    print(f"  {'Ticker':<8} {'Trades':>7} {'WR':>7} {'AvgTOM':>9} {'AvgRest':>9} {'TotalTOM':>10} {'TotalRest':>10} {'TOMShare':>9} {'PF':>6}")
    print(f"  {'─' * 76}")
    for t, s in etf_stats.items():
        label = "CTRL" if t == "SPY" else "INTL"
        print(f"  {t:<5}({label}) {s['n']:>5} {s['wr']:>6.1f}% {s['avg_tom']:>+8.3f}% {s['avg_rest']:>+8.3f}% {s['total_tom']:>+9.1f}% {s['total_rest']:>+9.1f}% {s['tom_share']:>8.1f}% {s['pf']:>5}")

    # ── Combined portfolio (international only) ─────────────────────────
    intl_tickers = ["EEM", "EWJ", "VGK"]
    intl_trades = combined[combined["ticker"].isin(intl_tickers)]

    print(f"\n{'=' * 72}")
    print("  COMBINED PORTFOLIO: Equal-weight EEM+EWJ+VGK (TOM only)")
    print(f"{'=' * 72}")

    port = compute_portfolio_equity(intl_trades, intl_tickers)
    if port:
        print(f"    Period:          {port['years']:.1f} years ({port['n_months']} monthly windows)")
        print(f"    Total return:    {port['total_return'] * 100:+.1f}%")
        print(f"    Annual return:   {port['annual_return'] * 100:+.2f}%")
        print(f"    Sharpe ratio:    {port['sharpe']:.2f}")
        print(f"    Max drawdown:    {port['max_drawdown'] * 100:.1f}%")

    # ── Kill / Keep Verdict ─────────────────────────────────────────────
    print(f"\n{'=' * 72}")
    print("  KILL / KEEP VERDICT")
    print(f"{'=' * 72}")

    verdicts = []

    # Check 1: TOM captures >60% of returns for international ETFs
    intl_shares = [etf_stats[t]["tom_share"] for t in intl_tickers if t in etf_stats]
    avg_intl_share = np.mean(intl_shares) if intl_shares else 0
    check1 = avg_intl_share > 60
    verdicts.append(check1)
    print(f"  [{'PASS' if check1 else 'FAIL'}] TOM captures >60% of intl returns: avg={avg_intl_share:.1f}%")

    # Check 2: Annual return > 3% with max DD < 15%
    check2 = False
    if port:
        ann_ok = port["annual_return"] * 100 > 3
        dd_ok = abs(port["max_drawdown"] * 100) < 15
        check2 = ann_ok and dd_ok
        print(f"  [{'PASS' if check2 else 'FAIL'}] Annual > 3% and DD < 15%: ann={port['annual_return']*100:.2f}%, DD={port['max_drawdown']*100:.1f}%")
    else:
        print(f"  [FAIL] No portfolio data")
    verdicts.append(check2)

    # Check 3: SPY TOM should be weaker than international (edge is intl-specific)
    spy_share = etf_stats.get("SPY", {}).get("tom_share", 0)
    check3 = avg_intl_share > spy_share
    verdicts.append(check3)
    print(f"  [{'PASS' if check3 else 'FAIL'}] Intl TOM share ({avg_intl_share:.1f}%) > SPY TOM share ({spy_share:.1f}%): edge is international-specific")

    # Check 4: Post-2015 decay
    intl_post = []
    intl_pre = []
    for t in intl_tickers:
        sub = combined[combined["ticker"] == t]
        ps = period_split(sub)
        intl_post.append(ps["post"]["avg"])
        intl_pre.append(ps["pre"]["avg"])
    avg_pre = np.mean(intl_pre)
    avg_post = np.mean(intl_post)
    decay_pct = ((avg_post - avg_pre) / abs(avg_pre) * 100) if avg_pre != 0 else 0
    # Pass if post-2015 still positive (even if decayed)
    check4 = avg_post > 0
    verdicts.append(check4)
    print(f"  [{'PASS' if check4 else 'FAIL'}] Post-2015 still positive: pre={avg_pre:+.3f}%, post={avg_post:+.3f}% (decay={decay_pct:+.0f}%)")

    # Final verdict
    passes = sum(verdicts)
    if passes >= 3:
        final = "KEEP"
    elif passes >= 2:
        final = "MARGINAL — needs more evidence"
    else:
        final = "KILL"

    print(f"\n  >>> FINAL VERDICT: {final} ({passes}/4 checks passed) <<<")
    print(f"{'=' * 72}")


if __name__ == "__main__":
    main()
