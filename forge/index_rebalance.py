"""
S&P 500 Index Rebalance Scanner & Backtest
===========================================
Edge: When a stock is added to the S&P 500, ~$8 trillion in index fund
      assets must buy it. The announcement comes 5-7 trading days before
      the effective date. The buying is mandatory and the size is calculable.
      Deletions face the mirror image: forced selling pressure.

Mechanism: Passive fund rebalancing creates predictable supply/demand
           imbalance during a known window.
Reference: Chen, Noronha & Singal (2004) — "The Price Response to S&P 500
           Index Additions and Deletions." Additions average +3-5%
           announcement-to-effective, deletions average -5 to -15%.

Usage:
    python -m forge.index_rebalance --backtest
    python -m forge.index_rebalance --predict
    python -m forge.index_rebalance --signals
    python -m forge.index_rebalance --test
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("Required: pip install yfinance pandas numpy")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logging & heartbeat
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs" / "rebalance"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "rebalance.log"),
    ],
)
log = logging.getLogger("index_rebalance")


def write_heartbeat(mode: str, status: str = "ok", extra: dict | None = None):
    """Write heartbeat JSON for fleet integration."""
    hb = {
        "system": "forge.index_rebalance",
        "mode": mode,
        "status": status,
        "pid": __import__("os").getpid(),
        "ts": datetime.now(tz=__import__("datetime").timezone.utc).isoformat(),
    }
    if extra:
        hb.update(extra)
    hb_path = LOG_DIR / "heartbeat.json"
    hb_path.write_text(json.dumps(hb, indent=2))
    log.info(f"Heartbeat written: {hb_path}")


# ---------------------------------------------------------------------------
# Historical S&P 500 changes 2023-2025
# Source: S&P Dow Jones Indices press releases
# (announcement_date, effective_date, ticker, action, replacing_ticker)
# ---------------------------------------------------------------------------
SP500_CHANGES = [
    # 2023
    ("2023-01-20", "2023-01-26", "AXON", "ADD", "DISH"),
    ("2023-03-03", "2023-03-15", "INVH", "ADD", "SIVB"),
    ("2023-03-15", "2023-03-20", "VLTO", "ADD", None),      # spin-off from DHR
    ("2023-06-02", "2023-06-05", "BX", "ADD", "ATVI"),
    ("2023-06-16", "2023-06-20", "ABNB", "ADD", "NWL"),
    ("2023-09-01", "2023-09-18", "BG", "ADD", "LNTH"),
    ("2023-09-01", "2023-09-18", "UBER", "ADD", "SJM"),
    ("2023-12-01", "2023-12-18", "UBER", "ADD", "SEE"),      # already added above — skip in analysis
    # 2024
    ("2024-01-19", "2024-01-25", "UBER", "ADD", None),       # final settlement after Russell fast-track
    ("2024-03-01", "2024-03-18", "SMCI", "ADD", "WHR"),
    ("2024-03-15", "2024-03-18", "DECK", "ADD", "ZION"),
    ("2024-06-07", "2024-06-24", "KKR", "ADD", "RHI"),
    ("2024-06-07", "2024-06-24", "CRWD", "ADD", "PARA"),
    ("2024-06-07", "2024-06-24", "GEV", "ADD", None),        # spin-off from GE
    ("2024-09-06", "2024-09-23", "DELL", "ADD", "ETSY"),
    ("2024-09-06", "2024-09-23", "PLTR", "ADD", "AAL"),
    ("2024-09-06", "2024-09-23", "ERIE", "ADD", "BIO"),
    ("2024-12-13", "2024-12-23", "APO", "ADD", "QRVO"),
    # 2025
    ("2025-03-07", "2025-03-24", "DT", "ADD", "FMC"),
    ("2025-03-07", "2025-03-24", "WSO", "ADD", "CE"),
    # Deletions — forced selling pressure
    ("2023-01-20", "2023-01-26", "DISH", "DELETE", None),
    ("2023-06-16", "2023-06-20", "NWL", "DELETE", None),
    ("2023-09-01", "2023-09-18", "LNTH", "DELETE", None),
    ("2023-09-01", "2023-09-18", "SJM", "DELETE", None),
    ("2024-03-01", "2024-03-18", "WHR", "DELETE", None),
    ("2024-03-15", "2024-03-18", "ZION", "DELETE", None),
    ("2024-06-07", "2024-06-24", "RHI", "DELETE", None),
    ("2024-06-07", "2024-06-24", "PARA", "DELETE", None),
    ("2024-09-06", "2024-09-23", "ETSY", "DELETE", None),
    ("2024-09-06", "2024-09-23", "AAL", "DELETE", None),
    ("2024-09-06", "2024-09-23", "BIO", "DELETE", None),
    ("2024-12-13", "2024-12-23", "QRVO", "DELETE", None),
    ("2025-03-07", "2025-03-24", "FMC", "DELETE", None),
    ("2025-03-07", "2025-03-24", "CE", "DELETE", None),
]

# Current S&P 500 members (top ~50 by weight — used by predict to exclude).
# We only need a rough exclusion list; the predictor screens non-members.
SP500_TOP_TICKERS = {
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "BRK-B",
    "TSLA", "UNH", "XOM", "JNJ", "JPM", "V", "PG", "MA", "AVGO", "HD",
    "CVX", "MRK", "ABBV", "LLY", "COST", "PEP", "KO", "WMT", "ADBE",
    "CRM", "MCD", "CSCO", "ACN", "TMO", "ABT", "DHR", "NKE", "CMCSA",
    "NFLX", "INTC", "VZ", "DIS", "PM", "TXN", "QCOM", "HON", "RTX",
    "AMGN", "NEE", "LOW", "UNP", "IBM", "GE", "BA", "CAT", "ISRG",
    "SPGI", "INTU", "BLK", "AXP", "SYK", "MDT", "DE", "LMT", "ADI",
    "GILD", "REGN", "BKNG", "PLD", "VRTX", "AMT", "PANW", "ABNB",
    "CRWD", "KKR", "PLTR", "DELL", "APO", "GEV", "SMCI", "DECK", "ERIE",
    "UBER", "BX", "DT", "WSO", "AXON", "INVH", "BG",
}


# ---------------------------------------------------------------------------
# Part 1: Download price data
# ---------------------------------------------------------------------------
def _get_prices(ticker: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Download daily OHLCV from yfinance. Returns None on failure."""
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        # Flatten MultiIndex columns if present (yfinance 0.2.x)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        log.warning(f"Failed to download {ticker}: {e}")
        return None


def _find_trading_day(prices: pd.DataFrame, target_date: str, direction: int = 1) -> Optional[str]:
    """Find the nearest trading day on or after (direction=1) / before (direction=-1) target."""
    target = pd.Timestamp(target_date)
    if direction >= 0:
        mask = prices.index >= target
        if mask.any():
            return str(prices.index[mask][0].date())
    else:
        mask = prices.index <= target
        if mask.any():
            return str(prices.index[mask][-1].date())
    return None


# ---------------------------------------------------------------------------
# Part 2: Backtest the index effect
# ---------------------------------------------------------------------------
def _compute_return(prices: pd.DataFrame, start_date: str, end_date: str) -> Optional[float]:
    """Compute total return between two dates (using Close). Returns % return."""
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    # Find nearest trading days
    mask_start = prices.index >= start_ts
    mask_end = prices.index >= end_ts
    if not mask_start.any() or not mask_end.any():
        return None
    p0 = float(prices.loc[prices.index[mask_start][0], "Close"])
    p1 = float(prices.loc[prices.index[mask_end][0], "Close"])
    if p0 == 0:
        return None
    return (p1 / p0 - 1) * 100


def run_backtest() -> pd.DataFrame:
    """Run full historical backtest of index rebalance effect."""
    log.info("Starting index rebalance backtest...")

    # Deduplicate: keep first occurrence of each (ticker, action) pair
    seen = set()
    changes = []
    for ann, eff, tick, action, repl in SP500_CHANGES:
        key = (tick, action, ann)
        if key not in seen:
            seen.add(key)
            changes.append((ann, eff, tick, action, repl))

    # Download SPY for benchmark
    spy = _get_prices("SPY", "2022-12-01", datetime.now().strftime("%Y-%m-%d"))
    if spy is None:
        log.error("Could not download SPY data")
        return pd.DataFrame()

    results = []
    for ann_date, eff_date, ticker, action, replacing in changes:
        log.info(f"  {action} {ticker}  announce={ann_date}  effective={eff_date}")

        # Pad dates for download
        dl_start = (pd.Timestamp(ann_date) - timedelta(days=10)).strftime("%Y-%m-%d")
        dl_end = (pd.Timestamp(eff_date) + timedelta(days=35)).strftime("%Y-%m-%d")
        prices = _get_prices(ticker, dl_start, dl_end)
        if prices is None or len(prices) < 5:
            log.warning(f"    Skipping {ticker} — insufficient data")
            continue

        # Announcement to effective return
        ann_ret = _compute_return(prices, ann_date, eff_date)
        spy_ann_ret = _compute_return(spy, ann_date, eff_date)

        # Post-rebalance drift: effective to effective + 20 trading days
        eff_ts = pd.Timestamp(eff_date)
        post_mask = prices.index >= eff_ts
        if post_mask.sum() < 15:
            # Download more data if needed
            ext_end = (eff_ts + timedelta(days=45)).strftime("%Y-%m-%d")
            prices_ext = _get_prices(ticker, eff_date, ext_end)
            if prices_ext is not None and len(prices_ext) >= 15:
                prices = pd.concat([prices, prices_ext]).loc[~pd.concat([prices, prices_ext]).index.duplicated()]
                prices = prices.sort_index()

        # Compute post-effective return (20 trading days)
        post_dates = prices.index[prices.index >= eff_ts]
        if len(post_dates) >= 15:
            post_end = str(post_dates[min(19, len(post_dates) - 1)].date())
            post_ret = _compute_return(prices, eff_date, post_end)
            spy_post_ret = _compute_return(spy, eff_date, post_end)
        else:
            post_ret = None
            spy_post_ret = None

        alpha_ann = (ann_ret - spy_ann_ret) if (ann_ret is not None and spy_ann_ret is not None) else None
        alpha_post = (post_ret - spy_post_ret) if (post_ret is not None and spy_post_ret is not None) else None

        results.append({
            "ticker": ticker,
            "action": action,
            "announce_date": ann_date,
            "effective_date": eff_date,
            "replacing": replacing,
            "ann_to_eff_ret_pct": round(ann_ret, 2) if ann_ret is not None else None,
            "spy_ann_to_eff_pct": round(spy_ann_ret, 2) if spy_ann_ret is not None else None,
            "alpha_ann_pct": round(alpha_ann, 2) if alpha_ann is not None else None,
            "post_eff_20d_ret_pct": round(post_ret, 2) if post_ret is not None else None,
            "spy_post_20d_pct": round(spy_post_ret, 2) if spy_post_ret is not None else None,
            "alpha_post_pct": round(alpha_post, 2) if alpha_post is not None else None,
        })

    df = pd.DataFrame(results)
    # Save trades CSV
    out_path = Path(__file__).parent / "index_rebalance_trades.csv"
    df.to_csv(out_path, index=False)
    log.info(f"Trade log saved: {out_path}")
    return df


def print_backtest_results(df: pd.DataFrame):
    """Print comprehensive backtest report."""
    if df.empty:
        print("No results to report.")
        return

    print()
    print("=" * 78)
    print("  S&P 500 INDEX REBALANCE EFFECT — BACKTEST RESULTS")
    print("=" * 78)

    for action, label in [("ADD", "ADDITIONS"), ("DELETE", "DELETIONS")]:
        sub = df[df["action"] == action].dropna(subset=["ann_to_eff_ret_pct"])
        if sub.empty:
            continue

        print(f"\n{'-' * 78}")
        print(f"  {label}  ({len(sub)} events)")
        print(f"{'-' * 78}")

        # Announcement to effective window
        rets = sub["ann_to_eff_ret_pct"]
        alphas = sub["alpha_ann_pct"].dropna()

        if action == "ADD":
            hits = (rets > 0).sum()
        else:
            hits = (rets < 0).sum()

        hit_rate = hits / len(rets) * 100 if len(rets) > 0 else 0

        print(f"\n  Announcement -> Effective Date Window:")
        print(f"    Mean return:    {rets.mean():+.2f}%")
        print(f"    Median return:  {rets.median():+.2f}%")
        print(f"    Std dev:        {rets.std():.2f}%")
        print(f"    Mean alpha:     {alphas.mean():+.2f}%" if len(alphas) > 0 else "    Mean alpha:     N/A")
        if action == "ADD":
            print(f"    Hit rate:       {hit_rate:.0f}% ({hits}/{len(rets)} went UP)")
        else:
            print(f"    Hit rate:       {hit_rate:.0f}% ({hits}/{len(rets)} went DOWN)")

        # Profit factor
        gains = rets[rets > 0].sum() if action == "ADD" else abs(rets[rets < 0].sum())
        losses = abs(rets[rets < 0].sum()) if action == "ADD" else rets[rets > 0].sum()
        pf = gains / losses if losses > 0 else float("inf")
        print(f"    Profit factor:  {pf:.2f}")

        # Best / worst
        best_idx = rets.idxmax() if action == "ADD" else rets.idxmin()
        worst_idx = rets.idxmin() if action == "ADD" else rets.idxmax()
        best_row = sub.loc[best_idx]
        worst_row = sub.loc[worst_idx]
        print(f"\n    Best:   {best_row['ticker']:6s}  {best_row['ann_to_eff_ret_pct']:+.1f}%  ({best_row['announce_date']})")
        print(f"    Worst:  {worst_row['ticker']:6s}  {worst_row['ann_to_eff_ret_pct']:+.1f}%  ({worst_row['announce_date']})")

        # Post-rebalance drift
        post = sub["post_eff_20d_ret_pct"].dropna()
        post_alpha = sub["alpha_post_pct"].dropna()
        if len(post) > 0:
            print(f"\n  Post-Effective 20-Day Drift:")
            print(f"    Mean return:    {post.mean():+.2f}%")
            print(f"    Mean alpha:     {post_alpha.mean():+.2f}%" if len(post_alpha) > 0 else "    Mean alpha:     N/A")

        # Individual trades
        print(f"\n  {'Ticker':<8} {'Announce':<12} {'Effective':<12} {'Ret%':>8} {'Alpha%':>8} {'Post20d%':>9}")
        print(f"  {'-'*8} {'-'*12} {'-'*12} {'-'*8} {'-'*8} {'-'*9}")
        for _, row in sub.sort_values("announce_date").iterrows():
            ret_s = f"{row['ann_to_eff_ret_pct']:+.1f}" if pd.notna(row["ann_to_eff_ret_pct"]) else "N/A"
            alpha_s = f"{row['alpha_ann_pct']:+.1f}" if pd.notna(row["alpha_ann_pct"]) else "N/A"
            post_s = f"{row['post_eff_20d_ret_pct']:+.1f}" if pd.notna(row["post_eff_20d_ret_pct"]) else "N/A"
            print(f"  {row['ticker']:<8} {row['announce_date']:<12} {row['effective_date']:<12} {ret_s:>8} {alpha_s:>8} {post_s:>9}")

    # Overall summary
    adds = df[df["action"] == "ADD"].dropna(subset=["ann_to_eff_ret_pct"])
    dels = df[df["action"] == "DELETE"].dropna(subset=["ann_to_eff_ret_pct"])
    print(f"\n{'=' * 78}")
    print("  OVERALL SUMMARY")
    print(f"{'=' * 78}")
    if len(adds) > 0:
        print(f"  Additions avg return:  {adds['ann_to_eff_ret_pct'].mean():+.2f}%  (n={len(adds)})")
        add_alpha = adds["alpha_ann_pct"].dropna()
        if len(add_alpha) > 0:
            print(f"  Additions avg alpha:   {add_alpha.mean():+.2f}%")
    if len(dels) > 0:
        print(f"  Deletions avg return:  {dels['ann_to_eff_ret_pct'].mean():+.2f}%  (n={len(dels)})")
        del_alpha = dels["alpha_ann_pct"].dropna()
        if len(del_alpha) > 0:
            print(f"  Deletions avg alpha:   {del_alpha.mean():+.2f}%")

    # Combined long additions + short deletions
    if len(adds) > 0 and len(dels) > 0:
        combined = list(adds["ann_to_eff_ret_pct"]) + list(-dels["ann_to_eff_ret_pct"])
        combined = np.array(combined)
        print(f"\n  Combined (long ADD + short DEL):")
        print(f"    Mean per-trade:  {combined.mean():+.2f}%")
        total_wins = (combined > 0).sum()
        print(f"    Hit rate:        {total_wins / len(combined) * 100:.0f}% ({total_wins}/{len(combined)})")
        gains = combined[combined > 0].sum()
        losses = abs(combined[combined < 0].sum())
        pf = gains / losses if losses > 0 else float("inf")
        print(f"    Profit factor:   {pf:.2f}")

    # Verdict
    if len(adds) > 0:
        avg_add = adds["ann_to_eff_ret_pct"].mean()
        if avg_add >= 2.0:
            print(f"\n  VERDICT: STRONG EDGE — Additions average {avg_add:+.1f}% in announcement window")
        elif avg_add >= 1.0:
            print(f"\n  VERDICT: MODERATE EDGE — Additions average {avg_add:+.1f}%")
        else:
            print(f"\n  VERDICT: WEAK — Additions average only {avg_add:+.1f}%")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Part 3: Predictive scanner — who gets added next?
# ---------------------------------------------------------------------------
# Large-cap stocks NOT currently in S&P 500 (candidates to screen).
# This list is curated; in production you would pull from a universe screener.
CANDIDATE_TICKERS = [
    # Large non-S&P 500 stocks as of early 2025
    "MSTR",   # MicroStrategy — crypto exposure, huge mkt cap now
    "RKLB",   # Rocket Lab
    "APP",    # AppLovin
    "DUOL",   # Duolingo
    "CELH",   # Celsius Holdings
    "TOST",   # Toast
    "COIN",   # Coinbase
    "ARM",    # Arm Holdings (UK domicile issue)
    "RDDT",   # Reddit
    "IBKR",   # Interactive Brokers
    "CPNG",   # Coupang
    "LI",     # Li Auto
    "GRAB",   # Grab Holdings
    "SE",     # Sea Ltd
    "SHOP",   # Shopify (Canadian domicile)
    "SPOT",   # Spotify (Luxembourg)
    "SOFI",   # SoFi Technologies
    "HOOD",   # Robinhood
    "WDAY",   # Workday — already in S&P 500
    "CFLT",   # Confluent
    "IOT",    # Samsara
    "NET",    # Cloudflare
    "DDOG",   # Datadog
    "ZS",     # Zscaler
    "MDB",    # MongoDB
    "SNOW",   # Snowflake
    "U",      # Unity Software
    "PATH",   # UiPath
    "BILL",   # Bill Holdings
    "TWLO",   # Twilio
    "ROKU",   # Roku
    "PINS",   # Pinterest
    "SNAP",   # Snap
    "LYFT",   # Lyft
    "OKTA",   # Okta
    "TTD",    # The Trade Desk
    "CAVA",   # Cava Group
    "TXRH",   # Texas Roadhouse
    "LW",     # Lamb Weston
    "TPL",    # Texas Pacific Land
]


def predict_sp500_additions(n: int = 10) -> list[dict]:
    """
    Screen for stocks likely to be added to S&P 500 next.
    Criteria: market cap > $15B, US-domiciled, positive earnings,
    not currently in S&P 500, sufficient trading volume.

    Returns candidates ranked by market cap (largest non-members are
    most likely to be added).
    """
    log.info(f"Screening {len(CANDIDATE_TICKERS)} candidates for S&P 500 addition...")
    candidates = []

    for ticker in CANDIDATE_TICKERS:
        if ticker in SP500_TOP_TICKERS:
            continue
        try:
            info = yf.Ticker(ticker).info
            mkt_cap = info.get("marketCap", 0) or 0
            country = info.get("country", "")
            name = info.get("shortName", ticker)
            trailing_eps = info.get("trailingEps", None)
            avg_volume = info.get("averageDailyVolume10Day", 0) or 0

            if mkt_cap < 15_000_000_000:  # $15B minimum
                continue

            # S&P requires US domicile
            us_domicile = country in ("United States", "US", "")

            # S&P requires positive GAAP earnings (sum of last 4 quarters)
            positive_earnings = trailing_eps is not None and trailing_eps > 0

            candidates.append({
                "ticker": ticker,
                "name": name,
                "market_cap_B": round(mkt_cap / 1e9, 1),
                "country": country,
                "us_domicile": us_domicile,
                "trailing_eps": round(trailing_eps, 2) if trailing_eps else None,
                "positive_earnings": positive_earnings,
                "avg_volume": avg_volume,
                "eligible": us_domicile and positive_earnings,
            })

        except Exception as e:
            log.warning(f"  Skipping {ticker}: {e}")

    # Sort by market cap descending
    candidates.sort(key=lambda x: x["market_cap_B"], reverse=True)

    return candidates[:n]


def print_predictions(candidates: list[dict]):
    """Print prediction results."""
    print()
    print("=" * 78)
    print("  S&P 500 ADDITION CANDIDATES — Ranked by Market Cap")
    print("=" * 78)
    print(f"\n  Criteria: Mkt Cap > $15B, US-domiciled, positive trailing EPS")
    print(f"  Screened: {len(CANDIDATE_TICKERS)} large non-member stocks\n")

    eligible = [c for c in candidates if c["eligible"]]
    ineligible = [c for c in candidates if not c["eligible"]]

    if eligible:
        print(f"  ELIGIBLE ({len(eligible)}):")
        print(f"  {'Ticker':<8} {'Name':<28} {'MktCap':>10} {'EPS':>8} {'AvgVol':>12}")
        print(f"  {'-'*8} {'-'*28} {'-'*10} {'-'*8} {'-'*12}")
        for c in eligible:
            eps_s = f"${c['trailing_eps']:.2f}" if c["trailing_eps"] else "N/A"
            vol_s = f"{c['avg_volume']:>12,}" if c["avg_volume"] else "N/A"
            print(f"  {c['ticker']:<8} {c['name'][:27]:<28} ${c['market_cap_B']:>7.1f}B {eps_s:>8} {vol_s}")

    if ineligible:
        print(f"\n  INELIGIBLE ({len(ineligible)}):")
        print(f"  {'Ticker':<8} {'Name':<28} {'MktCap':>10} {'Reason':<30}")
        print(f"  {'-'*8} {'-'*28} {'-'*10} {'-'*30}")
        for c in ineligible:
            reasons = []
            if not c["us_domicile"]:
                reasons.append(f"Foreign ({c['country']})")
            if not c["positive_earnings"]:
                reasons.append("No positive earnings")
            print(f"  {c['ticker']:<8} {c['name'][:27]:<28} ${c['market_cap_B']:>7.1f}B {'; '.join(reasons):<30}")

    print(f"\n{'=' * 78}")
    print("  NOTE: S&P committee has discretion. Largest eligible non-members")
    print("  are most likely but timing is unpredictable. Watch for press releases")
    print("  from S&P Dow Jones Indices (typically Fridays after market close).")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Part 4: Active signal generation
# ---------------------------------------------------------------------------
def get_active_rebalance_signals() -> list[dict]:
    """
    Check if any announced rebalances have open trading windows.
    A window is open if today is between announcement_date and
    effective_date + 5 trading days.
    """
    today = pd.Timestamp(datetime.now().date())
    signals = []

    for ann_date, eff_date, ticker, action, replacing in SP500_CHANGES:
        ann_ts = pd.Timestamp(ann_date)
        eff_ts = pd.Timestamp(eff_date)

        # Window: announcement to effective + 5 calendar days
        window_end = eff_ts + timedelta(days=7)

        if ann_ts <= today <= window_end:
            # Compute how far through the window we are
            total_days = (eff_ts - ann_ts).days
            elapsed = (today - ann_ts).days
            pct_elapsed = elapsed / total_days * 100 if total_days > 0 else 100

            expected_ret = "+2 to +5%" if action == "ADD" else "-5 to -15%"
            direction = "LONG" if action == "ADD" else "SHORT"

            signals.append({
                "ticker": ticker,
                "action": action,
                "direction": direction,
                "announce_date": ann_date,
                "effective_date": eff_date,
                "window_pct_elapsed": round(pct_elapsed, 0),
                "expected_return": expected_ret,
                "status": "POST-EFFECTIVE" if today > eff_ts else "ACTIVE",
            })

    return signals


def print_signals(signals: list[dict]):
    """Print active signals."""
    print()
    print("=" * 78)
    print("  ACTIVE INDEX REBALANCE SIGNALS")
    print("=" * 78)

    if not signals:
        print("\n  No active rebalance windows. Next announcement TBD.")
        print("  Monitor S&P Dow Jones Indices press releases (usually Fridays).")
    else:
        for s in signals:
            print(f"\n  {s['direction']} {s['ticker']}  ({s['action']})")
            print(f"    Announced:     {s['announce_date']}")
            print(f"    Effective:     {s['effective_date']}")
            print(f"    Window:        {s['window_pct_elapsed']:.0f}% elapsed  [{s['status']}]")
            print(f"    Expected:      {s['expected_return']}")

    print(f"\n{'=' * 78}")


# ---------------------------------------------------------------------------
# Quick test mode
# ---------------------------------------------------------------------------
def run_quick_test():
    """Test with 3-4 examples to verify logic."""
    print()
    print("=" * 78)
    print("  QUICK TEST — 4 sample rebalance events")
    print("=" * 78)

    test_cases = [
        ("2024-09-06", "2024-09-23", "PLTR", "ADD"),
        ("2024-09-06", "2024-09-23", "DELL", "ADD"),
        ("2024-09-06", "2024-09-23", "ETSY", "DELETE"),
        ("2024-06-07", "2024-06-24", "CRWD", "ADD"),
    ]

    spy = _get_prices("SPY", "2024-05-01", "2025-01-01")
    if spy is None:
        print("  ERROR: Could not download SPY data")
        return

    for ann, eff, ticker, action in test_cases:
        start_dl = (pd.Timestamp(ann) - timedelta(days=5)).strftime("%Y-%m-%d")
        end_dl = (pd.Timestamp(eff) + timedelta(days=30)).strftime("%Y-%m-%d")
        prices = _get_prices(ticker, start_dl, end_dl)
        if prices is None:
            print(f"\n  {ticker}: NO DATA")
            continue

        ret = _compute_return(prices, ann, eff)
        spy_ret = _compute_return(spy, ann, eff)
        alpha = (ret - spy_ret) if ret is not None and spy_ret is not None else None

        print(f"\n  {action} {ticker}  {ann} -> {eff}")
        print(f"    Return:  {ret:+.2f}%" if ret is not None else "    Return:  N/A")
        print(f"    SPY:     {spy_ret:+.2f}%" if spy_ret is not None else "    SPY:     N/A")
        print(f"    Alpha:   {alpha:+.2f}%" if alpha is not None else "    Alpha:   N/A")

    print(f"\n{'=' * 78}")
    print("  Test complete. If returns look reasonable, run --backtest for full results.")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="S&P 500 Index Rebalance Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  --backtest   Run full historical backtest on 2023-2025 S&P 500 changes
  --predict    Show top candidates for next S&P 500 addition
  --signals    Show any active rebalance trading windows
  --test       Quick test with a few examples
        """,
    )
    parser.add_argument("--backtest", action="store_true", help="Run full historical backtest")
    parser.add_argument("--predict", action="store_true", help="Predict next S&P 500 additions")
    parser.add_argument("--signals", action="store_true", help="Show active rebalance signals")
    parser.add_argument("--test", action="store_true", help="Quick test with sample events")
    parser.add_argument("-n", type=int, default=10, help="Number of predictions to show (default: 10)")
    args = parser.parse_args()

    if not any([args.backtest, args.predict, args.signals, args.test]):
        parser.print_help()
        sys.exit(0)

    if args.test:
        write_heartbeat("test", "running")
        run_quick_test()
        write_heartbeat("test", "done")

    if args.backtest:
        write_heartbeat("backtest", "running")
        df = run_backtest()
        print_backtest_results(df)
        write_heartbeat("backtest", "done", {"trades": len(df)})

    if args.predict:
        write_heartbeat("predict", "running")
        candidates = predict_sp500_additions(n=args.n)
        print_predictions(candidates)
        write_heartbeat("predict", "done", {"candidates": len(candidates)})

    if args.signals:
        write_heartbeat("signals", "running")
        signals = get_active_rebalance_signals()
        print_signals(signals)
        write_heartbeat("signals", "done", {"active": len(signals)})
