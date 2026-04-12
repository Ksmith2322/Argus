"""
FOMC Pre-Announcement Drift — Deep Filter Analysis
====================================================
Reads forge/fomc_drift_trades.csv and tests several filter variations
to determine if a stronger sub-edge exists or if the drift should be killed.

Usage:
    python -m forge.fomc_drift_deep
"""

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

CSV_PATH = Path(__file__).parent / "fomc_drift_trades.csv"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def calc_stats(df: pd.DataFrame, label: str) -> dict:
    """Calculate standard trade stats for a subset of trades."""
    n = len(df)
    if n == 0:
        return {"label": label, "n": 0, "win_rate": 0, "pf": 0, "avg_ret": 0, "total_ret": 0}
    wins = df[df["return_pct"] > 0]
    losses = df[df["return_pct"] <= 0]
    gross_w = wins["return_pct"].sum() if len(wins) > 0 else 0
    gross_l = abs(losses["return_pct"].sum()) if len(losses) > 0 else 0.0001
    pf = gross_w / gross_l
    return {
        "label": label,
        "n": n,
        "win_rate": len(wins) / n * 100,
        "pf": pf,
        "avg_ret": df["return_pct"].mean(),
        "total_ret": df["return_pct"].sum(),
        "median_ret": df["return_pct"].median(),
    }


def print_table(rows: list[dict], title: str):
    """Print a formatted summary table."""
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)
    print(f"  {'Filter':<30} {'N':>5} {'WinR%':>7} {'PF':>7} {'AvgRet%':>9} {'MedRet%':>9} {'TotRet%':>9}")
    print("-" * 78)
    for r in rows:
        if r["n"] == 0:
            print(f"  {r['label']:<30} {'--':>5} {'--':>7} {'--':>7} {'--':>9} {'--':>9} {'--':>9}")
        else:
            print(f"  {r['label']:<30} {r['n']:>5} {r['win_rate']:>6.1f}% {r['pf']:>7.2f} {r['avg_ret']:>8.3f}% {r['median_ret']:>8.3f}% {r['total_ret']:>8.2f}%")
    print("-" * 78)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_trades() -> pd.DataFrame:
    """Load trades CSV."""
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found. Run forge.fomc_drift first.")
        sys.exit(1)
    df = pd.read_csv(CSV_PATH, parse_dates=["fomc_date", "entry_date"])
    print(f"Loaded {len(df)} trades from {CSV_PATH.name}")
    return df


def download_spy_ohlc() -> pd.DataFrame:
    """Download SPY daily data for overnight/intraday split."""
    print("Downloading SPY daily data for open/close decomposition ...")
    spy = yf.download("SPY", start="1999-12-01", end="2026-12-31",
                       auto_adjust=True, progress=False)
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = spy.columns.get_level_values(0)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    print(f"  SPY: {len(spy)} bars")
    return spy


def download_vix() -> pd.DataFrame:
    """Download VIX daily close."""
    print("Downloading ^VIX data ...")
    vix = yf.download("^VIX", start="1999-12-01", end="2026-12-31",
                       auto_adjust=True, progress=False)
    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = vix.columns.get_level_values(0)
    vix.index = pd.to_datetime(vix.index).tz_localize(None)
    print(f"  VIX: {len(vix)} bars")
    return vix


# ---------------------------------------------------------------------------
# Analysis 1: Overnight vs Intraday decomposition
# ---------------------------------------------------------------------------

def analysis_overnight_vs_intraday(trades: pd.DataFrame, spy: pd.DataFrame):
    """Decompose each trade into overnight (prior close -> FOMC open) and
    intraday (FOMC open -> FOMC close) components."""
    records = []
    for _, t in trades.iterrows():
        fomc_ts = pd.Timestamp(t["fomc_date"])
        if fomc_ts not in spy.index:
            continue
        entry_price = t["entry_price"]  # prior day close
        fomc_open = float(spy.loc[fomc_ts, "Open"])
        fomc_close = float(spy.loc[fomc_ts, "Close"])

        overnight_ret = (fomc_open - entry_price) / entry_price * 100
        intraday_ret = (fomc_close - fomc_open) / fomc_open * 100
        total_ret = t["return_pct"]

        records.append({
            "fomc_date": t["fomc_date"],
            "year": t["year"],
            "overnight_ret": overnight_ret,
            "intraday_ret": intraday_ret,
            "total_ret": total_ret,
        })

    df = pd.DataFrame(records)

    # Build stats for each component
    rows = []
    for col, label in [("total_ret", "Total (prior close->close)"),
                        ("overnight_ret", "Overnight (close->open)"),
                        ("intraday_ret", "Intraday (open->close)")]:
        tmp = df.rename(columns={col: "return_pct"})
        rows.append(calc_stats(tmp, label))

    print_table(rows, "ANALYSIS 1: Overnight vs Intraday Decomposition")

    # Decade breakdown for each component
    for col, label in [("overnight_ret", "Overnight"), ("intraday_ret", "Intraday")]:
        decade_rows = []
        for decade_start in [2000, 2010, 2020]:
            decade_end = decade_start + 9
            mask = (df["year"] >= decade_start) & (df["year"] <= decade_end)
            sub = df[mask].rename(columns={col: "return_pct"})
            decade_rows.append(calc_stats(sub, f"{label} {decade_start}s"))
        print_table(decade_rows, f"  {label} by Decade")


# ---------------------------------------------------------------------------
# Analysis 2: VIX regime filter
# ---------------------------------------------------------------------------

def analysis_vix_regime(trades: pd.DataFrame, vix: pd.DataFrame):
    """Split trades by VIX level at entry."""
    records = []
    for _, t in trades.iterrows():
        entry_ts = pd.Timestamp(t["entry_date"])
        # Find VIX on entry date (or nearest prior)
        vix_dates = vix.index[vix.index <= entry_ts]
        if len(vix_dates) == 0:
            continue
        vix_close = float(vix.loc[vix_dates[-1], "Close"])
        records.append({**t.to_dict(), "vix": vix_close})

    df = pd.DataFrame(records)
    rows = [
        calc_stats(df, "All trades"),
        calc_stats(df[df["vix"] > 20], "VIX > 20"),
        calc_stats(df[df["vix"] <= 20], "VIX <= 20"),
        calc_stats(df[df["vix"] > 25], "VIX > 25"),
        calc_stats(df[df["vix"] > 30], "VIX > 30"),
    ]
    print_table(rows, "ANALYSIS 2: VIX Regime Filter")


# ---------------------------------------------------------------------------
# Analysis 3: Market regime (SPY 60-day return as proxy)
# ---------------------------------------------------------------------------

def analysis_market_regime(trades: pd.DataFrame, spy: pd.DataFrame):
    """Classify regime by SPY's 60-trading-day return before entry."""
    records = []
    for _, t in trades.iterrows():
        entry_ts = pd.Timestamp(t["entry_date"])
        if entry_ts not in spy.index:
            # find nearest prior
            prior = spy.index[spy.index <= entry_ts]
            if len(prior) == 0:
                continue
            entry_ts = prior[-1]

        idx = spy.index.get_loc(entry_ts)
        if idx < 60:
            continue
        price_now = float(spy.loc[entry_ts, "Close"])
        price_60 = float(spy.iloc[idx - 60]["Close"])
        ret_60d = (price_now - price_60) / price_60 * 100

        if ret_60d < -5:
            regime = "Crisis (<-5%)"
        elif ret_60d > 5:
            regime = "Bull (>+5%)"
        else:
            regime = "Neutral (-5/+5%)"

        records.append({**t.to_dict(), "regime": regime, "spy_60d": ret_60d})

    df = pd.DataFrame(records)
    rows = [calc_stats(df, "All trades")]
    for regime in ["Crisis (<-5%)", "Neutral (-5/+5%)", "Bull (>+5%)"]:
        sub = df[df["regime"] == regime]
        rows.append(calc_stats(sub, regime))

    print_table(rows, "ANALYSIS 3: Market Regime (SPY 60-day return)")


# ---------------------------------------------------------------------------
# Analysis 4: Post-2015 only (post-publication)
# ---------------------------------------------------------------------------

def analysis_post_publication(trades: pd.DataFrame):
    """Stats on 2015-2026 trades only — after Lucca/Moench paper published."""
    rows = [
        calc_stats(trades, "All trades (2000-2026)"),
        calc_stats(trades[trades["year"] < 2015], "Pre-publication (2000-2014)"),
        calc_stats(trades[trades["year"] >= 2015], "Post-publication (2015-2026)"),
        calc_stats(trades[trades["year"] >= 2020], "Recent (2020-2026)"),
    ]
    print_table(rows, "ANALYSIS 4: Post-Publication Edge Decay")


# ---------------------------------------------------------------------------
# Analysis 5: Gap between meetings
# ---------------------------------------------------------------------------

def analysis_meeting_gap(trades: pd.DataFrame):
    """Does edge differ after long gaps (>6 weeks) vs regular cadence?"""
    trades_sorted = trades.sort_values("fomc_date").reset_index(drop=True)
    records = []
    for i, t in trades_sorted.iterrows():
        if i == 0:
            gap_days = 99  # first meeting, treat as long gap
        else:
            prev = trades_sorted.iloc[i - 1]
            gap_days = (pd.Timestamp(t["fomc_date"]) - pd.Timestamp(prev["fomc_date"])).days

        gap_type = "Long gap (>42 days)" if gap_days > 42 else "Regular cadence (<=42d)"
        records.append({**t.to_dict(), "gap_days": gap_days, "gap_type": gap_type})

    df = pd.DataFrame(records)
    rows = [
        calc_stats(df, "All trades"),
        calc_stats(df[df["gap_type"] == "Long gap (>42 days)"], "Long gap (>42 days)"),
        calc_stats(df[df["gap_type"] == "Regular cadence (<=42d)"], "Regular cadence (<=42d)"),
    ]
    print_table(rows, "ANALYSIS 5: Meeting Gap (>6 weeks vs regular)")


# ---------------------------------------------------------------------------
# Final recommendation
# ---------------------------------------------------------------------------

def final_recommendation(trades: pd.DataFrame):
    """Print honest verdict."""
    post_pub = trades[trades["year"] >= 2015]
    recent = trades[trades["year"] >= 2020]

    post_wins = post_pub[post_pub["return_pct"] > 0]
    post_losses = post_pub[post_pub["return_pct"] <= 0]
    post_gw = post_wins["return_pct"].sum() if len(post_wins) else 0
    post_gl = abs(post_losses["return_pct"].sum()) if len(post_losses) else 0.0001
    post_pf = post_gw / post_gl

    rec_wins = recent[recent["return_pct"] > 0]
    rec_losses = recent[recent["return_pct"] <= 0]
    rec_gw = rec_wins["return_pct"].sum() if len(rec_wins) else 0
    rec_gl = abs(rec_losses["return_pct"].sum()) if len(rec_losses) else 0.0001
    rec_pf = rec_gw / rec_gl

    print()
    print("=" * 78)
    print("  FINAL RECOMMENDATION")
    print("=" * 78)
    print()
    print(f"  Post-publication PF (2015+):  {post_pf:.2f}  ({len(post_pub)} trades)")
    print(f"  Recent PF (2020+):            {rec_pf:.2f}  ({len(recent)} trades)")
    print()

    if post_pf < 1.2 and rec_pf < 1.2:
        print("  VERDICT: KILL FOMC DRIFT")
        print("  -------")
        print("  The edge has decayed to noise post-publication. The 2000s produced")
        print("  the entire profit factor. Post-2015, PF is near 1.0 — no edge.")
        print("  This is textbook anomaly arbitrage: once published, it dies.")
        print()
        print("  Action items:")
        print("  - Remove FOMC drift from live consideration")
        print("  - Do NOT allocate capital to this pattern")
        print("  - Redirect research time to strategies with recent edge")
    elif post_pf >= 1.3:
        print("  VERDICT: CONDITIONAL KEEP")
        print("  -------")
        print("  Post-publication PF still decent. Look for a filter that")
        print("  concentrates the edge (VIX regime, crisis periods, etc).")
        print("  Consider a filtered variant with fewer but higher-quality trades.")
    else:
        print("  VERDICT: MARGINAL — LIKELY KILL")
        print("  -------")
        print(f"  Post-publication PF of {post_pf:.2f} is too thin to trade after costs.")
        print("  Unless a specific filter produces PF > 1.5 on 20+ trades post-2015,")
        print("  this should be killed. Check the filter analyses above.")
        print()
        print("  The honest answer: FOMC drift was a real anomaly that has been")
        print("  arbitraged away. Publication + algorithmic trading killed it.")

    print()
    print("=" * 78)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    trades = load_trades()
    spy = download_spy_ohlc()
    vix = download_vix()

    analysis_overnight_vs_intraday(trades, spy)
    analysis_vix_regime(trades, vix)
    analysis_market_regime(trades, spy)
    analysis_post_publication(trades)
    analysis_meeting_gap(trades)
    final_recommendation(trades)


if __name__ == "__main__":
    main()
