# adaptive_confluence.py
# --------- LINE ABOVE: # adaptive_confluence.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Any, Union

from confluence import ConfluenceResult
from regime import RegimeResult


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _clamp(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


@dataclass
class AdaptiveConfluenceResult:
    """
    Output of adaptive shaping, intended to *overlay* your base ConfluenceResult.
    """
    base_score: int
    base_gate: str
    adjusted_score: int
    adjusted_gate: str
    score_delta: int
    reason: str


class AdaptiveConfluenceEngine:
    """
    Phase 4 – Adaptive confluence shaping.

    Takes the base ConfluenceResult (from confluence.py) and applies a regime-aware overlay.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

        # Base thresholds (same defaults as config.py)
        self.watch_base = _as_int(cfg.get("CONFLUENCE_WATCH_SCORE", 60), 60)
        self.trade_base = _as_int(cfg.get("CONFLUENCE_TRADE_SCORE", 80), 80)

        # Score bonus/penalty per regime
        self.bonus_trend_up = _as_int(cfg.get("AC_BONUS_TREND_UP", 5), 5)
        self.bonus_trend_down = _as_int(cfg.get("AC_BONUS_TREND_DOWN", 5), 5)

        self.penalty_range = _as_int(cfg.get("AC_PENALTY_RANGE", 7), 7)
        self.penalty_volatile_range = _as_int(cfg.get("AC_PENALTY_VOLATILE_RANGE", 12), 12)

        # Threshold shifts per regime (positive means "harder to trade")
        self.trade_shift_range = _as_int(cfg.get("AC_TRADE_SHIFT_RANGE", 5), 5)
        self.trade_shift_volatile_range = _as_int(cfg.get("AC_TRADE_SHIFT_VOLATILE_RANGE", 10), 10)

        self.watch_shift_range = _as_int(cfg.get("AC_WATCH_SHIFT_RANGE", 0), 0)
        self.watch_shift_volatile_range = _as_int(cfg.get("AC_WATCH_SHIFT_VOLATILE_RANGE", 5), 5)

        # Optional gating using trend strength
        self.min_trend_strength_for_trade = _as_decimal(
            cfg.get("AC_MIN_TREND_STRENGTH_FOR_TRADE", "0"), "0"
        )  # 0 disables

        # Clamp sanity
        self.bonus_trend_up = max(-25, min(25, self.bonus_trend_up))
        self.bonus_trend_down = max(-25, min(25, self.bonus_trend_down))
        self.penalty_range = max(-50, min(50, self.penalty_range))
        self.penalty_volatile_range = max(-50, min(50, self.penalty_volatile_range))

        self.trade_shift_range = max(-25, min(25, self.trade_shift_range))
        self.trade_shift_volatile_range = max(-25, min(25, self.trade_shift_volatile_range))
        self.watch_shift_range = max(-25, min(25, self.watch_shift_range))
        self.watch_shift_volatile_range = max(-25, min(25, self.watch_shift_volatile_range))

    # --------- LINE ABOVE: def __init__(self, cfg: Dict[str, Any]):
    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "AdaptiveConfluenceEngine":
        return cls(cfg)

    def _gate_from_score(self, score: int, watch_th: int, trade_th: int) -> str:
        if score >= trade_th:
            return "TRADE"
        if score >= watch_th:
            return "WATCH"
        return "HOLD"

    def adjust(
        self,
        base: Optional[ConfluenceResult],
        *,
        regime: Optional[Union[str, RegimeResult]] = None,
        trend_strength: Optional[Decimal] = None,
        vol: Optional[Decimal] = None,
    ) -> AdaptiveConfluenceResult:
        """
        Args:
          base: ConfluenceResult from confluence.py
          regime: string ("TREND_UP"...), OR RegimeResult from regime.py
          trend_strength/vol: optional overrides (if you don't pass a RegimeResult)
        """
        if base is None:
            return AdaptiveConfluenceResult(
                base_score=0,
                base_gate="NONE",
                adjusted_score=0,
                adjusted_gate="HOLD",
                score_delta=0,
                reason="NO_BASE_CONFLUENCE",
            )

        # --------- LINE ABOVE: if base is None:
        # If caller passed a RegimeResult, unpack it automatically.
        regime_str: Optional[str] = None
        if isinstance(regime, RegimeResult):
            regime_str = regime.regime
            if trend_strength is None:
                trend_strength = regime.trend_strength
            if vol is None:
                vol = regime.vol
        else:
            regime_str = regime

        base_score = _as_int(getattr(base, "confluence_score", 0), 0)
        base_gate = str(getattr(base, "gate", "") or "HOLD")

        r = (regime_str or "UNKNOWN").upper()

        # Start with base thresholds
        watch_th = int(self.watch_base)
        trade_th = int(self.trade_base)

        delta = 0
        why = []

        # Regime-based shaping
        if r == "TREND_UP":
            delta += int(self.bonus_trend_up)
            why.append(f"bonus_trend_up=+{self.bonus_trend_up}")

        elif r == "TREND_DOWN":
            delta += int(self.bonus_trend_down)
            why.append(f"bonus_trend_down=+{self.bonus_trend_down}")

        elif r == "RANGE":
            delta -= int(self.penalty_range)
            trade_th += int(self.trade_shift_range)
            watch_th += int(self.watch_shift_range)
            why.append(f"penalty_range=-{self.penalty_range}")
            why.append(f"trade_shift_range=+{self.trade_shift_range}")
            if self.watch_shift_range:
                why.append(f"watch_shift_range=+{self.watch_shift_range}")

        elif r == "VOLATILE_RANGE":
            delta -= int(self.penalty_volatile_range)
            trade_th += int(self.trade_shift_volatile_range)
            watch_th += int(self.watch_shift_volatile_range)
            why.append(f"penalty_vol_range=-{self.penalty_volatile_range}")
            why.append(f"trade_shift_vol_range=+{self.trade_shift_volatile_range}")
            if self.watch_shift_volatile_range:
                why.append(f"watch_shift_vol_range=+{self.watch_shift_volatile_range}")

        else:
            why.append("regime_unknown=no_adjust")

        # Optional additional guard: require trend strength for TRADE
        if self.min_trend_strength_for_trade > 0:
            ts = _as_decimal(trend_strength, "0")
            if ts < self.min_trend_strength_for_trade:
                trade_th += 5
                why.append(f"min_trend_strength_block ts={ts:.5f}<min={self.min_trend_strength_for_trade}")

        # Compute adjusted score
        adj = base_score + delta
        adj = int(_clamp(Decimal(adj), Decimal("0"), Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))

        # Gate from adjusted score + adjusted thresholds
        adj_gate = self._gate_from_score(adj, watch_th=watch_th, trade_th=trade_th)

        # Trace
        why.append(f"regime={r}")
        why.append(f"base_score={base_score}")
        why.append(f"delta={delta}")
        why.append(f"adj_score={adj}")
        why.append(f"watch_th={watch_th}")
        why.append(f"trade_th={trade_th}")
        if trend_strength is not None:
            why.append(f"trend_strength={_as_decimal(trend_strength, '0'):.5f}")
        if vol is not None:
            why.append(f"vol={_as_decimal(vol, '0'):.5f}")

        return AdaptiveConfluenceResult(
            base_score=int(base_score),
            base_gate=str(base_gate),
            adjusted_score=int(adj),
            adjusted_gate=str(adj_gate),
            score_delta=int(delta),
            reason=" | ".join(why),
        )
