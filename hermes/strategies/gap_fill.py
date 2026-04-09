"""hermes/strategies/gap_fill.py -- Gap fill detection and scoring.

Core logic:
  1. Detect gaps: today's open vs yesterday's close
  2. Filter: gap must be >= min_gap_pct (default 2%)
  3. Score based on: gap size, volume, RSI, distance from EMA, historical fill rate
  4. Entry: at open (or close of gap day for backtest)
  5. Target: yesterday's close (the "fill")
  6. Stop: 2x gap size beyond the gap direction
  7. Timeout: 3 trading days

Gap types:
  - Gap UP:   open > prev_close * (1 + gap_pct) -> SHORT to fill
  - Gap DOWN: open < prev_close * (1 - gap_pct) -> LONG to fill
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class GapSignal:
    """Detected gap with fill probability scoring."""
    symbol: str
    date: str
    gap_type: str            # "GAP_UP" or "GAP_DOWN"
    direction: str           # "short" (gap up fill) or "long" (gap down fill)
    gap_pct: float           # gap size as %
    prev_close: float        # yesterday's close (fill target)
    open_price: float        # today's open (entry)
    entry_price: float       # entry price
    stop_price: float        # stop loss
    target_price: float      # fill target (prev close)
    risk_reward: float
    score: int               # 0-100 fill probability score
    volume_ratio: float
    rsi: float
    reason: str


def detect_gaps(df: pd.DataFrame, min_gap_pct: float = 2.0) -> list[dict]:
    """Scan daily data for gap events.

    Returns list of gap events with context.
    """
    if len(df) < 30:
        return []

    close = df["Close"].values
    open_px = df["Open"].values
    high = df["High"].values
    low = df["Low"].values
    volume = df["Volume"].values if "Volume" in df.columns else np.zeros(len(df))
    dates = df.index

    # Pre-compute indicators
    vol_20 = pd.Series(volume).rolling(20).mean().values
    rsi = _compute_rsi(close, 14)
    ema_20 = pd.Series(close).ewm(span=20).mean().values
    ema_50 = pd.Series(close).ewm(span=50).mean().values

    gaps = []
    for i in range(1, len(close)):
        prev_close = close[i - 1]
        cur_open = open_px[i]

        if prev_close <= 0:
            continue

        gap_pct = (cur_open - prev_close) / prev_close * 100

        if abs(gap_pct) < min_gap_pct:
            continue

        # Gap detected
        gap_type = "GAP_UP" if gap_pct > 0 else "GAP_DOWN"
        direction = "short" if gap_type == "GAP_UP" else "long"

        # Score the fill probability
        score = _score_gap(
            gap_pct=gap_pct,
            gap_type=gap_type,
            volume_ratio=volume[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1,
            rsi=rsi[i] if not np.isnan(rsi[i]) else 50,
            close=close[i],
            ema_20=ema_20[i] if not np.isnan(ema_20[i]) else close[i],
            ema_50=ema_50[i] if not np.isnan(ema_50[i]) else close[i],
        )

        # Stop: 2x gap size beyond the gap
        gap_size = abs(cur_open - prev_close)
        if direction == "short":
            stop = cur_open + gap_size  # 2x gap above open for gap-up short
            target = prev_close
        else:
            stop = cur_open - gap_size  # 2x gap below open for gap-down long
            target = prev_close

        risk = abs(cur_open - stop)
        reward = abs(cur_open - target)
        rr = reward / risk if risk > 0 else 0

        gaps.append({
            "index": i,
            "date": str(dates[i])[:10] if hasattr(dates[i], 'date') else str(dates[i])[:10],
            "gap_type": gap_type,
            "direction": direction,
            "gap_pct": round(gap_pct, 2),
            "prev_close": round(prev_close, 2),
            "open_price": round(cur_open, 2),
            "entry_price": round(cur_open, 2),
            "stop_price": round(stop, 2),
            "target_price": round(target, 2),
            "risk_reward": round(rr, 2),
            "score": score,
            "volume_ratio": round(volume[i] / vol_20[i], 2) if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1.0,
            "rsi": round(rsi[i], 1) if not np.isnan(rsi[i]) else 50.0,
        })

    return gaps


def _score_gap(gap_pct, gap_type, volume_ratio, rsi, close, ema_20, ema_50):
    """Score gap fill probability 0-100."""
    score = 50  # baseline

    gap_abs = abs(gap_pct)

    # Gap size: 2-5% gaps fill most reliably. >8% often don't fill (breakaway)
    if 2 <= gap_abs <= 4:
        score += 20  # sweet spot
    elif 4 < gap_abs <= 6:
        score += 10
    elif gap_abs > 8:
        score -= 15  # breakaway gap — don't fade

    # Volume: low volume gaps fill more (no conviction behind the move)
    if volume_ratio < 0.8:
        score += 15  # low volume gap = likely fills
    elif volume_ratio > 2.0:
        score -= 15  # high volume = conviction, might not fill
    elif volume_ratio > 1.5:
        score -= 5

    # RSI: overextended in gap direction = more likely to fill
    if gap_type == "GAP_UP" and rsi > 70:
        score += 15  # overbought gap up — likely to fill
    elif gap_type == "GAP_DOWN" and rsi < 30:
        score += 15  # oversold gap down — likely to fill
    elif gap_type == "GAP_UP" and rsi < 40:
        score -= 10  # gap up but RSI low — trend continuation
    elif gap_type == "GAP_DOWN" and rsi > 60:
        score -= 10

    # EMA context: gaps against the trend fill more often
    if gap_type == "GAP_UP" and close > ema_50 * 1.05:
        score -= 5  # far above EMA, gap up might be continuation
    elif gap_type == "GAP_DOWN" and close < ema_50 * 0.95:
        score -= 5  # far below EMA, gap down might be continuation
    elif gap_type == "GAP_UP" and close < ema_20:
        score += 10  # gap up but still below EMA20 — mean reversion likely
    elif gap_type == "GAP_DOWN" and close > ema_20:
        score += 10

    return max(0, min(100, score))


def backtest_gaps(
    df: pd.DataFrame,
    symbol: str,
    min_gap_pct: float = 2.0,
    min_score: int = 50,
    max_hold_days: int = 3,
) -> list[dict]:
    """Backtest gap fill strategy on a single symbol.

    Returns list of completed trades.
    """
    gaps = detect_gaps(df, min_gap_pct=min_gap_pct)
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values

    trades = []
    for gap in gaps:
        if gap["score"] < min_score:
            continue

        i = gap["index"]
        entry = gap["entry_price"]
        stop = gap["stop_price"]
        target = gap["target_price"]
        direction = gap["direction"]

        # Simulate forward from gap day
        exit_reason = None
        exit_price = entry
        days_held = 0

        for j in range(i, min(i + max_hold_days + 1, len(close))):
            if j == i:
                continue  # skip entry day (entered at open)
            days_held = j - i

            if direction == "long":
                if low[j] <= stop:
                    exit_reason = "stop"
                    exit_price = stop
                    break
                if high[j] >= target:
                    exit_reason = "fill"
                    exit_price = target
                    break
            else:  # short
                if high[j] >= stop:
                    exit_reason = "stop"
                    exit_price = stop
                    break
                if low[j] <= target:
                    exit_reason = "fill"
                    exit_price = target
                    break

        if exit_reason is None:
            exit_reason = "timeout"
            exit_price = close[min(i + max_hold_days, len(close) - 1)]
            days_held = min(max_hold_days, len(close) - 1 - i)

        if direction == "long":
            pnl_pct = (exit_price - entry) / entry * 100
        else:
            pnl_pct = (entry - exit_price) / entry * 100

        trades.append({
            "symbol": symbol,
            "date": gap["date"],
            "gap_type": gap["gap_type"],
            "direction": direction,
            "gap_pct": gap["gap_pct"],
            "entry_price": entry,
            "exit_price": round(exit_price, 2),
            "target_price": target,
            "stop_price": stop,
            "pnl_pct": round(pnl_pct, 2),
            "exit_reason": exit_reason,
            "days_held": days_held,
            "score": gap["score"],
            "volume_ratio": gap["volume_ratio"],
            "rsi": gap["rsi"],
        })

    return trades


def _compute_rsi(close, period=14):
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gains).rolling(period).mean().values
    avg_loss = pd.Series(losses).rolling(period).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain), where=avg_loss > 0)
    return 100 - (100 / (1 + rs))
