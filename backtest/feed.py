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


def _normalize_tick_mode(mode: Optional[str]) -> str:
    s = str(mode or "close").strip().lower().replace("-", "_")
    if s in ("intrabar", "ohlc", "ohlc_path", "path"):
        return "intrabar"
    return "close"


def _normalize_intrabar_order(order: Optional[str]) -> str:
    s = str(order or "auto").strip().lower().replace("-", "").replace("_", "")
    if s in ("ohlc", "olhc"):
        return s
    return "auto"


def _intrabar_path_prices(
    *,
    open_px: Decimal,
    high_px: Decimal,
    low_px: Decimal,
    close_px: Decimal,
    order: str,
) -> List[Decimal]:
    if order == "ohlc":
        raw = [open_px, high_px, low_px, close_px]
    elif order == "olhc":
        raw = [open_px, low_px, high_px, close_px]
    elif close_px >= open_px:
        raw = [open_px, low_px, high_px, close_px]
    else:
        raw = [open_px, high_px, low_px, close_px]

    out: List[Decimal] = []
    for px in raw:
        px_dec = _d(px, None)
        if px_dec is None or px_dec <= 0:
            continue
        if not out or px_dec != out[-1]:
            out.append(px_dec)

    return out if out else [close_px]


def _intrabar_epochs(start_s: int, candle_seconds: int, count: int) -> List[int]:
    if count <= 1:
        return [int(start_s + max(0, candle_seconds - 1))]

    span = max(0, int(candle_seconds) - 1)
    out: List[int] = []
    for idx in range(count):
        if idx == count - 1:
            epoch = int(start_s + span)
        else:
            epoch = int(start_s + ((span * idx) // (count - 1)))
        if out and epoch < out[-1]:
            epoch = out[-1]
        out.append(epoch)
    return out


def _scaled_total_volume(vol_total: Optional[Decimal], *, idx: int, count: int) -> Optional[Decimal]:
    if vol_total is None or vol_total <= 0 or count <= 0:
        return None
    if idx >= count - 1:
        return vol_total
    try:
        return vol_total * Decimal(idx + 1) / Decimal(count)
    except Exception:
        return vol_total


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
    tick_mode: str = "close",
    intrabar_order: str = "auto",
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

    Backtest realism:
      - tick_mode="close" emits one close tick per candle (legacy behavior).
      - tick_mode="intrabar" emits a deterministic OHLC path inside each candle.
        AUTO path uses OLHC for green candles and OHLC for red candles.
    """
    cs = int(candle_seconds) if candle_seconds and int(candle_seconds) > 0 else 60
    mode = _normalize_tick_mode(tick_mode)
    path_order = _normalize_intrabar_order(intrabar_order)

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

        if mode != "intrabar":
            row: Tuple[Any, ...] = (start_s, close, vol, bid, ask, atr_norm)
            yield from ticks_from_close_series(
                [row],
                as_candle_close=as_candle_close,
                candle_seconds=cs,
                synth_spread_bps=synth_spread_bps,
            )
            continue

        open_px = _d(getattr(c, "open", close), None)
        high_px = _d(getattr(c, "high", close), None)
        low_px = _d(getattr(c, "low", close), None)
        close_px = _d(close, None)
        vol_total = _d(vol, None)
        bid_dec = _d(bid, None)
        ask_dec = _d(ask, None)
        atr_dec = _d(atr_norm, None)

        if (
            open_px is None
            or high_px is None
            or low_px is None
            or close_px is None
            or open_px <= 0
            or high_px <= 0
            or low_px <= 0
            or close_px <= 0
        ):
            row = (start_s, close, vol, bid, ask, atr_norm)
            yield from ticks_from_close_series(
                [row],
                as_candle_close=as_candle_close,
                candle_seconds=cs,
                synth_spread_bps=synth_spread_bps,
            )
            continue

        path_prices = _intrabar_path_prices(
            open_px=open_px,
            high_px=high_px,
            low_px=low_px,
            close_px=close_px,
            order=path_order,
        )
        epochs = _intrabar_epochs(start_s, cs, len(path_prices))

        for idx, (tick_epoch, tick_px) in enumerate(zip(epochs, path_prices)):
            tick_vol = _scaled_total_volume(vol_total, idx=idx, count=len(path_prices))
            tick_bid = bid_dec
            tick_ask = ask_dec

            if (tick_bid is None or tick_ask is None) and synth_spread_bps is not None:
                b2, a2 = _synth_bid_ask(tick_px, spread_bps=synth_spread_bps)
                tick_bid = tick_bid if tick_bid is not None else b2
                tick_ask = tick_ask if tick_ask is not None else a2

            yield PriceTick(
                ts=_iso_utc(tick_epoch),
                px=tick_px,
                epoch=int(tick_epoch),
                bid=tick_bid,
                ask=tick_ask,
                vol_1m=tick_vol,
                atr_norm=atr_dec,
            )
