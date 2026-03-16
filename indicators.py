from decimal import Decimal, getcontext
from typing import Optional, List
from collections import deque
import math

getcontext().prec = 28


def _to_float(v) -> float:
    """Convert Decimal/str/float to float efficiently."""
    if isinstance(v, float):
        return v
    return float(v)


def _to_decimal(v) -> Decimal:
    """Convert float back to Decimal at API boundary."""
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


class IndicatorEngine:
    """
    Phase 4–ready indicator engine.

    Performance-optimised: uses float internally for all computation,
    returns Decimal at API boundaries for backward compatibility.
    Uses incremental running totals for SMA (O(1) per tick instead of O(n)).

    Backward compatible:
      - push_close
      - sma
      - fast_slow_spread_bps

    New:
      - log_return()
      - volatility(n)
      - atr_proxy(n)
      - zscore(n)
      - returns_series(n)
    """

    def __init__(self, maxlen: int):
        self.maxlen = int(maxlen)
        # Float-based ring buffer for fast math
        self._closes: deque = deque(maxlen=self.maxlen)
        # Incremental SMA running sums: {window_size: running_sum}
        self._sma_sums: dict = {}
        self._sma_windows: dict = {}  # {window_size: deque of floats}

    @property
    def closes(self) -> List[Decimal]:
        """Backward-compat: return Decimal list (only used for len checks)."""
        return [Decimal(str(c)) for c in self._closes]

    @closes.setter
    def closes(self, value):
        """Backward-compat setter — convert to float deque."""
        self._closes = deque((_to_float(v) for v in value), maxlen=self.maxlen)
        # Invalidate running sums since buffer changed
        self._sma_sums.clear()
        self._sma_windows.clear()

    def push_close(self, close) -> None:
        val = _to_float(close)
        self._closes.append(val)

        # Update all active incremental SMA windows
        for n, win in self._sma_windows.items():
            if len(win) >= n:
                # Remove oldest value from sum
                self._sma_sums[n] -= win[0]
            win.append(val)
            self._sma_sums[n] += val

    def _ensure_sma_window(self, n: int) -> None:
        """Lazily initialise incremental SMA tracking for window size n."""
        if n in self._sma_windows:
            return
        win = deque(maxlen=n)
        s = 0.0
        # Populate from existing closes
        for c in list(self._closes)[-n:]:
            win.append(c)
            s += c
        self._sma_windows[n] = win
        self._sma_sums[n] = s

    def sma(self, n: int) -> Optional[Decimal]:
        n = int(n)
        if n <= 0 or len(self._closes) < n:
            return None
        self._ensure_sma_window(n)
        result = self._sma_sums[n] / n
        return Decimal(str(result))

    def fast_slow_spread_bps(
        self,
        ma_fast: Optional[Decimal],
        ma_slow: Optional[Decimal],
    ) -> Optional[Decimal]:
        if ma_fast is None or ma_slow is None or ma_slow == 0:
            return None
        return (abs(ma_fast - ma_slow) / ma_slow) * Decimal("10000")

    # =========================
    # Phase 4 additions
    # =========================

    def log_return(self) -> Optional[Decimal]:
        """
        Natural log return of last close vs previous close.
        """
        if len(self._closes) < 2:
            return None
        prev = self._closes[-2]
        cur = self._closes[-1]
        if prev <= 0 or cur <= 0:
            return None
        return Decimal(str(math.log(cur / prev)))

    def returns_series(self, n: int) -> Optional[List[Decimal]]:
        """
        Returns last n log-returns as Decimals.
        """
        n = int(n)
        if len(self._closes) < n + 1:
            return None

        closes = self._closes
        out: List[Decimal] = []
        start = len(closes) - n - 1
        for i in range(start, start + n):
            a = closes[i]
            b = closes[i + 1]
            if a <= 0 or b <= 0:
                return None
            out.append(Decimal(str(math.log(b / a))))
        return out

    def _returns_series_float(self, n: int) -> Optional[List[float]]:
        """Internal float version for volatility computation."""
        if len(self._closes) < n + 1:
            return None

        closes = self._closes
        out: List[float] = []
        start = len(closes) - n - 1
        for i in range(start, start + n):
            a = closes[i]
            b = closes[i + 1]
            if a <= 0 or b <= 0:
                return None
            out.append(math.log(b / a))
        return out

    def volatility(self, n: int) -> Optional[Decimal]:
        """
        Rolling std-dev of log returns (fractional, e.g. 0.004 = 0.4%)
        """
        rets = self._returns_series_float(n)
        if not rets:
            return None

        count = len(rets)
        mean = sum(rets) / count
        var = sum((r - mean) ** 2 for r in rets) / count

        if var <= 0:
            return None

        return Decimal(str(math.sqrt(var)))

    def atr_proxy(self, n: int) -> Optional[Decimal]:
        """
        Close-to-close ATR proxy (no highs/lows needed).
        Absolute move, not normalized.
        """
        n = int(n)
        if len(self._closes) < n + 1:
            return None

        closes = self._closes
        total = 0.0
        start = len(closes) - n - 1
        for i in range(start, start + n):
            total += abs(closes[i + 1] - closes[i])

        return Decimal(str(total / n))

    def zscore(self, n: int) -> Optional[Decimal]:
        """
        Z-score of the most recent close vs rolling mean/std.
        """
        n = int(n)
        if len(self._closes) < n:
            return None

        # Use float math internally
        window = list(self._closes)[-n:]
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n

        if var <= 0:
            return None

        std = math.sqrt(var)
        if std == 0:
            return None

        return Decimal(str((self._closes[-1] - mean) / std))

    # =========================
    # Future Phase 5 helpers
    # =========================

    def atr_norm(self, n: int) -> Optional[Decimal]:
        """
        ATR normalized by price (fraction).
        Used by liquidity filters / exit intelligence.
        """
        atr = self.atr_proxy(n)
        if atr is None:
            return None
        px = self._closes[-1]
        if px <= 0:
            return None
        return Decimal(str(float(atr) / px))