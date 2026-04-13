"""
Options Flow Intelligence
=========================
Standalone options analysis module. Fetches options chain data via yfinance,
computes put/call ratios, unusual activity flags, max pain, and gamma bias.

Used by forge.conviction to add options sentiment as a conviction factor.

Usage:
    from forge.options_flow import check_options_flow
    result = check_options_flow("MSFT")

CLI:
    python -m forge.options_flow --test
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache — avoid hammering yfinance on repeated calls
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}  # ticker -> (timestamp, result)
CACHE_TTL = 300  # 5 minutes


def _get_cached(ticker: str) -> Optional[dict]:
    """Return cached result if fresh, else None."""
    if ticker in _cache:
        ts, result = _cache[ticker]
        if time.time() - ts < CACHE_TTL:
            return result
    return None


def _set_cache(ticker: str, result: dict) -> None:
    _cache[ticker] = (time.time(), result)


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def check_options_flow(ticker: str) -> Optional[dict]:
    """
    Fetch and analyze the nearest-expiry options chain for a ticker.

    Returns a dict with put/call ratios, unusual activity, bias, max pain,
    and data quality. Returns None if the ticker has no options.
    """
    cached = _get_cached(ticker)
    if cached is not None:
        return cached

    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed — options flow unavailable")
        return None

    try:
        tk = yf.Ticker(ticker)

        # Get available expiration dates
        expirations = tk.options
        if not expirations:
            log.debug("%s has no options chain", ticker)
            return None

        nearest_expiry = expirations[0]

        # Fetch the chain
        chain = tk.option_chain(nearest_expiry)
        calls = chain.calls
        puts = chain.puts

        if calls.empty and puts.empty:
            result = _empty_result(ticker, nearest_expiry, "unavailable")
            _set_cache(ticker, result)
            return result

        # Current price
        info = tk.info
        current_price = info.get("currentPrice") or info.get("regularMarketPrice")
        if current_price is None:
            hist = tk.history(period="1d")
            if not hist.empty:
                current_price = float(hist["Close"].iloc[-1])

        # Volume totals
        call_volume = int(calls["volume"].sum()) if "volume" in calls.columns else 0
        put_volume = int(puts["volume"].sum()) if "volume" in puts.columns else 0
        total_volume = call_volume + put_volume

        # Open interest totals
        call_oi = int(calls["openInterest"].sum()) if "openInterest" in calls.columns else 0
        put_oi = int(puts["openInterest"].sum()) if "openInterest" in puts.columns else 0

        # Put/call ratios
        pc_volume_ratio = put_volume / call_volume if call_volume > 0 else 999.0
        pc_oi_ratio = put_oi / call_oi if call_oi > 0 else 999.0

        # Bias from volume ratio
        if pc_volume_ratio < 0.7:
            bias = "BULLISH"
        elif pc_volume_ratio > 1.3:
            bias = "BEARISH"
        else:
            bias = "NEUTRAL"

        # Max pain — strike where total dollar pain for option holders is maximized
        max_pain_strike = _compute_max_pain(calls, puts)

        # Unusual activity — compare total volume to open interest as proxy
        # (True if volume is > 2x the total OI, suggesting aggressive new positioning)
        total_oi = call_oi + put_oi
        unusual = total_volume > (2 * total_oi) if total_oi > 0 else False

        # Data quality
        data_quality = "good" if total_volume > 100 else "stale"

        result = {
            "ticker": ticker,
            "pc_volume_ratio": round(pc_volume_ratio, 3),
            "pc_oi_ratio": round(pc_oi_ratio, 3),
            "call_volume": call_volume,
            "put_volume": put_volume,
            "total_volume": total_volume,
            "call_oi": call_oi,
            "put_oi": put_oi,
            "unusual_activity": unusual,
            "bias": bias,
            "max_pain": max_pain_strike,
            "current_price": current_price,
            "nearest_expiry": nearest_expiry,
            "data_quality": data_quality,
        }

        _set_cache(ticker, result)
        return result

    except Exception as e:
        log.warning("Options flow check failed for %s: %s", ticker, e)
        return _empty_result(ticker, "", "unavailable")


def _empty_result(ticker: str, expiry: str, quality: str) -> dict:
    return {
        "ticker": ticker,
        "pc_volume_ratio": 0.0,
        "pc_oi_ratio": 0.0,
        "call_volume": 0,
        "put_volume": 0,
        "total_volume": 0,
        "call_oi": 0,
        "put_oi": 0,
        "unusual_activity": False,
        "bias": "NEUTRAL",
        "max_pain": None,
        "current_price": None,
        "nearest_expiry": expiry,
        "data_quality": quality,
    }


# ---------------------------------------------------------------------------
# Max Pain
# ---------------------------------------------------------------------------

def _compute_max_pain(calls, puts) -> Optional[float]:
    """
    Compute the max pain strike — the price at which total option holder losses
    are maximized (i.e., market makers' ideal expiration price).

    For each candidate strike K:
      pain = sum of call OI * max(0, K - call_strike) * 100
            + sum of put OI * max(0, put_strike - K) * 100

    The strike K that MINIMIZES total intrinsic value for holders (= maximizes pain)
    is actually the K that minimizes total payout. We want the strike with minimum
    total dollar value to option holders.
    """
    try:
        all_strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
        if not all_strikes:
            return None

        call_data = list(zip(calls["strike"], calls["openInterest"].fillna(0)))
        put_data = list(zip(puts["strike"], puts["openInterest"].fillna(0)))

        min_pain = float("inf")
        max_pain_strike = all_strikes[0]

        for k in all_strikes:
            # Total intrinsic value to option holders at expiration price K
            call_pain = sum(oi * max(0, k - strike) for strike, oi in call_data)
            put_pain = sum(oi * max(0, strike - k) for strike, oi in put_data)
            total = call_pain + put_pain

            if total < min_pain:
                min_pain = total
                max_pain_strike = k

        return float(max_pain_strike)
    except Exception as e:
        log.debug("Max pain calculation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Max Pain Distance
# ---------------------------------------------------------------------------

def max_pain_distance(current_price: Optional[float], max_pain: Optional[float]) -> Optional[float]:
    """
    How far is the current price from max pain, as a percentage.

    Positive = price is ABOVE max pain (calls in the money).
    Negative = price is BELOW max pain (puts in the money).

    Near OpEx, prices tend to gravitate toward max pain ("pinning").
    """
    if current_price is None or max_pain is None or max_pain == 0:
        return None
    return round((current_price - max_pain) / max_pain, 4)


# ---------------------------------------------------------------------------
# Gamma Bias (simplified)
# ---------------------------------------------------------------------------

def gamma_bias(calls, puts, current_price: Optional[float]) -> Optional[str]:
    """
    Simplified gamma exposure direction.

    If more call open interest is concentrated near current price than put OI,
    dealers are short calls = positive gamma environment = upward pressure.
    If more put OI is near current price, dealers are short puts = negative gamma.

    Returns: "POSITIVE" (bullish amplifier), "NEGATIVE" (bearish amplifier), or "NEUTRAL".
    """
    if current_price is None:
        return None

    try:
        # "Near" = within 5% of current price
        margin = current_price * 0.05

        near_calls = calls[
            (calls["strike"] >= current_price - margin)
            & (calls["strike"] <= current_price + margin)
        ]
        near_puts = puts[
            (puts["strike"] >= current_price - margin)
            & (puts["strike"] <= current_price + margin)
        ]

        near_call_oi = int(near_calls["openInterest"].sum()) if not near_calls.empty else 0
        near_put_oi = int(near_puts["openInterest"].sum()) if not near_puts.empty else 0

        total_near = near_call_oi + near_put_oi
        if total_near == 0:
            return "NEUTRAL"

        call_ratio = near_call_oi / total_near

        if call_ratio > 0.6:
            return "POSITIVE"
        elif call_ratio < 0.4:
            return "NEGATIVE"
        else:
            return "NEUTRAL"
    except Exception as e:
        log.debug("Gamma bias calculation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_result(r: Optional[dict]) -> None:
    if r is None:
        print("  No options data available")
        return
    print(f"  Ticker:          {r['ticker']}")
    print(f"  Nearest expiry:  {r['nearest_expiry']}")
    print(f"  Data quality:    {r['data_quality']}")
    print(f"  P/C vol ratio:   {r['pc_volume_ratio']:.3f}")
    print(f"  P/C OI ratio:    {r['pc_oi_ratio']:.3f}")
    print(f"  Call volume:     {r['call_volume']:,}")
    print(f"  Put volume:      {r['put_volume']:,}")
    print(f"  Total volume:    {r['total_volume']:,}")
    print(f"  Unusual:         {r['unusual_activity']}")
    print(f"  Bias:            {r['bias']}")
    print(f"  Max pain:        {r['max_pain']}")
    print(f"  Current price:   {r['current_price']}")
    mp_dist = max_pain_distance(r.get("current_price"), r.get("max_pain"))
    if mp_dist is not None:
        print(f"  Max pain dist:   {mp_dist:+.2%}")


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Options flow intelligence")
    parser.add_argument("--test", action="store_true", help="Run test on MSFT, NVDA, AAPL, SPY")
    parser.add_argument("--ticker", type=str, help="Check a single ticker")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.test:
        tickers = ["MSFT", "NVDA", "AAPL", "SPY"]
        print("=" * 60)
        print("OPTIONS FLOW INTELLIGENCE — TEST RUN")
        print("=" * 60)
        for t in tickers:
            print(f"\n--- {t} ---")
            result = check_options_flow(t)
            _print_result(result)
        print()
    elif args.ticker:
        result = check_options_flow(args.ticker)
        _print_result(result)
    else:
        parser.print_help()
        sys.exit(1)
