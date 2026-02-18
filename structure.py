# structure.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .candles import Candle


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


def _pct_dist(a: Decimal, b: Decimal) -> Optional[Decimal]:
    """
    Fractional distance: (a - b) / b
    """
    if b is None or b == 0:
        return None
    return (a - b) / b


def _abs_frac_dist(a: Decimal, b: Decimal) -> Optional[Decimal]:
    """
    abs((a - b) / b)
    """
    if b is None or b == 0:
        return None
    return abs((a - b) / b)


@dataclass
class StructureResult:
    # Nearest levels
    nearest_support: Optional[Decimal] = None
    nearest_resistance: Optional[Decimal] = None
    dist_support: Optional[Decimal] = None          # fraction (px - support)/support
    dist_resistance: Optional[Decimal] = None       # fraction (res - px)/res

    # Proximity flags
    near_support: bool = False
    near_resistance: bool = False

    # Break / retest
    broke_up: bool = False
    broke_down: bool = False
    retest_ok: bool = False
    failed_retest: bool = False

    # Rejections
    rejection_at_res: bool = False
    rejection_at_sup: bool = False

    # Debug / telemetry
    reasons: str = ""


class StructureEngine:
    """
    Phase 5A — Market Structure Engine

    What it computes (deterministic, no silent None):
      - nearest Support/Resistance from pivot clusters
      - proximity flags (near_support/near_resistance)
      - break up/down (px beyond level + buffer)
      - retest success/fail (using last_closed candle within bar window)
      - rejection at levels via wick/body heuristics
      - reasons string (always populated with something meaningful)

    Determinism:
      - pivot confirmation is strictly based on closed candles
      - pivot detection uses a fixed left/right window
      - clustering uses a fixed tolerance and weighted mean merge

    Integration:
      - update_on_close(closed_1m_candle) on each 1m close
      - evaluate(px, last_closed_1m_candle) on each tick
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}

        # Pivot params
        self.left = _clamp_i(self.cfg.get("STRUCT_SWING_LEFT", 3), 1, 20)
        self.right = _clamp_i(self.cfg.get("STRUCT_SWING_RIGHT", 3), 1, 20)

        # Level logic
        self.near_pct = _d(self.cfg.get("STRUCT_NEAR_LEVEL_PCT", "0.0015"), "0.0015")
        self.cluster_pct = _d(self.cfg.get("STRUCT_LEVEL_CLUSTER_PCT", "0.0010"), "0.0010")
        self.break_pct = _d(self.cfg.get("STRUCT_BREAK_PCT", "0.0008"), "0.0008")
        self.retest_max_bars = _clamp_i(self.cfg.get("STRUCT_RETEST_MAX_BARS", 12), 1, 500)
        self.max_levels = _clamp_i(self.cfg.get("STRUCT_MAX_LEVELS", 12), 1, 200)

        # Rejection logic
        self.use_rejection = _as_bool(self.cfg.get("STRUCT_USE_REJECTION", True), True)
        self.reject_wick_ratio = _d(self.cfg.get("STRUCT_REJECT_WICK_RATIO", "1.5"), "1.5")
        self.reject_body_max_pct = _d(self.cfg.get("STRUCT_REJECT_BODY_MAX_PCT", "0.0025"), "0.0025")

        # History
        self._candles: List[Candle] = []

        # Levels stored as clusters (price, count)
        self._supports: List[Tuple[Decimal, int]] = []
        self._resistances: List[Tuple[Decimal, int]] = []

        # Break state
        self._last_break_dir: Optional[str] = None   # "UP" / "DOWN"
        self._last_break_level: Optional[Decimal] = None
        self._last_break_bar_index: Optional[int] = None

    # --------- LINE ABOVE: def __init__(self, cfg: Dict[str, Any]):

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "StructureEngine":
        return cls(cfg)

    def update_on_close(self, candle: Candle):
        """
        Feed ONE closed candle at a time (1m).
        Confirms pivots only after `right` candles have printed after the pivot candle.
        """
        if candle is None:
            return

        # Defensive: ensure candle has required fields (do not throw; just ignore malformed candle)
        try:
            _ = candle.high
            _ = candle.low
            _ = candle.close
            _ = candle.start_epoch
        except Exception:
            return

        self._candles.append(candle)

        # Keep enough history for pivoting + some extra for retests and stability
        max_keep = max(800, (self.left + self.right + 5) * 20)
        if len(self._candles) > max_keep:
            self._candles = self._candles[-max_keep:]

        # Not enough to confirm a pivot
        if len(self._candles) < (self.left + self.right + 1):
            return

        # Candle at idx becomes "pivot candidate" once we have `right` after it
        idx = len(self._candles) - self.right - 1
        if idx < self.left:
            return

        self._maybe_add_pivot_level(idx)

        # Keep the most reinforced clusters (deterministic ordering)
        self._supports = sorted(self._supports, key=lambda x: (-x[1], x[0]))[: self.max_levels]
        self._resistances = sorted(self._resistances, key=lambda x: (-x[1], x[0]))[: self.max_levels]

    def evaluate(self, px: Decimal, last_closed: Optional[Candle]) -> StructureResult:
        """
        Evaluate structure relative to current price.
        `last_closed` is used for rejection detection (wick/body) and retest classification.

        Returns stable outputs:
          - Never returns None
          - reasons always non-empty
        """
        px = _d(px, "0")
        out = StructureResult()
        reasons: List[str] = []

        if px <= 0:
            out.reasons = "STRUCT:px<=0"
            return out

        # If no levels exist yet, be explicit (no silent None)
        if not self._supports:
            reasons.append("no_support_levels")
        if not self._resistances:
            reasons.append("no_resistance_levels")

        sup = self._nearest_level(px, self._supports, kind="SUP")
        res = self._nearest_level(px, self._resistances, kind="RES")

        # --- Nearest support
        if sup is not None and sup > 0:
            out.nearest_support = sup
            ds = _pct_dist(px, sup)  # (px - sup)/sup
            out.dist_support = ds
            if ds is not None and abs(ds) <= self.near_pct:
                out.near_support = True
                reasons.append(f"near_sup(d={ds:.6f})")
        elif self._supports:
            # we had support clusters, but none below px
            reasons.append("no_support_below_px")

        # --- Nearest resistance
        if res is not None and res > 0:
            out.nearest_resistance = res
            dr = (res - px) / res  # (res - px)/res
            out.dist_resistance = dr
            if dr is not None and abs(dr) <= self.near_pct:
                out.near_resistance = True
                reasons.append(f"near_res(d={dr:.6f})")
        elif self._resistances:
            # we had resistance clusters, but none above px
            reasons.append("no_resistance_above_px")

        # --- Break detection
        # Rule: break must be beyond level by break_pct (buffer)
        if res is not None and res > 0 and px > (res * (Decimal("1") + self.break_pct)):
            out.broke_up = True
            reasons.append("broke_up")
            self._mark_break("UP", res)

        if sup is not None and sup > 0 and px < (sup * (Decimal("1") - self.break_pct)):
            out.broke_down = True
            reasons.append("broke_down")
            self._mark_break("DOWN", sup)

        # --- Retest classification
        retest_reason = self._classify_retest(last_closed)
        if retest_reason == "retest_ok":
            out.retest_ok = True
            bars_since = self._bars_since_break() or 0
            reasons.append(f"retest_ok(bars={bars_since})")
        elif retest_reason == "failed_retest":
            out.failed_retest = True
            bars_since = self._bars_since_break() or 0
            reasons.append(f"failed_retest(bars={bars_since})")

        # --- Rejection flags
        if self.use_rejection and last_closed is not None:
            rej_res, rej_sup, rej_reason = self._rejection_flags(
                last_closed,
                out.nearest_support,
                out.nearest_resistance,
            )
            out.rejection_at_res = bool(rej_res)
            out.rejection_at_sup = bool(rej_sup)
            if rej_reason:
                reasons.append(rej_reason)

        # Always provide a reason string
        out.reasons = "STRUCT:" + (";".join(reasons) if reasons else "ok")
        return out

    # -------------------------
    # Internal helpers
    # -------------------------

    def _maybe_add_pivot_level(self, idx: int):
        """
        Confirm pivot at candles[idx] using left/right window.
        Deterministic rule:
          - pivot_high requires unique max(high) in window
          - pivot_low requires unique min(low) in window
        """
        c = self._candles[idx]
        window = self._candles[idx - self.left: idx + self.right + 1]
        if len(window) != (self.left + self.right + 1):
            return

        highs = [_d(w.high) for w in window]
        lows = [_d(w.low) for w in window]

        ch = _d(c.high)
        cl = _d(c.low)

        pivot_high = (ch == max(highs)) and (highs.count(ch) == 1)
        pivot_low = (cl == min(lows)) and (lows.count(cl) == 1)

        if pivot_high and ch > 0:
            self._resistances = self._cluster_add(self._resistances, ch, kind="RES")

        if pivot_low and cl > 0:
            self._supports = self._cluster_add(self._supports, cl, kind="SUP")

    def _cluster_add(self, clusters: List[Tuple[Decimal, int]], level: Decimal, *, kind: str) -> List[Tuple[Decimal, int]]:
        """
        Merge `level` into existing cluster if within cluster_pct; else add new.
        Cluster represented as (price, count). Weighted average on merge.
        Deterministic: first cluster within tolerance wins.
        """
        if level is None or level <= 0:
            return clusters

        tol = self.cluster_pct
        for i, (p, cnt) in enumerate(clusters):
            if p <= 0:
                continue
            dist = _abs_frac_dist(level, p)
            if dist is not None and dist <= tol:
                new_cnt = int(cnt) + 1
                new_p = (p * Decimal(cnt) + level) / Decimal(new_cnt)
                clusters[i] = (new_p, new_cnt)
                return clusters

        clusters.append((level, 1))
        return clusters

    def _nearest_level(self, px: Decimal, clusters: List[Tuple[Decimal, int]], *, kind: str) -> Optional[Decimal]:
        """
        SUP: nearest level <= px (max below)
        RES: nearest level >= px (min above)
        """
        if not clusters:
            return None

        if kind == "SUP":
            candidates = [p for (p, _cnt) in clusters if p > 0 and p <= px]
            return max(candidates) if candidates else None

        candidates = [p for (p, _cnt) in clusters if p > 0 and p >= px]
        return min(candidates) if candidates else None

    def _mark_break(self, direction: str, level: Decimal):
        """
        Anchor break to the most recent closed candle index.
        Deterministic: last break overwrites previous break state.
        """
        self._last_break_dir = str(direction).upper()
        self._last_break_level = _d(level)
        self._last_break_bar_index = len(self._candles) - 1

    def _bars_since_break(self) -> Optional[int]:
        if self._last_break_bar_index is None:
            return None
        return max(0, (len(self._candles) - 1) - int(self._last_break_bar_index))

    def _classify_retest(self, last_closed: Optional[Candle]) -> str:
        """
        Returns:
          - "retest_ok"
          - "failed_retest"
          - "" (no retest signal / out of window / no break recorded)
        """
        if (
            self._last_break_dir is None
            or self._last_break_level is None
            or self._last_break_bar_index is None
            or last_closed is None
        ):
            return ""

        bars_since = self._bars_since_break()
        if bars_since is None or bars_since > self.retest_max_bars:
            return ""

        lvl = self._last_break_level
        if lvl <= 0:
            return ""

        # Pull candle values
        try:
            hi = _d(last_closed.high)
            lo = _d(last_closed.low)
            cl = _d(last_closed.close)
        except Exception:
            return ""

        # Define "touch" band using near_pct around the level
        up_touch = lvl * (Decimal("1") + self.near_pct)
        dn_touch = lvl * (Decimal("1") - self.near_pct)

        if self._last_break_dir == "UP":
            # retest: candle low touches near level, and closes >= level (holds)
            touched = lo <= up_touch
            held = cl >= lvl
            if touched and held:
                return "retest_ok"
            if touched and (not held):
                return "failed_retest"
            return ""

        if self._last_break_dir == "DOWN":
            # retest: candle high touches near level, and closes <= level (holds)
            touched = hi >= dn_touch
            held = cl <= lvl
            if touched and held:
                return "retest_ok"
            if touched and (not held):
                return "failed_retest"
            return ""

        return ""

    def _rejection_flags(
        self,
        c: Candle,
        support: Optional[Decimal],
        resistance: Optional[Decimal],
    ) -> Tuple[bool, bool, str]:
        """
        Wick/body rejection near levels.

        Rejection at resistance:
          - close near resistance (within near_pct)
          - small body (<= reject_body_max_pct of close)
          - upper wick >= reject_wick_ratio * body

        Rejection at support:
          - close near support (within near_pct)
          - small body (<= reject_body_max_pct of close)
          - lower wick >= reject_wick_ratio * body
        """
        o = _d(c.open)
        h = _d(c.high)
        l = _d(c.low)
        cl = _d(c.close)

        body = abs(cl - o)
        body_for_ratio = body if body > Decimal("0") else Decimal("0.00000001")

        upper_wick = h - max(o, cl)
        lower_wick = min(o, cl) - l

        body_frac = (body / cl) if cl > 0 else Decimal("0")

        rej_res = False
        rej_sup = False

        # Rejection at resistance
        if resistance is not None and resistance > 0:
            near_res = _abs_frac_dist(cl, resistance)
            if near_res is not None and near_res <= self.near_pct:
                if body_frac <= self.reject_body_max_pct:
                    if upper_wick >= (self.reject_wick_ratio * body_for_ratio):
                        rej_res = True

        # Rejection at support
        if support is not None and support > 0:
            near_sup = _abs_frac_dist(cl, support)
            if near_sup is not None and near_sup <= self.near_pct:
                if body_frac <= self.reject_body_max_pct:
                    if lower_wick >= (self.reject_wick_ratio * body_for_ratio):
                        rej_sup = True

        if rej_res and rej_sup:
            return True, True, "reject_both"
        if rej_res:
            return True, False, "reject_res"
        if rej_sup:
            return False, True, "reject_sup"
        return False, False, ""
