"""
Cue Banks Confluence Detection Engine
=======================================
Core of Cue Banks' strategy — detecting where multiple technical levels overlap.

Per rulebook: the edge comes from OVERLAPPING technical levels. One level is
not enough. Requires 3+ of:
  1. Horizontal S/R (psychological round numbers, role-flip)
  2. Fibonacci retracement (38.2%, 61.8% key levels)
  3. Fibonacci extensions (-27%, -61.8% for targets)
  4. Market structure (HH/HL = bullish, LH/LL = bearish)
  5. Exhaustion patterns (wicks rejecting, higher lows)
  6. Trendline / counter-trend line breaks
  7. Consolidation zone breaks

"Where multiple elements overlap = the trade."
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Fibonacci
# ---------------------------------------------------------------------------

def compute_fib_levels(swing_high: float, swing_low: float) -> dict:
    """
    Compute Fibonacci retracement and extension levels.
    Retracements: 0.236, 0.382, 0.500, 0.618, 0.786
    Extensions: -0.272, -0.618, -1.000
    Returns: {retracements: {level: price}, extensions: {level: price}}
    """
    diff = swing_high - swing_low

    retracements = {
        "0.236": swing_high - diff * 0.236,
        "0.382": swing_high - diff * 0.382,
        "0.500": swing_high - diff * 0.500,
        "0.618": swing_high - diff * 0.618,
        "0.786": swing_high - diff * 0.786,
    }

    extensions = {
        "-0.272": swing_high + diff * 0.272,
        "-0.618": swing_high + diff * 0.618,
        "-1.000": swing_high + diff * 1.000,
    }

    return {"retracements": retracements, "extensions": extensions}


# ---------------------------------------------------------------------------
# Swing points
# ---------------------------------------------------------------------------

def find_swing_points(df: pd.DataFrame, left: int = 10, right: int = 5) -> tuple:
    """
    Find swing highs and lows for Fibonacci drawing.
    A swing high has 'left' lower highs before and 'right' lower highs after.
    A swing low has 'left' higher lows before and 'right' higher lows after.

    Returns: (swing_highs, swing_lows) — each a list of {idx, price, bar_idx}
    """
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    swing_highs = []
    swing_lows = []

    for i in range(left, n - right):
        # Swing high check
        is_sh = True
        for j in range(1, left + 1):
            if highs[i - j] >= highs[i]:
                is_sh = False
                break
        if is_sh:
            for j in range(1, right + 1):
                if highs[i + j] >= highs[i]:
                    is_sh = False
                    break
        if is_sh:
            swing_highs.append({
                "idx": df.index[i],
                "price": float(highs[i]),
                "bar_idx": i,
            })

        # Swing low check
        is_sl = True
        for j in range(1, left + 1):
            if lows[i - j] <= lows[i]:
                is_sl = False
                break
        if is_sl:
            for j in range(1, right + 1):
                if lows[i + j] <= lows[i]:
                    is_sl = False
                    break
        if is_sl:
            swing_lows.append({
                "idx": df.index[i],
                "price": float(lows[i]),
                "bar_idx": i,
            })

    return swing_highs, swing_lows


# ---------------------------------------------------------------------------
# Horizontal S/R
# ---------------------------------------------------------------------------

def find_horizontal_sr(
    df: pd.DataFrame,
    tolerance_pct: float = 0.001,
    min_touches: int = 2,
) -> list:
    """
    Find horizontal S/R levels including:
    - Prior swing highs/lows that were respected
    - Psychological round numbers (nearest 1000, 500, 100)
    - Role-flip levels (was support, now resistance or vice versa)

    Returns: [{level, touches, type, is_round_number, is_role_flip}]
    """
    swing_highs, swing_lows = find_swing_points(df, left=8, right=4)

    # Collect candidate levels
    candidates = []
    for sh in swing_highs:
        candidates.append({"price": sh["price"], "source": "high", "bar_idx": sh["bar_idx"]})
    for sl in swing_lows:
        candidates.append({"price": sl["price"], "source": "low", "bar_idx": sl["bar_idx"]})

    if not candidates:
        return []

    # Cluster nearby levels
    candidates.sort(key=lambda x: x["price"])
    clusters = []
    current_cluster = [candidates[0]]

    for i in range(1, len(candidates)):
        ref_price = current_cluster[0]["price"]
        if abs(candidates[i]["price"] - ref_price) / ref_price < tolerance_pct * 3:
            current_cluster.append(candidates[i])
        else:
            clusters.append(current_cluster)
            current_cluster = [candidates[i]]
    clusters.append(current_cluster)

    # Build S/R levels
    levels = []
    last_price = float(df["Close"].iloc[-1])

    for cluster in clusters:
        avg_price = np.mean([c["price"] for c in cluster])
        touches = len(cluster)
        if touches < min_touches:
            continue

        sources = [c["source"] for c in cluster]
        has_high = "high" in sources
        has_low = "low" in sources

        # Role-flip: was both support and resistance
        is_role_flip = has_high and has_low

        # Type relative to current price
        level_type = "support" if avg_price < last_price else "resistance"

        # Psychological round numbers
        is_round = (avg_price % 1000 < 50 or avg_price % 1000 > 950 or
                    avg_price % 500 < 25 or avg_price % 500 > 475)

        levels.append({
            "level": round(float(avg_price), 2),
            "touches": touches,
            "type": level_type,
            "is_round_number": is_round,
            "is_role_flip": is_role_flip,
        })

    # Add psychological round numbers near current price
    for rnd in [1000, 500, 100]:
        base = round(last_price / rnd) * rnd
        for offset in [-rnd, 0, rnd]:
            lvl = base + offset
            if abs(lvl - last_price) / last_price < 0.03:
                # Check if already covered
                already = any(abs(l["level"] - lvl) / lvl < tolerance_pct for l in levels)
                if not already:
                    levels.append({
                        "level": float(lvl),
                        "touches": 0,
                        "type": "support" if lvl < last_price else "resistance",
                        "is_round_number": True,
                        "is_role_flip": False,
                    })

    levels.sort(key=lambda x: x["level"])
    return levels


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------

def detect_exhaustion(df: pd.DataFrame, bar_idx: int) -> dict | None:
    """
    Detect exhaustion patterns: long wicks rejecting a level.
    Wick > 2x body = exhaustion signal.
    Returns: {direction, wick_ratio, bar_idx} or None
    """
    if bar_idx < 0 or bar_idx >= len(df):
        return None

    row = df.iloc[bar_idx]
    o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])

    body = abs(c - o)
    if body < 0.01:
        body = 0.01  # avoid division by zero

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    # Bullish exhaustion: long lower wick (rejection of lows)
    if lower_wick > 2 * body and lower_wick > upper_wick:
        return {
            "direction": "bullish",
            "wick_ratio": round(lower_wick / body, 2),
            "bar_idx": bar_idx,
        }

    # Bearish exhaustion: long upper wick (rejection of highs)
    if upper_wick > 2 * body and upper_wick > lower_wick:
        return {
            "direction": "bearish",
            "wick_ratio": round(upper_wick / body, 2),
            "bar_idx": bar_idx,
        }

    return None


# ---------------------------------------------------------------------------
# Market structure
# ---------------------------------------------------------------------------

def detect_structure(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    Detect market structure: HH/HL = bullish, LH/LL = bearish.
    Returns: {bias, swings: [{type, price, idx}]}
    """
    if len(df) < lookback:
        return {"bias": "neutral", "swings": []}

    window = df.iloc[-lookback:]
    swing_highs, swing_lows = find_swing_points(window, left=4, right=2)

    swings = []
    for sh in swing_highs:
        swings.append({"type": "high", "price": sh["price"], "idx": sh["idx"]})
    for sl in swing_lows:
        swings.append({"type": "low", "price": sl["price"], "idx": sl["idx"]})

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return {"bias": "neutral", "swings": swings}

    # Check recent swing structure
    recent_highs = swing_highs[-3:]
    recent_lows = swing_lows[-3:]

    hh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] > recent_highs[i - 1]["price"])
    hl_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] > recent_lows[i - 1]["price"])

    lh_count = sum(1 for i in range(1, len(recent_highs))
                   if recent_highs[i]["price"] < recent_highs[i - 1]["price"])
    ll_count = sum(1 for i in range(1, len(recent_lows))
                   if recent_lows[i]["price"] < recent_lows[i - 1]["price"])

    bull_score = hh_count + hl_count
    bear_score = lh_count + ll_count

    if bull_score >= 2 and bull_score > bear_score:
        bias = "bullish"
    elif bear_score >= 2 and bear_score > bull_score:
        bias = "bearish"
    else:
        bias = "neutral"

    return {"bias": bias, "swings": swings}


# ---------------------------------------------------------------------------
# Consolidation break
# ---------------------------------------------------------------------------

def detect_consolidation_break(
    df: pd.DataFrame, bar_idx: int, lookback: int = 30,
) -> dict | None:
    """
    Detect if price just broke out of a consolidation range.
    Range = price staying within 0.5% band for 10+ bars.
    Break = candle closing outside the range.
    Returns: {direction, range_high, range_low, break_price} or None
    """
    if bar_idx < lookback or bar_idx >= len(df):
        return None

    window = df.iloc[bar_idx - lookback:bar_idx]
    current = df.iloc[bar_idx]

    highs = window["High"].values
    lows = window["Low"].values

    # Find longest consolidation range ending near current bar
    best_range = None
    best_count = 0

    for start in range(len(window) - 10):
        range_high = float(highs[start])
        range_low = float(lows[start])
        count = 1

        for j in range(start + 1, len(window)):
            if float(highs[j]) > range_high:
                range_high = float(highs[j])
            if float(lows[j]) < range_low:
                range_low = float(lows[j])

            band_pct = (range_high - range_low) / range_low if range_low > 0 else 0
            if band_pct > 0.005:  # 0.5% band exceeded
                break
            count += 1

        if count >= 10 and count > best_count:
            best_count = count
            best_range = (range_high, range_low)

    if best_range is None:
        return None

    range_high, range_low = best_range
    close = float(current["Close"])

    if close > range_high:
        return {
            "direction": "bullish",
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "break_price": round(close, 2),
        }
    elif close < range_low:
        return {
            "direction": "bearish",
            "range_high": round(range_high, 2),
            "range_low": round(range_low, 2),
            "break_price": round(close, 2),
        }

    return None


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------

def detect_gap(df: pd.DataFrame, min_gap_pct: float = 0.3) -> dict | None:
    """
    Detect opening session gaps (prior close vs current open).
    Returns: {direction, gap_size_pct, gap_open, gap_close, filled} or None
    """
    if len(df) < 2:
        return None

    prior_close = float(df["Close"].iloc[-2])
    current_open = float(df["Open"].iloc[-1])
    current_close = float(df["Close"].iloc[-1])
    current_low = float(df["Low"].iloc[-1])
    current_high = float(df["High"].iloc[-1])

    gap_pct = abs(current_open - prior_close) / prior_close * 100

    if gap_pct < min_gap_pct:
        return None

    if current_open > prior_close:
        direction = "gap_up"
        # Filled if price came back down to prior close
        filled = current_low <= prior_close
    else:
        direction = "gap_down"
        filled = current_high >= prior_close

    return {
        "direction": direction,
        "gap_size_pct": round(gap_pct, 3),
        "gap_open": round(current_open, 2),
        "gap_close": round(prior_close, 2),
        "filled": filled,
    }


def detect_gap_at_session_open(
    df: pd.DataFrame, date: str, min_gap_pct: float = 0.3,
) -> dict | None:
    """
    Detect opening gap for a specific trading day.
    Compares prior day's close to current day's open.
    """
    # Get bars for this date
    day_bars = df[df.index.strftime("%Y-%m-%d") == date]
    if day_bars.empty:
        return None

    current_open = float(day_bars["Open"].iloc[0])

    # Get prior trading day close
    prior_bars = df[df.index.strftime("%Y-%m-%d") < date]
    if prior_bars.empty:
        return None

    prior_close = float(prior_bars["Close"].iloc[-1])

    gap_pct = abs(current_open - prior_close) / prior_close * 100
    if gap_pct < min_gap_pct:
        return None

    # Check if gap was filled during the day
    if current_open > prior_close:
        direction = "gap_up"
        filled = float(day_bars["Low"].min()) <= prior_close
    else:
        direction = "gap_down"
        filled = float(day_bars["High"].max()) >= prior_close

    return {
        "direction": direction,
        "gap_size_pct": round(gap_pct, 3),
        "gap_open": round(current_open, 2),
        "gap_close": round(prior_close, 2),
        "filled": filled,
    }


# ---------------------------------------------------------------------------
# Confluence scorer
# ---------------------------------------------------------------------------

def score_confluence(
    price: float,
    sr_levels: list,
    fib_levels: dict,
    structure: dict,
    exhaustion: dict | None,
    trendline_break: bool,
    consolidation_break: dict | None,
    gap: dict | None,
    tolerance_pct: float = 0.002,
) -> dict:
    """
    Score how many confluence factors align at the current price.

    Returns:
        score, factors, tradeable, sniper, direction, nearest_sr,
        fib_level, entry_price, stop_price, tp1, tp2, tp3
    """
    score = 0
    factors = []
    direction = None
    nearest_sr = None
    nearest_sr_dist = float("inf")
    matched_fib = None

    # 1. Horizontal S/R — within tolerance
    for sr in sr_levels:
        dist = abs(price - sr["level"]) / price
        if dist < tolerance_pct:
            base_score = 1
            if sr.get("is_round_number"):
                base_score += 0.5
            if sr.get("is_role_flip"):
                base_score += 0.5
            score += base_score
            factors.append(f"S/R {sr['type']} @ {sr['level']:.0f}"
                           + (" [round]" if sr.get("is_round_number") else "")
                           + (" [flip]" if sr.get("is_role_flip") else ""))
        if dist < nearest_sr_dist:
            nearest_sr_dist = dist
            nearest_sr = sr["level"]

    # 2. Fibonacci retracement
    if fib_levels and "retracements" in fib_levels:
        for level_name, level_price in fib_levels["retracements"].items():
            if abs(price - level_price) / price < tolerance_pct:
                score += 1
                matched_fib = level_name
                factors.append(f"Fib {level_name} @ {level_price:.0f}")
                break  # only count once

    # 3. Market structure
    bias = structure.get("bias", "neutral")
    if bias in ("bullish", "bearish"):
        score += 1
        factors.append(f"Structure {bias}")
        direction = "LONG" if bias == "bullish" else "SHORT"

    # 4. Exhaustion
    if exhaustion:
        score += 1
        factors.append(f"Exhaustion {exhaustion['direction']} (wick {exhaustion['wick_ratio']}x)")
        if direction is None:
            direction = "LONG" if exhaustion["direction"] == "bullish" else "SHORT"

    # 5. Trendline break
    if trendline_break:
        score += 1
        factors.append("Trendline break")

    # 6. Consolidation break
    if consolidation_break:
        score += 1
        factors.append(f"Consolidation break {consolidation_break['direction']}")
        if direction is None:
            direction = "LONG" if consolidation_break["direction"] == "bullish" else "SHORT"

    # 7. Gap
    if gap:
        score += 1
        factors.append(f"Gap {gap['direction']} ({gap['gap_size_pct']:.2f}%)")

    # Determine direction from S/R type if still None
    if direction is None:
        for sr in sr_levels:
            if abs(price - sr["level"]) / price < tolerance_pct:
                direction = "LONG" if sr["type"] == "support" else "SHORT"
                break

    # Compute entry/stop/targets
    tradeable = int(score) >= 3
    sniper = int(score) >= 4

    entry_price = round(price, 2)
    # Stop: just outside nearest S/R or swing (tight, M5-based)
    atr_proxy = price * 0.001  # ~0.1% as proxy when no ATR available
    if direction == "LONG":
        stop_price = round((nearest_sr if nearest_sr and nearest_sr < price else price) - atr_proxy * 2, 2)
    elif direction == "SHORT":
        stop_price = round((nearest_sr if nearest_sr and nearest_sr > price else price) + atr_proxy * 2, 2)
    else:
        stop_price = round(price - atr_proxy * 2, 2)

    stop_dist = abs(entry_price - stop_price)

    # Targets using Fib extensions / measured move
    if direction == "LONG":
        tp1 = round(entry_price + stop_dist * 3.82, 2)   # Fib 38.2% move
        tp2 = round(entry_price + stop_dist * 6.18, 2)   # Fib 61.8% move
        tp3 = round(entry_price + stop_dist * 7.27, 2)   # -27% extension (1:7+ R:R)
    elif direction == "SHORT":
        tp1 = round(entry_price - stop_dist * 3.82, 2)
        tp2 = round(entry_price - stop_dist * 6.18, 2)
        tp3 = round(entry_price - stop_dist * 7.27, 2)
    else:
        tp1 = tp2 = tp3 = entry_price

    return {
        "score": round(score, 1),
        "factors": factors,
        "tradeable": tradeable,
        "sniper": sniper,
        "direction": direction,
        "nearest_sr": nearest_sr,
        "fib_level": matched_fib,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
    }
