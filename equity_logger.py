#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional


def _artifact_root() -> str:
    return os.environ.get("ARGUS_ARTIFACT_ROOT", r"C:\Argus\argus-lab")


def _out_dir() -> str:
    return os.path.join(_artifact_root(), "ops", "logs")


def equity_csv_path() -> str:
    run_id = os.environ.get("ARGUS_RUN_ID", "").strip() or "live"
    return os.path.join(_out_dir(), f"equity_{run_id}.csv")


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(str(v))
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _csv_sanitize(v: Any) -> str:
    try:
        s = "" if v is None else str(v)
    except Exception:
        s = ""
    return s.replace("\r", " ").replace("\n", " ").replace(",", " ")


def _ensure_parent_dir(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _write_header_if_missing(path: str) -> None:
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    _ensure_parent_dir(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write("ts,run_id,epoch,symbol,price,equity_usd,cash_usd,position_qty,realized_usd,unrealized_usd\n")


def append_equity_row(*, snap: Any, symbol: str) -> None:
    """
    Append one row to the equity time-series CSV.

    Requires snap to expose (preferably):
      - ts, epoch, px, equity_usd, cash_usd, position_qty, realized_usd, unrealized_usd
    If some are missing, we fall back to blanks/0.
    """
    path = equity_csv_path()
    _write_header_if_missing(path)

    run_id = os.environ.get("ARGUS_RUN_ID", "").strip()
    ts = getattr(snap, "ts", None) or datetime.now(timezone.utc).isoformat()
    epoch = _safe_int(getattr(snap, "epoch", 0), 0)
    px = _safe_float(getattr(snap, "px", getattr(snap, "price", 0.0)), 0.0)

    equity = _safe_float(getattr(snap, "equity_usd", getattr(snap, "equity", 0.0)), 0.0)
    cash = _safe_float(getattr(snap, "cash_usd", getattr(snap, "cash", 0.0)), 0.0)
    qty = _safe_float(getattr(snap, "position_qty", getattr(snap, "qty", 0.0)), 0.0)

    realized = _safe_float(getattr(snap, "realized_usd", getattr(snap, "realized", 0.0)), 0.0)
    unrealized = _safe_float(getattr(snap, "unrealized_usd", getattr(snap, "unrl", 0.0)), 0.0)

    line = (
        f"{_csv_sanitize(ts)},{_csv_sanitize(run_id)},{epoch},{_csv_sanitize(symbol)},{px},"
        f"{equity},{cash},{qty},{realized},{unrealized}\n"
    )
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(line)


class EquityRateLimiter:
    """
    Drop-in rate limiter so you can call maybe_log() every tick without spamming disk.
    """
    def __init__(self, every_s: Optional[float] = None) -> None:
        env = os.environ.get("ARGUS_EQUITY_LOG_EVERY_S", "").strip()
        self.every_s = float(env) if env else float(every_s if every_s is not None else 5.0)
        self._next_t = 0.0

    def maybe_log(self, *, snap: Any, symbol: str) -> None:
        now = time.time()
        if now < self._next_t:
            return
        self._next_t = now + max(self.every_s, 0.25)
        append_equity_row(snap=snap, symbol=symbol)