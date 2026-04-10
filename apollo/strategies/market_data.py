"""apollo/strategies/market_data.py -- Extended market data for catalyst scoring.

Pulls short interest, institutional ownership, analyst targets, valuation metrics,
and known event calendars. All from free yfinance data.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

try:
    import yfinance as yf
except ImportError:
    yf = None

_log = logging.getLogger("apollo.data")


def get_short_interest(symbol: str) -> dict:
    """Get short interest data — high short interest + positive catalyst = squeeze potential."""
    result = {
        "short_pct_float": 0, "short_ratio": 0,
        "short_change_pct": 0,  # month-over-month change
        "signal": 0, "details": [],
    }
    if yf is None:
        return result

    try:
        info = yf.Ticker(symbol).info or {}
        shares_short = info.get("sharesShort", 0) or 0
        shares_short_prior = info.get("sharesShortPriorMonth", 0) or 0
        short_pct = (info.get("shortPercentOfFloat", 0) or 0) * 100
        short_ratio = info.get("shortRatio", 0) or 0

        result["short_pct_float"] = round(short_pct, 2)
        result["short_ratio"] = round(short_ratio, 2)

        if shares_short_prior > 0:
            result["short_change_pct"] = round((shares_short / shares_short_prior - 1) * 100, 1)

        # Score
        if short_pct > 20:
            result["signal"] += 15
            result["details"].append(f"HIGH_SHORT: {short_pct:.1f}% float short — squeeze potential")
        elif short_pct > 10:
            result["signal"] += 5
            result["details"].append(f"ELEVATED_SHORT: {short_pct:.1f}% float")

        if result["short_change_pct"] > 15:
            result["signal"] -= 5
            result["details"].append(f"SHORT_BUILDING: +{result['short_change_pct']:.0f}% MoM")
        elif result["short_change_pct"] < -15:
            result["signal"] += 10
            result["details"].append(f"SHORT_COVERING: {result['short_change_pct']:.0f}% MoM — bullish")

        if short_ratio > 5:
            result["signal"] += 10
            result["details"].append(f"DAYS_TO_COVER: {short_ratio:.1f}d — squeeze fuel")

    except Exception as e:
        _log.debug(f"{symbol} short interest error: {e}")

    return result


def get_analyst_targets(symbol: str) -> dict:
    """Get analyst price targets — upside/downside from current price."""
    result = {
        "current_price": 0, "target_mean": 0, "target_high": 0, "target_low": 0,
        "upside_pct": 0, "recommendation": 0,
        "signal": 0, "details": [],
    }
    if yf is None:
        return result

    try:
        info = yf.Ticker(symbol).info or {}
        price = info.get("currentPrice", info.get("regularMarketPrice", 0)) or 0
        target = info.get("targetMeanPrice", 0) or 0
        target_high = info.get("targetHighPrice", 0) or 0
        target_low = info.get("targetLowPrice", 0) or 0
        rec = info.get("recommendationMean", 3) or 3  # 1=Strong Buy, 5=Sell

        result["current_price"] = round(price, 2)
        result["target_mean"] = round(target, 2)
        result["target_high"] = round(target_high, 2)
        result["target_low"] = round(target_low, 2)
        result["recommendation"] = round(rec, 2)

        if price > 0 and target > 0:
            upside = (target / price - 1) * 100
            result["upside_pct"] = round(upside, 1)

            if upside > 30:
                result["signal"] += 15
                result["details"].append(f"BIG_UPSIDE: +{upside:.0f}% to analyst target ${target:.0f}")
            elif upside > 15:
                result["signal"] += 8
                result["details"].append(f"UPSIDE: +{upside:.0f}% to target")
            elif upside < -10:
                result["signal"] -= 10
                result["details"].append(f"OVERVALUED: {upside:.0f}% above target ${target:.0f}")

        if rec <= 1.5:
            result["signal"] += 10
            result["details"].append(f"STRONG_BUY consensus ({rec:.1f}/5)")
        elif rec >= 3.5:
            result["signal"] -= 10
            result["details"].append(f"SELL consensus ({rec:.1f}/5)")

    except Exception as e:
        _log.debug(f"{symbol} target error: {e}")

    return result


def get_valuation_context(symbol: str) -> dict:
    """Get valuation metrics for context — cheap stocks with catalysts = best setup."""
    result = {
        "forward_pe": 0, "trailing_pe": 0, "price_to_book": 0,
        "revenue_growth": 0, "earnings_growth": 0, "profit_margin": 0,
        "free_cashflow": 0,
        "signal": 0, "details": [],
    }
    if yf is None:
        return result

    try:
        info = yf.Ticker(symbol).info or {}
        fpe = info.get("forwardPE", 0) or 0
        tpe = info.get("trailingPE", 0) or 0
        ptb = info.get("priceToBook", 0) or 0
        rev_g = (info.get("revenueGrowth", 0) or 0) * 100
        earn_g = (info.get("earningsGrowth", 0) or 0) * 100
        margin = (info.get("profitMargins", 0) or 0) * 100
        fcf = info.get("freeCashflow", 0) or 0

        result["forward_pe"] = round(fpe, 1)
        result["trailing_pe"] = round(tpe, 1)
        result["price_to_book"] = round(ptb, 2)
        result["revenue_growth"] = round(rev_g, 1)
        result["earnings_growth"] = round(earn_g, 1)
        result["profit_margin"] = round(margin, 1)
        result["free_cashflow"] = fcf

        # Growth + value = best combo
        if rev_g > 30 and fpe > 0 and fpe < 30:
            result["signal"] += 10
            result["details"].append(f"GROWTH_VALUE: {rev_g:.0f}% rev growth at {fpe:.0f}x fwd PE")
        elif rev_g > 50:
            result["signal"] += 5
            result["details"].append(f"HIGH_GROWTH: {rev_g:.0f}% rev growth")

        if earn_g > 50:
            result["signal"] += 5
            result["details"].append(f"EARNINGS_ACCEL: {earn_g:.0f}% earnings growth")

    except Exception as e:
        _log.debug(f"{symbol} valuation error: {e}")

    return result


def get_full_profile(symbol: str) -> dict:
    """Get complete market data profile for a symbol."""
    short = get_short_interest(symbol)
    targets = get_analyst_targets(symbol)
    valuation = get_valuation_context(symbol)

    total_signal = short["signal"] + targets["signal"] + valuation["signal"]
    all_details = short["details"] + targets["details"] + valuation["details"]

    return {
        "symbol": symbol,
        "total_signal": total_signal,
        "details": all_details,
        "short_interest": short,
        "analyst_targets": targets,
        "valuation": valuation,
    }
