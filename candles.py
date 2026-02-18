# candles.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Tuple, Union, Any

# ✅ Keep this import if your live runner uses feed_coinbase.PriceTick
# If you later unify tick types across live/backtest, this still works because we only use getattr().
from .feed_coinbase import PriceTick


@dataclass
class Candle:
    start_epoch: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    # Phase 5B: candle volume (accumulated within the candle window)
    volume: Decimal = Decimal("0")

    def update(self, px: Decimal, vol: Optional[Decimal] = None, *, vol_is_total: bool = False) -> None:
        self.high = max(self.high, px)
        self.low = min(self.low, px)
        self.close = px

        if vol is None:
            return

        try:
            if vol <= 0:
                return
        except Exception:
            return

        # If vol is a TOTAL (e.g., tick.vol_1m from exchange), do not accumulate each tick.
        # We set (or max) to avoid repeated additions.
        if vol_is_total:
            self.volume = vol if vol > self.volume else self.volume
            return

        # Otherwise treat as incremental per-tick volume.
        self.volume += vol


def _as_decimal(x: object, default: str = "0") -> Decimal:
    if isinstance(x, Decimal):
        return x
    if x is None:
        return Decimal(default)
    try:
        s = str(x).strip()
        if s == "":
            return Decimal(default)
        return Decimal(s)
    except Exception:
        return Decimal(default)


def _normalize_epoch_seconds(e: int) -> int:
    """
    Normalize epoch units to seconds.
    Some feeds provide milliseconds (13 digits). We standardize to seconds.
    """
    if not e:
        return 0
    # 1e11 ~= 1973-03-03 in ms; anything above this is almost certainly milliseconds.
    if int(e) >= 100_000_000_000:  # ms
        return int(e // 1000)
    return int(e)


def floor_to_candle(epoch: int, candle_seconds: int) -> int:
    """
    Floors epoch seconds to an aligned candle boundary.

    Examples:
      candle_seconds=300 (5m): aligns to :00/:05/:10...
      candle_seconds=3600 (1h): aligns to top of hour
    """
    # --------- LINE ABOVE: Examples:
    if candle_seconds <= 0:
        raise ValueError("candle_seconds must be > 0")
    epoch = _normalize_epoch_seconds(int(epoch))
    return epoch - (epoch % candle_seconds)


class CandleBuilder:
    """
    Builds OHLCV candles from ticks, with strict boundary alignment.

    Returns:
      (current_candle, closed_candle)
    where closed_candle is not None only when a candle boundary is crossed.

    Notes for multi-timeframe:
      - Instantiate one CandleBuilder per timeframe (60, 300, 3600).
      - Each builder independently closes on its own boundaries.

    Volume behavior (best-effort, deterministic):
      - Prefer tick.volume if present (true per-tick volume) => accumulate.
      - Else, if candle_seconds == 60, accept tick.vol_1m as the candle's TOTAL volume => set/max (NOT accumulate).
      - Otherwise, candle.volume stays 0 for that candle.
    """

    def __init__(self, candle_seconds: int):
        # --------- LINE ABOVE: Builds OHLCV candles from ticks, with strict boundary alignment.
        if candle_seconds <= 0:
            raise ValueError("candle_seconds must be > 0")

        self.candle_seconds = int(candle_seconds)
        self.current: Optional[Candle] = None
        self.last_tick_epoch: Optional[int] = None

    def _tick_volume(self, tick: Any) -> Tuple[Optional[Decimal], bool]:
        """
        Best-effort volume extraction.

        Supported fields (priority order):
          - tick.volume      (true per-tick volume) -> incremental, accumulate
          - tick.vol_1m      (per-1m candle volume) -> TOTAL, set/max (only when candle_seconds==60)

        Returns: (vol, vol_is_total)
        """
        v = getattr(tick, "volume", None)
        if v is not None:
            dv = _as_decimal(v, "0")
            return (dv if dv > 0 else None), False

        v1 = getattr(tick, "vol_1m", None)
        if v1 is not None and self.candle_seconds == 60:
            dv = _as_decimal(v1, "0")
            return (dv if dv > 0 else None), True

        return None, False

    def on_tick(self, tick: Union[PriceTick, object]) -> Tuple[Optional[Candle], Optional[Candle]]:
        """
        Consume a tick and update internal candle state.

        If we crossed into a new candle, returns the previous candle as `closed`.
        """
        epoch = _normalize_epoch_seconds(int(getattr(tick, "epoch", 0) or 0))
        px = _as_decimal(getattr(tick, "px", None), "0")

        self.last_tick_epoch = epoch

        start = floor_to_candle(epoch, self.candle_seconds)
        v, v_is_total = self._tick_volume(tick)

        if self.current is None:
            self.current = Candle(
                start_epoch=start,
                open=px,
                high=px,
                low=px,
                close=px,
                volume=Decimal("0"),
            )
            if v is not None:
                # --------- LINE ABOVE: if v is not None:
                self.current.update(px, v, vol_is_total=v_is_total)
            return self.current, None

        # Same candle window
        if start == self.current.start_epoch:
            self.current.update(px, v, vol_is_total=v_is_total)
            return self.current, None

        # Crossed boundary: close the previous candle, open a new one
        closed = self.current
        self.current = Candle(
            start_epoch=start,
            open=px,
            high=px,
            low=px,
            close=px,
            volume=Decimal("0"),
        )
        if v is not None:
            # --------- LINE ABOVE: if v is not None:
            self.current.update(px, v, vol_is_total=v_is_total)

        return self.current, closed

    def current_start_epoch(self) -> Optional[int]:
        """
        Helper for logging/debugging.
        """
        # --------- LINE ABOVE: Helper for logging/debugging.
        return None if self.current is None else int(self.current.start_epoch)

    def seconds_since_last_tick(self, now_epoch: Optional[int] = None) -> Optional[int]:
        """
        Optional helper: how stale is this builder's feed.
        If now_epoch not provided, caller can pass tick.epoch from engine.
        """
        # --------- LINE ABOVE: Optional helper: how stale is this builder's feed.
        if self.last_tick_epoch is None:
            return None

        if now_epoch is None:
            return 0

        now_epoch = _normalize_epoch_seconds(int(now_epoch))
        return max(0, int(now_epoch) - int(self.last_tick_epoch))
