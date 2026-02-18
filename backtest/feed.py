# backtest/feed.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, List, Optional, Tuple


# Canonical tick shape (compatible with candles.py + engine.py expectations)
# Phase 5B parity:
#   - bid/ask (real if present, else deterministic synthetic model)
#   - vol_1m (from candle volume or series)
#   - atr_norm passthrough (if your backtest dataset provides it)
@dataclass(frozen=True)
class PriceTick:
    ts: str
    px: Decimal
    epoch: int

    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    vol_1m: Optional[Decimal] = None
    atr_norm: Optional[Decimal] = None


def _iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()


def _normalize_epoch_seconds(e: int) -> int:
    """
    Normalize epoch units to seconds.
    If epoch is milliseconds (13 digits), convert to seconds.
    """
    if not e:
        return 0
    if int(e) >= 100_000_000_000:  # ms
        return int(e // 1000)
    return int(e)


def _d(x: Any, default: Optional[Decimal] = None) -> Optional[Decimal]:
    if x is None:
        return default
    if isinstance(x, Decimal):
        return x
    try:
        s = str(x).strip()
        if s == "":
            return default
        return Decimal(s)
    except Exception:
        return default


def _synth_bid_ask(
    px: Decimal,
    *,
    spread_bps: Optional[Decimal],
) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    """
    BACKTEST SPREAD MODEL (deterministic):
      bid/ask synthesized from px using a FIXED spread in bps.

    This is intentionally simple + reproducible:
      mid = px
      half_spread_frac = (spread_bps / 10_000) / 2
      bid = px * (1 - half_spread_frac)
      ask = px * (1 + half_spread_frac)

    If spread_bps is None or <= 0 => returns (None, None) and LiquidityEngine sees spread=NA.
    """
    if px is None or px <= 0:
        return None, None

    sb = _d(spread_bps, None)
    if sb is None or sb <= 0:
        return None, None

    try:
        half = (sb / Decimal("10000")) / Decimal("2")
        bid = px * (Decimal("1") - half)
        ask = px * (Decimal("1") + half)
        if bid <= 0 or ask <= 0 or ask < bid:
            return None, None
        return bid, ask
    except Exception:
        return None, None


def ticks_from_close_series(
    close_series: Iterable[Tuple[Any, ...]],
    *,
    as_candle_close: bool = True,
    candle_seconds: int = 60,
    synth_spread_bps: Optional[Decimal] = None,
) -> Iterator[PriceTick]:
    """
    Convert candle tuples into a tick stream.

    Accepted tuple shapes (positional, to keep it fast + dumb):
      - (start_epoch, close_px)
      - (start_epoch, close_px, vol_1m)
      - (start_epoch, close_px, vol_1m, bid, ask)
      - (start_epoch, close_px, vol_1m, bid, ask, atr_norm)

    If as_candle_close=True:
      - tick.epoch is candle_start + candle_seconds - 1
        (so CandleBuilder closes cleanly at the boundary).
    Else:
      - tick.epoch is exactly provided epoch.

    Phase 5B parity:
      - If bid/ask missing and synth_spread_bps provided, we synthesize bid/ask via the backtest spread model.
    """
    cs = int(candle_seconds) if candle_seconds and int(candle_seconds) > 0 else 60

    for row in close_series:
        if not row:
            continue

        # --------- LINE ABOVE: for row in close_series:
        candle_start_epoch = row[0] if len(row) > 0 else None
        close_px = row[1] if len(row) > 1 else None
        vol_1m = row[2] if len(row) > 2 else None
        bid_in = row[3] if len(row) > 3 else None
        ask_in = row[4] if len(row) > 4 else None
        atr_in = row[5] if len(row) > 5 else None

        if candle_start_epoch is None or close_px is None:
            continue

        start_s = _normalize_epoch_seconds(int(candle_start_epoch))
        if start_s <= 0:
            continue

        e = start_s + cs - 1 if as_candle_close else start_s

        px_dec = _d(close_px, None)
        if px_dec is None or px_dec <= 0:
            continue

        v_dec = _d(vol_1m, None)
        bid_dec = _d(bid_in, None)
        ask_dec = _d(ask_in, None)
        atr_dec = _d(atr_in, None)

        # --------- LINE ABOVE: atr_dec = _d(atr_in, None)
        # If bid/ask missing, optionally synthesize deterministically.
        if (bid_dec is None or ask_dec is None) and synth_spread_bps is not None:
            b2, a2 = _synth_bid_ask(px_dec, spread_bps=synth_spread_bps)
            bid_dec = bid_dec if bid_dec is not None else b2
            ask_dec = ask_dec if ask_dec is not None else a2

        yield PriceTick(
            ts=_iso_utc(e),
            px=px_dec,
            epoch=int(e),
            bid=bid_dec,
            ask=ask_dec,
            vol_1m=v_dec,
            atr_norm=atr_dec,
        )


def ticks_from_candles(
    candles: List[Any],
    *,
    candle_seconds: int = 60,
    as_candle_close: bool = True,
    synth_spread_bps: Optional[Decimal] = None,
) -> Iterator[PriceTick]:
    """
    Takes a list of CandleRow-like objects with:
      - .epoch
      - .close
      - optionally .volume (mapped to vol_1m)
      - optionally .bid / .ask
      - optionally .atr_norm

    Phase 5B parity:
      - Emits bid/ask if present; else can synthesize if synth_spread_bps is provided.
      - Emits volume as vol_1m if present.
      - Emits atr_norm if present.
    """
    cs = int(candle_seconds) if candle_seconds and int(candle_seconds) > 0 else 60

    for c in candles:
        epoch = getattr(c, "epoch", None)
        close = getattr(c, "close", None)
        if epoch is None or close is None:
            continue

        start_s = _normalize_epoch_seconds(int(epoch))
        if start_s <= 0:
            continue

        vol = getattr(c, "volume", None)
        bid = getattr(c, "bid", None)
        ask = getattr(c, "ask", None)
        atr_norm = getattr(c, "atr_norm", None)

        # --------- LINE ABOVE: atr_norm = getattr(c, "atr_norm", None)
        # Canonical positional row: (start_epoch, close, vol_1m, bid, ask, atr_norm)
        row: Tuple[Any, ...] = (start_s, close, vol, bid, ask, atr_norm)

        yield from ticks_from_close_series(
            [row],
            as_candle_close=as_candle_close,
            candle_seconds=cs,
            synth_spread_bps=synth_spread_bps,
        )
