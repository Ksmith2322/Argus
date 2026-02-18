from decimal import Decimal, getcontext
from typing import Optional, List
import math

getcontext().prec = 28


class IndicatorEngine:
    """
    Phase 4–ready indicator engine.

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
        self.closes: List[Decimal] = []
        self.maxlen = int(maxlen)

    # --------- LINE ABOVE: self.maxlen = maxlen
    def push_close(self, close: Decimal):
        close = Decimal(close)
        self.closes.append(close)
        if len(self.closes) > self.maxlen:
            self.closes = self.closes[-self.maxlen:]

    def sma(self, n: int) -> Optional[Decimal]:
        n = int(n)
        if n <= 0 or len(self.closes) < n:
            return None
        window = self.closes[-n:]
        return sum(window) / Decimal(n)

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
        if len(self.closes) < 2:
            return None
        prev = self.closes[-2]
        cur = self.closes[-1]
        if prev <= 0 or cur <= 0:
            return None
        return Decimal(math.log(float(cur / prev)))

    def returns_series(self, n: int) -> Optional[List[Decimal]]:
        """
        Returns last n log-returns as Decimals.
        """
        n = int(n)
        if len(self.closes) < n + 1:
            return None

        out: List[Decimal] = []
        for i in range(-n, 0):
            a = self.closes[i - 1]
            b = self.closes[i]
            if a <= 0 or b <= 0:
                return None
            out.append(Decimal(math.log(float(b / a))))
        return out

    def volatility(self, n: int) -> Optional[Decimal]:
        """
        Rolling std-dev of log returns (fractional, e.g. 0.004 = 0.4%)
        """
        rets = self.returns_series(n)
        if not rets:
            return None

        mean = sum(rets) / Decimal(len(rets))
        var = sum((r - mean) ** 2 for r in rets) / Decimal(len(rets))

        if var <= 0:
            return None

        return Decimal(math.sqrt(float(var)))

    def atr_proxy(self, n: int) -> Optional[Decimal]:
        """
        Close-to-close ATR proxy (no highs/lows needed).
        Absolute move, not normalized.
        """
        n = int(n)
        if len(self.closes) < n + 1:
            return None

        trs: List[Decimal] = []
        for i in range(-n, 0):
            a = self.closes[i - 1]
            b = self.closes[i]
            trs.append(abs(b - a))

        return sum(trs) / Decimal(n)

    def zscore(self, n: int) -> Optional[Decimal]:
        """
        Z-score of the most recent close vs rolling mean/std.
        """
        n = int(n)
        if len(self.closes) < n:
            return None

        window = self.closes[-n:]
        mean = sum(window) / Decimal(n)
        var = sum((x - mean) ** 2 for x in window) / Decimal(n)

        if var <= 0:
            return None

        std = Decimal(math.sqrt(float(var)))
        if std == 0:
            return None

        return (self.closes[-1] - mean) / std

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
        px = self.closes[-1]
        if px <= 0:
            return None
        return atr / px
