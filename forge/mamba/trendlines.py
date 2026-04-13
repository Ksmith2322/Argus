"""
Mamba Trendline Detection Engine
=================================
Identifies trendlines on 5-min NAS100 data, counts touches, detects breakouts.

Core algorithm:
1. Find swing highs/lows (pivot points)
2. Fit trendlines through recent pivots
3. Count how many times price touches/respects the line
4. Detect when price breaks through with conviction (close + volume)
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def find_pivot_highs(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> list[dict]:
    """Find swing highs where High[i] is the highest in [i-left, i+right]."""
    highs = df["High"].values
    pivots = []
    for i in range(left_bars, len(highs) - right_bars):
        window_left = highs[i - left_bars : i]
        window_right = highs[i + 1 : i + right_bars + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if highs[i] >= window_left.max() and highs[i] >= window_right.max():
            pivots.append({
                "index": i,
                "price": float(highs[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df.iloc[i].get("Date", i),
            })
    return pivots


def find_pivot_lows(df: pd.DataFrame, left_bars: int = 5, right_bars: int = 2) -> list[dict]:
    """Find swing lows where Low[i] is the lowest in [i-left, i+right]."""
    lows = df["Low"].values
    pivots = []
    for i in range(left_bars, len(lows) - right_bars):
        window_left = lows[i - left_bars : i]
        window_right = lows[i + 1 : i + right_bars + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if lows[i] <= window_left.min() and lows[i] <= window_right.min():
            pivots.append({
                "index": i,
                "price": float(lows[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df.iloc[i].get("Date", i),
            })
    return pivots


# ---------------------------------------------------------------------------
# Trendline fitting
# ---------------------------------------------------------------------------

def fit_trendline(pivots: list[dict], max_pivots: int = 2) -> Optional[dict]:
    """
    Fit a line through the 2 most recent pivots.
    Returns: {slope, intercept, start_bar, end_bar, direction, pivots}
    """
    if len(pivots) < 2:
        return None

    # Use the most recent pivots
    recent = pivots[-max_pivots:]
    p1, p2 = recent[0], recent[1]

    dx = p2["index"] - p1["index"]
    if dx == 0:
        return None

    slope = (p2["price"] - p1["price"]) / dx
    intercept = p1["price"] - slope * p1["index"]

    direction = "descending" if slope < 0 else "ascending"

    return {
        "slope": slope,
        "intercept": intercept,
        "start_bar": p1["index"],
        "end_bar": p2["index"],
        "direction": direction,
        "pivots": recent,
    }


def trendline_value_at(tl: dict, bar_index: int) -> float:
    """Get the trendline's price value at a given bar index."""
    return tl["slope"] * bar_index + tl["intercept"]


# ---------------------------------------------------------------------------
# Touch counting
# ---------------------------------------------------------------------------

def count_touches(df: pd.DataFrame, trendline: dict, tolerance_atr_mult: float = 0.2) -> int:
    """
    Count how many bars touch/respect the trendline within tolerance.
    A touch = relevant price extreme comes within tolerance * ATR of the trendline.
    For descending trendlines: check High
    For ascending trendlines: check Low
    """
    atr = df["ATR"].values
    start = trendline["start_bar"]
    end = min(trendline["end_bar"], len(df) - 1)

    touches = 0
    # Don't count the anchor pivots themselves — only bars in between and beyond
    pivot_indices = {p["index"] for p in trendline["pivots"]}

    for i in range(start, end + 1):
        if i in pivot_indices:
            touches += 1  # Anchor pivots always count
            continue

        tl_val = trendline_value_at(trendline, i)
        tol = tolerance_atr_mult * atr[i] if i < len(atr) else 0

        if trendline["direction"] == "descending":
            # For descending (resistance): High should approach the line from below
            price = df["High"].iloc[i]
            if abs(price - tl_val) <= tol and price <= tl_val + tol:
                touches += 1
        else:
            # For ascending (support): Low should approach the line from above
            price = df["Low"].iloc[i]
            if abs(price - tl_val) <= tol and price >= tl_val - tol:
                touches += 1

    return touches


# ---------------------------------------------------------------------------
# Breakout detection
# ---------------------------------------------------------------------------

def detect_breakout(
    df: pd.DataFrame,
    trendline: dict,
    bar_index: int,
    atr: float,
) -> Optional[dict]:
    """
    Check if the bar at bar_index breaks the trendline.
    Breakout requires: close beyond line + volume confirmation.
    """
    if bar_index < 0 or bar_index >= len(df):
        return None

    close = df["Close"].iloc[bar_index]
    tl_val = trendline_value_at(trendline, bar_index)

    # Volume confirmation: volume > 1.5x 20-bar average
    vol_window = 20
    if bar_index >= vol_window:
        avg_vol = df["Volume"].iloc[bar_index - vol_window : bar_index].mean()
    else:
        avg_vol = df["Volume"].iloc[: bar_index].mean() if bar_index > 0 else 1
    current_vol = df["Volume"].iloc[bar_index]
    volume_ratio = current_vol / avg_vol if avg_vol > 0 else 0

    breakout_threshold = atr * 0.1

    if trendline["direction"] == "descending":
        # Bullish breakout: close above descending trendline
        if close > tl_val + breakout_threshold and volume_ratio >= 1.5:
            return {
                "direction": "LONG",
                "break_price": float(close),
                "trendline_value": float(tl_val),
                "volume_ratio": round(volume_ratio, 2),
                "bar_index": bar_index,
            }
    else:
        # Bearish breakout: close below ascending trendline
        if close < tl_val - breakout_threshold and volume_ratio >= 1.5:
            return {
                "direction": "SHORT",
                "break_price": float(close),
                "trendline_value": float(tl_val),
                "volume_ratio": round(volume_ratio, 2),
                "bar_index": bar_index,
            }

    return None


# ---------------------------------------------------------------------------
# Full scan
# ---------------------------------------------------------------------------

MIN_TRENDLINE_BARS = 6  # 30 minutes on 5-min chart

def scan_for_setups(df: pd.DataFrame, min_touches: int = 3) -> list[dict]:
    """
    Full scan: find all active trendlines, count touches, check for breakouts.
    Returns list of setups sorted by conviction (highest first).
    """
    if "ATR" not in df.columns:
        raise ValueError("DataFrame must have ATR column. Compute ATR(14) first.")

    setups = []
    pivot_highs = find_pivot_highs(df)
    pivot_lows = find_pivot_lows(df)

    last_bar = len(df) - 1
    current_atr = df["ATR"].iloc[last_bar]

    # --- Descending trendlines (through swing highs) ---
    # Try multiple combinations of recent pivot highs
    for end_idx in range(len(pivot_highs) - 1, 0, -1):
        for start_idx in range(end_idx - 1, max(end_idx - 5, -1), -1):
            pair = [pivot_highs[start_idx], pivot_highs[end_idx]]
            tl = fit_trendline(pair)
            if tl is None:
                continue

            # Minimum duration check
            if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                continue

            # Extend end_bar to current bar for touch counting
            extended_tl = {**tl, "end_bar": last_bar}
            touches = count_touches(df, extended_tl)

            if touches < min_touches:
                continue

            # Check for breakout on the most recent bar
            breakout = detect_breakout(df, extended_tl, last_bar, current_atr)

            conviction = _score_setup(touches, breakout, tl)

            setups.append({
                "trendline": extended_tl,
                "touches": touches,
                "breakout": breakout,
                "conviction": conviction,
                "tl_value_now": trendline_value_at(extended_tl, last_bar),
                "current_close": float(df["Close"].iloc[last_bar]),
                "current_atr": float(current_atr),
            })

    # --- Ascending trendlines (through swing lows) ---
    for end_idx in range(len(pivot_lows) - 1, 0, -1):
        for start_idx in range(end_idx - 1, max(end_idx - 5, -1), -1):
            pair = [pivot_lows[start_idx], pivot_lows[end_idx]]
            tl = fit_trendline(pair)
            if tl is None:
                continue

            if tl["end_bar"] - tl["start_bar"] < MIN_TRENDLINE_BARS:
                continue

            extended_tl = {**tl, "end_bar": last_bar}
            touches = count_touches(df, extended_tl)

            if touches < min_touches:
                continue

            breakout = detect_breakout(df, extended_tl, last_bar, current_atr)
            conviction = _score_setup(touches, breakout, tl)

            setups.append({
                "trendline": extended_tl,
                "touches": touches,
                "breakout": breakout,
                "conviction": conviction,
                "tl_value_now": trendline_value_at(extended_tl, last_bar),
                "current_close": float(df["Close"].iloc[last_bar]),
                "current_atr": float(current_atr),
            })

    # Sort by conviction descending
    setups.sort(key=lambda s: s["conviction"], reverse=True)

    # Deduplicate: if two trendlines are very similar, keep the higher conviction one
    setups = _deduplicate(setups, df)

    return setups


def _score_setup(touches: int, breakout: Optional[dict], tl: dict) -> float:
    """Score a setup from 0-100 based on quality factors."""
    score = 0.0

    # Touch count: more touches = more validated
    score += min(touches * 12, 60)

    # Trendline duration: longer = more significant
    duration = tl["end_bar"] - tl["start_bar"]
    score += min(duration * 0.5, 20)

    # Active breakout bonus
    if breakout:
        score += 15
        # Extra credit for high volume ratio
        if breakout["volume_ratio"] > 2.0:
            score += 5

    return min(score, 100)


def _deduplicate(setups: list[dict], df: pd.DataFrame) -> list[dict]:
    """Remove near-duplicate trendlines (similar slope and intercept)."""
    if len(setups) <= 1:
        return setups

    kept = [setups[0]]
    for s in setups[1:]:
        is_dup = False
        for k in kept:
            slope_diff = abs(s["trendline"]["slope"] - k["trendline"]["slope"])
            # Compare trendline values at the midpoint
            mid = len(df) // 2
            val_diff = abs(
                trendline_value_at(s["trendline"], mid)
                - trendline_value_at(k["trendline"], mid)
            )
            atr_mid = df["ATR"].iloc[mid] if mid < len(df) else 1
            if slope_diff < 0.05 and val_diff < atr_mid * 0.5:
                is_dup = True
                break
        if not is_dup:
            kept.append(s)
    return kept
