# backtest/loader.py
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class CandleRow:
    """
    Minimal OHLCV candle representation for backtests.
    Epoch is candle START epoch (aligned to timeframe), in seconds.
    """
    epoch: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Optional[Decimal] = None


def _to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    s = str(x).strip()
    if s == "":
        return Decimal("0")
    return Decimal(s)


def _normalize_epoch_seconds(e: int) -> int:
    "Normalize epoch to seconds. If epoch is milliseconds (13 digits), convert to seconds."
    if not e:
        return 0
    if int(e) >= 100_000_000_000:
        return int(e // 1000)
    return int(e)


def _parse_epoch(x: Any) -> int:
    s = str(x).strip()
    if not s:
        raise ValueError("Empty epoch/time value")

    # unix epoch seconds or ms
    if s.isdigit():
        return _normalize_epoch_seconds(int(s))

    # ISO timestamp
    # e.g. 2026-01-23T01:58:19Z or 2026-01-23T01:58:19+00:00
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x)))
        except Exception:
            return default


def _lower_map(header_raw: List[str]) -> Dict[str, int]:
    """
    Header -> index map, normalized.
    - lowercased
    - stripped
    """
    m: Dict[str, int] = {}
    for i, h in enumerate(header_raw):
        k = str(h).strip().lower()
        if k and k not in m:
            m[k] = i
    return m


def _pick_index(hmap: Dict[str, int], *names: str) -> Optional[int]:
    for n in names:
        k = str(n).strip().lower()
        if k in hmap:
            return hmap[k]
    return None


def _is_header_row(row: List[str]) -> bool:
    """
    Heuristic: if row contains any non-numeric fieldnames typical of candles/logs.
    """
    if not row:
        return False
    joined = ",".join([str(x).strip().lower() for x in row])
    # common schema tokens
    tokens = (
        "epoch", "time", "timestamp", "date",
        "open", "high", "low", "close", "volume", "vol",
        "candle_start", "candle_start_1m",
        "candle_close", "candle_close_1m",
        "px", "price", "action", "next_poll",
    )
    return any(t in joined for t in tokens)


def load_candles_csv(
    path: str,
    *,
    format_hint: str = "auto",
    limit: Optional[int] = None,
) -> List[CandleRow]:
    """
    Loads candles from CSV, preserving OHLCV when available.

    Supported formats:

    1) "coinbase_exchange" (Coinbase Exchange candles):
       Columns: time, low, high, open, close, volume
       - 'time' is epoch seconds candle start (sometimes ISO)
       - order may be newest->oldest; we sort ascending
       - volume preserved

    2) "generic_ohlc":
       Columns include any of:
         epoch/time/timestamp/date, open, high, low, close, volume/vol
       - epoch can be unix seconds or ISO string
       - we sort ascending
       - volume preserved when present

    3) "close_only":
       Columns: epoch/time/timestamp/date, close (and optionally volume/vol)
       - open/high/low are filled with close
       - volume preserved when present

    4) "live_signals" (your bot logs ./logs/live_signals.csv):
       Columns include:
         candle_start_1m (preferred) OR candle_start
         candle_close_1m OR candle_close OR px/price
         optional candle_volume_1m / vol_1m / volume / vol
       - epoch uses candle_start_* (start)
       - close uses candle_close_* else px/price
       - OHLC filled with close
       - volume preserved when present (helps LiquidityEngine baseline in backtests)

    format_hint:
      - "auto" detects based on headers
      - else explicitly set:
          "coinbase_exchange" | "generic_ohlc" | "close_only" | "live_signals"

    Aliases:
      "coinbase"/"cb"/"coinbase-pro" -> "coinbase_exchange"
    """
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r is not None]

    if not rows:
        return []

    header_raw = rows[0]
    has_header = _is_header_row(header_raw)
    body = rows[1:] if has_header else rows

    hmap = _lower_map(header_raw) if has_header else {}

    def detect() -> str:
        if has_header:
            # Recognize your bot's live_signals schema
            has_actionish = any(k in hmap for k in ("action_reason", "next_poll_s", "trend_ok_1m", "trend_ok", "action"))
            has_candle_start = any(k in hmap for k in ("candle_start_1m", "candle_start"))
            has_priceish = any(k in hmap for k in ("px", "price", "candle_close_1m", "candle_close"))
            if has_actionish and has_candle_start and has_priceish:
                return "live_signals"

            # Coinbase exchange export headers
            if all(k in hmap for k in ("time", "low", "high", "open", "close")):
                return "coinbase_exchange"

            # Generic OHLC headers
            if ("close" in hmap) and any(k in hmap for k in ("open", "high", "low")):
                return "generic_ohlc"

            # Close-only headers
            if ("close" in hmap) and any(k in hmap for k in ("epoch", "time", "timestamp", "date")):
                return "close_only"

        # Fallback by column count
        if body and len(body[0]) >= 6:
            return "coinbase_exchange"
        if body and len(body[0]) >= 5:
            return "generic_ohlc"
        return "close_only"

    # --------- LINE ABOVE: hint = (format_hint or "auto").strip().lower()
    hint = (format_hint or "auto").strip().lower()
    fmt = detect() if hint == "auto" else hint

    ALIASES = {
        "coinbase": "coinbase_exchange",
        "cb": "coinbase_exchange",
        "coinbase-pro": "coinbase_exchange",
        "coinbase_pro": "coinbase_exchange",
        "pro": "coinbase_exchange",
        "exchange": "coinbase_exchange",
        "coinbase-exchange": "coinbase_exchange",
    }
    fmt = ALIASES.get(fmt, fmt)

    out: List[CandleRow] = []

    # -------------------------
    # live_signals (bot logs)
    # -------------------------
    if fmt == "live_signals":
        if not has_header:
            raise ValueError("live_signals requires a header row (expected ./logs/live_signals.csv)")

        i_start = _pick_index(hmap, "candle_start_1m", "candle_start")
        i_close = _pick_index(hmap, "candle_close_1m", "candle_close")
        i_px = _pick_index(hmap, "px", "price")
        i_vol = _pick_index(hmap, "candle_volume_1m", "vol_1m", "volume_1m", "volume", "vol")

        if i_start is None:
            raise ValueError("live_signals requires candle_start_1m or candle_start column")
        if i_close is None and i_px is None:
            raise ValueError("live_signals requires candle_close_1m/candle_close or px/price column")

        for r in body:
            if not r or len(r) <= int(i_start):
                continue

            cs = (r[int(i_start)] or "").strip()
            if not cs:
                continue

            start_epoch = _normalize_epoch_seconds(_safe_int(cs, 0))
            if start_epoch <= 0:
                continue

            close_s = ""
            if i_close is not None and int(i_close) < len(r):
                close_s = (r[int(i_close)] or "").strip()
            if (not close_s) and i_px is not None and int(i_px) < len(r):
                close_s = (r[int(i_px)] or "").strip()
            if not close_s:
                continue

            try:
                close_px = _to_decimal(close_s)
            except Exception:
                continue

            vol_val: Optional[Decimal] = None
            if i_vol is not None and int(i_vol) < len(r):
                vs = (r[int(i_vol)] or "").strip()
                if vs != "":
                    try:
                        vol_val = _to_decimal(vs)
                    except Exception:
                        vol_val = None

            out.append(
                CandleRow(
                    epoch=int(start_epoch),
                    open=close_px,
                    high=close_px,
                    low=close_px,
                    close=close_px,
                    volume=vol_val,
                )
            )

    # -------------------------
    # Coinbase Exchange candles
    # -------------------------
    elif fmt == "coinbase_exchange":
        # Coinbase exchange: time, low, high, open, close, volume
        if has_header and "time" in hmap:
            i_time = _pick_index(hmap, "time")
            i_low = _pick_index(hmap, "low")
            i_high = _pick_index(hmap, "high")
            i_open = _pick_index(hmap, "open")
            i_close = _pick_index(hmap, "close")
            i_vol = _pick_index(hmap, "volume", "vol")

            if None in (i_time, i_low, i_high, i_open, i_close):
                raise ValueError("coinbase_exchange format requires time, low, high, open, close columns")

            need_max = max(int(i_time), int(i_low), int(i_high), int(i_open), int(i_close))
            for r in body:
                if not r or len(r) <= need_max:
                    continue

                epoch = _normalize_epoch_seconds(int(_parse_epoch(r[int(i_time)])))
                low = _to_decimal(r[int(i_low)])
                high = _to_decimal(r[int(i_high)])
                opn = _to_decimal(r[int(i_open)])
                clo = _to_decimal(r[int(i_close)])

                vol_val: Optional[Decimal] = None
                if i_vol is not None and int(i_vol) < len(r):
                    vs = (r[int(i_vol)] or "").strip()
                    if vs != "":
                        vol_val = _to_decimal(vs)

                out.append(CandleRow(epoch=int(epoch), open=opn, high=high, low=low, close=clo, volume=vol_val))

        else:
            # no header, assume: time, low, high, open, close, volume?
            for r in body:
                if len(r) < 5:
                    continue
                epoch = _normalize_epoch_seconds(int(_parse_epoch(r[0])))
                low = _to_decimal(r[1])
                high = _to_decimal(r[2])
                opn = _to_decimal(r[3])
                clo = _to_decimal(r[4])
                vol = _to_decimal(r[5]) if len(r) > 5 and (r[5] or "").strip() != "" else None
                out.append(CandleRow(epoch=int(epoch), open=opn, high=high, low=low, close=clo, volume=vol))

    # -------------------------
    # Generic OHLCV
    # -------------------------
    elif fmt == "generic_ohlc":
        i_epoch = _pick_index(hmap, "epoch", "time", "timestamp", "date")
        i_open = _pick_index(hmap, "open")
        i_high = _pick_index(hmap, "high")
        i_low = _pick_index(hmap, "low")
        i_close = _pick_index(hmap, "close")
        i_vol = _pick_index(hmap, "volume", "vol")

        if i_epoch is None or i_close is None:
            raise ValueError("generic_ohlc requires epoch/time/timestamp/date and close columns")

        need_max = max(int(i_epoch), int(i_close))
        for r in body:
            if not r or len(r) <= need_max:
                continue

            epoch = _normalize_epoch_seconds(int(_parse_epoch(r[int(i_epoch)])))
            clo = _to_decimal(r[int(i_close)])

            opn = clo
            if i_open is not None and int(i_open) < len(r):
                s = (r[int(i_open)] or "").strip()
                if s != "":
                    opn = _to_decimal(s)

            hig = clo
            if i_high is not None and int(i_high) < len(r):
                s = (r[int(i_high)] or "").strip()
                if s != "":
                    hig = _to_decimal(s)

            low = clo
            if i_low is not None and int(i_low) < len(r):
                s = (r[int(i_low)] or "").strip()
                if s != "":
                    low = _to_decimal(s)

            vol_val: Optional[Decimal] = None
            if i_vol is not None and int(i_vol) < len(r):
                s = (r[int(i_vol)] or "").strip()
                if s != "":
                    try:
                        vol_val = _to_decimal(s)
                    except Exception:
                        vol_val = None

            out.append(CandleRow(epoch=int(epoch), open=opn, high=hig, low=low, close=clo, volume=vol_val))

    # -------------------------
    # Close-only (optionally with volume)
    # -------------------------
    elif fmt == "close_only":
        if has_header:
            i_epoch = _pick_index(hmap, "epoch", "time", "timestamp", "date")
            i_close = _pick_index(hmap, "close")
            i_vol = _pick_index(hmap, "volume", "vol")

            if i_epoch is None or i_close is None:
                raise ValueError("close_only requires epoch/time/timestamp/date and close columns")

            need_max = max(int(i_epoch), int(i_close))
            for r in body:
                if not r or len(r) <= need_max:
                    continue

                epoch = _normalize_epoch_seconds(int(_parse_epoch(r[int(i_epoch)])))
                clo = _to_decimal(r[int(i_close)])

                vol_val: Optional[Decimal] = None
                if i_vol is not None and int(i_vol) < len(r):
                    s = (r[int(i_vol)] or "").strip()
                    if s != "":
                        try:
                            vol_val = _to_decimal(s)
                        except Exception:
                            vol_val = None

                out.append(CandleRow(epoch=int(epoch), open=clo, high=clo, low=clo, close=clo, volume=vol_val))

        else:
            # no header: expect epoch, close, [volume]
            for r in body:
                if len(r) < 2:
                    continue
                epoch = _normalize_epoch_seconds(int(_parse_epoch(r[0])))
                clo = _to_decimal(r[1])
                vol = _to_decimal(r[2]) if len(r) > 2 and (r[2] or "").strip() != "" else None
                out.append(CandleRow(epoch=int(epoch), open=clo, high=clo, low=clo, close=clo, volume=vol))

    else:
        raise ValueError(f"Unknown format_hint: {fmt}")

    # sort ascending by epoch
    out.sort(key=lambda c: int(c.epoch))

    # If limit provided, keep most recent N (after sorting)
    if limit is not None and int(limit) > 0:
        out = out[-int(limit):]

    return out


def candles_to_close_series(candles: List[CandleRow]) -> List[Tuple[int, Decimal]]:
    """
    Backward-compatible convenience: returns list of (epoch, close) ascending.

    NOTE:
      - This intentionally drops volume for legacy code paths.
      - Use candles_to_close_series_with_vol() for Phase 5B parity.
    """
    return [(int(c.epoch), c.close) for c in candles]


def candles_to_close_series_with_vol(candles: List[CandleRow]) -> List[Tuple[int, Decimal, Optional[Decimal]]]:
    """
    Phase 5B parity convenience: returns list of (epoch, close, volume) ascending.

    This is the one your backtest feed should use if you want:
      - vol_1m to exist in ticks
      - LiquidityEngine baseline to be meaningful in backtests
    """
    return [(int(c.epoch), c.close, c.volume) for c in candles]


def candles_to_ohlcv_series(
    candles: List[CandleRow],
) -> List[Tuple[int, Decimal, Decimal, Decimal, Decimal, Optional[Decimal]]]:
    """
    Convenience: returns list of (epoch, open, high, low, close, volume) ascending.
    Useful when building backtest ticks that include vol_1m.
    """
    return [(int(c.epoch), c.open, c.high, c.low, c.close, c.volume) for c in candles]
