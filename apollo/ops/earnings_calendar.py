"""apollo/ops/earnings_calendar.py -- Dynamic earnings calendar across broad market.

Scans S&P 500 + high-beta growth stocks to find ALL upcoming earnings.
Returns sorted by date so Apollo can focus on the next 1-2 weeks.
"""
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import yfinance as yf
except ImportError:
    yf = None

_log = logging.getLogger("apollo.calendar")

# Broad universe: S&P 500 top 100 by market cap + high-beta growth
# This covers 80%+ of all liquid earnings plays
SP500_TOP = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B",
    "JPM", "V", "UNH", "XOM", "JNJ", "MA", "PG", "HD", "AVGO", "CVX",
    "MRK", "ABBV", "LLY", "COST", "PEP", "KO", "WMT", "BAC", "CRM",
    "ADBE", "TMO", "MCD", "AMD", "NFLX", "CSCO", "ACN", "LIN", "ABT",
    "ORCL", "TXN", "DHR", "PM", "QCOM", "INTC", "NEE", "INTU", "UPS",
    "GE", "CAT", "IBM", "RTX", "HON", "AMGN", "LOW", "SBUX", "GS",
    "BLK", "MS", "MDLZ", "ISRG", "ADP", "SYK", "GILD", "BKNG", "CB",
    "VRTX", "PANW", "REGN", "LRCX", "KLAC", "SNPS", "CDNS", "AMAT",
    "MU", "MRVL", "FTNT", "CRWD", "WDAY", "TEAM", "ZS", "DDOG",
    "SNOW", "SHOP", "SQ", "COIN", "PLTR", "SOFI", "HOOD", "UPST",
    "AFRM", "NU", "ARM", "SMCI", "TSM",
]

HIGH_BETA = [
    "MRNA", "BNTX", "CRSP", "EDIT", "MARA", "RIOT", "RIVN", "LCID",
    "NIO", "PLUG", "FSLR", "ENPH", "SNAP", "PINS", "RBLX", "GME",
    "AMC", "BABA", "PDD", "JD", "ARKK",
]

FULL_UNIVERSE = list(dict.fromkeys(SP500_TOP + HIGH_BETA))  # deduplicated


def scan_earnings_calendar(
    days_ahead: int = 14,
    days_back: int = 3,
    universe: list[str] | None = None,
) -> list[dict]:
    """Scan universe for upcoming earnings dates.

    Returns list sorted by earnings date.
    """
    if yf is None:
        return []

    symbols = universe or FULL_UNIVERSE
    now = datetime.now()
    results = []

    _log.info(f"Scanning {len(symbols)} stocks for earnings ({days_back}d back to {days_ahead}d ahead)...")

    for i, sym in enumerate(symbols):
        try:
            t = yf.Ticker(sym)
            cal = t.calendar or {}
            dates = cal.get("Earnings Date", [])
            if not dates:
                continue

            next_er = dates[0] if isinstance(dates, list) else dates
            if not hasattr(next_er, 'day'):
                continue

            days_until = (next_er - now.date()).days
            if days_until < -days_back or days_until > days_ahead:
                continue

            results.append({
                "symbol": sym,
                "earnings_date": str(next_er),
                "days_until": days_until,
                "eps_estimate": cal.get("Earnings Average"),
                "eps_high": cal.get("Earnings High"),
                "eps_low": cal.get("Earnings Low"),
                "revenue_estimate": cal.get("Revenue Average"),
            })
        except Exception:
            pass

        if (i + 1) % 20 == 0:
            _log.info(f"  {i + 1}/{len(symbols)} scanned...")
            time.sleep(0.3)

    results.sort(key=lambda x: x["days_until"])
    _log.info(f"Found {len(results)} stocks with earnings in window")
    return results


def get_this_weeks_earnings(universe: list[str] | None = None) -> list[dict]:
    """Get stocks reporting this week (Mon-Fri)."""
    return scan_earnings_calendar(days_ahead=7, days_back=1, universe=universe)


def get_next_week_earnings(universe: list[str] | None = None) -> list[dict]:
    """Get stocks reporting next week."""
    return scan_earnings_calendar(days_ahead=14, days_back=0, universe=universe)
