# regime.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Dict, Any

from .utils import pct_dist


def _d(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


@dataclass
class RegimeResult:
    regime: str
    vol: Decimal
    trend_strength: Decimal
    ma_spread: Decimal
    reasons: str


class RegimeEngine:
    """
    Phase 4 – Market regime classifier.

    Output regimes:
      - TREND_UP
      - TREND_DOWN
      - RANGE
      - VOLATILE_RANGE

    Notes:
      - Expects `st.volatility` to be a FRACTION (0.004 = 0.4%).
      - Uses MA fast/slow relationship + spread as a trend proxy.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

        # Volatility thresholds (fractional)
        self.vol_high = _d(cfg.get("REGIME_VOL_HIGH", "0.01"), "0.01")
        self.vol_low = _d(cfg.get("REGIME_VOL_LOW", "0.003"), "0.003")  # not strictly required, kept for tuning/debug

        # Trend thresholds (fractional)
        self.trend_slope_min = _d(cfg.get("REGIME_TREND_SLOPE_MIN", "0.0003"), "0.0003")
        self.ma_spread_min = _d(cfg.get("REGIME_MA_SPREAD_MIN", "0.0015"), "0.0015")

        # Clamp to sane ranges
        if self.vol_high < 0:
            self.vol_high = Decimal("0")
        if self.vol_low < 0:
            self.vol_low = Decimal("0")

        self.trend_slope_min = max(Decimal("0"), self.trend_slope_min)
        self.ma_spread_min = max(Decimal("0"), self.ma_spread_min)

    # --------- LINE ABOVE: def __init__(self, cfg: Dict[str, Any]):
    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "RegimeEngine":
        return cls(cfg)

    def evaluate(self, st) -> Optional[RegimeResult]:
        """
        st = StrategyState (1m or HTF)

        Requires:
          st.ma_fast
          st.ma_slow
          st.ma200   (not used for direction today; kept as required input for future expansion)
          st.volatility (fraction, ex 0.004)
        """
        if st is None:
            return None

        ma_fast = getattr(st, "ma_fast", None)
        ma_slow = getattr(st, "ma_slow", None)
        ma200 = getattr(st, "ma200", None)
        vol = getattr(st, "volatility", None)

        # If any core inputs missing, no regime
        if ma_fast is None or ma_slow is None or ma200 is None or vol is None:
            return None

        ma_fast = _d(ma_fast)
        ma_slow = _d(ma_slow)
        ma200 = _d(ma200)
        vol = _d(vol)

        if ma_fast <= 0 or ma_slow <= 0 or ma200 <= 0:
            return None
        if vol < 0:
            return None

        # Trend proxy:
        # - direction via fast vs slow
        # - strength via MA spread (fractional)
        # pct_dist(a,b) = (a-b)/b
        slope = pct_dist(ma_fast, ma_slow)
        spread = abs(slope) if slope is not None else Decimal("0")

        reasons = []

        # TREND if MA spread clears threshold
        if spread >= self.ma_spread_min and spread >= self.trend_slope_min:
            if ma_fast > ma_slow:
                regime = "TREND_UP"
            else:
                regime = "TREND_DOWN"
            reasons.append("trend")
            reasons.append(f"slope={(_d(slope)):.5f}")
            reasons.append(f"spread={spread:.5f}")
        else:
            # RANGE vs VOLATILE_RANGE by vol_high
            if vol >= self.vol_high:
                regime = "VOLATILE_RANGE"
                reasons.append("high_vol_range")
            else:
                regime = "RANGE"
                reasons.append("low_vol_range")

            reasons.append(f"spread={spread:.5f}")

        reasons.append(f"vol={vol:.5f}")
        reasons.append(f"ma_fast={ma_fast}")
        reasons.append(f"ma_slow={ma_slow}")

        return RegimeResult(
            regime=regime,
            vol=vol,
            trend_strength=spread,
            ma_spread=spread,
            reasons=" | ".join(reasons),
        )
