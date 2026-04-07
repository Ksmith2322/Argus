"""Breakout and fakeout detection relative to support/resistance levels."""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Level Proximity
# ---------------------------------------------------------------------------

def price_at_level(
    price: float,
    level: float,
    atr: float,
    tolerance_atr: float = 0.5,
) -> bool:
    """Return True if *price* is within *tolerance_atr* * *atr* of *level*."""
    if atr <= 0:
        return False
    return abs(price - level) <= tolerance_atr * atr


# ---------------------------------------------------------------------------
# Fakeout Risk
# ---------------------------------------------------------------------------

def fakeout_risk_score(
    recent_breaks: list[dict],
    lookback_bars: int = 50,
) -> float:
    """Score 0-1 based on how many recent fakeouts occurred.

    Parameters
    ----------
    recent_breaks : list[dict]
        Each dict should have at least ``{"bar_index": int, "was_fakeout": bool}``.
    lookback_bars : int
        Only consider breaks within this many bars of the most recent bar.
    """
    if not recent_breaks:
        return 0.0

    max_bar = max(b["bar_index"] for b in recent_breaks)
    cutoff = max_bar - lookback_bars

    relevant = [b for b in recent_breaks if b["bar_index"] >= cutoff]
    if not relevant:
        return 0.0

    fakeouts = sum(1 for b in relevant if b.get("was_fakeout", False))
    total = len(relevant)

    # Scale: 0 fakeouts -> 0, 3+ fakeouts in window -> ~1.0
    return float(np.clip(fakeouts / max(total, 1) + fakeouts * 0.15, 0.0, 1.0))


# ---------------------------------------------------------------------------
# Breakout Detection
# ---------------------------------------------------------------------------

def check_breakout(
    current_price: float,
    prev_bars: pd.DataFrame,
    sr_levels: list[dict],
    volume_series: pd.Series | None = None,
) -> dict:
    """Check if price has broken through any S/R level.

    Parameters
    ----------
    current_price : float
        Latest price.
    prev_bars : pd.DataFrame
        Last ~10 bars of OHLC data (needs at least Close column).
    sr_levels : list[dict]
        Each dict: ``{"price": float, "kind": "support"|"resistance", ...}``.
    volume_series : pd.Series | None
        If provided, used for volume confirmation (last 20+ bars preferred).

    Returns
    -------
    dict with keys: is_breakout, is_fakeout, direction, level,
    volume_confirmed, bars_since_break, fakeout_risk.
    """
    result = {
        "is_breakout": False,
        "is_fakeout": False,
        "direction": "none",
        "level": 0.0,
        "volume_confirmed": False,
        "bars_since_break": 0,
        "fakeout_risk": 0.0,
    }

    if prev_bars.empty or not sr_levels:
        return result

    closes = prev_bars["Close"].values
    n = len(closes)
    check_window = min(3, n)  # look at last 3 bars

    # Volume confirmation helper
    vol_confirmed = False
    if volume_series is not None and len(volume_series) >= 20:
        avg_vol = volume_series.iloc[-20:].mean()
        recent_vol = volume_series.iloc[-1]
        vol_confirmed = recent_vol > 1.5 * avg_vol

    best_break: dict | None = None
    best_bars_since = 0

    for lvl in sr_levels:
        lp = lvl["price"]
        kind = lvl.get("kind", "")

        # Determine expected break direction
        if kind == "resistance":
            # Bullish breakout: close above resistance
            consec_above = 0
            first_break_idx = -1
            for i in range(n - check_window, n):
                if closes[i] > lp:
                    consec_above += 1
                    if first_break_idx < 0:
                        first_break_idx = i
                else:
                    consec_above = 0
                    first_break_idx = -1

            if consec_above >= 2:
                bars_since = n - 1 - first_break_idx
                candidate = {
                    "direction": "long",
                    "level": lp,
                    "bars_since_break": bars_since,
                }
                if best_break is None or bars_since < best_bars_since:
                    best_break = candidate
                    best_bars_since = bars_since

            # Fakeout check: broke above then came back
            elif consec_above == 0 and first_break_idx >= 0:
                # Broke then returned
                result["is_fakeout"] = True
                result["direction"] = "long"
                result["level"] = lp
                result["fakeout_risk"] = 0.7

        elif kind == "support":
            # Bearish breakout: close below support
            consec_below = 0
            first_break_idx = -1
            for i in range(n - check_window, n):
                if closes[i] < lp:
                    consec_below += 1
                    if first_break_idx < 0:
                        first_break_idx = i
                else:
                    consec_below = 0
                    first_break_idx = -1

            if consec_below >= 2:
                bars_since = n - 1 - first_break_idx
                candidate = {
                    "direction": "short",
                    "level": lp,
                    "bars_since_break": bars_since,
                }
                if best_break is None or bars_since < best_bars_since:
                    best_break = candidate
                    best_bars_since = bars_since

            elif consec_below == 0 and first_break_idx >= 0:
                result["is_fakeout"] = True
                result["direction"] = "short"
                result["level"] = lp
                result["fakeout_risk"] = 0.7

    if best_break is not None:
        result["is_breakout"] = True
        result["is_fakeout"] = False
        result["direction"] = best_break["direction"]
        result["level"] = best_break["level"]
        result["bars_since_break"] = best_break["bars_since_break"]
        result["volume_confirmed"] = vol_confirmed
        # Lower fakeout risk for volume-confirmed breaks
        result["fakeout_risk"] = 0.15 if vol_confirmed else 0.35

    return result
