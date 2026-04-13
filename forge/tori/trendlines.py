"""
Tori Trades — Multi-Timeframe Trendline Engine
================================================
Higher-timeframe trendline detection for swing trading on 4H bars.
Stricter A+ criteria than Mamba: 3+ touches, 6+ candle spacing,
<45-degree slope, 3+ weeks duration, no price intersection.

Three setups: bounce, break, break & retest.
Action Line (entry) + Safety Line (stop/exit).
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pivot detection
# ---------------------------------------------------------------------------

def find_swing_highs(df: pd.DataFrame, left: int = 5, right: int = 5) -> list[dict]:
    """Pivot highs on any timeframe."""
    highs = df["High"].values
    pivots = []
    for i in range(left, len(highs) - right):
        window_left = highs[i - left : i]
        window_right = highs[i + 1 : i + right + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if highs[i] >= window_left.max() and highs[i] >= window_right.max():
            pivots.append({
                "index": i,
                "price": float(highs[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else i,
            })
    return pivots


def find_swing_lows(df: pd.DataFrame, left: int = 5, right: int = 5) -> list[dict]:
    """Pivot lows on any timeframe."""
    lows = df["Low"].values
    pivots = []
    for i in range(left, len(lows) - right):
        window_left = lows[i - left : i]
        window_right = lows[i + 1 : i + right + 1]
        if len(window_left) == 0 or len(window_right) == 0:
            continue
        if lows[i] <= window_left.min() and lows[i] <= window_right.min():
            pivots.append({
                "index": i,
                "price": float(lows[i]),
                "date": df.index[i] if isinstance(df.index, pd.DatetimeIndex) else i,
            })
    return pivots


# ---------------------------------------------------------------------------
# Trendline fitting
# ---------------------------------------------------------------------------

def _trendline_value_at(tl: dict, bar_index: int) -> float:
    """Get the trendline price at a given bar index."""
    return tl["slope"] * bar_index + tl["intercept"]


def fit_ascending_trendline(lows: list[dict]) -> Optional[dict]:
    """
    Connect swing lows to form an ascending (bullish support) trendline.
    Uses the two most separated lows that produce an upward slope
    and respects all intermediate lows (none below the line).
    Returns {slope, intercept, touches, start_idx, end_idx, direction, pivots}
    """
    if len(lows) < 2:
        return None

    best = None
    best_touches = 0

    # Try all pairs, prefer those with most touches
    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            p1, p2 = lows[i], lows[j]
            dx = p2["index"] - p1["index"]
            if dx == 0:
                continue
            slope = (p2["price"] - p1["price"]) / dx
            if slope < 0:
                continue  # must be ascending
            intercept = p1["price"] - slope * p1["index"]

            # Count touches: how many lows are within 0.3% of the line
            touches = 0
            valid = True
            for low in lows:
                tl_val = slope * low["index"] + intercept
                dist_pct = (low["price"] - tl_val) / tl_val if tl_val != 0 else 0
                if dist_pct < -0.003:  # price significantly below the line = intersection
                    valid = False
                    break
                if abs(dist_pct) <= 0.003:
                    touches += 1

            if valid and touches >= 2 and touches > best_touches:
                best_touches = touches
                best = {
                    "slope": slope,
                    "intercept": intercept,
                    "touches": touches,
                    "start_idx": p1["index"],
                    "end_idx": p2["index"],
                    "direction": "ascending",
                    "pivots": [low for low in lows
                               if abs(low["price"] - (slope * low["index"] + intercept))
                               / max(abs(slope * low["index"] + intercept), 1) <= 0.003],
                }

    return best


def fit_descending_trendline(highs: list[dict]) -> Optional[dict]:
    """
    Connect swing highs to form a descending (bearish resistance) trendline.
    Returns {slope, intercept, touches, start_idx, end_idx, direction, pivots}
    """
    if len(highs) < 2:
        return None

    best = None
    best_touches = 0

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            p1, p2 = highs[i], highs[j]
            dx = p2["index"] - p1["index"]
            if dx == 0:
                continue
            slope = (p2["price"] - p1["price"]) / dx
            if slope > 0:
                continue  # must be descending
            intercept = p1["price"] - slope * p1["index"]

            touches = 0
            valid = True
            for high in highs:
                tl_val = slope * high["index"] + intercept
                dist_pct = (high["price"] - tl_val) / tl_val if tl_val != 0 else 0
                if dist_pct > 0.003:
                    valid = False
                    break
                if abs(dist_pct) <= 0.003:
                    touches += 1

            if valid and touches >= 2 and touches > best_touches:
                best_touches = touches
                best = {
                    "slope": slope,
                    "intercept": intercept,
                    "touches": touches,
                    "start_idx": p1["index"],
                    "end_idx": p2["index"],
                    "direction": "descending",
                    "pivots": [high for high in highs
                               if abs(high["price"] - (slope * high["index"] + intercept))
                               / max(abs(slope * high["index"] + intercept), 1) <= 0.003],
                }

    return best


# ---------------------------------------------------------------------------
# Quality scoring — A+ criteria
# ---------------------------------------------------------------------------

def score_trendline_quality(trendline: dict, df: pd.DataFrame) -> dict:
    """
    Score against A+ criteria from the Tori Trades rulebook:
    - touches: 3+ = A+, 2 = acceptable
    - spacing: 6+ candles between taps = A+
    - slope: <45 degrees on 3-month view = A+
    - duration: 3+ weeks of data = A+ (on 4H: 6 bars/day * 5 days * 3 weeks = 90 bars)
    - integrity: no price intersection = valid
    Returns: {grade, touches, avg_spacing, slope_degrees, duration_bars, valid}
    """
    touches = trendline["touches"]
    pivots = trendline.get("pivots", [])

    # --- Spacing ---
    spacings = []
    sorted_pivots = sorted(pivots, key=lambda p: p["index"])
    for i in range(1, len(sorted_pivots)):
        spacings.append(sorted_pivots[i]["index"] - sorted_pivots[i - 1]["index"])
    avg_spacing = float(np.mean(spacings)) if spacings else 0.0

    # --- Slope in degrees ---
    # Normalize: slope is points-per-bar. Convert to degrees using
    # price-range / bar-range normalization for the visible chart.
    duration_bars = trendline["end_idx"] - trendline["start_idx"]
    start = max(0, trendline["start_idx"])
    end = min(len(df), trendline["end_idx"] + 1)
    if end > start and end <= len(df):
        price_range = df["High"].iloc[start:end].max() - df["Low"].iloc[start:end].min()
    else:
        price_range = 1.0
    bar_range = max(duration_bars, 1)

    # Normalized slope: how steep relative to the chart view
    if price_range > 0:
        norm_slope = (abs(trendline["slope"]) * bar_range) / price_range
    else:
        norm_slope = 0
    slope_degrees = math.degrees(math.atan(norm_slope))

    # --- Integrity: check for price intersections ---
    valid = True
    slope = trendline["slope"]
    intercept = trendline["intercept"]
    direction = trendline["direction"]

    check_start = max(trendline["start_idx"], 0)
    check_end = min(trendline["end_idx"] + 1, len(df))
    for i in range(check_start, check_end):
        tl_val = slope * i + intercept
        if direction == "ascending":
            # Price should not close significantly below the ascending line
            if df["Close"].iloc[i] < tl_val - 0.003 * abs(tl_val):
                # Allow brief wicks but not closes
                if df["Low"].iloc[i] < tl_val - 0.005 * abs(tl_val):
                    valid = False
                    break
        else:
            if df["Close"].iloc[i] > tl_val + 0.003 * abs(tl_val):
                if df["High"].iloc[i] > tl_val + 0.005 * abs(tl_val):
                    valid = False
                    break

    # --- Grade ---
    score = 0
    if touches >= 3:
        score += 3
    elif touches == 2:
        score += 1

    if avg_spacing >= 6:
        score += 2
    elif avg_spacing >= 3:
        score += 1

    if slope_degrees < 45:
        score += 2
    elif slope_degrees < 60:
        score += 1

    # 3+ weeks on 4H ~ 90 bars (6 bars/day * 5 days * 3 weeks)
    if duration_bars >= 90:
        score += 2
    elif duration_bars >= 30:
        score += 1

    if not valid:
        grade = "C"
    elif score >= 8:
        grade = "A+"
    elif score >= 6:
        grade = "A"
    elif score >= 4:
        grade = "B"
    else:
        grade = "C"

    return {
        "grade": grade,
        "touches": touches,
        "avg_spacing": round(avg_spacing, 1),
        "slope_degrees": round(slope_degrees, 1),
        "duration_bars": duration_bars,
        "valid": valid,
    }


# ---------------------------------------------------------------------------
# Setup detection
# ---------------------------------------------------------------------------

def _is_rejection_candle(bar: pd.Series, direction: str) -> bool:
    """
    Check if bar shows rejection (long wick toward the trendline).
    For ascending (bounce up): long lower wick, close in upper half.
    For descending (bounce down): long upper wick, close in lower half.
    """
    total_range = bar["High"] - bar["Low"]
    if total_range <= 0:
        return False
    body_top = max(bar["Open"], bar["Close"])
    body_bot = min(bar["Open"], bar["Close"])

    if direction == "ascending":
        lower_wick = body_bot - bar["Low"]
        return (lower_wick / total_range) >= 0.35
    else:
        upper_wick = bar["High"] - body_top
        return (upper_wick / total_range) >= 0.35


def check_bounce(
    df: pd.DataFrame,
    trendline: dict,
    bar_idx: int,
    atr: float,
) -> Optional[dict]:
    """
    Check if current bar is bouncing off a trendline.
    Bounce = price within 0.5 ATR of trendline AND rejection candle.
    Returns: {setup, direction, entry_price, action_line, safety_line}
    """
    if bar_idx < 1 or bar_idx >= len(df):
        return None

    tl_val = _trendline_value_at(trendline, bar_idx)
    bar = df.iloc[bar_idx]
    direction = trendline["direction"]

    if direction == "ascending":
        # Ascending line acts as support — price bounces UP
        dist = bar["Low"] - tl_val
        if 0 <= dist <= 0.5 * atr or (dist < 0 and abs(dist) <= 0.2 * atr):
            if _is_rejection_candle(bar, "ascending"):
                entry = float(bar["Close"])
                safety = tl_val - 0.3 * atr
                return {
                    "setup": "bounce",
                    "direction": "LONG",
                    "entry_price": entry,
                    "action_line": tl_val,
                    "safety_line": safety,
                    "bar_idx": bar_idx,
                }
    else:
        # Descending line acts as resistance — price bounces DOWN
        dist = tl_val - bar["High"]
        if 0 <= dist <= 0.5 * atr or (dist < 0 and abs(dist) <= 0.2 * atr):
            if _is_rejection_candle(bar, "descending"):
                entry = float(bar["Close"])
                safety = tl_val + 0.3 * atr
                return {
                    "setup": "bounce",
                    "direction": "SHORT",
                    "entry_price": entry,
                    "action_line": tl_val,
                    "safety_line": safety,
                    "bar_idx": bar_idx,
                }
    return None


def check_break(
    df: pd.DataFrame,
    trendline: dict,
    bar_idx: int,
    atr: float,
) -> Optional[dict]:
    """
    Check if 4H candle CLOSED beyond trendline (break).
    Requires: close beyond line by > 0.1 ATR.
    Returns: {setup, direction, entry_price, action_line, safety_line}
    """
    if bar_idx < 1 or bar_idx >= len(df):
        return None

    tl_val = _trendline_value_at(trendline, bar_idx)
    close = df["Close"].iloc[bar_idx]
    direction = trendline["direction"]

    threshold = 0.1 * atr

    if direction == "ascending":
        # Break below ascending support = SHORT
        if close < tl_val - threshold:
            # Safety line: need a descending line above, or recent swing high
            safety = _find_opposing_safety(df, bar_idx, "SHORT", atr)
            if safety is None:
                return None
            return {
                "setup": "break",
                "direction": "SHORT",
                "entry_price": float(close),
                "action_line": tl_val,
                "safety_line": safety,
                "bar_idx": bar_idx,
            }
    else:
        # Break above descending resistance = LONG
        if close > tl_val + threshold:
            safety = _find_opposing_safety(df, bar_idx, "LONG", atr)
            if safety is None:
                return None
            return {
                "setup": "break",
                "direction": "LONG",
                "entry_price": float(close),
                "action_line": tl_val,
                "safety_line": safety,
                "bar_idx": bar_idx,
            }
    return None


def _find_opposing_safety(
    df: pd.DataFrame, bar_idx: int, trade_direction: str, atr: float,
) -> Optional[float]:
    """
    Find the opposing Safety Line (recent swing high for SHORT, swing low for LONG).
    Returns the safety price level or None.
    """
    lookback = min(bar_idx, 60)  # look back up to 60 bars
    window = df.iloc[max(0, bar_idx - lookback) : bar_idx + 1]

    if trade_direction == "SHORT":
        # Safety = recent swing high + buffer
        highs = find_swing_highs(window, left=3, right=2)
        if highs:
            return float(max(h["price"] for h in highs[-3:])) + 0.3 * atr
    else:
        # Safety = recent swing low - buffer
        lows = find_swing_lows(window, left=3, right=2)
        if lows:
            return float(min(l["price"] for l in lows[-3:])) - 0.3 * atr
    return None


def check_retest(
    df: pd.DataFrame,
    broken_trendline: dict,
    bar_idx: int,
    atr: float,
    break_bar: int,
) -> Optional[dict]:
    """
    After a break, check if price returned to retest the broken line.
    Retest = price within 0.3 ATR of broken line + rejection candle.
    Returns: {setup, direction, entry_price, action_line, safety_line}
    """
    if bar_idx <= break_bar or bar_idx >= len(df):
        return None

    # Don't look too far from the break (max 30 bars ~ 5 days on 4H)
    if bar_idx - break_bar > 30:
        return None

    tl_val = _trendline_value_at(broken_trendline, bar_idx)
    bar = df.iloc[bar_idx]
    direction = broken_trendline["direction"]

    if direction == "ascending":
        # Was support, now resistance after break down
        # Retest from below: price comes up to line, rejected down
        dist = tl_val - bar["High"]
        if abs(dist) <= 0.3 * atr:
            if _is_rejection_candle(bar, "descending"):
                entry = float(bar["Close"])
                safety = tl_val + 0.3 * atr
                return {
                    "setup": "break_retest",
                    "direction": "SHORT",
                    "entry_price": entry,
                    "action_line": tl_val,
                    "safety_line": safety,
                    "bar_idx": bar_idx,
                    "break_bar": break_bar,
                }
    else:
        # Was resistance, now support after break up
        # Retest from above: price dips to line, rejected up
        dist = bar["Low"] - tl_val
        if abs(dist) <= 0.3 * atr:
            if _is_rejection_candle(bar, "ascending"):
                entry = float(bar["Close"])
                safety = tl_val - 0.3 * atr
                return {
                    "setup": "break_retest",
                    "direction": "LONG",
                    "entry_price": entry,
                    "action_line": tl_val,
                    "safety_line": safety,
                    "bar_idx": bar_idx,
                    "break_bar": break_bar,
                }
    return None


# ---------------------------------------------------------------------------
# Full scan
# ---------------------------------------------------------------------------

def scan_all_setups(
    df: pd.DataFrame,
    timeframe_label: str = "4H",
) -> list[dict]:
    """
    Full scan: find all A/A+ trendlines, check for bounce/break/retest setups.
    Returns list of setups sorted by quality grade.
    """
    if "ATR" not in df.columns:
        raise ValueError("DataFrame must have ATR column. Compute ATR(14) first.")

    setups = []
    last_bar = len(df) - 1
    atr = float(df["ATR"].iloc[last_bar])

    swing_highs = find_swing_highs(df)
    swing_lows = find_swing_lows(df)

    # Fit trendlines
    trendlines = []

    asc = fit_ascending_trendline(swing_lows)
    if asc:
        trendlines.append(asc)

    desc = fit_descending_trendline(swing_highs)
    if desc:
        trendlines.append(desc)

    # Also try sub-groups for multiple trendlines
    # Split pivots into overlapping windows to find multiple lines
    for chunk_size in [8, 12, 16]:
        for start in range(0, max(len(swing_lows) - chunk_size + 1, 1), chunk_size // 2):
            chunk = swing_lows[start : start + chunk_size]
            tl = fit_ascending_trendline(chunk)
            if tl and tl["touches"] >= 2:
                trendlines.append(tl)

        for start in range(0, max(len(swing_highs) - chunk_size + 1, 1), chunk_size // 2):
            chunk = swing_highs[start : start + chunk_size]
            tl = fit_descending_trendline(chunk)
            if tl and tl["touches"] >= 2:
                trendlines.append(tl)

    # Deduplicate similar trendlines
    trendlines = _deduplicate_trendlines(trendlines, df)

    # Score and filter
    scored_lines = []
    for tl in trendlines:
        quality = score_trendline_quality(tl, df)
        if quality["grade"] in ("A+", "A", "B") and quality["valid"]:
            scored_lines.append((tl, quality))

    # Check setups on A/A+ lines (B for lower priority)
    for tl, quality in scored_lines:
        # Bounce
        bounce = check_bounce(df, tl, last_bar, atr)
        if bounce:
            bounce["trendline"] = tl
            bounce["quality"] = quality
            bounce["timeframe"] = timeframe_label
            setups.append(bounce)

        # Break
        brk = check_break(df, tl, last_bar, atr)
        if brk:
            brk["trendline"] = tl
            brk["quality"] = quality
            brk["timeframe"] = timeframe_label
            setups.append(brk)

    # Sort: A+ first, then A, then B; within same grade, break_retest > bounce > break
    grade_order = {"A+": 0, "A": 1, "B": 2, "C": 3}
    setup_order = {"break_retest": 0, "bounce": 1, "break": 2}

    setups.sort(key=lambda s: (
        grade_order.get(s["quality"]["grade"], 9),
        setup_order.get(s["setup"], 9),
    ))

    return setups


def _deduplicate_trendlines(trendlines: list[dict], df: pd.DataFrame) -> list[dict]:
    """Remove near-duplicate trendlines."""
    if len(trendlines) <= 1:
        return trendlines

    kept = [trendlines[0]]
    mid = len(df) // 2

    for tl in trendlines[1:]:
        is_dup = False
        for k in kept:
            slope_diff = abs(tl["slope"] - k["slope"])
            val_diff = abs(
                _trendline_value_at(tl, mid) - _trendline_value_at(k, mid)
            )
            price_scale = abs(_trendline_value_at(k, mid)) or 1.0
            if slope_diff / max(abs(k["slope"]), 1e-9) < 0.15 and val_diff / price_scale < 0.005:
                # Keep the one with more touches
                if tl["touches"] > k["touches"]:
                    kept.remove(k)
                    kept.append(tl)
                is_dup = True
                break
        if not is_dup:
            kept.append(tl)
    return kept
