#!/usr/bin/env python3
"""titan/strategies/swing_engine.py -- Multi-strategy swing trade signal engine.

Evaluates 4 independent strategies across Daily/4H/1H timeframes.
Each strategy scores 0-100. Combined score determines signal strength.

Strategies:
  1. Trend Following   -- EMA alignment + ADX + momentum
  2. Breakout          -- BB squeeze -> expansion + volume confirmation
  3. Mean Reversion    -- Oversold/overbought at key levels (counter-trend)
  4. Trendline Cascade -- Multi-TF trendline support/resistance (Tori method)

Usage:
    from titan.strategies.swing_engine import SwingEngine
    engine = SwingEngine()
    signals = engine.evaluate("NVDA", daily_df, h4_df, h1_df)
"""
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class SwingSignal:
    """Output of the swing engine evaluation."""
    symbol: str
    direction: str              # "long" or "short"
    strength: int               # 0-100 composite score
    strategy: str               # primary strategy that triggered
    timeframe: str              # dominant timeframe for this signal
    entry_price: float
    stop_price: float
    target_price: float
    risk_reward: float          # target/stop ratio

    # Strategy sub-scores
    trend_score: int = 0
    breakout_score: int = 0
    mean_rev_score: int = 0
    trendline_score: int = 0

    # Context
    daily_trend: str = ""       # UP / DOWN / FLAT
    h4_trend: str = ""
    rsi_daily: float = 0
    rsi_4h: float = 0
    bb_pctile: float = 0
    volume_ratio: float = 0
    atr_pct: float = 0
    reason: str = ""


class SwingEngine:
    """Multi-strategy swing trade evaluator."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.min_signal_strength = cfg.get("min_signal_strength", 60)
        self.atr_stop_mult = cfg.get("atr_stop_mult", 2.0)
        self.atr_target_mult = cfg.get("atr_target_mult", 4.0)

    def evaluate(self, symbol: str, daily: pd.DataFrame,
                 h4: pd.DataFrame | None = None,
                 h1: pd.DataFrame | None = None) -> Optional[SwingSignal]:
        """Evaluate all strategies and return strongest signal or None."""
        if daily is None or len(daily) < 60:
            return None

        # Compute indicators on each timeframe
        d_ind = self._compute_indicators(daily)
        h4_ind = self._compute_indicators(h4) if h4 is not None and len(h4) >= 30 else None
        h1_ind = self._compute_indicators(h1) if h1 is not None and len(h1) >= 30 else None

        # Run each strategy
        trend_sig = self._strategy_trend_following(symbol, d_ind, h4_ind)
        breakout_sig = self._strategy_breakout(symbol, d_ind, h4_ind, h1_ind)
        mr_sig = self._strategy_mean_reversion(symbol, d_ind, h4_ind)
        tl_sig = self._strategy_trendline(symbol, d_ind, h4_ind)

        # Pick strongest
        candidates = [s for s in [trend_sig, breakout_sig, mr_sig, tl_sig] if s is not None]
        if not candidates:
            return None

        best = max(candidates, key=lambda s: s.strength)

        # Fill in sub-scores from all strategies
        best.trend_score = trend_sig.strength if trend_sig else 0
        best.breakout_score = breakout_sig.strength if breakout_sig else 0
        best.mean_rev_score = mr_sig.strength if mr_sig else 0
        best.trendline_score = tl_sig.strength if tl_sig else 0

        if best.strength < self.min_signal_strength:
            return None

        return best

    # ── Strategy 1: Trend Following ──────────────────────────

    def _strategy_trend_following(self, symbol: str, d: dict,
                                   h4: dict | None) -> Optional[SwingSignal]:
        """Strong trend + pullback to EMA = entry."""
        score = 0
        direction = None
        reasons = []

        # Daily trend (EMA alignment)
        if d["ema_8"] > d["ema_21"] > d["ema_50"]:
            direction = "long"
            score += 25
            reasons.append("Daily EMAs bullish aligned")
        elif d["ema_8"] < d["ema_21"] < d["ema_50"]:
            direction = "short"
            score += 25
            reasons.append("Daily EMAs bearish aligned")
        else:
            return None  # No clear trend

        # ADX strength
        if d["adx"] > 30:
            score += 20
            reasons.append(f"Strong trend ADX={d['adx']:.0f}")
        elif d["adx"] > 20:
            score += 10
            reasons.append(f"Moderate trend ADX={d['adx']:.0f}")
        else:
            score -= 10

        # Pullback to EMA (entry zone)
        dist_to_ema21 = abs(d["close"] - d["ema_21"]) / d["close"] * 100
        if dist_to_ema21 < 2.0:
            score += 20
            reasons.append(f"Price at EMA21 (dist={dist_to_ema21:.1f}%)")
        elif dist_to_ema21 < 4.0:
            score += 10
        else:
            score -= 10  # Too far from EMA

        # RSI confirmation
        if direction == "long" and 35 <= d["rsi"] <= 55:
            score += 15
            reasons.append(f"RSI pullback zone ({d['rsi']:.0f})")
        elif direction == "short" and 45 <= d["rsi"] <= 65:
            score += 15
            reasons.append(f"RSI rally zone ({d['rsi']:.0f})")

        # 4H confirmation
        if h4:
            if direction == "long" and h4["ema_8"] > h4["ema_21"]:
                score += 10
                reasons.append("4H confirms bullish")
            elif direction == "short" and h4["ema_8"] < h4["ema_21"]:
                score += 10
                reasons.append("4H confirms bearish")

        if score <= 0:
            return None

        entry = d["close"]
        atr = d["atr"]
        stop = entry - self.atr_stop_mult * atr if direction == "long" else entry + self.atr_stop_mult * atr
        target = entry + self.atr_target_mult * atr if direction == "long" else entry - self.atr_target_mult * atr
        rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0

        return SwingSignal(
            symbol=symbol, direction=direction, strength=min(100, max(0, score)),
            strategy="TREND_FOLLOW", timeframe="daily",
            entry_price=round(entry, 2), stop_price=round(stop, 2),
            target_price=round(target, 2), risk_reward=round(rr, 2),
            daily_trend="UP" if direction == "long" else "DOWN",
            h4_trend=("UP" if h4 and h4["ema_8"] > h4["ema_21"] else "DOWN") if h4 else "",
            rsi_daily=round(d["rsi"], 1), atr_pct=round(atr / entry * 100, 2),
            reason=" | ".join(reasons),
        )

    # ── Strategy 2: Breakout ─────────────────────────────────

    def _strategy_breakout(self, symbol: str, d: dict,
                            h4: dict | None, h1: dict | None) -> Optional[SwingSignal]:
        """BB squeeze -> expansion + volume spike = breakout entry."""
        score = 0
        direction = None
        reasons = []

        # BB squeeze detection
        if d["bb_pctile"] < 15:
            score += 30
            reasons.append(f"Extreme BB squeeze ({d['bb_pctile']:.0f}%ile)")
        elif d["bb_pctile"] < 30:
            score += 15
            reasons.append(f"BB squeeze ({d['bb_pctile']:.0f}%ile)")
        else:
            return None  # No squeeze

        # Direction from price vs BB
        if d["close"] > d["bb_upper"]:
            direction = "long"
            score += 15
            reasons.append("Breaking above upper BB")
        elif d["close"] < d["bb_lower"]:
            direction = "short"
            score += 15
            reasons.append("Breaking below lower BB")
        elif d["close"] > d["bb_mid"]:
            direction = "long"
            score += 5
        else:
            direction = "short"
            score += 5

        # Volume confirmation
        if d["vol_ratio"] > 2.0:
            score += 25
            reasons.append(f"Volume spike {d['vol_ratio']:.1f}x avg")
        elif d["vol_ratio"] > 1.5:
            score += 15
            reasons.append(f"Above-avg volume {d['vol_ratio']:.1f}x")
        elif d["vol_ratio"] < 0.8:
            score -= 10  # Low volume breakout = likely fakeout

        # EMA structure support
        if direction == "long" and d["close"] > d["ema_50"]:
            score += 10
            reasons.append("Above EMA50")
        elif direction == "short" and d["close"] < d["ema_50"]:
            score += 10

        # Narrow range days buildup
        if d.get("narrow_days", 0) >= 3:
            score += 10
            reasons.append(f"{d['narrow_days']} narrow range days")

        if score <= 0:
            return None

        entry = d["close"]
        atr = d["atr"]
        stop = entry - self.atr_stop_mult * atr if direction == "long" else entry + self.atr_stop_mult * atr
        target = entry + self.atr_target_mult * 1.5 * atr if direction == "long" else entry - self.atr_target_mult * 1.5 * atr
        rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0

        return SwingSignal(
            symbol=symbol, direction=direction, strength=min(100, max(0, score)),
            strategy="BREAKOUT", timeframe="daily",
            entry_price=round(entry, 2), stop_price=round(stop, 2),
            target_price=round(target, 2), risk_reward=round(rr, 2),
            bb_pctile=round(d["bb_pctile"], 1), volume_ratio=round(d["vol_ratio"], 2),
            rsi_daily=round(d["rsi"], 1), atr_pct=round(atr / entry * 100, 2),
            reason=" | ".join(reasons),
        )

    # ── Strategy 3: Mean Reversion ───────────────────────────

    def _strategy_mean_reversion(self, symbol: str, d: dict,
                                   h4: dict | None) -> Optional[SwingSignal]:
        """Overextended price at key levels = snap-back trade."""
        score = 0
        direction = None
        reasons = []

        # RSI extreme
        if d["rsi"] < 25:
            direction = "long"
            score += 30
            reasons.append(f"RSI oversold ({d['rsi']:.0f})")
        elif d["rsi"] > 75:
            direction = "short"
            score += 30
            reasons.append(f"RSI overbought ({d['rsi']:.0f})")
        elif d["rsi"] < 35 and d["close"] < d["ema_50"]:
            direction = "long"
            score += 15
            reasons.append(f"RSI low + below EMA50 ({d['rsi']:.0f})")
        elif d["rsi"] > 65 and d["close"] > d["ema_50"]:
            direction = "short"
            score += 15
            reasons.append(f"RSI high + above EMA50 ({d['rsi']:.0f})")
        else:
            return None  # Not extreme enough

        # Distance from EMA (overextended)
        dist_ema50 = (d["close"] - d["ema_50"]) / d["ema_50"] * 100
        if direction == "long" and dist_ema50 < -8:
            score += 20
            reasons.append(f"Price {dist_ema50:.1f}% below EMA50")
        elif direction == "short" and dist_ema50 > 8:
            score += 20
            reasons.append(f"Price +{dist_ema50:.1f}% above EMA50")
        elif abs(dist_ema50) > 5:
            score += 10

        # Volume exhaustion (declining volume at extremes = reversal setup)
        if d["vol_ratio"] < 0.7:
            score += 15
            reasons.append("Volume exhaustion")

        # Must be above 200 EMA for longs (structural support)
        if direction == "long" and d["close"] > d["ema_200"]:
            score += 10
            reasons.append("Above EMA200 (structural support)")
        elif direction == "long" and d["close"] < d["ema_200"]:
            score -= 15  # Catching falling knife

        if score <= 0:
            return None

        entry = d["close"]
        atr = d["atr"]
        # Tighter stops for mean reversion (counter-trend)
        stop = entry - 1.5 * atr if direction == "long" else entry + 1.5 * atr
        target = d["ema_21"]  # Target is mean (EMA21)
        rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0

        return SwingSignal(
            symbol=symbol, direction=direction, strength=min(100, max(0, score)),
            strategy="MEAN_REVERSION", timeframe="daily",
            entry_price=round(entry, 2), stop_price=round(stop, 2),
            target_price=round(target, 2), risk_reward=round(rr, 2),
            rsi_daily=round(d["rsi"], 1), atr_pct=round(atr / entry * 100, 2),
            reason=" | ".join(reasons),
        )

    # ── Strategy 4: Trendline Cascade ────────────────────────

    def _strategy_trendline(self, symbol: str, d: dict,
                             h4: dict | None) -> Optional[SwingSignal]:
        """Multi-TF trendline support/resistance approach.
        Daily defines the trend, 4H finds entry at trendline touch."""
        if not h4:
            return None

        score = 0
        direction = None
        reasons = []

        # Daily higher lows = uptrend trendline
        lows = d["recent_lows"]
        highs = d["recent_highs"]

        if len(lows) >= 3 and all(lows[i] < lows[i + 1] for i in range(len(lows) - 1)):
            # Rising lows = uptrend
            trendline_slope = (lows[-1] - lows[0]) / len(lows)
            projected_support = lows[-1] + trendline_slope

            dist_to_tl = (d["close"] - projected_support) / d["close"] * 100
            if 0 < dist_to_tl < 3:
                direction = "long"
                score += 35
                reasons.append(f"Price near rising trendline support (dist={dist_to_tl:.1f}%)")
            elif dist_to_tl < 0:
                # Below trendline = broken
                direction = "short"
                score += 20
                reasons.append("Trendline support broken")

        elif len(highs) >= 3 and all(highs[i] > highs[i + 1] for i in range(len(highs) - 1)):
            # Falling highs = downtrend
            trendline_slope = (highs[-1] - highs[0]) / len(highs)
            projected_resistance = highs[-1] + trendline_slope

            dist_to_tl = (projected_resistance - d["close"]) / d["close"] * 100
            if 0 < dist_to_tl < 3:
                direction = "short"
                score += 35
                reasons.append(f"Price near falling trendline resistance (dist={dist_to_tl:.1f}%)")

        if direction is None:
            return None

        # 4H confirmation: EMA trend agrees
        if direction == "long" and h4["ema_8"] > h4["ema_21"]:
            score += 15
            reasons.append("4H EMAs bullish")
        elif direction == "short" and h4["ema_8"] < h4["ema_21"]:
            score += 15
            reasons.append("4H EMAs bearish")

        # RSI not extreme (room to move)
        if 35 <= d["rsi"] <= 65:
            score += 10
            reasons.append(f"RSI neutral ({d['rsi']:.0f})")

        # Volume confirmation
        if d["vol_ratio"] > 1.2:
            score += 10
            reasons.append(f"Volume {d['vol_ratio']:.1f}x")

        if score <= 0:
            return None

        entry = d["close"]
        atr = d["atr"]
        stop = entry - self.atr_stop_mult * atr if direction == "long" else entry + self.atr_stop_mult * atr
        target = entry + self.atr_target_mult * atr if direction == "long" else entry - self.atr_target_mult * atr
        rr = abs(target - entry) / abs(entry - stop) if abs(entry - stop) > 0 else 0

        return SwingSignal(
            symbol=symbol, direction=direction, strength=min(100, max(0, score)),
            strategy="TRENDLINE", timeframe="4h",
            entry_price=round(entry, 2), stop_price=round(stop, 2),
            target_price=round(target, 2), risk_reward=round(rr, 2),
            daily_trend="UP" if direction == "long" else "DOWN",
            h4_trend="UP" if h4["ema_8"] > h4["ema_21"] else "DOWN",
            rsi_daily=round(d["rsi"], 1), atr_pct=round(atr / entry * 100, 2),
            volume_ratio=round(d["vol_ratio"], 2),
            reason=" | ".join(reasons),
        )

    # ── Indicator Computation ────────────────────────────────

    def _compute_indicators(self, df: pd.DataFrame) -> dict:
        """Compute all technical indicators for a single timeframe."""
        close = df["Close"].values
        high = df["High"].values
        low = df["Low"].values
        volume = df["Volume"].values if "Volume" in df.columns else np.zeros(len(df))

        n = len(close)
        i = n - 1  # latest bar

        # EMAs
        ema_8 = pd.Series(close).ewm(span=8).mean().values
        ema_21 = pd.Series(close).ewm(span=21).mean().values
        ema_50 = pd.Series(close).ewm(span=50).mean().values
        ema_200 = pd.Series(close).ewm(span=200).mean().values if n >= 200 else np.full(n, close[0])

        # RSI
        delta = np.diff(close, prepend=close[0])
        gains = np.where(delta > 0, delta, 0)
        losses = np.where(delta < 0, -delta, 0)
        avg_gain = pd.Series(gains).rolling(14).mean().values
        avg_loss = pd.Series(losses).rolling(14).mean().values
        rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain), where=avg_loss > 0)
        rsi = 100 - (100 / (1 + rs))

        # ATR
        tr = np.maximum(high[1:] - low[1:],
                         np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
        tr = np.concatenate([[high[0] - low[0]], tr])
        atr = pd.Series(tr).rolling(14).mean().values

        # Bollinger Bands
        sma_20 = pd.Series(close).rolling(20).mean().values
        std_20 = pd.Series(close).rolling(20).std().values
        bb_upper = sma_20 + 2 * std_20
        bb_lower = sma_20 - 2 * std_20
        bb_width = (2 * std_20) / sma_20
        bb_width_valid = bb_width[~np.isnan(bb_width)]
        bb_pctile = np.searchsorted(np.sort(bb_width_valid), bb_width[i]) / len(bb_width_valid) * 100 if len(bb_width_valid) > 20 else 50

        # Volume
        vol_20 = pd.Series(volume).rolling(20).mean().values
        vol_ratio = volume[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1

        # ADX (simplified)
        plus_dm = np.maximum(np.diff(high, prepend=high[0]), 0)
        minus_dm = np.maximum(-np.diff(low, prepend=low[0]), 0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0
        atr_smooth = pd.Series(tr).rolling(14).mean().values
        plus_di = 100 * pd.Series(plus_dm).rolling(14).mean().values / np.maximum(atr_smooth, 1e-10)
        minus_di = 100 * pd.Series(minus_dm).rolling(14).mean().values / np.maximum(atr_smooth, 1e-10)
        dx = 100 * np.abs(plus_di - minus_di) / np.maximum(plus_di + minus_di, 1e-10)
        adx = pd.Series(dx).rolling(14).mean().values

        # Swing highs/lows (last 5 from recent 60 bars)
        lookback = min(60, n)
        recent_close = close[max(0, n - lookback):n]
        recent_high = high[max(0, n - lookback):n]
        recent_low = low[max(0, n - lookback):n]

        swing_highs = []
        swing_lows = []
        order = 5
        for j in range(order, len(recent_close) - order):
            if recent_high[j] == max(recent_high[j - order:j + order + 1]):
                swing_highs.append(recent_high[j])
            if recent_low[j] == min(recent_low[j - order:j + order + 1]):
                swing_lows.append(recent_low[j])

        # Narrow range days
        daily_ranges = [(high[j] - low[j]) / close[j] * 100 for j in range(max(0, i - 10), i + 1)]
        avg_range = np.mean(daily_ranges) if daily_ranges else 1
        narrow_days = 0
        for j in range(i, max(0, i - 10), -1):
            if (high[j] - low[j]) / close[j] * 100 < avg_range * 0.7:
                narrow_days += 1
            else:
                break

        return {
            "close": close[i],
            "high": high[i],
            "low": low[i],
            "ema_8": ema_8[i],
            "ema_21": ema_21[i],
            "ema_50": ema_50[i],
            "ema_200": ema_200[i],
            "rsi": rsi[i] if not np.isnan(rsi[i]) else 50,
            "atr": atr[i] if not np.isnan(atr[i]) else 0,
            "adx": adx[i] if not np.isnan(adx[i]) else 0,
            "bb_upper": bb_upper[i] if not np.isnan(bb_upper[i]) else close[i],
            "bb_lower": bb_lower[i] if not np.isnan(bb_lower[i]) else close[i],
            "bb_mid": sma_20[i] if not np.isnan(sma_20[i]) else close[i],
            "bb_pctile": bb_pctile,
            "vol_ratio": vol_ratio,
            "narrow_days": narrow_days,
            "recent_highs": swing_highs[-5:],
            "recent_lows": swing_lows[-5:],
        }
