# trendlines.py
"""
Phase 18 — Dynamic Trendline Detection (1h timeframe)

Detects descending resistance trendlines (connecting lower highs) and
ascending support trendlines (connecting higher lows) using confirmed
pivot points on the 1h candle stream.

Architecture mirrors structure.py:
  - update_on_close(candle_1h)  — feed one closed 1h candle at a time
  - evaluate(px, last_closed_1h) — evaluate trendline context at current price

Output (TrendlineResult):
  - near_support_tl     : price within TL_NEAR_PCT of ascending support line
  - near_resist_tl      : price within TL_NEAR_PCT of descending resistance line
  - broke_above_resist  : price broke above resistance trendline by TL_BREAK_PCT
  - broke_below_support : price broke below support trendline by TL_BREAK_PCT
  - resist_slope_neg    : resistance trendline has negative slope (descending channel)
  - support_slope_pos   : support trendline has positive slope (ascending channel)
  - proj_resist         : projected resistance price at current epoch (or None)
  - proj_support        : projected support price at current epoch (or None)
  - dist_to_resist      : fractional distance (resist - px) / resist (positive = below resist)
  - dist_to_support     : fractional distance (px - support) / support (positive = above support)
  - reasons             : semicolon-separated explanation string
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


def _d(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _clamp_i(x: Any, lo: int, hi: int) -> int:
    try:
        x_i = int(x)
    except Exception:
        x_i = lo
    return max(lo, min(hi, x_i))


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _project_line(
    p1: Tuple[int, Decimal],
    p2: Tuple[int, Decimal],
    target_epoch: int,
) -> Optional[Decimal]:
    """
    Project the line defined by two (epoch, price) points to target_epoch.
    Returns None if epochs are identical (degenerate).
    """
    t1, y1 = p1
    t2, y2 = p2
    if t2 == t1:
        return None
    slope = (y2 - y1) / Decimal(str(t2 - t1))
    return y1 + slope * Decimal(str(target_epoch - t1))


@dataclass
class TrendlineResult:
    # Proximity flags
    near_support_tl: bool = False
    near_resist_tl: bool = False

    # Breakout/breakdown flags
    broke_above_resist: bool = False
    broke_below_support: bool = False

    # Slope direction
    resist_slope_neg: bool = False     # descending resistance (bearish macro)
    support_slope_pos: bool = False    # ascending support (bullish macro)

    # Projected prices at current epoch
    proj_resist: Optional[Decimal] = None
    proj_support: Optional[Decimal] = None

    # Distance metrics (positive = safe side, negative = broken through)
    dist_to_resist: Optional[Decimal] = None   # (resist - px) / resist
    dist_to_support: Optional[Decimal] = None  # (px - support) / support

    # How many pivot points form each trendline (quality indicator)
    resist_pivot_count: int = 0
    support_pivot_count: int = 0

    # Debug
    reasons: str = ""


class TrendlineEngine:
    """
    Detects dynamic trendlines from confirmed 1h pivot highs and lows.

    Config keys (all optional, with defaults):
      TL_SWING_LEFT          int   3     bars left of pivot candidate
      TL_SWING_RIGHT         int   3     bars right of pivot candidate (confirmation delay)
      TL_NEAR_PCT            str   0.005  within 0.5% = "near trendline"
      TL_BREAK_PCT           str   0.003  0.3% beyond line = confirmed break
      TL_MAX_PIVOT_HISTORY   int   20    how many pivot points to keep
      TL_MIN_PIVOTS          int   2     minimum pivots needed to draw a line
      TL_MAX_SLOPE_AGE_BARS  int   60    ignore trendlines older than N 1h bars (2.5 days)
      USE_TRENDLINES         bool  False  master on/off flag
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}

        self.left = _clamp_i(self.cfg.get("TL_SWING_LEFT", 3), 1, 20)
        self.right = _clamp_i(self.cfg.get("TL_SWING_RIGHT", 3), 1, 20)
        self.near_pct = _d(self.cfg.get("TL_NEAR_PCT", "0.005"), "0.005")
        self.break_pct = _d(self.cfg.get("TL_BREAK_PCT", "0.003"), "0.003")
        self.max_pivot_history = _clamp_i(self.cfg.get("TL_MAX_PIVOT_HISTORY", 20), 2, 100)
        self.min_pivots = _clamp_i(self.cfg.get("TL_MIN_PIVOTS", 2), 2, 10)
        self.max_slope_age_bars = _clamp_i(self.cfg.get("TL_MAX_SLOPE_AGE_BARS", 60), 5, 500)

        # History of closed 1h candles
        self._candles: list = []

        # Confirmed pivot points: list of (epoch: int, price: Decimal)
        self._pivot_highs: List[Tuple[int, Decimal]] = []
        self._pivot_lows: List[Tuple[int, Decimal]] = []

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "TrendlineEngine":
        return cls(cfg)

    def update_on_close(self, candle) -> None:
        """
        Feed one closed 1h candle. Call this whenever a 1h candle closes.
        Confirms pivots only after `right` additional candles have printed.
        """
        if candle is None:
            return
        try:
            _ = candle.high
            _ = candle.low
            _ = candle.start_epoch
        except Exception:
            return

        self._candles.append(candle)

        # Keep enough history for pivot confirmation
        max_keep = max(200, (self.left + self.right + 5) * 10)
        if len(self._candles) > max_keep:
            self._candles = self._candles[-max_keep:]

        # Need at least left + right + 1 candles to confirm a pivot
        if len(self._candles) < (self.left + self.right + 1):
            return

        # The candidate pivot is at idx = len - right - 1
        idx = len(self._candles) - self.right - 1
        if idx < self.left:
            return

        self._maybe_confirm_pivot(idx)

        # Trim pivot history
        if len(self._pivot_highs) > self.max_pivot_history:
            self._pivot_highs = self._pivot_highs[-self.max_pivot_history:]
        if len(self._pivot_lows) > self.max_pivot_history:
            self._pivot_lows = self._pivot_lows[-self.max_pivot_history:]

    def _maybe_confirm_pivot(self, idx: int) -> None:
        """Confirm whether candle at idx is a pivot high, pivot low, or both."""
        c = self._candles[idx]
        window = self._candles[idx - self.left: idx + self.right + 1]
        if len(window) != (self.left + self.right + 1):
            return

        highs = [_d(w.high) for w in window]
        lows = [_d(w.low) for w in window]

        ch = _d(c.high)
        cl = _d(c.low)

        epoch = int(getattr(c, "start_epoch", 0))

        # Pivot high: unique maximum in window
        if ch > 0 and (ch == max(highs)) and (highs.count(ch) == 1):
            # Don't duplicate if epoch already recorded
            if not any(e == epoch for e, _ in self._pivot_highs):
                self._pivot_highs.append((epoch, ch))

        # Pivot low: unique minimum in window
        if cl > 0 and (cl == min(lows)) and (lows.count(cl) == 1):
            if not any(e == epoch for e, _ in self._pivot_lows):
                self._pivot_lows.append((epoch, cl))

    def _get_resist_trendline(
        self, current_epoch: int
    ) -> Optional[Tuple[Tuple[int, Decimal], Tuple[int, Decimal]]]:
        """
        Return the two most recent pivot highs that form a descending resistance line
        (lower high pattern). Returns None if insufficient pivots or no descending pattern.
        """
        if len(self._pivot_highs) < self.min_pivots:
            return None

        # Use the two most recent pivot highs
        recent = sorted(self._pivot_highs, key=lambda x: x[0])[-self.min_pivots:]
        p1, p2 = recent[0], recent[-1]

        # Age check: newest pivot must be recent enough
        age_bars = (current_epoch - p2[0]) // 3600  # 1h bars
        if age_bars > self.max_slope_age_bars:
            return None

        return p1, p2

    def _get_support_trendline(
        self, current_epoch: int
    ) -> Optional[Tuple[Tuple[int, Decimal], Tuple[int, Decimal]]]:
        """
        Return the two most recent pivot lows that form an ascending support line
        (higher low pattern). Returns None if insufficient pivots or no ascending pattern.
        """
        if len(self._pivot_lows) < self.min_pivots:
            return None

        recent = sorted(self._pivot_lows, key=lambda x: x[0])[-self.min_pivots:]
        p1, p2 = recent[0], recent[-1]

        age_bars = (current_epoch - p2[0]) // 3600
        if age_bars > self.max_slope_age_bars:
            return None

        return p1, p2

    def evaluate(self, px: Decimal, last_closed_1h=None) -> TrendlineResult:
        """
        Evaluate trendline context at current price px.
        last_closed_1h is used to get the current epoch for projection.
        """
        out = TrendlineResult()
        reasons: list = []
        px = _d(px, "0")

        if px <= 0:
            out.reasons = "TL:px<=0"
            return out

        # Use last closed 1h candle epoch for projection, or last candle in buffer
        current_epoch: int = 0
        if last_closed_1h is not None:
            try:
                current_epoch = int(last_closed_1h.start_epoch)
            except Exception:
                pass
        if current_epoch == 0 and self._candles:
            try:
                current_epoch = int(self._candles[-1].start_epoch)
            except Exception:
                pass

        if current_epoch == 0:
            out.reasons = "TL:no_epoch"
            return out

        # --- Resistance trendline ---
        resist_pts = self._get_resist_trendline(current_epoch)
        if resist_pts is not None:
            p1, p2 = resist_pts
            out.resist_pivot_count = len(self._pivot_highs)
            proj_r = _project_line(p1, p2, current_epoch)
            if proj_r is not None and proj_r > 0:
                out.proj_resist = proj_r
                slope_r = p2[1] - p1[1]
                out.resist_slope_neg = bool(slope_r < 0)

                dist_r = (proj_r - px) / proj_r  # positive = below resistance
                out.dist_to_resist = dist_r

                if abs(dist_r) <= self.near_pct:
                    out.near_resist_tl = True
                    reasons.append(f"near_resist_tl(d={float(dist_r):.4f})")

                # Broke above resistance trendline
                if px > proj_r * (Decimal("1") + self.break_pct):
                    out.broke_above_resist = True
                    reasons.append("broke_above_resist_tl")

                slope_tag = "down" if out.resist_slope_neg else "up"
                reasons.append(f"resist_tl={float(proj_r):.2f}({slope_tag})")

        # --- Support trendline ---
        support_pts = self._get_support_trendline(current_epoch)
        if support_pts is not None:
            p1, p2 = support_pts
            out.support_pivot_count = len(self._pivot_lows)
            proj_s = _project_line(p1, p2, current_epoch)
            if proj_s is not None and proj_s > 0:
                out.proj_support = proj_s
                slope_s = p2[1] - p1[1]
                out.support_slope_pos = bool(slope_s > 0)

                dist_s = (px - proj_s) / proj_s  # positive = above support
                out.dist_to_support = dist_s

                if abs(dist_s) <= self.near_pct:
                    out.near_support_tl = True
                    reasons.append(f"near_support_tl(d={float(dist_s):.4f})")

                # Broke below support trendline
                if px < proj_s * (Decimal("1") - self.break_pct):
                    out.broke_below_support = True
                    reasons.append("broke_below_support_tl")

                slope_tag = "up" if out.support_slope_pos else "down"
                reasons.append(f"support_tl={float(proj_s):.2f}({slope_tag})")

        if not reasons:
            reasons.append("ok")

        out.reasons = "TL:" + ";".join(reasons)
        return out

    @property
    def pivot_high_count(self) -> int:
        return len(self._pivot_highs)

    @property
    def pivot_low_count(self) -> int:
        return len(self._pivot_lows)
