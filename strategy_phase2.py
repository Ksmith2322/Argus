# strategy_phase2.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict

from indicators import IndicatorEngine


@dataclass
class StrategyState:
    signal: int              # 1 = long bias, 0 = neutral
    trend_ok: bool
    ma_fast: Optional[Decimal]
    ma_slow: Optional[Decimal]
    ma50: Optional[Decimal]
    ma200: Optional[Decimal]
    score: int               # 0–100 heuristic confidence
    reasons: str             # semicolon-separated reasons


def ma200_entry_level(ma200: Optional[Decimal], band_pct: Decimal) -> Optional[Decimal]:
    if ma200 is None:
        return None
    return ma200 * (Decimal("1") + band_pct)


def ma200_exit_level(ma200: Optional[Decimal], band_pct: Decimal) -> Optional[Decimal]:
    if ma200 is None:
        return None
    return ma200 * (Decimal("1") - band_pct)


class Phase2Strategy:
    """
    Phase 2 strategy:
      - Candle-close based (NO intrabar decisions)
      - Trend filter via MA200 (preferred: less brittle than MA200+band hard gate)
      - Fast/slow MA confirmation
      - Spread sanity check
      - Outputs a scored StrategyState for downstream gating
    """

    def __init__(self, cfg: Dict):
        self.cfg = cfg
        maxlen = max(
            cfg["MA_TREND_200"],
            cfg["MA_TREND_50"],
            cfg["MA_SLOW"],
            cfg["MA_FAST"],
        ) + 10
        self.ind = IndicatorEngine(maxlen=maxlen)

    def on_candle_close(self, close: Decimal) -> StrategyState:
        cfg = self.cfg

        # Push candle close into indicator engine
        self.ind.push_close(close)

        # Compute indicators
        ma_fast = self.ind.sma(cfg["MA_FAST"])
        ma_slow = self.ind.sma(cfg["MA_SLOW"])
        ma50 = self.ind.sma(cfg["MA_TREND_50"])
        ma200 = self.ind.sma(cfg["MA_TREND_200"])

        # --------- LINE ABOVE: ma200 = self.ind.sma(cfg["MA_TREND_200"])
        # Preferred trend filter (less brittle):
        # - Keep MA200 band as a SCORE BOOST (soft evidence)
        # - Use simple MA200 for trend_ok (hard gate)
        entry_level = ma200_entry_level(ma200, cfg["MA200_BAND_PCT"])
        trend_ok = bool(ma200 is not None and close > ma200)  # CHANGED: was close > MA200+band

        # Spread sanity check
        spread_bps = self.ind.fast_slow_spread_bps(ma_fast, ma_slow)
        spread_ok = bool(spread_bps is not None and spread_bps >= cfg["MIN_SPREAD_BPS"])

        # --------- LINE ABOVE: signal = 0
        # Entry signal (binary)
        # Preferred: do NOT require trend_ok inside signal generation (avoid double-gating).
        # Engine/decisions can still require trend_ok for actual entries.
        signal = 0
        if ma_fast is not None and ma_slow is not None and spread_ok:  # CHANGED: removed `trend_ok and`
            signal = 1 if ma_fast > ma_slow else 0

        # Scoring (heuristic, additive)
        score = 0
        reasons = []

        # --------- LINE ABOVE: reasons = []
        # Soft evidence: above MA200+band gets extra confidence, but does not hard-block trading anymore.
        if entry_level is not None and close > entry_level:
            score += 40
            reasons.append("px>MA200_band")

        if ma50 is not None and ma200 is not None and ma50 > ma200:
            score += 20
            reasons.append("MA50>MA200")

        if ma_fast is not None and ma_slow is not None and ma_fast > ma_slow:
            score += 20
            reasons.append("fast>slow")

        if spread_ok and spread_bps is not None:
            score += 20
            reasons.append(f"spread_ok({spread_bps:.2f}bps)")

        # Cap score defensively (future-proof)
        score = min(100, score)

        return StrategyState(
            signal=signal,
            trend_ok=trend_ok,
            ma_fast=ma_fast,
            ma_slow=ma_slow,
            ma50=ma50,
            ma200=ma200,
            score=score,
            reasons=";".join(reasons),
        )
