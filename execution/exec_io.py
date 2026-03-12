#!/usr/bin/env python3
# exec_io.py

# line above: from __future__ import annotations
from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Mapping, Optional

from execution.intent import OrderIntent
from trade_journal import (
    TRADE_JOURNAL_FIELDS,
    TradeJournalRow,
    append_trade_journal_row as _append_trade_journal_row_canonical,
    build_trade_journal_row_from_mapping,
    ensure_trade_journal,
)


# =========================================================
# Orders surface (run-scoped orchestration truth)
# =========================================================

ORDERS_HEADER = [
    "run_id",
    "symbol",
    "intent_id",
    "intent_type",
    "intent_epoch",
    "intent_ts",
    "side",
    "qty",
    "order_type",
    "limit_px",
    "status",                 # PLANNED / ACKED / FILLED / CANCELLED / REJECTED
    "order_id",               # adapter-returned id
    "client_order_id",
    "entry_reason",
    "risk_blocked_reason",
    "take_profit",
    "stop_loss",
    "trail_stop",
]

VALID_ORDER_STATUSES = {
    "PLANNED",
    "ACKED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
}


# =========================================================
# Daily summary surface
# =========================================================

DAILY_SUMMARY_FIELDS = [
    "run_id",
    "trade_date",
    "generated_at",
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "trade_count",
    "invariant_equity_check",
]


# =========================================================
# Paths
# =========================================================

def orders_path(log_dir: str, run_id: str) -> str:
    return os.path.join(log_dir, f"orders_{run_id}.csv")


def trade_journal_path(log_dir: str, run_id: str) -> str:
    return os.path.join(log_dir, f"trade_journal_{run_id}.csv")


def daily_summary_path(log_dir: str, trade_date: str) -> str:
    return os.path.join(log_dir, f"daily_summary_{trade_date}.json")


# =========================================================
# Utilities
# =========================================================

def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def _to_decimal(v: Any, default: str = "0") -> Decimal:
    if v is None:
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    s = str(v).strip()
    if not s:
        return Decimal(default)
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _quantize_8(v: Any) -> str:
    dec = _to_decimal(v, "0").quantize(Decimal("0.00000001"))
    return format(dec, "f")


def _atomic_write_text(path: str, content: str) -> None:
    _ensure_parent_dir(path)
    parent = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _atomic_write_rows(path: str, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    _ensure_parent_dir(path)
    parent = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _safe_str(row.get(k, "")) for k in fieldnames})
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _read_csv_rows(path: str) -> List[Dict[str, str]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# =========================================================
# Order Intent → Planned Row
# =========================================================

def append_orders(
    log_dir: str,
    run_id: str,
    intents: Iterable[OrderIntent],
) -> None:
    """
    Append PLANNED order intents to run-scoped orders_<run>.csv.
    """

    p = orders_path(log_dir, run_id)
    _ensure_parent_dir(p)
    exists = os.path.exists(p)

    with open(p, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=ORDERS_HEADER,
            extrasaction="ignore",
        )

        if not exists:
            writer.writeheader()

        for it in intents:
            base_row: Dict[str, Any] = it.to_row()

            if it.intent_type.startswith("ENTRY"):
                client_id = it.client_order_id("ENTRY")
            elif it.intent_type == "EXIT":
                client_id = it.client_order_id("EXIT")
            else:
                client_id = it.client_order_id("GEN")

            row_out = {
                "run_id": _safe_str(run_id),
                "symbol": _safe_str(base_row.get("symbol", "")),
                "intent_id": _safe_str(base_row.get("intent_id", "")),
                "intent_type": _safe_str(base_row.get("intent_type", "")),
                "intent_epoch": _safe_str(base_row.get("intent_epoch", "")),
                "intent_ts": _safe_str(base_row.get("intent_ts", "")),
                "side": _safe_str(base_row.get("side", "")),
                "qty": _safe_str(base_row.get("qty", "")),
                "order_type": _safe_str(base_row.get("order_type", "")),
                "limit_px": _safe_str(base_row.get("limit_px", "")),
                "status": "PLANNED",
                "order_id": "",
                "client_order_id": client_id,
                "entry_reason": _safe_str(base_row.get("entry_reason", "")),
                "risk_blocked_reason": _safe_str(base_row.get("risk_blocked_reason", "")),
                "take_profit": _safe_str(base_row.get("take_profit", "")),
                "stop_loss": _safe_str(base_row.get("stop_loss", "")),
                "trail_stop": _safe_str(base_row.get("trail_stop", "")),
            }

            writer.writerow(row_out)


# =========================================================
# Order Status Update (idempotent rewrite)
# =========================================================

def update_order_status(
    log_dir: str,
    run_id: str,
    client_order_id: str,
    *,
    status: str,
    order_id: Optional[str] = None,
) -> bool:
    """
    Update a single order row in orders_<run>.csv.
    Rewrites file deterministically.
    Returns True if a row was updated.
    """

    if status not in VALID_ORDER_STATUSES:
        raise ValueError(f"Invalid order status: {status}")

    p = orders_path(log_dir, run_id)
    if not os.path.exists(p):
        return False

    rows: List[Dict[str, Any]] = []
    updated = False

    with open(p, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            row = dict(row)
            if row.get("client_order_id", "") == client_order_id:
                row["status"] = status
                if order_id is not None and str(order_id).strip():
                    row["order_id"] = str(order_id).strip()
                updated = True
            rows.append(row)

    _atomic_write_rows(p, ORDERS_HEADER, rows)
    return updated


# =========================================================
# Read helpers
# =========================================================

def load_orders(log_dir: str, run_id: str) -> List[Dict[str, Any]]:
    return _read_csv_rows(orders_path(log_dir, run_id))


def load_trade_journal(log_dir: str, run_id: str) -> List[Dict[str, Any]]:
    return _read_csv_rows(trade_journal_path(log_dir, run_id))


def load_daily_summary(log_dir: str, trade_date: str) -> Dict[str, Any]:
    p = daily_summary_path(log_dir, trade_date)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# =========================================================
# Trade Journal Append
# =========================================================

def append_trade_journal_row(
    log_dir: str,
    run_id: str,
    row: Dict[str, Any] | Mapping[str, Any] | TradeJournalRow,
) -> str:
    """
    Append a canonical closed-trade row to trade_journal_<run>.csv.

    This delegates schema enforcement, validation, and dedupe policy
    to trade_journal.py so this file does not become a second source
    of truth.
    """
    ensure_trade_journal(log_dir, run_id)

    if isinstance(row, TradeJournalRow):
        canonical_row = row
    else:
        canonical_row = build_trade_journal_row_from_mapping(row)

    out_path = _append_trade_journal_row_canonical(
        log_dir=log_dir,
        run_id=run_id,
        row=canonical_row,
        dedupe_on_trade_id=True,
    )
    return str(out_path)


def ensure_trade_journal_file(log_dir: str, run_id: str) -> str:
    return str(ensure_trade_journal(log_dir, run_id))


# =========================================================
# Daily Summary Writer
# =========================================================

def write_daily_summary(
    log_dir: str,
    trade_date: str,
    *,
    run_id: str,
    starting_equity: Any,
    ending_equity: Any,
    realized_pnl: Any,
    trade_count: int,
) -> str:
    """
    Emit deterministic daily_summary_<date>.json using Decimal-safe accounting.
    """

    p = daily_summary_path(log_dir, trade_date)
    _ensure_parent_dir(p)

    starting_equity_dec = _to_decimal(starting_equity, "0").quantize(Decimal("0.00000001"))
    ending_equity_dec = _to_decimal(ending_equity, "0").quantize(Decimal("0.00000001"))
    realized_pnl_dec = _to_decimal(realized_pnl, "0").quantize(Decimal("0.00000001"))

    invariant_ok = (starting_equity_dec + realized_pnl_dec) == ending_equity_dec

    summary = {
        "run_id": _safe_str(run_id),
        "trade_date": _safe_str(trade_date),
        "generated_at": _utc_now_iso(),
        "starting_equity": format(starting_equity_dec, "f"),
        "ending_equity": format(ending_equity_dec, "f"),
        "realized_pnl": format(realized_pnl_dec, "f"),
        "trade_count": int(trade_count),
        "invariant_equity_check": invariant_ok,
    }

    _atomic_write_text(p, json.dumps(summary, indent=2, sort_keys=False))
    return p


# =========================================================
# Daily Summary Derivation Helper
# =========================================================

def write_daily_summary_from_journal(
    log_dir: str,
    trade_date: str,
    *,
    run_id: str,
    starting_equity: Any,
    ending_equity: Any,
) -> str:
    """
    Convenience writer:
    realized_pnl = sum(trade_journal_<run>.csv pnl)
    trade_count  = number of journal rows
    """
    journal_rows = load_trade_journal(log_dir, run_id)

    total_pnl = Decimal("0")
    for row in journal_rows:
        total_pnl += _to_decimal(row.get("pnl"), "0")

    return write_daily_summary(
        log_dir,
        trade_date,
        run_id=run_id,
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        realized_pnl=total_pnl,
        trade_count=len(journal_rows),
    )


# =========================================================
# Reconciliation helpers (lightweight)
# =========================================================

def sum_trade_journal_pnl(log_dir: str, run_id: str) -> Decimal:
    total = Decimal("0")
    for row in load_trade_journal(log_dir, run_id):
        total += _to_decimal(row.get("pnl"), "0")
    return total.quantize(Decimal("0.00000001"))


def trade_journal_trade_ids(log_dir: str, run_id: str) -> List[str]:
    rows = load_trade_journal(log_dir, run_id)
    out: List[str] = []
    seen = set()

    for row in rows:
        trade_id = _safe_str(row.get("trade_id", "")).strip()
        if trade_id and trade_id not in seen:
            seen.add(trade_id)
            out.append(trade_id)

    return out


def trade_journal_exists(log_dir: str, run_id: str) -> bool:
    return os.path.exists(trade_journal_path(log_dir, run_id))


# =========================================================
# Smoke test
# =========================================================

if __name__ == "__main__":
    test_log_dir = os.path.join(os.path.dirname(__file__), "ops", "logs")
    test_run_id = "smoke_exec_io"
    test_trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ensure_trade_journal_file(test_log_dir, test_run_id)

    sample_row = {
        "trade_id": "T-EXECIO-0001",
        "run_id": test_run_id,
        "symbol": "ETH-USD",
        "entry_time": _utc_now_iso(),
        "exit_time": _utc_now_iso(),
        "entry_px": "3200.10",
        "exit_px": "3210.10",
        "qty": "0.01000000",
        "pnl": "0.10000000",
        "entry_reason": "FORCE_TEST_BUY",
        "exit_reason": "FORCE_TEST_SELL",
        "entry_order_id": "ORD-ENTRY-1",
        "exit_order_id": "ORD-EXIT-1",
        "entry_fill_id": "FILL-ENTRY-1",
        "exit_fill_id": "FILL-EXIT-1",
        "state_open_ts": _utc_now_iso(),
        "state_closed_ts": _utc_now_iso(),
    }

    try:
        out = append_trade_journal_row(test_log_dir, test_run_id, sample_row)
        print(f"[EXEC_IO] journal={out}")
    except ValueError as exc:
        print(f"[EXEC_IO] journal_append_skipped={exc}")

    ds_path = write_daily_summary_from_journal(
        test_log_dir,
        test_trade_date,
        run_id=test_run_id,
        starting_equity="475.00000000",
        ending_equity="475.10000000",
    )
    print(f"[EXEC_IO] daily_summary={ds_path}")
    print(f"[EXEC_IO] pnl_total={sum_trade_journal_pnl(test_log_dir, test_run_id)}")