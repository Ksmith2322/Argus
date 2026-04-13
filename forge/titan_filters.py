"""
Titan Pre-Trade Safety Filters
================================
Earnings gate, news sentiment check, and combined pre-trade screen.
Usable by any fleet system, designed primarily for Titan.

Usage:
    from forge.titan_filters import titan_pre_trade_check

    result = titan_pre_trade_check("MSFT", direction="LONG")
    if not result["allowed"]:
        log.warning("Blocked: %s", result["warnings"])

CLI:
    python -m forge.titan_filters --check MSFT
    python -m forge.titan_filters --scan MSFT AAPL NVDA
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Negative / positive keyword lists (fallback when FinBERT unavailable)
# ---------------------------------------------------------------------------

_NEGATIVE_KEYWORDS = [
    "crash", "collapse", "bankruptcy", "fraud", "lawsuit", "recall",
    "investigation", "downgrade", "layoff", "layoffs", "cut", "loss",
    "warning", "miss", "decline", "plunge", "scandal", "default",
    "SEC", "indictment", "fine", "penalty", "slump", "sell-off",
    "selloff", "bearish", "concern", "risk", "trouble", "weak",
    "worst", "fear", "drop", "tank", "tumble", "debt", "deficit",
]

_POSITIVE_KEYWORDS = [
    "beat", "upgrade", "growth", "profit", "record", "surge", "rally",
    "bullish", "outperform", "raise", "strong", "innovation", "deal",
    "partnership", "expansion", "approval", "breakout", "momentum",
    "buy", "optimism", "gain", "boost", "exceed", "impressive",
]


# =========================================================================
# A. Earnings Calendar Gate
# =========================================================================

def check_earnings_proximity(ticker: str, days_threshold: int = 5) -> dict:
    """
    Check if earnings are within N trading days.

    Returns:
        {
            "has_upcoming_earnings": bool,
            "earnings_date": str | None,
            "days_until": int | None,
        }

    If earnings are within threshold, Titan should SKIP this entry.
    Apollo handles earnings specifically -- Titan should avoid the binary risk.
    """
    result = {
        "has_upcoming_earnings": False,
        "earnings_date": None,
        "days_until": None,
    }

    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)

        # Try .calendar first (dict with 'Earnings Date')
        earnings_date = None
        try:
            cal = tk.calendar
            if cal is not None:
                # calendar can be a DataFrame or dict depending on yfinance version
                if hasattr(cal, "to_dict"):
                    cal = cal.to_dict()
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if ed:
                        if isinstance(ed, list):
                            ed = ed[0]
                        if isinstance(ed, str):
                            earnings_date = datetime.fromisoformat(ed).date()
                        elif hasattr(ed, "date"):
                            earnings_date = ed.date() if callable(getattr(ed, "date")) else ed
                        elif hasattr(ed, "year"):
                            # Already a date-like
                            from datetime import date as _date
                            earnings_date = _date(ed.year, ed.month, ed.day)
        except Exception:
            pass

        # Fallback: .earnings_dates
        if earnings_date is None:
            try:
                edates = tk.earnings_dates
                if edates is not None and not edates.empty:
                    now = datetime.now(timezone.utc)
                    future = edates.index[edates.index >= now]
                    if len(future) > 0:
                        next_dt = future.min()
                        if hasattr(next_dt, "date"):
                            earnings_date = next_dt.date()
                        else:
                            earnings_date = datetime.fromisoformat(str(next_dt)).date()
            except Exception:
                pass

        if earnings_date is None:
            result["has_upcoming_earnings"] = False
            return result

        from datetime import date as _date
        today = _date.today()
        days_until = (earnings_date - today).days

        result["earnings_date"] = earnings_date.isoformat()
        result["days_until"] = days_until

        if 0 <= days_until <= days_threshold:
            result["has_upcoming_earnings"] = True
        else:
            result["has_upcoming_earnings"] = False

        return result

    except ImportError:
        log.warning("yfinance not installed -- earnings check skipped")
        return result
    except Exception as e:
        log.debug("Earnings check failed for %s: %s", ticker, e)
        return result


# =========================================================================
# B. Per-Stock News Sentiment Filter
# =========================================================================

def _keyword_sentiment(headline: str) -> float:
    """Simple keyword-based sentiment score (-1 to +1)."""
    headline_lower = headline.lower()
    neg = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in headline_lower)
    pos = sum(1 for kw in _POSITIVE_KEYWORDS if kw in headline_lower)
    total = neg + pos
    if total == 0:
        return 0.0
    return round((pos - neg) / total, 4)


def _finbert_sentiment(headlines: list[str]) -> Optional[list[dict]]:
    """Try FinBERT scoring. Returns None if unavailable."""
    try:
        from forge.atlas.classify.finbert_scorer import score_batch
        results = score_batch(headlines)
        if results and results[0] is not None:
            return results
        return None
    except Exception:
        return None


def check_news_sentiment(ticker: str) -> dict:
    """
    Quick headline sentiment check before entry.

    Returns:
        {
            "sentiment": "positive" | "negative" | "neutral",
            "score": float,          # -1.0 to +1.0
            "headline_count": int,
            "block_entry": bool,
            "reason": str,
        }

    If recent headlines are strongly negative (score < -0.5), block entry.
    """
    result = {
        "sentiment": "neutral",
        "score": 0.0,
        "headline_count": 0,
        "block_entry": False,
        "reason": "no data",
    }

    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)
        news = tk.news

        if not news:
            result["reason"] = "no recent headlines"
            return result

        # Extract up to 5 most recent headlines
        headlines = []
        for item in news[:5]:
            title = item.get("title") or item.get("headline", "")
            if title:
                headlines.append(title)

        if not headlines:
            result["reason"] = "no parseable headlines"
            return result

        result["headline_count"] = len(headlines)

        # Try FinBERT first, fall back to keywords
        finbert_results = _finbert_sentiment(headlines)

        if finbert_results:
            scores = []
            for r in finbert_results:
                if r is not None:
                    # Map FinBERT labels to -1/0/+1 scale
                    if r["label"] == "positive":
                        scores.append(r["positive"])
                    elif r["label"] == "negative":
                        scores.append(-r["negative"])
                    else:
                        scores.append(0.0)
            avg_score = sum(scores) / len(scores) if scores else 0.0
            result["reason"] = f"FinBERT ({len(scores)} headlines)"
        else:
            scores = [_keyword_sentiment(h) for h in headlines]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            result["reason"] = f"keyword ({len(scores)} headlines)"

        avg_score = round(avg_score, 4)
        result["score"] = avg_score

        if avg_score > 0.3:
            result["sentiment"] = "positive"
        elif avg_score < -0.3:
            result["sentiment"] = "negative"
        else:
            result["sentiment"] = "neutral"

        # Block entry on strongly negative sentiment
        if avg_score < -0.5:
            result["block_entry"] = True
            result["reason"] += " -- BLOCKED: strongly negative sentiment"

        return result

    except ImportError:
        log.warning("yfinance not installed -- sentiment check skipped")
        result["reason"] = "yfinance not installed"
        return result
    except Exception as e:
        log.debug("News sentiment check failed for %s: %s", ticker, e)
        result["reason"] = f"error: {e}"
        return result


# =========================================================================
# C. Combined Pre-Trade Check
# =========================================================================

def titan_pre_trade_check(ticker: str, direction: str = "LONG") -> dict:
    """
    Run all pre-trade filters for Titan.

    Returns:
        {
            "allowed": True/False,
            "checks": {
                "earnings": {"passed": True, "detail": "..."},
                "sentiment": {"passed": True, "detail": "..."},
                "conviction": {"passed": True, "detail": "..."},
            },
            "size_modifier": 1.0,
            "warnings": [],
        }
    """
    direction = direction.upper()
    ticker = ticker.upper()

    result = {
        "allowed": True,
        "checks": {},
        "size_modifier": 1.0,
        "warnings": [],
    }

    # --- Earnings gate ---
    earnings = check_earnings_proximity(ticker)
    if earnings["has_upcoming_earnings"]:
        result["checks"]["earnings"] = {
            "passed": False,
            "detail": f"earnings in {earnings['days_until']}d ({earnings['earnings_date']})",
        }
        result["allowed"] = False
        result["warnings"].append(
            f"Earnings within threshold: {earnings['earnings_date']} "
            f"({earnings['days_until']}d away) -- Apollo territory"
        )
    else:
        days_str = (
            f"next earnings {earnings['earnings_date']} ({earnings['days_until']}d)"
            if earnings["earnings_date"]
            else "no earnings data found"
        )
        result["checks"]["earnings"] = {"passed": True, "detail": days_str}

    # --- News sentiment ---
    sentiment = check_news_sentiment(ticker)
    if sentiment["block_entry"]:
        result["checks"]["sentiment"] = {
            "passed": False,
            "detail": f"{sentiment['sentiment']}, score {sentiment['score']:.2f} -- {sentiment['reason']}",
        }
        result["allowed"] = False
        result["warnings"].append(
            f"Strongly negative sentiment: {sentiment['score']:.2f}"
        )
    else:
        result["checks"]["sentiment"] = {
            "passed": True,
            "detail": f"{sentiment['sentiment']}, score {sentiment['score']:.2f} ({sentiment['headline_count']} headlines)",
        }

    # --- Conviction score ---
    try:
        from forge.conviction import score_conviction

        conv = score_conviction(
            system="titan", ticker=ticker, direction=direction
        )
        mult = conv["size_multiplier"]
        conviction_level = conv["conviction"]

        if mult <= 0.25:
            result["checks"]["conviction"] = {
                "passed": False,
                "detail": f"{mult}x {conviction_level} -- signals conflicting",
            }
            result["allowed"] = False
            result["warnings"].append(
                f"Conviction too low: {conviction_level} ({mult}x)"
            )
        else:
            result["checks"]["conviction"] = {
                "passed": True,
                "detail": f"{mult}x {conviction_level} conviction",
            }
        result["size_modifier"] = mult

        # Propagate conviction warnings
        for w in conv.get("warnings", []):
            result["warnings"].append(f"conviction: {w}")

    except Exception as e:
        result["checks"]["conviction"] = {
            "passed": True,
            "detail": f"error loading conviction: {e}",
        }

    return result


# =========================================================================
# CLI
# =========================================================================

def _print_check(ticker: str, result: dict) -> None:
    """Pretty-print a single ticker check."""
    status = "ALLOWED" if result["allowed"] else "BLOCKED"
    print(f"\n{'=' * 60}")
    print(f"  {ticker}  --  {status}")
    print(f"{'=' * 60}")
    print(f"  Size modifier: {result['size_modifier']}x")
    print()

    for name, check in result["checks"].items():
        icon = "PASS" if check["passed"] else "FAIL"
        print(f"  [{icon}] {name:12s}  {check['detail']}")

    if result["warnings"]:
        print()
        print("  Warnings:")
        for w in result["warnings"]:
            print(f"    !! {w}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Titan Pre-Trade Safety Filters"
    )
    parser.add_argument(
        "--check", type=str, metavar="TICKER",
        help="Run all filters on a specific stock",
    )
    parser.add_argument(
        "--scan", nargs="+", metavar="TICKER",
        help="Check multiple tickers",
    )
    parser.add_argument(
        "--direction", type=str, default="LONG",
        choices=["LONG", "SHORT"],
        help="Trade direction (default: LONG)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    if args.check:
        result = titan_pre_trade_check(args.check, direction=args.direction)
        _print_check(args.check.upper(), result)

    elif args.scan:
        blocked = []
        allowed = []
        for ticker in args.scan:
            result = titan_pre_trade_check(ticker, direction=args.direction)
            _print_check(ticker.upper(), result)
            if result["allowed"]:
                allowed.append(ticker.upper())
            else:
                blocked.append(ticker.upper())

        print("=" * 60)
        print(f"  SCAN SUMMARY: {len(allowed)} allowed, {len(blocked)} blocked")
        if allowed:
            print(f"    Allowed: {', '.join(allowed)}")
        if blocked:
            print(f"    Blocked: {', '.join(blocked)}")
        print("=" * 60)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
