#!/usr/bin/env python3
"""
from_live_events.py

Reconstruct "paper" trades directly from logs/live_events.csv.

- Pairs SHOULD_BUY -> next SHOULD_SELL (FIFO).
- Tries to parse px and qty from the event detail string.
- If qty is missing:
    - Uses USD_PER_TRADE if set (qty = USD_PER_TRADE / entry_px), else DEFAULT_QTY (default 1).
- If px is missing in detail:
    - Trade is skipped (no price to value the trade).

Env vars (optional):
  LIVE_EVENTS_CSV        path to live_events.csv
  START_EQUITY_USD       starting equity (default: 500)
  BUY_EVENT_NAME         default: SHOULD_BUY
  SELL_EVENT_NAME        default: SHOULD_SELL
  DEFAULT_QTY            default: 1
  USD_PER_TRADE          if set, position size in USD per trade
  OUT_TRADES_CSV         if set, writes trades to this CSV path
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, List, Dict, Tuple


# -----------------------------
# Helpers
# -----------------------------
def _to_decimal(x: str) -> Optional[Decimal]:
    try:
        return Decimal(str(x).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _parse_iso_ts_to_epoch(ts: str) -> int:
    dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _find_first_decimal(detail: str, keys: List[str]) -> Optional[Decimal]:
    """
    Attempts to extract a numeric value after keys like:
      px=1234.56
      fill_px: 1234.56
      price 1234.56

    Also tolerates commas:
      px=1,234.56
    """
    if not detail:
        return None

    s = detail.strip()

    for k in keys:
        pat = re.compile(
            rf"(?:^|[\s,;]){re.escape(k)}\s*(?:=|:)?\s*([-+]?\d[\d,]*(?:\.\d+)?)",
            re.IGNORECASE,
        )
        m = pat.search(s)
        if m:
            raw = (m.group(1) or "").replace(",", "")
            return _to_decimal(raw)

    return None


@dataclass
class Trade:
    entry_ts: str
    entry_epoch: int
    entry_px: Decimal
    qty: Decimal
    exit_ts: Optional[str] = None
    exit_epoch: Optional[int] = None
    exit_px: Optional[Decimal] = None
    realized_usd: Optional[Decimal] = None


def _max_drawdown_pct(equity_curve: List[Tuple[int, Decimal]]) -> Decimal:
    if not equity_curve:
        return Decimal("0")

    peak = equity_curve[0][1]
    max_dd = Decimal("0")

    for _, eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    return max_dd * Decimal("100")


def run_from_live_events(
    *,
    live_events_csv: str,
    start_equity: Decimal,
    buy_event: str,
    sell_event: str,
    default_qty: Decimal,
    usd_per_trade: Optional[Decimal] = None,
    out_trades_csv: Optional[str] = None,
) -> Dict:
    if not os.path.exists(live_events_csv):
        raise FileNotFoundError(live_events_csv)

    # Read events
    events: List[Dict[str, str]] = []
    with open(live_events_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            events.append(row)

    if not events:
        return {
            "symbol": "UNKNOWN",
            "start_epoch": 0,
            "end_epoch": 0,
            "start_equity": str(start_equity),
            "end_equity": str(start_equity),
            "total_return_pct": "0.0000",
            "max_drawdown_pct": "0.0000",
            "trades_closed": 0,
            "win_rate_pct": "0.00",
            "realized_total_usd": "0",
        }

    symbol = (events[0].get("symbol") or "UNKNOWN").strip() or "UNKNOWN"

    # Sort by ts so FIFO pairing is deterministic even if file got appended oddly
    def _ts_key(e: Dict[str, str]) -> int:
        ts = (e.get("ts") or "").strip()
        if not ts:
            return 0
        try:
            return _parse_iso_ts_to_epoch(ts)
        except Exception:
            return 0

    events.sort(key=_ts_key)

    start_epoch = _ts_key(events[0])
    end_epoch = _ts_key(events[-1])

    open_trade: Optional[Trade] = None
    trades: List[Trade] = []

    equity = start_equity
    equity_curve: List[Tuple[int, Decimal]] = [(start_epoch, equity)]
    realized_total = Decimal("0")
    wins = 0
    closed = 0

    for row in events:
        ev = (row.get("event") or "").strip()
        ts = (row.get("ts") or "").strip()
        detail = (row.get("detail") or "").strip()

        if not ts:
            continue

        try:
            epoch = _parse_iso_ts_to_epoch(ts)
        except Exception:
            continue

        if ev == buy_event:
            if open_trade is not None and open_trade.exit_epoch is None:
                continue

            entry_px = _find_first_decimal(detail, ["fill_px", "px", "price", "entry_px"])
            if entry_px is None or entry_px <= 0:
                continue

            qty = _find_first_decimal(detail, ["qty", "size", "units"])
            if qty is None or qty <= 0:
                if usd_per_trade is not None and usd_per_trade > 0:
                    qty = (usd_per_trade / entry_px)
                else:
                    qty = default_qty

            open_trade = Trade(
                entry_ts=ts,
                entry_epoch=epoch,
                entry_px=entry_px,
                qty=qty,
            )
            trades.append(open_trade)

        elif ev == sell_event:
            if open_trade is None or open_trade.exit_epoch is not None:
                continue

            exit_px = _find_first_decimal(detail, ["fill_px", "px", "price", "exit_px"])
            if exit_px is None or exit_px <= 0:
                continue

            open_trade.exit_ts = ts
            open_trade.exit_epoch = epoch
            open_trade.exit_px = exit_px

            pnl = (exit_px - open_trade.entry_px) * open_trade.qty
            open_trade.realized_usd = pnl

            equity = equity + pnl
            equity_curve.append((epoch, equity))

            realized_total += pnl
            closed += 1
            if pnl > 0:
                wins += 1

            open_trade = None

    end_equity = equity
    total_return_pct = Decimal("0")
    if start_equity > 0:
        total_return_pct = (end_equity - start_equity) / start_equity * Decimal("100")

    max_dd_pct = _max_drawdown_pct(equity_curve)

    win_rate_pct = Decimal("0")
    if closed > 0:
        win_rate_pct = (Decimal(wins) / Decimal(closed)) * Decimal("100")

    # Optional: write trades
    if out_trades_csv:
        os.makedirs(os.path.dirname(out_trades_csv) or ".", exist_ok=True)
        with open(out_trades_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["entry_ts", "entry_px", "qty", "exit_ts", "exit_px", "realized_usd"])
            for t in trades:
                if t.exit_ts is None:
                    continue
                w.writerow(
                    [
                        t.entry_ts,
                        str(t.entry_px),
                        str(t.qty),
                        t.exit_ts,
                        str(t.exit_px),
                        str(t.realized_usd or Decimal("0")),
                    ]
                )

    return {
        "symbol": symbol,
        "start_epoch": int(start_epoch),
        "end_epoch": int(end_epoch),
        "start_equity": str(start_equity),
        "end_equity": f"{end_equity:.3f}",
        "total_return_pct": f"{total_return_pct:.4f}",
        "max_drawdown_pct": f"{max_dd_pct:.4f}",
        "trades_closed": int(closed),
        "win_rate_pct": f"{win_rate_pct:.2f}",
        "realized_total_usd": str(realized_total),
    }


if __name__ == "__main__":
    live_events_csv = os.environ.get(
        "LIVE_EVENTS_CSV",
        r"C:\Users\ksmit\Documents\Nova\nova_scripts\trade_bot\logs\live_events.csv",
    )
    start_equity = _to_decimal(os.environ.get("START_EQUITY_USD", "500")) or Decimal("500")
    buy_event = os.environ.get("BUY_EVENT_NAME", "SHOULD_BUY")
    sell_event = os.environ.get("SELL_EVENT_NAME", "SHOULD_SELL")

    default_qty = _to_decimal(os.environ.get("DEFAULT_QTY", "1")) or Decimal("1")
    usd_per_trade = _to_decimal(os.environ.get("USD_PER_TRADE", ""))  # optional

    out_trades_csv = os.environ.get("OUT_TRADES_CSV", "").strip() or None

    res = run_from_live_events(
        live_events_csv=live_events_csv,
        start_equity=start_equity,
        buy_event=buy_event,
        sell_event=sell_event,
        default_qty=default_qty,
        usd_per_trade=usd_per_trade,
        out_trades_csv=out_trades_csv,
    )
    print(res)
