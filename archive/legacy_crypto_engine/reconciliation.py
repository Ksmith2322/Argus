#!/usr/bin/env python3
# reconciliation.py

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================================
# Decimal / parsing helpers
# ============================================================================

D0 = Decimal("0")
Q8 = Decimal("0.00000001")


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


def _q8(v: Decimal) -> Decimal:
    return _to_decimal(v, "0").quantize(Q8, rounding=ROUND_DOWN)


def _safe_str(v: Any) -> str:
    return "" if v is None else str(v)


def _pick(row: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        if k in row:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
    return None


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    s = str(ts).strip()
    if not s:
        return None

    try:
        if s.replace(".", "", 1).isdigit():
            raw = float(s)
            if raw > 1_000_000_000_000:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
    except Exception:
        pass

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _ts_key(ts: str) -> Tuple[int, str]:
    dt = _parse_ts(ts)
    if dt is None:
        return (0, str(ts or ""))
    return (int(dt.timestamp()), str(ts))


def _row_ts(row: Dict[str, Any]) -> str:
    return _pick(row, "ts", "timestamp", "time", "updated_at", "created_at") or ""


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Data models
# ============================================================================

@dataclass
class ReconcileCheck:
    name: str
    ok: bool
    expected: str
    actual: str
    detail: str = ""


@dataclass
class ReconcileSummary:
    ok: bool
    generated_at: str
    run_id: str
    symbol: Optional[str]
    logs_dir: str
    state_dir: Optional[str]
    orders_files: List[str] = field(default_factory=list)
    fills_files: List[str] = field(default_factory=list)
    positions_files: List[str] = field(default_factory=list)
    trade_journal_files: List[str] = field(default_factory=list)
    adapter_orders_path: Optional[str] = None
    adapter_fills_path: Optional[str] = None
    adapter_positions_path: Optional[str] = None
    adapter_account_path: Optional[str] = None
    snapshot_path: Optional[str] = None
    daily_summary_path: Optional[str] = None
    counts: Dict[str, int] = field(default_factory=dict)
    computed: Dict[str, str] = field(default_factory=dict)
    checks: List[ReconcileCheck] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["checks"] = [asdict(c) for c in self.checks]
        return d


# ============================================================================
# CSV / JSON readers
# ============================================================================

def _read_csv_rows(path: str) -> List[Dict[str, str]]:
    if not path or not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(r) for r in reader]


def _read_json(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
            return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _glob_sorted(pattern: str) -> List[str]:
    return sorted(glob.glob(pattern))


# ============================================================================
# Artifact resolution
# ============================================================================

def _resolve_logs_dir(logs_dir: Optional[str]) -> str:
    if logs_dir:
        return logs_dir
    return os.path.join(os.path.dirname(__file__), "ops", "logs")


def _resolve_state_dir(state_dir: Optional[str]) -> Optional[str]:
    if state_dir:
        return state_dir
    default_state = os.path.join(os.path.dirname(__file__), "state")
    return default_state if os.path.isdir(default_state) else None


def _resolve_snapshot_path(state_dir: Optional[str], symbol: Optional[str]) -> Optional[str]:
    if not state_dir or not symbol:
        return None
    safe_symbol = symbol.replace("/", "_").replace("-", "_")
    path = os.path.join(state_dir, f"runtime_state_{safe_symbol}.json")
    return path if os.path.exists(path) else None


def _resolve_daily_summary_path(logs_dir: str, trade_date: Optional[str]) -> Optional[str]:
    if not trade_date:
        return None
    path = os.path.join(logs_dir, f"daily_summary_{trade_date}.json")
    return path if os.path.exists(path) else None


def _resolve_run_files(logs_dir: str, prefix: str, run_id: Optional[str]) -> List[str]:
    if run_id:
        p = os.path.join(logs_dir, f"{prefix}_{run_id}.csv")
        return [p] if os.path.exists(p) else []
    return _glob_sorted(os.path.join(logs_dir, f"{prefix}_*.csv"))


def _resolve_trade_journal_files(logs_dir: str, run_id: Optional[str]) -> List[str]:
    return _resolve_run_files(logs_dir, "trade_journal", run_id)


def _resolve_orders_files(logs_dir: str, run_id: Optional[str]) -> List[str]:
    return _resolve_run_files(logs_dir, "orders", run_id)


def _resolve_fills_files(logs_dir: str, run_id: Optional[str]) -> List[str]:
    return _resolve_run_files(logs_dir, "fills", run_id)


def _resolve_positions_files(logs_dir: str, run_id: Optional[str]) -> List[str]:
    return _resolve_run_files(logs_dir, "positions", run_id)


def _resolve_adapter_truth_paths(logs_dir: str) -> Dict[str, Optional[str]]:
    candidates = {
        "adapter_orders_path": os.path.join(logs_dir, "orders.csv"),
        "adapter_fills_path": os.path.join(logs_dir, "fills.csv"),
        "adapter_positions_path": os.path.join(logs_dir, "positions.csv"),
        "adapter_account_path": os.path.join(logs_dir, "account.csv"),
    }
    out: Dict[str, Optional[str]] = {}
    for k, path in candidates.items():
        out[k] = path if os.path.exists(path) else None
    return out


# ============================================================================
# Domain derivation helpers
# ============================================================================

def _filter_symbol(rows: Iterable[Dict[str, Any]], symbol: Optional[str]) -> List[Dict[str, Any]]:
    if not symbol:
        return list(rows)
    want = str(symbol).strip().upper()
    out: List[Dict[str, Any]] = []
    for row in rows:
        got = _safe_str(_pick(row, "symbol", "product_id")).strip().upper()
        if got == want:
            out.append(row)
    return out


def _sort_rows_by_ts(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(list(rows), key=lambda r: _ts_key(_row_ts(r)))


def _trade_journal_pnl(rows: List[Dict[str, Any]]) -> Decimal:
    total = D0
    for row in rows:
        total += _to_decimal(_pick(row, "pnl", "realized_pnl", "trade_pnl"), "0")
    return _q8(total)


def _trade_journal_completed_count(rows: List[Dict[str, Any]]) -> int:
    n = 0
    for row in rows:
        exit_ts = _pick(row, "exit_time", "state_closed_ts", "closed_at")
        pnl = _pick(row, "pnl", "realized_pnl", "trade_pnl")
        if exit_ts or pnl:
            n += 1
    return n


def _fills_realized_delta_from_position(rows: List[Dict[str, Any]], symbol: Optional[str]) -> Decimal:
    srows = _filter_symbol(rows, symbol)
    if not srows:
        return D0
    srows = _sort_rows_by_ts(srows)
    last = srows[-1]
    return _q8(_to_decimal(_pick(last, "realized_pnl"), "0"))


def _positions_last_qty(rows: List[Dict[str, Any]], symbol: Optional[str]) -> Decimal:
    srows = _filter_symbol(rows, symbol)
    if not srows:
        return D0
    srows = _sort_rows_by_ts(srows)
    last = srows[-1]
    return _q8(_to_decimal(_pick(last, "qty", "position_qty"), "0"))


def _extract_last_mark_px(positions_rows: List[Dict[str, Any]], symbol: Optional[str]) -> Optional[Decimal]:
    srows = _filter_symbol(positions_rows, symbol)
    if not srows:
        return None
    srows = _sort_rows_by_ts(srows)
    last = srows[-1]
    raw = _pick(last, "mark_px", "price", "px")
    return _q8(_to_decimal(raw, "0")) if raw is not None else None


def _snapshot_equity_snapshot(snapshot: Dict[str, Any], mark_px: Optional[Decimal] = None) -> Tuple[Decimal, Decimal]:
    cash = _q8(_to_decimal(snapshot.get("cash"), "0"))
    qty = _q8(_to_decimal(snapshot.get("position_qty"), "0"))
    avg = _q8(_to_decimal(snapshot.get("avg_entry"), "0"))
    px = mark_px if mark_px is not None else avg
    equity = _q8(cash + (qty * px))
    return (cash, equity)


def _derive_entry_exit_counts_from_fills(rows: List[Dict[str, Any]], symbol: Optional[str]) -> Dict[str, int]:
    srows = _filter_symbol(rows, symbol)
    buy = 0
    sell = 0
    for row in srows:
        side = _safe_str(_pick(row, "side")).upper()
        if side == "BUY":
            buy += 1
        elif side == "SELL":
            sell += 1
    return {"buy_fills": buy, "sell_fills": sell, "total_fills": len(srows)}


def _derive_trade_ids(rows: List[Dict[str, Any]]) -> List[str]:
    ids: List[str] = []
    seen = set()
    for row in rows:
        tid = _pick(row, "trade_id")
        if tid and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return ids


def _positions_qty_series(rows: List[Dict[str, Any]], symbol: Optional[str]) -> List[Tuple[str, Decimal]]:
    srows = _filter_symbol(rows, symbol)
    srows = _sort_rows_by_ts(srows)
    out: List[Tuple[str, Decimal]] = []
    for row in srows:
        qty = _q8(_to_decimal(_pick(row, "qty", "position_qty"), "0"))
        out.append((_row_ts(row), qty))
    return out


def _latest_position_qty_at_or_before(
    positions_rows: List[Dict[str, Any]],
    symbol: Optional[str],
    ts: Optional[str],
) -> Optional[Decimal]:
    if not ts:
        return None
    dt = _parse_ts(ts)
    if dt is None:
        return None

    series = _positions_qty_series(positions_rows, symbol)
    best_qty: Optional[Decimal] = None
    best_key: Optional[Tuple[int, str]] = None

    for pts, qty in series:
        k = _ts_key(pts)
        if k <= _ts_key(ts):
            if best_key is None or k >= best_key:
                best_key = k
                best_qty = qty

    return best_qty


def _account_cash_rows(rows: List[Dict[str, Any]]) -> List[Tuple[str, Decimal]]:
    srows = _sort_rows_by_ts(rows)
    out: List[Tuple[str, Decimal]] = []
    for row in srows:
        cash = _q8(_to_decimal(_pick(row, "cash", "cash_balance"), "0"))
        out.append((_row_ts(row), cash))
    return out


def _latest_cash_at_or_before(account_rows: List[Dict[str, Any]], ts: Optional[str]) -> Optional[Decimal]:
    if not ts:
        return None
    dt = _parse_ts(ts)
    if dt is None:
        return None

    series = _account_cash_rows(account_rows)
    best_cash: Optional[Decimal] = None
    best_key: Optional[Tuple[int, str]] = None

    for ats, cash in series:
        k = _ts_key(ats)
        if k <= _ts_key(ts):
            if best_key is None or k >= best_key:
                best_key = k
                best_cash = cash

    return best_cash


def _derive_lifecycle_bounds_from_journal(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not rows:
        return (None, None)

    entry_candidates: List[str] = []
    exit_candidates: List[str] = []

    for row in rows:
        et = _pick(row, "entry_time", "state_open_ts")
        xt = _pick(row, "exit_time", "state_closed_ts", "closed_at")
        if et:
            entry_candidates.append(et)
        if xt:
            exit_candidates.append(xt)

    start_ts = min(entry_candidates, key=_ts_key) if entry_candidates else None
    end_ts = max(exit_candidates, key=_ts_key) if exit_candidates else None
    return (start_ts, end_ts)


def _first_flat_cash_baseline(
    account_rows: List[Dict[str, Any]],
    positions_rows: List[Dict[str, Any]],
    symbol: Optional[str],
    lifecycle_start_ts: Optional[str],
) -> Tuple[Optional[str], Optional[Decimal]]:
    if not account_rows:
        return (None, None)

    srows = _sort_rows_by_ts(account_rows)

    for row in srows:
        ts = _row_ts(row)
        if lifecycle_start_ts and _ts_key(ts) > _ts_key(lifecycle_start_ts):
            break

        pos_qty = _latest_position_qty_at_or_before(positions_rows, symbol, ts)
        if pos_qty is None:
            continue
        if pos_qty == D0:
            cash = _q8(_to_decimal(_pick(row, "cash", "cash_balance"), "0"))
            return (ts, cash)

    first = srows[0]
    return (_row_ts(first), _q8(_to_decimal(_pick(first, "cash", "cash_balance"), "0")))


def _last_flat_cash_baseline(
    account_rows: List[Dict[str, Any]],
    positions_rows: List[Dict[str, Any]],
    symbol: Optional[str],
    lifecycle_end_ts: Optional[str],
) -> Tuple[Optional[str], Optional[Decimal]]:
    if not account_rows:
        return (None, None)

    srows = _sort_rows_by_ts(account_rows)

    # When lifecycle bounds are unknown (no trade_journal), skip the position-based
    # forward scan — it would incorrectly pick the BOOTSTRAP row (also flat, cash=$start)
    # over the post-close FILL_APPLIED row.  The last account row is the authoritative
    # post-close baseline when ended_flat is True.
    if lifecycle_end_ts is None:
        last = srows[-1]
        return (_row_ts(last), _q8(_to_decimal(_pick(last, "cash", "cash_balance"), "0")))

    best_ts: Optional[str] = None
    best_cash: Optional[Decimal] = None

    for row in srows:
        ts = _row_ts(row)
        if _ts_key(ts) < _ts_key(lifecycle_end_ts):
            continue

        pos_qty = _latest_position_qty_at_or_before(positions_rows, symbol, ts)
        if pos_qty is None:
            continue
        if pos_qty == D0:
            best_ts = ts
            best_cash = _q8(_to_decimal(_pick(row, "cash", "cash_balance"), "0"))
            break

    if best_ts is not None:
        return (best_ts, best_cash)

    last = srows[-1]
    return (_row_ts(last), _q8(_to_decimal(_pick(last, "cash", "cash_balance"), "0")))


def _latest_account_cash(rows: List[Dict[str, Any]]) -> Tuple[Decimal, Optional[str]]:
    if not rows:
        return (D0, None)
    srows = _sort_rows_by_ts(rows)
    last = srows[-1]
    return (_q8(_to_decimal(_pick(last, "cash", "cash_balance"), "0")), _row_ts(last))


def _account_cash_bounds_naive(rows: List[Dict[str, Any]]) -> Tuple[Decimal, Decimal]:
    if not rows:
        return (D0, D0)
    srows = _sort_rows_by_ts(rows)
    first = srows[0]
    last = srows[-1]
    start_cash = _to_decimal(_pick(first, "cash", "cash_balance"), "0")
    end_cash = _to_decimal(_pick(last, "cash", "cash_balance"), "0")
    return (_q8(start_cash), _q8(end_cash))


def _run_scope_exists(
    *,
    orders_files: List[str],
    fills_files: List[str],
    positions_files: List[str],
    trade_journal_files: List[str],
) -> bool:
    return bool(orders_files or fills_files or positions_files or trade_journal_files)


def _fill_sides_present(rows: List[Dict[str, Any]]) -> Tuple[bool, bool]:
    has_buy = False
    has_sell = False
    for row in rows:
        side = _safe_str(_pick(row, "side")).upper()
        if side == "BUY":
            has_buy = True
        elif side == "SELL":
            has_sell = True
    return has_buy, has_sell


def _has_any_rows(paths: List[str]) -> bool:
    for p in paths:
        if os.path.exists(p):
            rows = _read_csv_rows(p)
            if rows:
                return True
    return False


# ============================================================================
# Check builders
# ============================================================================

def _build_check(name: str, expected: Decimal, actual: Decimal, detail: str = "", tolerance: Decimal = D0) -> ReconcileCheck:
    e = _q8(expected)
    a = _q8(actual)
    if tolerance > D0:
        ok = abs(e - a) <= tolerance
    else:
        ok = (e == a)
    return ReconcileCheck(
        name=name,
        ok=ok,
        expected=str(e),
        actual=str(a),
        detail=detail,
    )


def _build_text_check(name: str, expected: str, actual: str, detail: str = "") -> ReconcileCheck:
    return ReconcileCheck(
        name=name,
        ok=(expected == actual),
        expected=expected,
        actual=actual,
        detail=detail,
    )


# ============================================================================
# Main reconciliation
# ============================================================================

def reconcile_phase8(
    *,
    run_id: Optional[str] = None,
    symbol: Optional[str] = None,
    logs_dir: Optional[str] = None,
    state_dir: Optional[str] = None,
    trade_date: Optional[str] = None,
    prefer_adapter_truth: bool = True,
    strict: bool = True,
) -> ReconcileSummary:
    logs_dir_resolved = _resolve_logs_dir(logs_dir)
    state_dir_resolved = _resolve_state_dir(state_dir)

    orders_files = _resolve_orders_files(logs_dir_resolved, run_id)
    fills_files = _resolve_fills_files(logs_dir_resolved, run_id)
    positions_files = _resolve_positions_files(logs_dir_resolved, run_id)
    trade_journal_files = _resolve_trade_journal_files(logs_dir_resolved, run_id)

    adapter_paths = _resolve_adapter_truth_paths(logs_dir_resolved)
    snapshot_path = _resolve_snapshot_path(state_dir_resolved, symbol)
    daily_summary_path = _resolve_daily_summary_path(logs_dir_resolved, trade_date)

    run_orders_rows: List[Dict[str, Any]] = []
    run_fills_rows: List[Dict[str, Any]] = []
    run_positions_rows: List[Dict[str, Any]] = []
    trade_journal_rows: List[Dict[str, Any]] = []

    for p in orders_files:
        run_orders_rows.extend(_read_csv_rows(p))
    for p in fills_files:
        run_fills_rows.extend(_read_csv_rows(p))
    for p in positions_files:
        run_positions_rows.extend(_read_csv_rows(p))
    for p in trade_journal_files:
        trade_journal_rows.extend(_read_csv_rows(p))

    adapter_orders_rows = _read_csv_rows(adapter_paths.get("adapter_orders_path") or "")
    adapter_fills_rows = _read_csv_rows(adapter_paths.get("adapter_fills_path") or "")
    adapter_positions_rows = _read_csv_rows(adapter_paths.get("adapter_positions_path") or "")
    adapter_account_rows = _read_csv_rows(adapter_paths.get("adapter_account_path") or "")

    snapshot = _read_json(snapshot_path)
    daily_summary = _read_json(daily_summary_path)

    # ------------------------------------------------------------------------
    # Truth surface selection
    # ------------------------------------------------------------------------

    use_run_scoped_truth = bool(run_id) or not (prefer_adapter_truth and adapter_fills_rows)
    effective_truth_source = "run_scoped" if use_run_scoped_truth else "adapter"

    if use_run_scoped_truth:
        effective_fills_rows = run_fills_rows
        effective_positions_rows = run_positions_rows
        effective_orders_rows = run_orders_rows
    else:
        effective_fills_rows = adapter_fills_rows
        effective_positions_rows = adapter_positions_rows
        effective_orders_rows = adapter_orders_rows

    effective_fills_rows = _filter_symbol(effective_fills_rows, symbol)
    effective_positions_rows = _filter_symbol(effective_positions_rows, symbol)
    effective_orders_rows = _filter_symbol(effective_orders_rows, symbol)
    trade_journal_rows = _filter_symbol(trade_journal_rows, symbol)

    checks: List[ReconcileCheck] = []
    warnings: List[str] = []

    run_scope_found = _run_scope_exists(
        orders_files=orders_files,
        fills_files=fills_files,
        positions_files=positions_files,
        trade_journal_files=trade_journal_files,
    )

    hard_fail = False
    hard_fail_reasons: List[str] = []

    # ------------------------------------------------------------------------
    # Strict run-id validation
    # ------------------------------------------------------------------------

    if run_id and not run_scope_found:
        hard_fail = True
        hard_fail_reasons.append(
            f"Requested run_id '{run_id}' did not match any run-scoped artifacts "
            f"(orders/fills/positions/trade_journal) under {logs_dir_resolved}."
        )

    if strict and run_id:
        if not orders_files:
            hard_fail = True
            hard_fail_reasons.append(
                f"Requested run_id '{run_id}' has no orders_<run_id>.csv artifact."
            )
        if not fills_files:
            hard_fail = True
            hard_fail_reasons.append(
                f"Requested run_id '{run_id}' has no fills_<run_id>.csv artifact."
            )
        if not positions_files:
            hard_fail = True
            hard_fail_reasons.append(
                f"Requested run_id '{run_id}' has no positions_<run_id>.csv artifact."
            )

    if not trade_journal_rows:
        warnings.append("No trade_journal rows found. Closed-trade reconciliation is partial.")
    if not effective_fills_rows:
        warnings.append("No fills rows found for effective truth source.")
    if not effective_positions_rows:
        warnings.append("No positions rows found for effective truth source.")
    if not adapter_account_rows:
        warnings.append("No adapter account.csv found; flat-cash baseline checks may be partial.")

    # ------------------------------------------------------------------------
    # Core computed numbers
    # ------------------------------------------------------------------------

    journal_closed_trade_pnl = _trade_journal_pnl(trade_journal_rows)
    completed_trades = _trade_journal_completed_count(trade_journal_rows)

    positions_realized_pnl_delta = _fills_realized_delta_from_position(effective_positions_rows, symbol)
    last_qty = _positions_last_qty(effective_positions_rows, symbol)
    last_mark_px = _extract_last_mark_px(effective_positions_rows, symbol)

    realized_pnl_delta = journal_closed_trade_pnl if trade_journal_rows else positions_realized_pnl_delta
    realized_pnl_delta = _q8(realized_pnl_delta)

    fill_counts = _derive_entry_exit_counts_from_fills(effective_fills_rows, symbol)
    trade_ids_from_fills = _derive_trade_ids(effective_fills_rows)

    naive_start_cash, naive_end_cash = _account_cash_bounds_naive(adapter_account_rows)
    latest_account_cash, latest_account_ts = _latest_account_cash(adapter_account_rows)

    snapshot_cash = D0
    snapshot_equity = D0
    if snapshot:
        snapshot_cash, snapshot_equity = _snapshot_equity_snapshot(snapshot, mark_px=last_mark_px)

    ds_pnl = _q8(_to_decimal(_pick(daily_summary, "pnl", "realized_pnl", "day_pnl"), "0")) if daily_summary else None
    ds_starting_equity = _q8(_to_decimal(_pick(daily_summary, "starting_equity"), "0")) if daily_summary and _pick(daily_summary, "starting_equity") is not None else None
    ds_ending_equity = _q8(_to_decimal(_pick(daily_summary, "ending_equity"), "0")) if daily_summary and _pick(daily_summary, "ending_equity") is not None else None

    lifecycle_start_ts, lifecycle_end_ts = _derive_lifecycle_bounds_from_journal(trade_journal_rows)

    flat_start_ts, flat_start_cash = _first_flat_cash_baseline(
        adapter_account_rows,
        effective_positions_rows,
        symbol,
        lifecycle_start_ts,
    )
    flat_end_ts, flat_end_cash = _last_flat_cash_baseline(
        adapter_account_rows,
        effective_positions_rows,
        symbol,
        lifecycle_end_ts,
    )

    has_flat_baselines = flat_start_cash is not None and flat_end_cash is not None
    ended_flat = (last_qty == D0)

    # ------------------------------------------------------------------------
    # Run-scoped contract validation
    # ------------------------------------------------------------------------

    if run_id:
        has_buy_fill, has_sell_fill = _fill_sides_present(effective_fills_rows)

        if trade_journal_rows:
            if not effective_fills_rows:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' has trade_journal rows but no fills rows."
                )
            if not effective_positions_rows:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' has trade_journal rows but no positions rows."
                )

        if completed_trades > 0:
            if not trade_journal_rows:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' appears to have a completed trade but has no trade_journal rows."
                )
            if not effective_positions_rows:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' appears to have a completed trade but has no positions rows."
                )
            if not effective_fills_rows:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' appears to have a completed trade but has no fills rows."
                )
            if strict and not has_sell_fill:
                hard_fail = True
                hard_fail_reasons.append(
                    f"Requested run_id '{run_id}' has completed trade evidence but no SELL fills."
                )

        if effective_fills_rows and has_sell_fill and not effective_positions_rows:
            hard_fail = True
            hard_fail_reasons.append(
                f"Requested run_id '{run_id}' contains SELL fills but has no positions rows. "
                "Close-run artifact contract is incomplete."
            )

    # ------------------------------------------------------------------------
    # Primary PnL check
    # ------------------------------------------------------------------------

    if trade_journal_rows:
        checks.append(
            _build_check(
                "trade_journal_pnl_equals_positions_realized_pnl",
                journal_closed_trade_pnl,
                positions_realized_pnl_delta,
                detail="sum(trade_journal.pnl) vs latest realized_pnl from positions truth",
            )
        )
    else:
        warnings.append(
            "Primary closed-trade truth missing; using positions.realized_pnl as fallback realized PnL truth."
        )

    # ------------------------------------------------------------------------
    # Daily summary checks
    # ------------------------------------------------------------------------

    if ds_pnl is not None:
        checks.append(
            _build_check(
                "daily_summary_pnl_equals_realized_pnl",
                realized_pnl_delta,
                ds_pnl,
                detail="daily_summary.pnl vs primary realized PnL truth",
            )
        )

    if ds_starting_equity is not None and ds_ending_equity is not None and ended_flat:
        checks.append(
            _build_check(
                "starting_equity_plus_realized_pnl_equals_ending_equity",
                _q8(ds_starting_equity + realized_pnl_delta),
                ds_ending_equity,
                detail="daily_summary starting_equity + realized_pnl vs ending_equity when final state is flat",
            )
        )
    elif ds_starting_equity is not None and ds_ending_equity is not None and not ended_flat:
        warnings.append(
            "Skipped starting_equity_plus_realized_pnl_equals_ending_equity strict check because final state is not flat."
        )

    # ------------------------------------------------------------------------
    # Flat-cash lifecycle check
    # ------------------------------------------------------------------------

    if adapter_account_rows and has_flat_baselines and ended_flat:
        checks.append(
            _build_check(
                "flat_start_cash_plus_realized_pnl_equals_flat_end_cash",
                _q8(flat_start_cash + realized_pnl_delta),
                _q8(flat_end_cash),
                detail=(
                    "first known flat cash baseline before/at lifecycle start + realized_pnl "
                    "vs first known flat cash baseline after/at lifecycle end"
                ),
                # Allow 2 ULP (2e-8) — ROUND_DOWN truncation on each fill step can produce
                # a 1–2 unit rounding difference between positions.realized_pnl and the
                # actual account cash movement.
                tolerance=Decimal("0.00000002"),
            )
        )
    elif adapter_account_rows and not has_flat_baselines:
        warnings.append(
            "Skipped flat baseline cash check because flat start/end cash baselines could not be derived."
        )
    elif adapter_account_rows and not ended_flat:
        warnings.append(
            "Skipped flat baseline cash check because final position is still open."
        )

    # ------------------------------------------------------------------------
    # Legacy naive cash check warning only
    # ------------------------------------------------------------------------

    if adapter_account_rows:
        naive_expected = _q8(naive_start_cash + realized_pnl_delta)
        naive_actual = _q8(naive_end_cash)
        if naive_expected != naive_actual:
            warnings.append(
                "Naive account cash identity does not hold "
                f"(start_cash={naive_start_cash} + realized_pnl={realized_pnl_delta} != end_cash={naive_end_cash}). "
                "This usually means the first account row is not a true flat baseline."
            )

    # ------------------------------------------------------------------------
    # Trade journal completeness
    # ------------------------------------------------------------------------

    if completed_trades > 0:
        checks.append(
            _build_text_check(
                "trade_journal_has_completed_rows",
                expected=">0",
                actual=">0",
                detail=f"completed_trades={completed_trades}",
            )
        )

    if trade_journal_rows and trade_ids_from_fills:
        journal_trade_ids = {
            _safe_str(_pick(r, "trade_id")).strip()
            for r in trade_journal_rows
            if _pick(r, "trade_id")
        }
        fill_trade_ids = {t for t in trade_ids_from_fills if t}
        if journal_trade_ids:
            missing = sorted(fill_trade_ids - journal_trade_ids)
            if missing:
                warnings.append(
                    "Trade IDs found in fills but missing in trade_journal: " + ", ".join(missing[:10])
                )

    # ------------------------------------------------------------------------
    # Snapshot / equity consistency
    # ------------------------------------------------------------------------

    if ds_ending_equity is not None and snapshot and last_qty > D0:
        if _q8(snapshot_equity) != _q8(ds_ending_equity):
            warnings.append(
                f"Open-position equity mismatch: snapshot_equity={snapshot_equity} "
                f"daily_summary.ending_equity={ds_ending_equity}"
            )

    if flat_start_ts is None:
        warnings.append("Could not identify explicit flat_start_ts from account/positions alignment.")
    if flat_end_ts is None:
        warnings.append("Could not identify explicit flat_end_ts from account/positions alignment.")

    # ------------------------------------------------------------------------
    # Canonical vs run-scoped surface diagnostics
    # ------------------------------------------------------------------------

    if run_id and adapter_fills_rows and run_fills_rows:
        adapter_symbol_fills = _filter_symbol(adapter_fills_rows, symbol)
        run_symbol_fills = _filter_symbol(run_fills_rows, symbol)
        if len(run_symbol_fills) > len(adapter_symbol_fills):
            warnings.append(
                "Run-scoped fills exceed canonical adapter fills count. "
                "This may indicate legacy/mixed artifacts or duplicate rows."
            )

    for reason in hard_fail_reasons:
        checks.append(
            ReconcileCheck(
                name="run_scope_contract_valid",
                ok=False,
                expected="valid",
                actual="invalid",
                detail=reason,
            )
        )

    ok = (not hard_fail) and all(c.ok for c in checks)

    computed: Dict[str, str] = {
        "journal_closed_trade_pnl": str(_q8(journal_closed_trade_pnl)),
        "positions_realized_pnl_delta": str(_q8(positions_realized_pnl_delta)),
        "realized_pnl_delta": str(_q8(realized_pnl_delta)),
        "naive_start_cash": str(_q8(naive_start_cash)),
        "naive_end_cash": str(_q8(naive_end_cash)),
        "flat_start_cash": str(_q8(flat_start_cash if flat_start_cash is not None else D0)),
        "flat_end_cash": str(_q8(flat_end_cash if flat_end_cash is not None else D0)),
        "latest_account_cash": str(_q8(latest_account_cash)),
        "snapshot_cash": str(_q8(snapshot_cash)),
        "snapshot_equity": str(_q8(snapshot_equity)),
        "last_position_qty": str(_q8(last_qty)),
        "completed_trades": str(completed_trades),
        "buy_fills": str(fill_counts["buy_fills"]),
        "sell_fills": str(fill_counts["sell_fills"]),
        "total_fills": str(fill_counts["total_fills"]),
        "effective_truth_source": effective_truth_source,
        "truth_mode": "strict" if strict else "lenient",
        "ended_flat": str(ended_flat),
        "flat_start_ts": flat_start_ts or "",
        "flat_end_ts": flat_end_ts or "",
        "lifecycle_start_ts": lifecycle_start_ts or "",
        "lifecycle_end_ts": lifecycle_end_ts or "",
        "latest_account_ts": latest_account_ts or "",
        "run_scope_found": str(run_scope_found),
    }

    return ReconcileSummary(
        ok=ok,
        generated_at=_now_utc_iso(),
        run_id=run_id or "ALL",
        symbol=symbol,
        logs_dir=logs_dir_resolved,
        state_dir=state_dir_resolved,
        orders_files=orders_files,
        fills_files=fills_files,
        positions_files=positions_files,
        trade_journal_files=trade_journal_files,
        adapter_orders_path=adapter_paths.get("adapter_orders_path"),
        adapter_fills_path=adapter_paths.get("adapter_fills_path"),
        adapter_positions_path=adapter_paths.get("adapter_positions_path"),
        adapter_account_path=adapter_paths.get("adapter_account_path"),
        snapshot_path=snapshot_path,
        daily_summary_path=daily_summary_path,
        counts={
            "run_orders_rows": len(run_orders_rows),
            "run_fills_rows": len(run_fills_rows),
            "run_positions_rows": len(run_positions_rows),
            "trade_journal_rows": len(trade_journal_rows),
            "adapter_orders_rows": len(adapter_orders_rows),
            "adapter_fills_rows": len(adapter_fills_rows),
            "adapter_positions_rows": len(adapter_positions_rows),
            "adapter_account_rows": len(adapter_account_rows),
        },
        computed=computed,
        checks=checks,
        warnings=warnings,
    )


# ============================================================================
# Persistence / console formatting
# ============================================================================

def write_reconcile_report(path: str, summary: ReconcileSummary) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary.to_dict(), f, indent=2, sort_keys=False)


def format_reconcile_lines(summary: ReconcileSummary) -> List[str]:
    lines = [
        f"[RECON] ok={summary.ok} run_id={summary.run_id} symbol={summary.symbol or 'ALL'}",
        f"[RECON] logs_dir={summary.logs_dir}",
        f"[RECON] truth={summary.computed.get('effective_truth_source', '')}",
        f"[RECON] truth_mode={summary.computed.get('truth_mode', '')}",
        f"[RECON] journal_closed_trade_pnl={summary.computed.get('journal_closed_trade_pnl', '0')}",
        f"[RECON] positions_realized_pnl_delta={summary.computed.get('positions_realized_pnl_delta', '0')}",
        f"[RECON] realized_pnl_delta={summary.computed.get('realized_pnl_delta', '0')}",
        f"[RECON] naive_start_cash={summary.computed.get('naive_start_cash', '0')} naive_end_cash={summary.computed.get('naive_end_cash', '0')}",
        f"[RECON] flat_start_cash={summary.computed.get('flat_start_cash', '0')} flat_end_cash={summary.computed.get('flat_end_cash', '0')}",
        f"[RECON] ended_flat={summary.computed.get('ended_flat', 'False')} last_position_qty={summary.computed.get('last_position_qty', '0')}",
        f"[RECON] completed_trades={summary.computed.get('completed_trades', '0')} total_fills={summary.computed.get('total_fills', '0')}",
    ]

    for c in summary.checks:
        status = "PASS" if c.ok else "FAIL"
        lines.append(f"[RECON][{status}] {c.name} expected={c.expected} actual={c.actual}")
        if c.detail:
            lines.append(f"[RECON][DETAIL] {c.detail}")

    for w in summary.warnings:
        lines.append(f"[RECON][WARN] {w}")

    return lines


# ============================================================================
# CLI
# ============================================================================

def _default_report_path(logs_dir: str, run_id: Optional[str], symbol: Optional[str]) -> str:
    suffix = []
    if run_id:
        suffix.append(run_id)
    if symbol:
        suffix.append(symbol.replace("/", "_").replace("-", "_"))
    tail = "_".join(suffix) if suffix else "all"
    return os.path.join(logs_dir, f"reconcile_phase8_{tail}.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="Argus Phase 8 reconciliation utility")
    parser.add_argument("--run-id", default="", help="specific run_id to reconcile")
    parser.add_argument("--symbol", default="", help="symbol filter, e.g. ETH-USD")
    parser.add_argument("--logs-dir", default="", help="artifact root; default repo/ops/logs")
    parser.add_argument("--state-dir", default="", help="runtime state dir; default repo/state if present")
    parser.add_argument("--trade-date", default="", help="daily summary date, e.g. 2026-03-06")
    parser.add_argument(
        "--prefer-run-truth",
        action="store_true",
        help="prefer run-scoped orders_<run>/fills_<run>/positions_<run> over adapter canonical truth",
    )
    parser.add_argument(
        "--lenient",
        action="store_true",
        help="reduce hard-fail behavior for missing run-scoped artifacts",
    )
    parser.add_argument("--write-report", default="", help="optional explicit report path")
    args = parser.parse_args()

    logs_dir = _resolve_logs_dir(args.logs_dir or None)
    summary = reconcile_phase8(
        run_id=(args.run_id or None),
        symbol=(args.symbol or None),
        logs_dir=logs_dir,
        state_dir=(args.state_dir or None),
        trade_date=(args.trade_date or None),
        prefer_adapter_truth=not bool(args.prefer_run_truth),
        strict=not bool(args.lenient),
    )

    for line in format_reconcile_lines(summary):
        print(line)

    report_path = args.write_report or _default_report_path(
        logs_dir=logs_dir,
        run_id=(args.run_id or None),
        symbol=(args.symbol or None),
    )
    write_reconcile_report(report_path, summary)
    print(f"[RECON] report={report_path}")

    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())