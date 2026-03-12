#!/usr/bin/env python3
# runner_live.py
import asyncio
import time
import os
import sys
import inspect
import traceback
import json
import csv
import tempfile
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional, List, Dict, Tuple


# --- BOOTSTRAP FOR FILE EXECUTION --------------------------------------------
# Repo-root layout (C:\Argus\repo\*.py):
# Allows:
#   1) python -m runner_live
#   2) python runner_live.py
#
# Ensures repo root is on sys.path so `from config import ...` works.
if __package__ in (None, ""):
    # line above: here = os.path.dirname(os.path.abspath(__file__))
    here = os.path.dirname(os.path.abspath(__file__))   # ...\repo
    if here not in sys.path:
        sys.path.insert(0, here)


# --- PACKAGE IMPORTS ----------------------------------------------------------
# line above: from config import load_config
from config import load_config
from feed_coinbase import make_http, fetch_spot_price, preload_indicator_history
from io_logs import (
    ensure_logs,
    ensure_signals_header_matches_file,
    is_kill_switch_on,
    is_paused,
    log_event,
    log_signal_snapshot,
)
from notify import maybe_notify_discord
from state import BotState
from engine import step
from utils import safe_str
import runtime_mode
from ops.health import run_health_checks, log_health_report
from ops.run_manifest import create_manifest, save_manifest, finalize_manifest, check_config_drift
from ops.auto_throttle import apply_throttle_to_qty, record_trade_result
from ops.artifact_integrity import verify_artifact_integrity, save_integrity_manifest, check_fills_csv_integrity
from ops.alerting import alert_invariant_failure, alert_mode_transition, alert_halt


# -----------------------------
# Phase 8 imports
# -----------------------------
# line above: from utils import safe_str
from execution.adapter import OrderRequest, FillState, OrderState, PositionState
from execution.recovery import (
    reconcile_on_startup as canonical_reconcile_on_startup,
    format_recovery_log_lines,
    write_reconcile_report,
    BOT_STATE_FLAT,
    BOT_STATE_ENTERING,
    BOT_STATE_OPEN,
    BOT_STATE_EXITING,
    BOT_STATE_RECOVERY_HALT,
)
from trade_journal import (
    row_from_closed_trade,  # retained for compatibility / existing imports
    append_trade_journal_row as append_canonical_trade_journal_row,
)


class ControlledAbort(RuntimeError):
    pass


def _as_bool(v: Any, default: bool = False) -> bool:
    """
    Robust bool coercion for env/config values.

    Critical for Phase 8 because bool("false") == True in Python.
    """
    # line above: if isinstance(v, bool):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


def _load_cfg(cfg_path: Optional[str] = None) -> dict[str, Any]:
    """
    Robust config loader:
      - If load_config supports cfg_path/path/filename (kw or positional), pass it.
      - Otherwise, call load_config() with no args.

    This prevents: "load_config() takes 0 positional arguments but 1 was given"
    """
    # line above: if not cfg_path:
    if not cfg_path:
        return load_config()

    try:
        sig = inspect.signature(load_config)
        params = sig.parameters
    except Exception:
        return load_config()

    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        try:
            return load_config(cfg_path=cfg_path)  # type: ignore[call-arg]
        except Exception:
            return load_config(path=cfg_path)      # type: ignore[call-arg]

    for key in ("cfg_path", "path", "filename", "config_path", "config"):
        if key in params:
            try:
                return load_config(**{key: cfg_path})  # type: ignore[call-arg]
            except TypeError:
                break

    positional_slots = [
        p for p in params.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional_slots) >= 1:
        try:
            return load_config(cfg_path)  # type: ignore[misc]
        except TypeError:
            pass

    cfg = load_config()
    try:
        log_event(
            "?",
            "CFG_PATH_IGNORED",
            f"load_config signature={sig} does not accept path; ignored cfg_path={cfg_path}",
        )
    except Exception:
        pass
    return cfg


async def _close_http(http: Any) -> None:
    """
    Close http client whether close() is sync or async.
    """
    # line above: close = getattr(http, "close", None)
    try:
        close = getattr(http, "close", None)
        if not callable(close):
            return
        r = close()
        if inspect.isawaitable(r):
            await r
    except Exception:
        pass


def _clamp_tick_time(*, state: Any, tick: Any, cfg: dict[str, Any], symbol: str) -> None:
    """
    Enforce:
      1) epoch present and not wildly drifting vs system time
      2) monotonic non-decreasing epoch across ticks (no rewinds)

    Mutates tick.epoch and tick.ts (best-effort).
    """
    # line above: now_sys = int(time.time())
    now_sys = int(time.time())
    te = int(getattr(tick, "epoch", 0) or 0)
    max_drift = int(cfg.get("MAX_EPOCH_DRIFT_SECONDS", 3600))

    changed = False
    orig = te

    if te <= 0 or abs(te - now_sys) > max_drift:
        te = now_sys
        changed = True

    last_te = int(getattr(state, "_last_tick_epoch", 0) or 0)
    if last_te > 0 and te < last_te:
        te = last_te
        changed = True

    setattr(state, "_last_tick_epoch", te)

    try:
        setattr(tick, "epoch", te)
    except Exception:
        pass
    try:
        setattr(tick, "ts", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(te)) + "+00:00")
    except Exception:
        pass

    if changed:
        try:
            log_event(symbol, "EPOCH_CLAMP", f"tick.epoch={orig} -> {te} (max_drift={max_drift}s, last={last_te})")
        except Exception:
            pass


def _execution_mode(cfg: dict[str, Any], state: Any) -> str:
    mode = str(getattr(state, "execution_mode", "") or cfg.get("EXECUTION_MODE", "ENGINE")).strip().upper()
    return mode or "ENGINE"


def _adapter_mode_enabled(cfg: dict[str, Any], state: Any) -> bool:
    if _execution_mode(cfg, state) != "ADAPTER":
        return False
    return getattr(state, "execution_adapter", None) is not None


def _runner_should_write_canonical_execution(cfg: dict[str, Any]) -> bool:
    """
    Canonical execution surfaces (orders.csv/fills.csv/positions.csv/account.csv)
    have exactly one owner.

    In ADAPTER mode, the execution adapter owns these files.
    In non-ADAPTER modes, runner may own them.
    """
    # line above: mode = str(cfg.get("EXECUTION_MODE", "ENGINE")).strip().upper()
    mode = str(cfg.get("EXECUTION_MODE", "ENGINE")).strip().upper()
    return mode != "ADAPTER"


def _normalize_epoch_seconds(e: int) -> int:
    # line above: def _adapter_mode_enabled(cfg: dict[str, Any], state: Any) -> bool:
    if not e:
        return 0
    if e >= 100_000_000_000:
        return int(e // 1000)
    return int(e)


def _to_decimal(v: Any, default: str = "0") -> Decimal:
    if v is None:
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(default)


# line above: def _to_decimal(v: Any, default: str = "0") -> Decimal:
_MONEY_QUANT = Decimal("0.00000001")
_QTY_EPSILON = Decimal("0.00000001")


def _q_money8(v: Any) -> Decimal:
    return _to_decimal(v, "0").quantize(_MONEY_QUANT, rounding=ROUND_DOWN)


def _q_qty8(v: Any) -> Decimal:
    return _to_decimal(v, "0").quantize(_MONEY_QUANT, rounding=ROUND_DOWN)


def _is_effectively_flat_qty(v: Any) -> bool:
    return abs(_q_qty8(v)) < _QTY_EPSILON or _q_qty8(v) <= Decimal("0")


def _is_effectively_open_qty(v: Any) -> bool:
    return _q_qty8(v) >= _QTY_EPSILON


def _repo_root() -> str:
    # line above: def _q_money8(v: Any) -> Decimal:
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_runtime_log_dir(cfg: dict[str, Any]) -> str:
    """
    Single canonical live/runtime artifact root.

    Precedence:
      1) RUNTIME_LOG_DIR
      2) OPS_LOG_DIR
      3) LOG_DIR
      4) repo/ops/logs

    This intentionally collapses split-brain artifact roots.
    """
    # line above: repo = _repo_root()
    repo = _repo_root()
    candidates = [
        cfg.get("RUNTIME_LOG_DIR"),
        cfg.get("OPS_LOG_DIR"),
        cfg.get("LOG_DIR"),
        os.path.join(repo, "ops", "logs"),
    ]
    for c in candidates:
        if c and str(c).strip():
            return os.path.abspath(str(c))
    return os.path.abspath(os.path.join(repo, "ops", "logs"))


def _resolve_runtime_state_dir(cfg: dict[str, Any]) -> str:
    """
    Runtime state dir anchored under the repo unless explicitly overridden.
    """
    # line above: repo = _repo_root()
    repo = _repo_root()
    candidates = [
        cfg.get("STATE_DIR"),
        os.path.join(repo, "state"),
    ]
    for c in candidates:
        if c and str(c).strip():
            return os.path.abspath(str(c))
    return os.path.abspath(os.path.join(repo, "state"))


def _atomic_write_text(path: str, content: str) -> None:
    # line above: directory = os.path.dirname(path)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".swap", dir=directory or None, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _build_fallback_order_row(run_id: str, order: OrderState) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "ts": _normalize_epoch_seconds(int(order.ts or 0)),
        "order_id": str(order.order_id),
        "client_order_id": str(order.client_order_id),
        "symbol": str(order.symbol),
        "side": str(order.side),
        "qty": str(order.qty),
        "order_type": str(order.order_type),
        "limit_px": "" if order.limit_px is None else str(order.limit_px),
        "stop_px": "" if order.stop_px is None else str(order.stop_px),
        "status": str(order.status),
        "filled_qty": str(order.filled_qty),
        "remaining_qty": str(order.remaining_qty),
        "avg_fill_px": "" if order.avg_fill_px is None else str(order.avg_fill_px),
    }


def _build_fallback_fill_row(run_id: str, fill: FillState) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "ts": _normalize_epoch_seconds(int(fill.ts or 0)),
        "fill_id": str(fill.fill_id),
        "trade_id": str(fill.trade_id or ""),
        "order_id": str(fill.order_id),
        "client_order_id": str(fill.client_order_id),
        "symbol": str(fill.symbol),
        "side": str(fill.side),
        "qty": str(fill.qty),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "fee_currency": str(fill.fee_currency),
        "liquidity": str(fill.liquidity or ""),
    }


def _ensure_csv_with_header(path: str, header: List[str]) -> None:
    # line above: if not os.path.exists(path):
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(header) + "\n")
            f.flush()
            os.fsync(f.fileno())


def _append_csv_row(path: str, row: Dict[str, Any], header: List[str]) -> None:
    _ensure_csv_with_header(path, header)
    vals: List[str] = []
    for k in header:
        v = row.get(k, "")
        s = "" if v is None else str(v)
        s = s.replace('"', '""')
        if any(ch in s for ch in [",", "\n", '"']):
            s = f'"{s}"'
        vals.append(s)
    with open(path, "a", encoding="utf-8", newline="") as f:
        f.write(",".join(vals) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _append_orders_rows(log_dir: str, run_id: str, rows: List[Dict[str, Any]]) -> None:
    # line above: header = [... ]
    header = [
        "run_id",
        "ts",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "order_type",
        "limit_px",
        "stop_px",
        "status",
        "filled_qty",
        "remaining_qty",
        "avg_fill_px",
    ]
    path = os.path.join(log_dir, f"orders_{run_id}.csv")
    for row in rows:
        _append_csv_row(path, row, header)


def _append_fills_rows(log_dir: str, run_id: str, rows: List[Dict[str, Any]]) -> None:
    # line above: header = [... ]
    header = [
        "run_id",
        "ts",
        "fill_id",
        "trade_id",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "price",
        "fee",
        "fee_currency",
        "liquidity",
    ]
    path = os.path.join(log_dir, f"fills_{run_id}.csv")
    for row in rows:
        _append_csv_row(path, row, header)


def _append_positions_rows(log_dir: str, run_id: str, positions: List[PositionState], now_ts: int) -> None:
    # line above: header = [... ]
    header = [
        "run_id",
        "ts",
        "symbol",
        "qty",
        "avg_entry_px",
        "mark_px",
        "unrealized_pnl",
        "realized_pnl",
        "side",
    ]
    path = os.path.join(log_dir, f"positions_{run_id}.csv")
    for pos in positions:
        row = {
            "run_id": run_id,
            "ts": _normalize_epoch_seconds(now_ts),
            "symbol": str(pos.symbol),
            "qty": str(pos.qty),
            "avg_entry_px": str(pos.avg_entry_px),
            "mark_px": "" if pos.mark_px is None else str(pos.mark_px),
            "unrealized_pnl": str(pos.unrealized_pnl),
            "realized_pnl": str(pos.realized_pnl),
            "side": str(pos.side),
        }
        _append_csv_row(path, row, header)


def _ledger_realized_pnl(state: Any) -> Decimal:
    # line above: def _append_positions_rows(log_dir: str, run_id: str, positions: List[PositionState], now_ts: int) -> None:
    for attr in ("realized_pnl", "realized_pnl_usd", "realized", "pnl_realized"):
        if hasattr(state.ledger, attr):
            return _to_decimal(getattr(state.ledger, attr), "0")
    return Decimal("0")


def _append_terminal_flat_position_row(
    log_dir: str,
    run_id: str,
    symbol: str,
    mark_px: Any,
    realized_pnl: Decimal,
    now_ts: int,
) -> None:
    # line above: def _ledger_realized_pnl(state: Any) -> Decimal:
    header = [
        "run_id",
        "ts",
        "symbol",
        "qty",
        "avg_entry_px",
        "mark_px",
        "unrealized_pnl",
        "realized_pnl",
        "side",
    ]
    path = os.path.join(log_dir, f"positions_{run_id}.csv")
    row = {
        "run_id": run_id,
        "ts": _normalize_epoch_seconds(now_ts),
        "symbol": str(symbol),
        "qty": "0",
        "avg_entry_px": "0",
        "mark_px": "" if mark_px is None else str(mark_px),
        "unrealized_pnl": "0",
        "realized_pnl": str(realized_pnl),
        "side": "FLAT",
    }
    _append_csv_row(path, row, header)


# -----------------------------------------------------------------------------
# Canonical execution-surface writers
# -----------------------------------------------------------------------------

def _canonical_orders_header() -> List[str]:
    return [
        "ts",
        "event_type",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "order_type",
        "status",
        "qty",
        "filled_qty",
        "remaining_qty",
        "avg_fill_px",
        "limit_px",
        "stop_px",
        "adapter",
        "raw_json",
    ]


def _canonical_fills_header() -> List[str]:
    return [
        "ts",
        "fill_id",
        "trade_id",
        "order_id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "price",
        "fee",
        "fee_currency",
        "liquidity",
        "adapter",
        "raw_json",
    ]


def _canonical_positions_header() -> List[str]:
    return [
        "ts",
        "reason",
        "symbol",
        "qty",
        "avg_entry_px",
        "mark_px",
        "unrealized_pnl",
        "realized_pnl",
        "side",
        "adapter",
        "raw_json",
    ]


def _canonical_account_header() -> List[str]:
    return [
        "ts",
        "reason",
        "equity",
        "cash",
        "buying_power",
        "realized_pnl",
        "unrealized_pnl",
        "currency",
        "adapter",
        "raw_json",
    ]


def _append_canonical_order_row(cfg: dict[str, Any], order: OrderState, event_type: str = "SNAPSHOT") -> None:
    # line above: if not _runner_should_write_canonical_execution(cfg):
    if not _runner_should_write_canonical_execution(cfg):
        return

    raw_json = json.dumps(order.raw or {}, sort_keys=True)
    row = {
        "ts": int(order.ts or 0),
        "event_type": str(event_type),
        "order_id": str(order.order_id),
        "client_order_id": str(order.client_order_id),
        "symbol": str(order.symbol),
        "side": str(order.side),
        "order_type": str(order.order_type),
        "status": str(order.status),
        "qty": str(order.qty),
        "filled_qty": str(order.filled_qty),
        "remaining_qty": str(order.remaining_qty),
        "avg_fill_px": "" if order.avg_fill_px is None else str(order.avg_fill_px),
        "limit_px": "" if order.limit_px is None else str(order.limit_px),
        "stop_px": "" if order.stop_px is None else str(order.stop_px),
        "adapter": "paper",
        "raw_json": raw_json,
    }
    _append_csv_row(str(cfg["LIVE_ORDERS_CSV"]), row, _canonical_orders_header())


def _append_canonical_fill_row(cfg: dict[str, Any], fill: FillState) -> None:
    # line above: if not _runner_should_write_canonical_execution(cfg):
    if not _runner_should_write_canonical_execution(cfg):
        return

    raw_json = json.dumps(fill.raw or {}, sort_keys=True)
    row = {
        "ts": int(fill.ts or 0),
        "fill_id": str(fill.fill_id),
        "trade_id": str(fill.trade_id or ""),
        "order_id": str(fill.order_id),
        "client_order_id": str(fill.client_order_id),
        "symbol": str(fill.symbol),
        "side": str(fill.side),
        "qty": str(fill.qty),
        "price": str(fill.price),
        "fee": str(fill.fee),
        "fee_currency": str(fill.fee_currency),
        "liquidity": str(fill.liquidity or ""),
        "adapter": "paper",
        "raw_json": raw_json,
    }
    _append_csv_row(str(cfg["LIVE_FILLS_CSV"]), row, _canonical_fills_header())


def _append_canonical_position_row(
    cfg: dict[str, Any],
    *,
    reason: str,
    ts_ms: int,
    symbol: str,
    qty: Any,
    avg_entry_px: Any,
    mark_px: Any,
    unrealized_pnl: Any,
    realized_pnl: Any,
    side: str,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    # line above: if not _runner_should_write_canonical_execution(cfg):
    if not _runner_should_write_canonical_execution(cfg):
        return

    raw_json = json.dumps(raw or {}, sort_keys=True)
    row = {
        "ts": int(ts_ms or 0),
        "reason": str(reason),
        "symbol": str(symbol),
        "qty": str(qty),
        "avg_entry_px": "" if avg_entry_px is None else str(avg_entry_px),
        "mark_px": "" if mark_px is None else str(mark_px),
        "unrealized_pnl": str(unrealized_pnl),
        "realized_pnl": str(realized_pnl),
        "side": str(side),
        "adapter": "paper",
        "raw_json": raw_json,
    }
    _append_csv_row(str(cfg["LIVE_POSITIONS_CSV"]), row, _canonical_positions_header())


def _append_canonical_account_row(
    cfg: dict[str, Any],
    *,
    reason: str,
    ts_ms: int,
    equity: Any,
    cash: Any,
    buying_power: Any,
    realized_pnl: Any,
    unrealized_pnl: Any,
    currency: str,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    # line above: if not _runner_should_write_canonical_execution(cfg):
    if not _runner_should_write_canonical_execution(cfg):
        return

    raw_json = json.dumps(raw or {}, sort_keys=True)
    row = {
        "ts": int(ts_ms or 0),
        "reason": str(reason),
        "equity": str(equity),
        "cash": str(cash),
        "buying_power": str(buying_power),
        "realized_pnl": str(realized_pnl),
        "unrealized_pnl": str(unrealized_pnl),
        "currency": str(currency),
        "adapter": "paper",
        "raw_json": raw_json,
    }
    _append_csv_row(str(cfg["LIVE_ACCOUNT_CSV"]), row, _canonical_account_header())


def _mirror_adapter_state_to_canonical(
    *,
    cfg: dict[str, Any],
    state: Any,
    reason: str,
    ts_ms: int,
    symbol: str,
    force_flat_row: bool = False,
    mark_px: Any = None,
) -> None:
    """
    Mirror adapter-derived position/account state into canonical recovery surfaces.

    In ADAPTER mode the adapter already owns canonical execution CSVs, so
    runner must not co-write them.
    """
    # line above: if not _runner_should_write_canonical_execution(cfg):
    if not _runner_should_write_canonical_execution(cfg):
        return

    adapter = getattr(state, "execution_adapter", None)
    if adapter is None:
        return

    try:
        acct = adapter.get_account_state()
    except Exception:
        acct = None

    try:
        positions = list(adapter.get_positions() or [])
    except Exception:
        positions = []

    pos = None
    for p in positions:
        if str(getattr(p, "symbol", "")) == str(symbol):
            pos = p
            break

    if force_flat_row and pos is None:
        _append_canonical_position_row(
            cfg,
            reason=reason,
            ts_ms=ts_ms,
            symbol=symbol,
            qty="0",
            avg_entry_px="0",
            mark_px="" if mark_px is None else str(mark_px),
            unrealized_pnl="0",
            realized_pnl=str(_ledger_realized_pnl(state)),
            side="FLAT",
            raw={},
        )
    elif pos is not None:
        _append_canonical_position_row(
            cfg,
            reason=reason,
            ts_ms=ts_ms,
            symbol=str(pos.symbol),
            qty=str(pos.qty),
            avg_entry_px=str(pos.avg_entry_px),
            mark_px="" if pos.mark_px is None else str(pos.mark_px),
            unrealized_pnl=str(pos.unrealized_pnl),
            realized_pnl=str(pos.realized_pnl),
            side=str(pos.side),
            raw=getattr(pos, "raw", {}) or {},
        )

    if acct is not None:
        _append_canonical_account_row(
            cfg,
            reason=reason,
            ts_ms=ts_ms,
            equity=str(acct.equity),
            cash=str(acct.cash),
            buying_power=str(acct.buying_power),
            realized_pnl=str(acct.realized_pnl),
            unrealized_pnl=str(acct.unrealized_pnl),
            currency=str(acct.currency),
            raw=getattr(acct, "raw", {}) or {},
        )


def _persist_submit_transition(
    *,
    cfg: dict[str, Any],
    log_dir: str,
    run_id: str,
    state: Any,
    order: OrderState,
    symbol: str,
    ts_ms: int,
) -> None:
    # line above: order_row = _build_fallback_order_row(run_id, order)
    order_row = _build_fallback_order_row(run_id, order)
    _append_orders_rows(log_dir, run_id, [order_row])
    _append_canonical_order_row(cfg, order, event_type="SUBMIT")
    _mirror_adapter_state_to_canonical(
        cfg=cfg,
        state=state,
        reason="ORDER_SUBMIT",
        ts_ms=ts_ms,
        symbol=symbol,
    )


def _persist_fill_transition(
    *,
    cfg: dict[str, Any],
    log_dir: str,
    run_id: str,
    state: Any,
    fill: FillState,
    symbol: str,
    recovery_state: str,
    wrote_flat_row: bool,
) -> None:
    # line above: fill_row = _build_fallback_fill_row(run_id, fill)
    fill_row = _build_fallback_fill_row(run_id, fill)
    _append_fills_rows(log_dir, run_id, [fill_row])
    _append_canonical_fill_row(cfg, fill)
    _mirror_adapter_state_to_canonical(
        cfg=cfg,
        state=state,
        reason="FILL_APPLIED",
        ts_ms=int(fill.ts or 0),
        symbol=symbol,
        force_flat_row=(recovery_state == BOT_STATE_FLAT and wrote_flat_row),
        mark_px=getattr(fill, "price", None),
    )


# -----------------------------------------------------------------------------
# Trade journal helpers
# -----------------------------------------------------------------------------

def _get_closed_trade_keys(state: Any) -> set[str]:
    # line above: keys = getattr(state, "_closed_trade_keys", None)
    keys = getattr(state, "_closed_trade_keys", None)
    if isinstance(keys, set):
        return keys
    if isinstance(keys, list):
        out = set(str(x) for x in keys)
        setattr(state, "_closed_trade_keys", out)
        return out
    out: set[str] = set()
    setattr(state, "_closed_trade_keys", out)
    return out


def _get_open_trade_ctx(state: Any) -> Dict[str, Any]:
    # line above: ctx = getattr(state, "_open_trade_ctx", None)
    ctx = getattr(state, "_open_trade_ctx", None)
    if isinstance(ctx, dict):
        return ctx
    ctx = {}
    setattr(state, "_open_trade_ctx", ctx)
    return ctx


def _set_open_trade_ctx(state: Any, ctx: Dict[str, Any]) -> None:
    # line above: setattr(state, "_open_trade_ctx", dict(ctx))
    setattr(state, "_open_trade_ctx", dict(ctx))


def _clear_open_trade_ctx(state: Any) -> None:
    # line above: setattr(state, "_open_trade_ctx", {})
    setattr(state, "_open_trade_ctx", {})


def _iso_from_epoch(epoch_s: int) -> str:
    # line above: epoch_s = _normalize_epoch_seconds(int(epoch_s or 0))
    epoch_s = _normalize_epoch_seconds(int(epoch_s or 0))
    if epoch_s <= 0:
        epoch_s = int(time.time())
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_s)) + "+00:00"


def _build_journal_key(
    *,
    symbol: str,
    trade_id: str,
    entry_order_id: str,
    exit_order_id: str,
    entry_fill_id: str,
    exit_fill_id: str,
) -> str:
    # line above: return "|".join([
    return "|".join([
        str(symbol or ""),
        str(trade_id or ""),
        str(entry_order_id or ""),
        str(exit_order_id or ""),
        str(entry_fill_id or ""),
        str(exit_fill_id or ""),
    ])


def _capture_entry_context_from_fill(
    *,
    state: Any,
    fill: FillState,
    snap: Any,
    cfg: Dict[str, Any],
) -> None:
    # line above: fill_ts = _normalize_epoch_seconds(int(fill.ts or 0))
    fill_ts = _normalize_epoch_seconds(int(fill.ts or 0))
    _slip_raw = cfg.get("PAPER_SLIPPAGE_BPS", cfg.get("SLIPPAGE_BPS", ""))
    slippage_bps_val = "" if _slip_raw is None else str(_slip_raw)
    ctx = {
        "symbol": str(fill.symbol),
        "trade_id": str(fill.trade_id or getattr(state, "open_trade_id", "") or ""),
        "entry_fill_id": str(fill.fill_id),
        "entry_order_id": str(fill.order_id),
        "entry_client_order_id": str(fill.client_order_id),
        "entry_ts": fill_ts,
        "entry_time": _iso_from_epoch(fill_ts),
        "entry_px": str(fill.price),
        "qty": str(fill.qty),
        "entry_reason": str(
            getattr(snap, "action_reason", "") or
            getattr(snap, "execution_reason", "") or
            "ENTRY"
        ),
        # Phase 9 fields captured at entry time
        "entry_fee": str(fill.fee if fill.fee is not None else "0"),
        "regime_at_entry": str(getattr(snap, "regime", "UNKNOWN") or "UNKNOWN"),
        "entry_mid_px": str(getattr(snap, "px", "") or ""),
        "slippage_bps": slippage_bps_val,
    }
    _set_open_trade_ctx(state, ctx)


def _derive_open_trade_ctx_from_fills_rows(
    *,
    fills_rows: List[Dict[str, str]],
    symbol: str,
) -> Dict[str, Any]:
    """
    Reconstruct the currently open trade context from fills.
    Assumes single-position lifecycle: BUY opens, SELL closes.
    """
    # line above: rows = [
    rows = [
        r for r in fills_rows
        if str(r.get("symbol", "") or "") == symbol
    ]
    rows = sorted(
        rows,
        key=lambda r: _normalize_epoch_seconds(int(str(r.get("ts", "0") or "0"))),
    )

    open_ctx: Dict[str, Any] = {}
    open_qty = Decimal("0")

    for row in rows:
        side = str(row.get("side", "") or "").upper()
        qty = _to_decimal(row.get("qty"), "0")
        px = _to_decimal(row.get("price"), "0")
        ts = _normalize_epoch_seconds(int(str(row.get("ts", "0") or "0")))

        if qty <= 0 or px <= 0 or side not in {"BUY", "SELL"}:
            continue

        if side == "BUY":
            open_qty += qty
            open_ctx = {
                "symbol": str(row.get("symbol", "") or symbol),
                "trade_id": str(row.get("trade_id", "") or ""),
                "entry_fill_id": str(row.get("fill_id", "") or ""),
                "entry_order_id": str(row.get("order_id", "") or ""),
                "entry_client_order_id": str(row.get("client_order_id", "") or ""),
                "entry_ts": ts,
                "entry_time": _iso_from_epoch(ts),
                "entry_px": str(px),
                "qty": str(qty),
                "entry_reason": "ENTRY",
                "entry_fee": str(_to_decimal(row.get("fee"), "0")),
            }

        elif side == "SELL":
            open_qty -= qty
            if open_qty <= 0:
                open_qty = Decimal("0")
                open_ctx = {}

    return open_ctx if open_qty > 0 else {}


def _build_close_journal_row_from_fill(
    *,
    state: Any,
    fill: FillState,
    snap: Any,
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """
    Build exactly one canonical completed-trade row on the transition to flat.
    Assumes ledger has already applied the SELL fill so realized pnl is current.
    """
    # line above: entry_ctx = _get_open_trade_ctx(state)
    entry_ctx = _get_open_trade_ctx(state)
    if not entry_ctx:
        return None

    entry_ts = _normalize_epoch_seconds(int(entry_ctx.get("entry_ts", 0) or 0))
    exit_ts = _normalize_epoch_seconds(int(fill.ts or 0))

    entry_px = _to_decimal(entry_ctx.get("entry_px"), "0")
    exit_px = _to_decimal(fill.price, "0")
    qty = _to_decimal(entry_ctx.get("qty"), "0")
    fee = _to_decimal(fill.fee, "0")

    if entry_px <= 0 or exit_px <= 0 or qty <= 0:
        return None

    # line above: if entry_px <= 0 or exit_px <= 0 or qty <= 0:
    net_pnl = _q_money8((exit_px - entry_px) * qty - fee)

    entry_order_id = str(entry_ctx.get("entry_order_id", "") or "")
    exit_order_id = str(fill.order_id)
    entry_fill_id = str(entry_ctx.get("entry_fill_id", "") or "")
    exit_fill_id = str(fill.fill_id)
    trade_id = str(fill.trade_id or entry_ctx.get("trade_id", "") or getattr(state, "open_trade_id", "") or "")

    journal_key = _build_journal_key(
        symbol=symbol,
        trade_id=trade_id,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        entry_fill_id=entry_fill_id,
        exit_fill_id=exit_fill_id,
    )

    # Phase 9: assemble lifecycle finalization fields
    entry_fee_val = str(entry_ctx.get("entry_fee", "") or "0")
    exit_fee_val = str(fill.fee if fill.fee is not None else "0")
    try:
        total_fee_val = str(_q_money8(_to_decimal(entry_fee_val, "0") + _to_decimal(exit_fee_val, "0")))
    except Exception:
        total_fee_val = ""
    mae_pct_val = str(entry_ctx.get("final_mae_pct", "") or "0")
    mfe_pct_val = str(entry_ctx.get("final_mfe_pct", "") or "0")
    regime_val = str(entry_ctx.get("regime_at_entry", "") or "UNKNOWN")
    entry_mid_px_val = str(entry_ctx.get("entry_mid_px", "") or "")
    exit_mid_px_val = str(getattr(snap, "px", "") or "")
    slippage_bps_val = str(entry_ctx.get("slippage_bps", "") or "")
    try:
        dur_s = max(0, exit_ts - entry_ts) if entry_ts and exit_ts else 0
        trade_duration_s_val = str(dur_s)
    except Exception:
        trade_duration_s_val = ""

    return {
        "trade_id": trade_id,
        "run_id": "",
        "symbol": str(symbol),
        "entry_time": str(entry_ctx.get("entry_time", "") or _iso_from_epoch(entry_ts)),
        "exit_time": _iso_from_epoch(exit_ts),
        "entry_px": str(entry_px),
        "exit_px": str(exit_px),
        "qty": str(qty),
        "pnl": str(net_pnl),
        "entry_reason": str(entry_ctx.get("entry_reason", "") or "ENTRY"),
        "exit_reason": str(
            getattr(snap, "action_reason", "") or
            getattr(snap, "execution_reason", "") or
            "EXIT"
        ),
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "entry_fill_id": entry_fill_id,
        "exit_fill_id": exit_fill_id,
        "state_open_ts": _iso_from_epoch(entry_ts),
        "state_closed_ts": _iso_from_epoch(exit_ts),
        # Phase 9 lifecycle finalization
        "entry_fee": entry_fee_val,
        "exit_fee": exit_fee_val,
        "total_fee": total_fee_val,
        "mae_pct": mae_pct_val,
        "mfe_pct": mfe_pct_val,
        "regime_at_entry": regime_val,
        "trade_duration_s": trade_duration_s_val,
        "slippage_bps": slippage_bps_val,
        "entry_mid_px": entry_mid_px_val,
        "exit_mid_px": exit_mid_px_val,
        "journal_key": journal_key,
    }


def _append_trade_journal_row(
    *,
    log_dir: str,
    run_id: str,
    row: Dict[str, Any],
) -> None:
    """
    Delegate to canonical trade_journal.py writer.
    """
    # line above: payload = dict(row)
    payload = dict(row)
    payload["run_id"] = run_id
    payload.pop("journal_key", None)
    append_canonical_trade_journal_row(log_dir=log_dir, run_id=run_id, row=payload)


# line above:     append_canonical_trade_journal_row(log_dir=log_dir, run_id=run_id, row=payload)
def _load_existing_trade_journal_keys(log_dir: str) -> set[str]:
    keys: set[str] = set()

    if not os.path.isdir(log_dir):
        return keys

    files: List[str] = []
    try:
        for name in os.listdir(log_dir):
            if name.startswith("trade_journal") and name.endswith(".csv"):
                files.append(os.path.join(log_dir, name))
    except Exception:
        return keys

    files.sort()

    for path in files:
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    journal_key = str(row.get("journal_key", "") or "").strip()
                    if not journal_key:
                        journal_key = _build_journal_key(
                            symbol=str(row.get("symbol", "") or ""),
                            trade_id=str(row.get("trade_id", "") or ""),
                            entry_order_id=str(row.get("entry_order_id", "") or ""),
                            exit_order_id=str(row.get("exit_order_id", "") or ""),
                            entry_fill_id=str(row.get("entry_fill_id", "") or ""),
                            exit_fill_id=str(row.get("exit_fill_id", "") or ""),
                        )
                    if journal_key.strip("|"):
                        keys.add(journal_key)
        except Exception:
            pass

    return keys


def _derive_latest_closed_trade_from_fills_rows(
    *,
    fills_rows: List[Dict[str, str]],
    symbol: str,
) -> Optional[Dict[str, Any]]:
    """
    Reconstruct the latest fully closed trade from canonical fills only.
    Single-position lifecycle assumption:
      BUY opens / accumulates
      SELL reduces / closes
    """
    rows = [
        r for r in fills_rows
        if str(r.get("symbol", "") or "") == symbol
    ]
    rows = sorted(
        rows,
        key=lambda r: _normalize_epoch_seconds(int(str(r.get("ts", "0") or "0"))),
    )

    if not rows:
        return None

    open_qty = Decimal("0")
    avg_entry = Decimal("0")
    realized = Decimal("0")
    entry_ctx: Dict[str, Any] = {}
    latest_closed: Optional[Dict[str, Any]] = None

    for row in rows:
        side = str(row.get("side", "") or "").upper()
        qty = _to_decimal(row.get("qty"), "0")
        px = _to_decimal(row.get("price"), "0")
        fee = _to_decimal(row.get("fee"), "0")
        ts = _normalize_epoch_seconds(int(str(row.get("ts", "0") or "0")))
        fill_id = str(row.get("fill_id", "") or "")
        order_id = str(row.get("order_id", "") or "")
        client_order_id = str(row.get("client_order_id", "") or "")
        trade_id = str(row.get("trade_id", "") or "")

        if qty <= 0 or px <= 0 or side not in {"BUY", "SELL"}:
            continue

        if side == "BUY":
            if open_qty <= 0:
                entry_ctx = {
                    "symbol": symbol,
                    "trade_id": trade_id,
                    "entry_fill_id": fill_id,
                    "entry_order_id": order_id,
                    "entry_client_order_id": client_order_id,
                    "entry_ts": ts,
                    "entry_time": _iso_from_epoch(ts),
                    "entry_px": str(px),
                    "qty": str(qty),
                    "entry_reason": "RECOVERED_ENTRY",
                    "entry_fee": str(fee),
                }
                open_qty = qty
                avg_entry = px
                realized = Decimal("0")
            else:
                notional_before = open_qty * avg_entry
                notional_add = qty * px
                open_qty += qty
                avg_entry = (notional_before + notional_add) / open_qty
                entry_ctx["qty"] = str(_q_money8(_to_decimal(entry_ctx.get("qty"), "0") + qty))

        elif side == "SELL" and open_qty > 0:
            close_qty = qty if qty <= open_qty else open_qty
            realized += (px - avg_entry) * close_qty - fee
            open_qty -= close_qty

            if open_qty <= 0:
                open_qty = Decimal("0")
                trade_id_final = trade_id or str(entry_ctx.get("trade_id", "") or "")
                entry_order_id = str(entry_ctx.get("entry_order_id", "") or "")
                entry_fill_id = str(entry_ctx.get("entry_fill_id", "") or "")
                exit_order_id = order_id
                exit_fill_id = fill_id
                entry_ts_val = int(entry_ctx.get("entry_ts", 0) or 0)
                entry_fee_val = str(_to_decimal(entry_ctx.get("entry_fee", "0"), "0"))
                exit_fee_val = str(fee)
                try:
                    total_fee_val = str(_q_money8(
                        _to_decimal(entry_fee_val, "0") + _to_decimal(exit_fee_val, "0")
                    ))
                except Exception:
                    total_fee_val = ""
                try:
                    dur_s = max(0, ts - entry_ts_val) if entry_ts_val and ts else 0
                    trade_duration_s_val = str(dur_s)
                except Exception:
                    trade_duration_s_val = ""

                latest_closed = {
                    "trade_id": trade_id_final,
                    "run_id": "",
                    "symbol": symbol,
                    "entry_time": str(entry_ctx.get("entry_time", "") or _iso_from_epoch(entry_ts_val)),
                    "exit_time": _iso_from_epoch(ts),
                    "entry_px": str(_to_decimal(entry_ctx.get("entry_px"), "0")),
                    "exit_px": str(px),
                    "qty": str(_to_decimal(entry_ctx.get("qty"), "0")),
                    "pnl": str(_q_money8(realized)),
                    "entry_reason": str(entry_ctx.get("entry_reason", "") or "RECOVERED_ENTRY"),
                    "exit_reason": "RECOVERED_CLOSE_FINALIZE",
                    "entry_order_id": entry_order_id,
                    "exit_order_id": exit_order_id,
                    "entry_fill_id": entry_fill_id,
                    "exit_fill_id": exit_fill_id,
                    "state_open_ts": str(entry_ctx.get("entry_time", "") or _iso_from_epoch(entry_ts_val)),
                    "state_closed_ts": _iso_from_epoch(ts),
                    # Phase 9 fields recoverable from fills
                    "entry_fee": entry_fee_val,
                    "exit_fee": exit_fee_val,
                    "total_fee": total_fee_val,
                    "trade_duration_s": trade_duration_s_val,
                    "journal_key": _build_journal_key(
                        symbol=symbol,
                        trade_id=trade_id_final,
                        entry_order_id=entry_order_id,
                        exit_order_id=exit_order_id,
                        entry_fill_id=entry_fill_id,
                        exit_fill_id=exit_fill_id,
                    ),
                }
                entry_ctx = {}
                avg_entry = Decimal("0")
                realized = Decimal("0")

    return latest_closed


def _finalize_recovered_flat_close(
    *,
    state: Any,
    cfg: dict[str, Any],
    log_dir: str,
    run_id: str,
    symbol: str,
    recovery_state: str,
    recovery_source: str,
) -> Tuple[bool, str]:
    """
    If startup recovered terminal FLAT from canonical execution truth, but the close
    journal row was never written due to an interrupted close boundary, backfill it
    exactly once here.
    """
    if str(recovery_state or "").upper() != BOT_STATE_FLAT:
        return False, "not_flat"

    if str(recovery_source or "").lower() != "canonical_execution":
        return False, "not_canonical_execution"

    fills_path = str(cfg.get("LIVE_FILLS_CSV", "") or "")
    if not fills_path:
        return False, "missing_fills_path"

    fills_rows = _read_csv_rows(fills_path)
    if not fills_rows:
        return False, "no_fills"

    latest_closed = _derive_latest_closed_trade_from_fills_rows(
        fills_rows=fills_rows,
        symbol=symbol,
    )
    if not latest_closed:
        return False, "no_closed_trade_derived"

    # Hydrate Phase 9 fields that can be derived from cfg (unavailable in fills.csv)
    if not str(latest_closed.get("slippage_bps", "") or ""):
        slippage_bps_cfg = str(cfg.get("PAPER_SLIPPAGE_BPS", cfg.get("SLIPPAGE_BPS", "0")) or "0")
        latest_closed["slippage_bps"] = slippage_bps_cfg
        try:
            slip = _to_decimal(slippage_bps_cfg, "0") / Decimal("10000")
            if slip < Decimal("1"):
                entry_fill_px = _to_decimal(latest_closed.get("entry_px"), "0")
                exit_fill_px = _to_decimal(latest_closed.get("exit_px"), "0")
                if entry_fill_px > 0 and not str(latest_closed.get("entry_mid_px", "") or ""):
                    latest_closed["entry_mid_px"] = str(
                        _q_money8(entry_fill_px / (Decimal("1") + slip))
                    )
                if exit_fill_px > 0 and not str(latest_closed.get("exit_mid_px", "") or ""):
                    latest_closed["exit_mid_px"] = str(
                        _q_money8(exit_fill_px / (Decimal("1") - slip))
                    )
        except Exception:
            pass

    journal_key = str(latest_closed.get("journal_key", "") or "")
    if not journal_key:
        return False, "missing_journal_key"

    existing_keys = _load_existing_trade_journal_keys(log_dir)
    existing_keys |= _get_closed_trade_keys(state)

    if journal_key in existing_keys:
        _get_closed_trade_keys(state).add(journal_key)
        _clear_open_trade_ctx(state)
        return False, "already_present"

    _append_trade_journal_row(
        log_dir=log_dir,
        run_id=run_id,
        row=latest_closed,
    )

    _get_closed_trade_keys(state).add(journal_key)
    _clear_open_trade_ctx(state)

    try:
        setattr(state, "open_trade_id", "")
    except Exception:
        pass

    return True, "backfilled_closed_trade_journal"


def _is_fill_eligible_for_current_run_journal(
    *,
    fill: FillState,
    run_start_ts: int,
    startup_fill_ids: set[str],
) -> Tuple[bool, str]:
    """
    Strong run-boundary guard:
      - fill must not be historical at startup
      - fill ts must be at/after current run start
    """
    # line above: fill_id = str(fill.fill_id or "")
    fill_id = str(fill.fill_id or "")
    fill_ts = _normalize_epoch_seconds(int(fill.ts or 0))

    if not fill_id:
        return False, "missing_fill_id"

    if fill_id in startup_fill_ids:
        return False, "startup_historical_fill"

    if fill_ts <= 0:
        return False, "missing_fill_ts"

    if fill_ts < int(run_start_ts or 0):
        return False, "fill_ts_before_run_start"

    return True, ""


def _maybe_write_closed_trade_journal(
    *,
    state: Any,
    log_dir: str,
    run_id: str,
    fill: FillState,
    snap: Any,
    symbol: str,
    prior_qty: Decimal,
    post_qty: Decimal,
    run_start_ts: int,
    startup_fill_ids: set[str],
) -> Tuple[bool, str]:
    """
    Write exactly one row when a SELL fill transitions the system to flat
    AND the close belongs to the current process lifecycle.
    Returns (appended, reason).
    """
    # line above: fill_side = str(fill.side).upper()
    fill_side = str(fill.side).upper()
    if fill_side != "SELL":
        return False, "not_sell"

    if not (_is_effectively_open_qty(prior_qty) and _is_effectively_flat_qty(post_qty)):
        return False, "not_open_to_flat_transition"

    eligible, reason = _is_fill_eligible_for_current_run_journal(
        fill=fill,
        run_start_ts=run_start_ts,
        startup_fill_ids=startup_fill_ids,
    )
    if not eligible:
        return False, reason

    row = _build_close_journal_row_from_fill(state=state, fill=fill, snap=snap, symbol=symbol)
    if not row:
        return False, "row_build_failed"

    journal_key = str(row.get("journal_key", "") or "")
    if not journal_key:
        return False, "missing_journal_key"

    closed_trade_keys = _get_closed_trade_keys(state)
    if journal_key in closed_trade_keys:
        return False, "duplicate_journal_key"

    _append_trade_journal_row(log_dir=log_dir, run_id=run_id, row=row)
    closed_trade_keys.add(journal_key)
    _clear_open_trade_ctx(state)
    return True, "appended"


def _adapter_update_market_from_tick(adapter: Any, symbol: str, tick: Any) -> None:
    """
    Best-effort market context push for the paper adapter.
    """
    # line above: if not hasattr(adapter, "update_market"):
    if not hasattr(adapter, "update_market"):
        return

    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    last = getattr(tick, "px", None)
    ts = _normalize_epoch_seconds(int(getattr(tick, "epoch", 0) or 0))

    try:
        kwargs: Dict[str, Any] = {"symbol": symbol, "ts": ts}
        if bid is not None:
            kwargs["bid"] = bid
        if ask is not None:
            kwargs["ask"] = ask
        if last is not None:
            kwargs["last"] = last
        adapter.update_market(**kwargs)
    except Exception:
        pass


def _poll_new_fills(
    *,
    state: Any,
    since_ts: int,
) -> List[FillState]:
    adapter = getattr(state, "execution_adapter", None)
    if adapter is None:
        return []
    try:
        fills = adapter.fetch_fills(since_ts=since_ts)
        return list(fills or [])
    except Exception:
        return []


def _submit_adapter_order_from_snapshot(
    *,
    state: Any,
    snap: Any,
    cfg: dict[str, Any],
) -> Optional[OrderState]:
    adapter = getattr(state, "execution_adapter", None)
    if adapter is None:
        return None

    action = str(getattr(snap, "action", "") or "").upper()
    if action not in ("BUY", "SELL"):
        return None

    client_order_id = str(getattr(snap, "client_order_id", "") or "")
    if not client_order_id:
        return None

    qty = Decimal("0")

    if action == "BUY":
        qty = _to_decimal(getattr(snap, "execution_qty", None), "0")
        if qty <= 0:
            raise RuntimeError("BUY signal missing execution_qty or execution_qty <= 0")

    elif action == "SELL":
        qty = _to_decimal(getattr(snap, "execution_qty", None), "0")
        if qty <= 0:
            ledger_qty = _to_decimal(getattr(state.ledger, "position_qty", None), "0")
            if ledger_qty > 0:
                qty = ledger_qty

    if qty <= 0:
        return None

    req = OrderRequest(
        symbol=str(getattr(snap, "symbol", state.symbol)),
        side=action,
        qty=qty,
        order_type="MARKET",
        client_order_id=client_order_id,
        tags={
            "intent_id": str(getattr(snap, "intent_id", "") or ""),
            "entry_intent_id": str(getattr(snap, "entry_intent_id", "") or ""),
            "exit_intent_id": str(getattr(snap, "exit_intent_id", "") or ""),
            "action_reason": str(getattr(snap, "action_reason", "") or ""),
        },
    )

    return adapter.place_order(req)


def _apply_fill_to_ledger(
    *,
    state: Any,
    fill: FillState,
    cfg: dict[str, Any],
) -> None:
    """
    Ledger must be driven by fills, not by raw engine decisions.
    This is a best-effort bridge against existing ledger APIs.
    """
    # line above: ledger = state.ledger
    ledger = state.ledger
    side = str(fill.side).upper()
    px = _to_decimal(fill.price, "0")
    qty = _to_decimal(fill.qty, "0")
    now_e = _normalize_epoch_seconds(int(fill.ts or 0))
    fee = _to_decimal(fill.fee, "0")

    if qty <= 0 or px <= 0:
        return

    if side == "BUY":
        if hasattr(ledger, "buy"):
            ledger.buy(qty, px, now_e, cfg)
        if hasattr(state.risk, "record_entry"):
            state.risk.record_entry(now_e, cfg)

        state.entry_epoch = now_e
        state.peak_price = px
        state.trend_below_count = 0
        state.mfe_pct = Decimal("0")
        state.mae_pct = Decimal("0")
        state.high_since_entry = px
        state.low_since_entry = px

    elif side == "SELL":
        realized_trade = Decimal("0")

        if hasattr(ledger, "sell_all"):
            _, _, realized_trade = ledger.sell_all(px, now_e, cfg)
        elif hasattr(ledger, "sell"):
            try:
                out = ledger.sell(qty, px, now_e, cfg)
                if isinstance(out, tuple) and len(out) >= 3:
                    realized_trade = _to_decimal(out[2], "0")
            except Exception:
                pass

        realized_trade -= fee

        if hasattr(state.risk, "record_exit"):
            state.risk.record_exit(realized_trade, now_e, cfg)

        # Phase 9: snapshot final MAE/MFE into entry_ctx BEFORE reset so the
        # journal writer can read them (they are cleared in the next lines).
        try:
            ctx = _get_open_trade_ctx(state)
            if ctx:
                ctx["final_mae_pct"] = str(getattr(state, "mae_pct", Decimal("0")))
                ctx["final_mfe_pct"] = str(getattr(state, "mfe_pct", Decimal("0")))
                _set_open_trade_ctx(state, ctx)
        except Exception:
            pass

        state.cooldown_until_epoch = now_e + int(cfg.get("COOLDOWN_SECONDS", 0))
        state.trend_below_count = 0
        state.peak_price = None
        state.entry_epoch = None
        state.mfe_pct = Decimal("0")
        state.mae_pct = Decimal("0")
        state.high_since_entry = None
        state.low_since_entry = None


def _runtime_snapshot_path(state_dir: str, symbol: str) -> str:
    # line above: safe_symbol = symbol.replace("/", "_").replace("-", "_")
    safe_symbol = symbol.replace("/", "_").replace("-", "_")
    return os.path.join(state_dir, f"runtime_state_{safe_symbol}.json")


def _read_csv_rows(path: str) -> List[Dict[str, str]]:
    # line above: if not os.path.exists(path):
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            return [dict(row) for row in reader]
    except Exception:
        return []


def _find_latest_matching_file(log_dir: str, prefix: str, suffix: str = ".csv") -> Optional[str]:
    # line above: if not os.path.isdir(log_dir):
    if not os.path.isdir(log_dir):
        return None

    cands: List[Tuple[float, str]] = []
    try:
        for name in os.listdir(log_dir):
            if name.startswith(prefix) and name.endswith(suffix):
                full = os.path.join(log_dir, name)
                try:
                    cands.append((os.path.getmtime(full), full))
                except Exception:
                    pass
    except Exception:
        return None

    if not cands:
        return None

    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1]


def _load_latest_orders_rows(log_dir: str) -> List[Dict[str, str]]:
    # line above: path = _find_latest_matching_file(log_dir, "orders_")
    path = _find_latest_matching_file(log_dir, "orders_")
    if not path:
        return []
    return _read_csv_rows(path)


def _load_all_matching_rows(log_dir: str, prefix: str, suffix: str = ".csv") -> List[Dict[str, str]]:
    # line above: if not os.path.isdir(log_dir):
    if not os.path.isdir(log_dir):
        return []

    files: List[str] = []
    try:
        for name in os.listdir(log_dir):
            if name.startswith(prefix) and name.endswith(suffix):
                files.append(os.path.join(log_dir, name))
    except Exception:
        return []

    files.sort()
    rows: List[Dict[str, str]] = []
    for path in files:
        rows.extend(_read_csv_rows(path))
    return rows


def _load_all_fills_rows(log_dir: str) -> List[Dict[str, str]]:
    # line above: return _read_rows(path)
    return _load_all_matching_rows(log_dir, "fills_")


def _load_latest_positions_rows(log_dir: str) -> List[Dict[str, str]]:
    # line above: path = _find_latest_matching_file(log_dir, "positions_")
    path = _find_latest_matching_file(log_dir, "positions_")
    if not path:
        return []
    return _read_csv_rows(path)


def _safe_json_load(path: str) -> Dict[str, Any]:
    # line above: if not os.path.exists(path):
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
            if isinstance(raw, dict):
                return raw
    except Exception:
        pass
    return {}


def _safe_json_dump(path: str, payload: Dict[str, Any]) -> None:
    # line above: text = json.dumps(payload, indent=2, sort_keys=True)
    text = json.dumps(payload, indent=2, sort_keys=True)
    _atomic_write_text(path, text + "\n")


# line above: val = getattr(state.ledger, "position_qty", None)
def _ledger_position_qty(state: Any) -> Decimal:
    # line above: val = getattr(state.ledger, "position_qty", None)
    val = getattr(state.ledger, "position_qty", None)
    return _to_decimal(val, "0")


def _adapter_symbol_position(state: Any, symbol: str) -> Optional[PositionState]:
    # line above: return _to_decimal(val, "0")
    adapter = getattr(state, "execution_adapter", None)
    if adapter is None:
        return None
    try:
        positions = list(adapter.get_positions() or [])
    except Exception:
        return None

    for pos in positions:
        if str(getattr(pos, "symbol", "")) == str(symbol):
            return pos
    return None


def _adapter_symbol_position_qty(state: Any, symbol: str) -> Decimal:
    # line above: return None
    pos = _adapter_symbol_position(state, symbol)
    if pos is None:
        return Decimal("0")
    return _to_decimal(getattr(pos, "qty", None), "0")


def _effective_runtime_position_qty(state: Any, symbol: str) -> Decimal:
    """
    In ADAPTER mode, adapter position truth wins over local ledger state.
    In non-ADAPTER mode, ledger remains the fallback truth.
    """
    # line above: return _to_decimal(getattr(pos, "qty", None), "0")
    adapter_qty = _adapter_symbol_position_qty(state, symbol)
    if _adapter_mode_enabled({}, state) or adapter_qty != Decimal("0"):
        return adapter_qty
    return _ledger_position_qty(state)


def _ledger_avg_entry(state: Any) -> Decimal:
    # line above: for attr in ("avg_entry", "avg_entry_px", "avg_cost", "entry_price"):
    for attr in ("avg_entry", "avg_entry_px", "avg_cost", "entry_price"):
        if hasattr(state.ledger, attr):
            return _to_decimal(getattr(state.ledger, attr), "0")
    return Decimal("0")


def _ledger_cash(state: Any) -> Decimal:
    # line above: for attr in ("cash", "cash_usd", "cash_balance"):
    for attr in ("cash", "cash_usd", "cash_balance"):
        if hasattr(state.ledger, attr):
            return _to_decimal(getattr(state.ledger, attr), "0")
    return Decimal("0")


def _restore_ledger_from_recovery(
    *,
    state: Any,
    recovered_qty: Decimal,
    recovered_avg_entry: Decimal,
    recovered_cash: Optional[Decimal],
    recovered_realized_pnl: Optional[Decimal] = None,
) -> None:
    """
    Best-effort direct restore for existing ledger implementations.
    """
    # line above: ledger = state.ledger
    ledger = state.ledger

    for attr in ("position_qty", "qty"):
        if hasattr(ledger, attr):
            try:
                setattr(ledger, attr, recovered_qty)
            except Exception:
                pass

    for attr in ("avg_entry", "avg_entry_px", "avg_cost", "entry_price"):
        if hasattr(ledger, attr):
            try:
                setattr(ledger, attr, recovered_avg_entry)
            except Exception:
                pass

    if recovered_cash is not None:
        for attr in ("cash", "cash_usd", "cash_balance"):
            if hasattr(ledger, attr):
                try:
                    setattr(ledger, attr, recovered_cash)
                except Exception:
                    pass

    if recovered_realized_pnl is not None:
        for attr in ("realized_pnl", "realized_pnl_usd", "realized", "pnl_realized"):
            if hasattr(ledger, attr):
                try:
                    setattr(ledger, attr, recovered_realized_pnl)
                except Exception:
                    pass

    # line above: if recovered_qty > 0:
    if recovered_qty > 0:
        try:
            state.entry_epoch = getattr(state, "entry_epoch", None) or int(time.time())
        except Exception:
            pass
        try:
            state.peak_price = recovered_avg_entry
        except Exception:
            pass
        try:
            state.high_since_entry = recovered_avg_entry
        except Exception:
            pass
        try:
            state.low_since_entry = recovered_avg_entry
        except Exception:
            pass
    else:
        try:
            state.entry_epoch = None
        except Exception:
            pass
        try:
            state.peak_price = None
        except Exception:
            pass
        try:
            state.high_since_entry = None
        except Exception:
            pass
        try:
            state.low_since_entry = None
        except Exception:
            pass


def _snapshot_payload(
    *,
    state: Any,
    symbol: str,
    run_id: str,
    recovery_state: str,
    last_fill_poll_ts: int,
    applied_fill_ids: set[str],
    recovery_source: str = "",
) -> Dict[str, Any]:
    # line above: payload = {
    payload = {
        "state_version": 3,
        "saved_at": int(time.time()),
        "run_id": str(run_id),
        "session_id": str(getattr(state, "session_id", "") or run_id),
        "symbol": str(symbol),
        "recovery_state": str(recovery_state),
        "recovery_source": str(recovery_source or getattr(state, "recovery_source", "") or ""),
        "bot_state": str(getattr(state, "bot_state", "") or recovery_state),
        "position_qty": str(_ledger_position_qty(state)),
        "avg_entry": str(_ledger_avg_entry(state)),
        "cash": str(_ledger_cash(state)),
        "realized_pnl": str(_ledger_realized_pnl(state)),
        "open_trade_id": str(getattr(state, "open_trade_id", "") or ""),
        "entry_order_id": str(getattr(state, "entry_order_id", "") or ""),
        "exit_order_id": str(getattr(state, "exit_order_id", "") or ""),
        "last_processed_fill_id": str(getattr(state, "last_processed_fill_id", "") or ""),
        "last_processed_order_id": str(getattr(state, "last_processed_order_id", "") or ""),
        "last_fill_poll_ts": int(last_fill_poll_ts),
        "applied_fill_ids": sorted(str(x) for x in applied_fill_ids),
        "closed_trade_keys": sorted(str(x) for x in _get_closed_trade_keys(state)),
        "open_trade_ctx": dict(_get_open_trade_ctx(state)),
        "cooldown_until": int(getattr(state, "cooldown_until_epoch", 0) or 0),
        "last_action_ts": int(getattr(state, "last_action_ts", 0) or 0),
        "entry_epoch": int(getattr(state, "entry_epoch", 0) or 0),
        "forced_request_state": dict(_get_forced_request_state(state)),
    }
    return payload


def _save_runtime_snapshot(
    *,
    state_dir: str,
    state: Any,
    symbol: str,
    run_id: str,
    recovery_state: str,
    last_fill_poll_ts: int,
    applied_fill_ids: set[str],
    recovery_source: str = "",
) -> None:
    # line above: path = _runtime_snapshot_path(state_dir, symbol)
    path = _runtime_snapshot_path(state_dir, symbol)
    payload = _snapshot_payload(
        state=state,
        symbol=symbol,
        run_id=run_id,
        recovery_state=recovery_state,
        last_fill_poll_ts=last_fill_poll_ts,
        applied_fill_ids=applied_fill_ids,
        recovery_source=recovery_source,
    )
    _safe_json_dump(path, payload)


def _load_runtime_snapshot(state_dir: str, symbol: str) -> Dict[str, Any]:
    # line above: path = _runtime_snapshot_path(state_dir, symbol)
    path = _runtime_snapshot_path(state_dir, symbol)
    return _safe_json_load(path)


def _derive_position_from_fills(
    *,
    fills_rows: List[Dict[str, str]],
    symbol: str,
) -> Dict[str, Any]:
    """
    Derive position truth from fills. Retained for diagnostics / compatibility.
    """
    # line above: net_qty = Decimal("0")
    net_qty = Decimal("0")
    avg_entry = Decimal("0")
    cash_delta = Decimal("0")
    realized_pnl = Decimal("0")
    open_trade_id = ""
    last_fill_ts = 0
    applied_ids: List[str] = []
    buy_lots: List[Tuple[Decimal, Decimal]] = []

    rows = sorted(
        [r for r in fills_rows if str(r.get("symbol", "") or "") == symbol],
        key=lambda r: _normalize_epoch_seconds(int(str(r.get("ts", "0") or "0"))),
    )

    for row in rows:
        side = str(row.get("side", "") or "").upper()
        qty = _to_decimal(row.get("qty"), "0")
        px = _to_decimal(row.get("price"), "0")
        fee = _to_decimal(row.get("fee"), "0")
        fill_id = str(row.get("fill_id", "") or "")
        trade_id = str(row.get("trade_id", "") or "")
        ts = _normalize_epoch_seconds(int(str(row.get("ts", "0") or "0")))

        if qty <= 0 or px <= 0 or side not in ("BUY", "SELL"):
            continue

        last_fill_ts = max(last_fill_ts, ts)
        if fill_id:
            applied_ids.append(fill_id)

        if side == "BUY":
            buy_lots.append((qty, px))
            net_qty += qty
            cash_delta -= (qty * px + fee)
            if trade_id:
                open_trade_id = trade_id

        elif side == "SELL":
            remaining = qty
            cash_delta += (qty * px - fee)

            while remaining > 0 and buy_lots:
                lot_qty, lot_px = buy_lots[0]
                close_qty = min(remaining, lot_qty)
                realized_pnl += (px - lot_px) * close_qty
                lot_qty -= close_qty
                remaining -= close_qty
                net_qty -= close_qty

                if lot_qty <= 0:
                    buy_lots.pop(0)
                else:
                    buy_lots[0] = (lot_qty, lot_px)

            if net_qty <= 0:
                open_trade_id = ""

    if net_qty > 0 and buy_lots:
        total_cost = sum((q * p for q, p in buy_lots), Decimal("0"))
        total_qty = sum((q for q, _ in buy_lots), Decimal("0"))
        if total_qty > 0:
            avg_entry = total_cost / total_qty
    else:
        avg_entry = Decimal("0")

    return {
        "qty": net_qty if net_qty > 0 else Decimal("0"),
        "avg_entry": avg_entry if net_qty > 0 else Decimal("0"),
        "cash_delta": cash_delta,
        "realized_pnl": realized_pnl,
        "open_trade_id": open_trade_id,
        "last_fill_ts": last_fill_ts,
        "applied_fill_ids": applied_ids,
        "has_fills": bool(applied_ids),
    }


def _derive_position_from_positions(
    *,
    positions_rows: List[Dict[str, str]],
    symbol: str,
) -> Dict[str, Any]:
    # line above: rows = [r for r in positions_rows if str(r.get("symbol", "") or "") == symbol]
    rows = [r for r in positions_rows if str(r.get("symbol", "") or "") == symbol]
    rows = sorted(
        rows,
        key=lambda r: _normalize_epoch_seconds(int(str(r.get("ts", "0") or "0"))),
    )

    if not rows:
        return {
            "qty": Decimal("0"),
            "avg_entry": Decimal("0"),
            "last_ts": 0,
            "side": "",
        }

    last = rows[-1]
    qty = _to_decimal(last.get("qty"), "0")
    avg_entry = _to_decimal(last.get("avg_entry_px"), "0")
    ts = _normalize_epoch_seconds(int(str(last.get("ts", "0") or "0")))
    side = str(last.get("side", "") or "").upper()

    if side != "LONG" or qty <= 0:
        qty = Decimal("0")
        avg_entry = Decimal("0")

    return {
        "qty": qty,
        "avg_entry": avg_entry,
        "last_ts": ts,
        "side": side,
    }


def _derive_state_from_orders(
    *,
    order_rows: List[Dict[str, str]],
    symbol: str,
) -> Dict[str, Any]:
    """
    Infer whether a pending order existed before restart.
    Retained for diagnostics / compatibility.
    """
    # line above: rows = [r for r in order_rows if ...]
    rows = [r for r in order_rows if str(r.get("symbol", "") or "") == symbol]
    rows = sorted(
        rows,
        key=lambda r: _normalize_epoch_seconds(int(str(r.get("ts", "0") or "0"))),
    )

    last_open_side = ""
    last_order_id = ""
    last_client_order_id = ""
    last_status = ""

    openish_status = {
        "NEW",
        "OPEN",
        "WORKING",
        "ACCEPTED",
        "PARTIAL",
        "PARTIALLY_FILLED",
        "PENDING",
        "SUBMITTED",
        "ACKED",
    }

    for row in rows:
        status = str(row.get("status", "") or "").upper()
        side = str(row.get("side", "") or "").upper()
        order_id = str(row.get("order_id", "") or "")
        client_order_id = str(row.get("client_order_id", "") or "")
        filled_qty = _to_decimal(row.get("filled_qty"), "0")
        remaining_qty = _to_decimal(row.get("remaining_qty"), "0")

        if status in openish_status or remaining_qty > 0:
            last_open_side = side
            last_order_id = order_id
            last_client_order_id = client_order_id
            last_status = status
        elif status in {"FILLED", "CANCELLED", "CANCELED", "REJECTED", "DONE"} and filled_qty >= 0:
            if order_id == last_order_id:
                last_open_side = ""
                last_status = status

    return {
        "pending_side": last_open_side,
        "order_id": last_order_id,
        "client_order_id": last_client_order_id,
        "status": last_status,
    }


def _get_forced_request_state(state: Any) -> Dict[str, Any]:
    # line above: def _derive_state_from_orders(
    data = getattr(state, "_forced_request_state", None)
    if isinstance(data, dict):
        return data
    data = {
        "requested_action": "",
        "inflight": False,
        "submitted_order_id": "",
        "submitted_at": 0,
        "fill_verified": False,
    }
    setattr(state, "_forced_request_state", data)
    return data


def _set_forced_request_inflight(
    *,
    state: Any,
    requested_action: str,
    submitted_order_id: str,
    submitted_at: int,
) -> None:
    # line above: def _get_forced_request_state(state: Any) -> Dict[str, Any]:
    data = _get_forced_request_state(state)
    data["requested_action"] = str(requested_action or "").upper()
    data["inflight"] = True
    data["submitted_order_id"] = str(submitted_order_id or "")
    data["submitted_at"] = int(submitted_at or 0)
    data["fill_verified"] = False
    setattr(state, "_forced_request_state", data)


def _mark_forced_request_fill_verified(state: Any, side: str) -> None:
    # line above: setattr(state, "_forced_request_state", data)
    data = _get_forced_request_state(state)
    if str(data.get("requested_action", "") or "").upper() == str(side or "").upper():
        data["fill_verified"] = True
        setattr(state, "_forced_request_state", data)


def _clear_forced_request_state(state: Any) -> None:
    # line above: def _mark_forced_request_fill_verified(state: Any, side: str) -> None:
    setattr(
        state,
        "_forced_request_state",
        {
            "requested_action": "",
            "inflight": False,
            "submitted_order_id": "",
            "submitted_at": 0,
            "fill_verified": False,
        },
    )


def _restore_runtime_meta_from_recovery(
    *,
    state: Any,
    recovered_state: str,
    open_trade_id: str,
    entry_order_id: str,
    exit_order_id: str,
    last_fill_id: str,
    last_order_id: str,
    cooldown_until: int,
    snapshot: Dict[str, Any],
    recovery_source: str = "",
) -> None:
    # line above: setattr(state, "bot_state", recovered_state)
    try:
        setattr(state, "bot_state", recovered_state)
    except Exception:
        pass

    try:
        setattr(state, "recovery_state", recovered_state)
    except Exception:
        pass

    try:
        setattr(state, "recovery_source", recovery_source)
    except Exception:
        pass

    try:
        setattr(state, "open_trade_id", open_trade_id)
    except Exception:
        pass

    try:
        setattr(state, "entry_order_id", entry_order_id)
    except Exception:
        pass

    try:
        setattr(state, "exit_order_id", exit_order_id)
    except Exception:
        pass

    try:
        setattr(state, "last_processed_fill_id", last_fill_id)
    except Exception:
        pass

    try:
        setattr(state, "last_processed_order_id", last_order_id)
    except Exception:
        pass

    try:
        setattr(state, "cooldown_until_epoch", int(cooldown_until or 0))
    except Exception:
        pass

    # line above:     except Exception:
    if str(recovered_state or "").upper() == BOT_STATE_FLAT:
        try:
            setattr(state, "open_trade_id", "")
        except Exception:
            pass
        try:
            _clear_open_trade_ctx(state)
        except Exception:
            pass
        try:
            _clear_forced_request_state(state)
        except Exception:
            pass

    try:
        closed_keys = snapshot.get("closed_trade_keys", []) if isinstance(snapshot, dict) else []
        setattr(state, "_closed_trade_keys", set(str(x) for x in (closed_keys or [])))
    except Exception:
        pass

    try:
        open_trade_ctx = snapshot.get("open_trade_ctx", {}) if isinstance(snapshot, dict) else {}
        if isinstance(open_trade_ctx, dict):
            setattr(state, "_open_trade_ctx", dict(open_trade_ctx))
    except Exception:
        pass

    # Only restore forced_request_state from snapshot when recovery is NOT FLAT.
    # When recovery says FLAT, the clear above (line ~2167) must stand — restoring
    # a stale inflight forced-state from the snapshot would undo it.
    if str(recovered_state or "").upper() != BOT_STATE_FLAT:
        try:
            forced_request_state = snapshot.get("forced_request_state", {}) if isinstance(snapshot, dict) else {}
            if isinstance(forced_request_state, dict):
                setattr(state, "_forced_request_state", dict(forced_request_state))
        except Exception:
            pass


def _reconcile_on_startup(
    *,
    state: Any,
    cfg: dict[str, Any],
    symbol: str,
    log_dir: str,
    state_dir: str,
    run_id: str,
) -> Dict[str, Any]:
    """
    Runner-side startup restore wrapper.

    Truth arbitration lives in execution/recovery.py.
    This wrapper only:
      - calls canonical recovery
      - logs the result
      - restores ledger/runtime meta
      - restores snapshot-derived runtime-only metadata
    """
    # line above: snapshot = _load_runtime_snapshot(state_dir, symbol)
    snapshot = _load_runtime_snapshot(state_dir, symbol)

    recovery_result = canonical_reconcile_on_startup(cfg)

    for line in format_recovery_log_lines(recovery_result):
        try:
            log_event(symbol, "RECOVERY", line)
        except Exception:
            pass

    try:
        write_reconcile_report(
            os.path.join(log_dir, f"recovery_{run_id}.json"),
            recovery_result,
        )
    except Exception:
        pass

    recovered_qty = _to_decimal(recovery_result.position_qty, "0")
    recovered_avg = _to_decimal(recovery_result.avg_entry, "0")
    recovered_cash = _to_decimal(recovery_result.cash, "0")
    recovered_realized = _to_decimal(recovery_result.realized_pnl, "0")

    _restore_ledger_from_recovery(
        state=state,
        recovered_qty=recovered_qty,
        recovered_avg_entry=recovered_avg,
        recovered_cash=recovered_cash,
        recovered_realized_pnl=recovered_realized,
    )

    snap_cooldown_until = int(snapshot.get("cooldown_until", 0) or 0)
    snap_last_fill_poll_ts = int(snapshot.get("last_fill_poll_ts", 0) or 0)
    snap_applied_fill_ids = set(str(x) for x in (snapshot.get("applied_fill_ids", []) or []))

    _restore_runtime_meta_from_recovery(
        state=state,
        recovered_state=str(recovery_result.bot_state or BOT_STATE_FLAT),
        open_trade_id=str(recovery_result.open_trade_id or ""),
        entry_order_id=str(recovery_result.entry_order_id or ""),
        exit_order_id=str(recovery_result.exit_order_id or ""),
        last_fill_id=str(recovery_result.last_processed_fill_id or ""),
        last_order_id=str(recovery_result.last_processed_order_id or ""),
        cooldown_until=snap_cooldown_until,
        snapshot=snapshot,
        recovery_source=str(recovery_result.source_of_truth or ""),
    )

    # line above:     _restore_runtime_meta_from_recovery(
    if (
        recovery_result.ok
        and str(recovery_result.bot_state or BOT_STATE_FLAT).upper() == BOT_STATE_FLAT
        and str(recovery_result.source_of_truth or "").lower() == "canonical_execution"
    ):
        try:
            finalized, finalize_reason = _finalize_recovered_flat_close(
                state=state,
                cfg=cfg,
                log_dir=log_dir,
                run_id=run_id,
                symbol=symbol,
                recovery_state=str(recovery_result.bot_state or BOT_STATE_FLAT),
                recovery_source=str(recovery_result.source_of_truth or ""),
            )
            if finalized:
                log_event(symbol, "RECOVERY_FINALIZE_CLOSE", finalize_reason)
            else:
                log_event(symbol, "RECOVERY_FINALIZE_CLOSE_SKIP", finalize_reason)
        except Exception as e:
            log_event(symbol, "RECOVERY_FINALIZE_CLOSE_FAIL", str(e))

    if recovered_qty > 0:
        existing_ctx = _get_open_trade_ctx(state)
        needs_ctx_hydrate = (
            not existing_ctx
            or not str(existing_ctx.get("entry_fill_id", "") or "")
            or not str(existing_ctx.get("entry_order_id", "") or "")
            or not str(existing_ctx.get("entry_px", "") or "")
        )

        if needs_ctx_hydrate:
            fills_path = str(cfg.get("LIVE_FILLS_CSV", "") or "")
            fills_rows = _read_csv_rows(fills_path) if fills_path else []
            derived_ctx = _derive_open_trade_ctx_from_fills_rows(
                fills_rows=fills_rows,
                symbol=symbol,
            )

            if derived_ctx:
                if not str(derived_ctx.get("trade_id", "") or ""):
                    derived_ctx["trade_id"] = str(recovery_result.open_trade_id or "")
                # Hydrate Phase 9 fields that fills.csv cannot provide directly
                slippage_bps_cfg = str(cfg.get("PAPER_SLIPPAGE_BPS", cfg.get("SLIPPAGE_BPS", "0")) or "0")
                derived_ctx.setdefault("slippage_bps", slippage_bps_cfg)
                if not str(derived_ctx.get("entry_mid_px", "") or ""):
                    try:
                        entry_fill_px = _to_decimal(derived_ctx.get("entry_px"), "0")
                        slip = _to_decimal(slippage_bps_cfg, "0") / Decimal("10000")
                        if entry_fill_px > 0 and slip < Decimal("1"):
                            derived_ctx["entry_mid_px"] = str(
                                _q_money8(entry_fill_px / (Decimal("1") + slip))
                            )
                    except Exception:
                        pass
                _set_open_trade_ctx(state, derived_ctx)
            else:
                ctx = {
                    "symbol": symbol,
                    "trade_id": str(recovery_result.open_trade_id or ""),
                    "entry_fill_id": "",
                    "entry_order_id": str(recovery_result.entry_order_id or ""),
                    "entry_client_order_id": "",
                    "entry_ts": int(time.time()),
                    "entry_time": _iso_from_epoch(int(time.time())),
                    "entry_px": str(recovered_avg),
                    "qty": str(recovered_qty),
                    "entry_reason": "RECOVERED_OPEN",
                }
                _set_open_trade_ctx(state, ctx)

    return {
        "ok": bool(recovery_result.ok),
        "recovery_state": str(recovery_result.bot_state or BOT_STATE_FLAT),
        "recovery_source": str(recovery_result.source_of_truth or ""),
        "position_qty": recovered_qty,
        "avg_entry": recovered_avg,
        "cash": recovered_cash,
        "realized_pnl": recovered_realized,
        "open_trade_id": str(recovery_result.open_trade_id or ""),
        "entry_order_id": str(recovery_result.entry_order_id or ""),
        "exit_order_id": str(recovery_result.exit_order_id or ""),
        "last_fill_poll_ts": int(
            max(
                int(snap_last_fill_poll_ts or 0),
                0,
            )
        ),
        "applied_fill_ids": snap_applied_fill_ids,
        "last_processed_fill_id": str(recovery_result.last_processed_fill_id or ""),
        "last_processed_order_id": str(recovery_result.last_processed_order_id or ""),
        "contradictions": list(recovery_result.contradictions or []),
        "notes": list(recovery_result.notes or []),
        "halt_reason": str(recovery_result.halt_reason or ""),
        "issues": [i.to_log_dict() for i in getattr(recovery_result, "issues", [])],
    }


def _should_suppress_action_during_recovery(
    *,
    action: str,
    recovery_state: str,
    ledger_qty: Decimal,
    state: Any,
) -> Tuple[bool, str]:
    """
    Guard against duplicate entry/exit after restart.

    Important:
    If a forced proof request is already in flight, do not suppress it merely
    because recovery_state is ENTERING/EXITING.
    """
    # line above: action = str(action or "").upper()
    action = str(action or "").upper()
    if action not in {"BUY", "SELL"}:
        return False, ""

    forced_state = _get_forced_request_state(state)
    inflight = bool(forced_state.get("inflight", False))
    inflight_action = str(forced_state.get("requested_action", "") or "").upper()

    if inflight and inflight_action == action:
        return False, ""

    if recovery_state == BOT_STATE_OPEN and _is_effectively_open_qty(ledger_qty) and action == "BUY":
        return True, "recovery_open_duplicate_buy_suppressed"

    if recovery_state in {BOT_STATE_ENTERING, BOT_STATE_EXITING}:
        return True, f"recovery_pending_state_{recovery_state.lower()}"

    if recovery_state == BOT_STATE_FLAT and _is_effectively_flat_qty(ledger_qty) and action == "SELL":
        return True, "recovery_flat_sell_suppressed"

    return False, ""


def _should_exit_after_forced_success(
    *,
    cfg: dict[str, Any],
    fill: FillState,
    state: Any,
    force_test_buy: bool,
    force_test_sell: bool,
) -> Tuple[bool, str]:
    """
    Forced test exit should happen on verified success, not on order submission.
    In ADAPTER mode, use adapter position truth because ledger may lag the adapter.
    """
    # line above: if not _as_bool(cfg.get("FORCE_TEST_EXIT_ON_SUCCESS", True), True):
    if not _as_bool(cfg.get("FORCE_TEST_EXIT_ON_SUCCESS", True), True):
        return False, ""

    side = str(fill.side).upper()
    symbol = str(getattr(fill, "symbol", "") or "")
    qty_now = _effective_runtime_position_qty(state, symbol)

    if force_test_buy and side == "BUY" and _is_effectively_open_qty(qty_now):
        return True, "forced_buy_fill_verified"

    if force_test_sell and side == "SELL" and _is_effectively_flat_qty(qty_now):
        return True, "forced_sell_fill_verified"

    return False, ""


# -----------------------------------------------------------------------------
# Force-proof mode / veto helpers
# -----------------------------------------------------------------------------

def _requested_forced_action(force_test_buy: bool, force_test_sell: bool) -> str:
    # line above: if force_test_buy and force_test_sell:
    if force_test_buy and force_test_sell:
        return "INVALID"
    if force_test_buy:
        return "BUY"
    if force_test_sell:
        return "SELL"
    return ""


def _force_action_legality(
    *,
    requested_action: str,
    recovery_state: str,
    ledger_qty: Decimal,
    paused: bool,
    dry_run: bool,
    adapter_enabled: bool,
) -> Tuple[bool, str]:
    """
    Hard legality gate independent of strategy output.
    """
    # line above: requested_action = str(requested_action or "").upper()
    requested_action = str(requested_action or "").upper()

    if not requested_action:
        return True, ""

    if requested_action == "INVALID":
        return False, "force_test_conflict_buy_and_sell_both_enabled"

    if not adapter_enabled:
        return False, "force_test_requires_adapter_mode"

    if dry_run:
        return False, "force_test_illegal_in_dry_run"

    if paused:
        return False, "force_test_blocked_paused"

    if recovery_state == BOT_STATE_RECOVERY_HALT:
        return False, "force_test_blocked_recovery_halt"

    if recovery_state in {BOT_STATE_ENTERING, BOT_STATE_EXITING}:
        return False, f"force_test_blocked_pending_state_{recovery_state.lower()}"

    if requested_action == "BUY":
        if _is_effectively_open_qty(ledger_qty) or recovery_state == BOT_STATE_OPEN:
            return False, "force_buy_blocked_already_open"
        return True, ""

    if requested_action == "SELL":
        if _is_effectively_flat_qty(ledger_qty) or recovery_state == BOT_STATE_FLAT:
            return False, "force_sell_blocked_flat"
        return True, ""

    return False, f"force_test_unknown_action_{requested_action.lower()}"


def _apply_force_veto_to_snapshot(snap: Any, veto_reason: str) -> None:
    # line above: setattr(snap, "execution_status", "FORCE_VETO")
    setattr(snap, "execution_status", "FORCE_VETO")
    setattr(snap, "execution_reason", veto_reason)
    setattr(snap, "risk_blocked_reason", veto_reason)
    setattr(snap, "action", "HOLD")
    setattr(snap, "action_reason", veto_reason)


def _enforce_force_proof_mode(
    *,
    snap: Any,
    state: Any,
    cfg: dict[str, Any],
    recovery_state: str,
    paused: bool,
    dry_run: bool,
    force_test_buy: bool,
    force_test_sell: bool,
    force_test_only_once: bool,
    forced_test_consumed: bool,
    forced_test_complete_reason: str,
) -> Tuple[bool, str]:
    """
    Deterministic control plane for forced proof runs.

    If force-only-once is enabled:
      - either the requested forced action is legal and emitted,
      - or a deterministic veto happens and the process exits cleanly.

    Critical behavior:
      - once a forced action has been submitted, do not re-veto it just because
        recovery_state moved to ENTERING/EXITING while waiting for fill proof.
    """
    # line above: requested = _requested_forced_action(force_test_buy, force_test_sell)
    requested = _requested_forced_action(force_test_buy, force_test_sell)
    if not requested:
        return False, ""

    if forced_test_complete_reason:
        _apply_force_veto_to_snapshot(snap, forced_test_complete_reason)
        return True, forced_test_complete_reason

    if forced_test_consumed and force_test_only_once:
        _apply_force_veto_to_snapshot(snap, "force_test_only_once_consumed")
        return True, "force_test_only_once_consumed"

    forced_state = _get_forced_request_state(state)
    inflight = bool(forced_state.get("inflight", False))
    inflight_action = str(forced_state.get("requested_action", "") or "").upper()

    if inflight and inflight_action == requested:
        current_action = str(getattr(snap, "action", "") or "").upper()
        if current_action in {"BUY", "SELL"}:
            setattr(snap, "action", "HOLD")
            setattr(snap, "action_reason", f"force_test_waiting_fill_{requested.lower()}")
            setattr(snap, "execution_status", "FORCE_TEST_INFLIGHT")
            setattr(snap, "execution_reason", f"force_test_waiting_fill_{requested.lower()}")
            setattr(snap, "risk_blocked_reason", f"force_test_waiting_fill_{requested.lower()}")
        return False, ""

    legal, legal_reason = _force_action_legality(
        requested_action=requested,
        recovery_state=recovery_state,
        ledger_qty=_ledger_position_qty(state),
        paused=paused,
        dry_run=dry_run,
        adapter_enabled=_adapter_mode_enabled(cfg, state),
    )
    if not legal:
        _apply_force_veto_to_snapshot(snap, legal_reason)
        return bool(force_test_only_once), legal_reason

    actual = str(getattr(snap, "action", "") or "").upper()
    if force_test_only_once and actual != requested:
        veto_reason = f"force_test_expected_{requested.lower()}_but_engine_emitted_{(actual or 'hold').lower()}"
        _apply_force_veto_to_snapshot(snap, veto_reason)
        return True, veto_reason

    return False, ""


# -----------------------------------------------------------------------------
# Kill-point hooks for restart proof testing
# -----------------------------------------------------------------------------

def _killpoint_aliases(key: str) -> List[str]:
    """
    Allow both canonical keys and shorter alias keys during Phase 8 proof runs.
    """
    # line above: aliases = {
    aliases = {
        "KILL_AFTER_ORDER_SUBMIT": ["KILL_AFTER_SUBMIT"],
        "KILL_BEFORE_JOURNAL_WRITE": ["KILL_BEFORE_JOURNAL"],
        "KILL_AFTER_JOURNAL_WRITE": ["KILL_AFTER_JOURNAL"],
        "KILL_AFTER_FILL_PERSIST": [],
        "KILL_AFTER_POSITIONS_WRITE": [],
    }
    return [key] + aliases.get(key, [])


def _killpoint_enabled(cfg: dict[str, Any], key: str) -> bool:
    """
    Resolve killpoint from cfg and env aliases.

    Precedence:
      1) exact cfg key
      2) alias cfg keys
      3) exact env var
      4) alias env vars
    """
    # line above: for candidate in _killpoint_aliases(key):
    for candidate in _killpoint_aliases(key):
        if candidate in cfg:
            return _as_bool(cfg.get(candidate), False)

    for candidate in _killpoint_aliases(key):
        env_val = os.getenv(candidate)
        if env_val is not None:
            return _as_bool(env_val, False)

    return False


def _maybe_abort_at_killpoint(
    *,
    cfg: dict[str, Any],
    key: str,
    symbol: str,
    detail: str,
) -> None:
    """
    Controlled crash hook for restart determinism proof.
    """
    # line above: if not _killpoint_enabled(cfg, key):
    if not _killpoint_enabled(cfg, key):
        return

    msg = f"{key} {detail}".strip()
    try:
        log_event(symbol, "CONTROLLED_ABORT", msg)
    except Exception:
        pass
    raise ControlledAbort(msg)


def _set_runtime_recovery_state(state: Any, recovery_state: str, recovery_source: str) -> None:
    # line above: def _maybe_abort_at_killpoint(
    try:
        setattr(state, "bot_state", recovery_state)
    except Exception:
        pass
    try:
        setattr(state, "recovery_state", recovery_state)
    except Exception:
        pass
    try:
        setattr(state, "recovery_source", recovery_source)
    except Exception:
        pass


def _finalize_runtime_flat_state(
    *,
    state: Any,
    symbol: str,
    fill: FillState,
    recovery_source: str,
) -> None:
    """
    Explicit control-plane close finalization.

    This exists to prevent the stranded EXITING bug:
      - adapter/ledger already flat
      - runtime state still EXITING
    """
    # line above: _set_runtime_recovery_state(state, recovery_state, recovery_source)
    _set_runtime_recovery_state(state, BOT_STATE_FLAT, recovery_source)

    try:
        setattr(state, "open_trade_id", "")
    except Exception:
        pass
    try:
        setattr(state, "entry_order_id", "")
    except Exception:
        pass
    try:
        setattr(state, "exit_order_id", "")
    except Exception:
        pass
    try:
        setattr(state, "entry_epoch", 0)
    except Exception:
        pass

    _clear_forced_request_state(state)
    _clear_open_trade_ctx(state)


async def run_live(
    *,
    cfg_path: Optional[str] = None,
    dry_run: Optional[bool] = None,
    once: Optional[bool] = None,
) -> int:
    """
    Live runner.

    Args:
      cfg_path: optional config file path (if load_config supports it)
      dry_run:  optional explicit override; if None, use cfg["DRY_RUN"]
      once:     optional explicit override; if None, default False
    """
    cfg = _load_cfg(cfg_path)

    # --------- LINE ABOVE: cfg = _load_cfg(cfg_path)
    dry_run = _as_bool(cfg.get("DRY_RUN", False), False) if dry_run is None else bool(dry_run)
    force_test_buy = _as_bool(cfg.get("FORCE_TEST_BUY", False), False)
    force_test_sell = _as_bool(cfg.get("FORCE_TEST_SELL", False), False)
    force_test_only_once = _as_bool(cfg.get("FORCE_TEST_ONLY_ONCE", True), True)
    once = False if once is None else bool(once)

    runtime_log_dir = _resolve_runtime_log_dir(cfg)
    state_dir = _resolve_runtime_state_dir(cfg)

    cfg["RUNTIME_LOG_DIR"] = runtime_log_dir
    cfg["OPS_LOG_DIR"] = runtime_log_dir
    cfg["LOG_DIR"] = runtime_log_dir
    cfg["STATE_DIR"] = state_dir

    # Keep these paths normalized for recovery/discovery.
    # In ADAPTER mode, adapter owns writes to these surfaces.
    cfg["LIVE_ORDERS_CSV"] = os.path.join(runtime_log_dir, "orders.csv")
    cfg["LIVE_FILLS_CSV"] = os.path.join(runtime_log_dir, "fills.csv")
    cfg["LIVE_POSITIONS_CSV"] = os.path.join(runtime_log_dir, "positions.csv")
    cfg["LIVE_ACCOUNT_CSV"] = os.path.join(runtime_log_dir, "account.csv")
    cfg["RUNTIME_STATE_PATH"] = _runtime_snapshot_path(
        state_dir,
        str(cfg.get("PRODUCT_ID") or cfg.get("SYMBOL") or "UNKNOWN"),
    )

    os.makedirs(runtime_log_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)

    ensure_logs()
    ensure_signals_header_matches_file()

    http = make_http()
    state = BotState.from_config(cfg)
    symbol = state.symbol
    exec_mode = _execution_mode(cfg, state)

    log_dir = runtime_log_dir

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_start_ts = int(time.time())

    last_fill_poll_ts = 0
    applied_fill_ids: set[str] = set()
    startup_fill_ids: set[str] = set()
    recovery_source = "DEFAULTS"

    recovery_state = BOT_STATE_FLAT
    recovery_info: Dict[str, Any] = {}
    forced_test_complete_reason = ""
    closed_trade_keys = _get_closed_trade_keys(state)
    _ = closed_trade_keys

    # --- Phase 16: artifact integrity check on startup ---
    if _adapter_mode_enabled(cfg, state):
        try:
            _ai_ok, _ai_issues = verify_artifact_integrity(log_dir)
            if not _ai_ok:
                _ai_detail = "; ".join(f"{i['artifact']}:{i['issue']}" for i in _ai_issues[:5])
                log_event(symbol, "ARTIFACT_INTEGRITY_FAIL", _ai_detail)
                print(f"  [WARN] Artifact integrity issues: {_ai_detail}")
                if cfg.get("DISCORD_WEBHOOK_URL"):
                    alert_invariant_failure(http, cfg["DISCORD_WEBHOOK_URL"], f"Startup artifact integrity: {_ai_detail}")
            else:
                log_event(symbol, "ARTIFACT_INTEGRITY_OK", "startup_check_pass")
            # Also check fills.csv specifically for truncation / duplicate IDs
            _fills_ok, _fills_detail = check_fills_csv_integrity(log_dir)
            if not _fills_ok:
                log_event(symbol, "FILLS_INTEGRITY_FAIL", _fills_detail)
                print(f"  [WARN] fills.csv integrity: {_fills_detail}")
        except Exception as _ai_err:
            log_event(symbol, "ARTIFACT_INTEGRITY_ERROR", str(_ai_err))

    if _adapter_mode_enabled(cfg, state):
        try:
            recovery_info = _reconcile_on_startup(
                state=state,
                cfg=cfg,
                symbol=symbol,
                log_dir=log_dir,
                state_dir=state_dir,
                run_id=run_id,
            )
            recovery_state = str(recovery_info.get("recovery_state", BOT_STATE_FLAT) or BOT_STATE_FLAT).upper()
            recovery_source = str(recovery_info.get("recovery_source", "DEFAULTS") or "DEFAULTS")
            last_fill_poll_ts = int(recovery_info.get("last_fill_poll_ts", 0) or 0)
            applied_fill_ids = set(str(x) for x in recovery_info.get("applied_fill_ids", set()) or set())
            startup_fill_ids = set(applied_fill_ids)

            log_event(
                symbol,
                "RECOVERY_COMPLETE",
                (
                    f"state={recovery_state} "
                    f"source={safe_str(recovery_source)} "
                    f"qty={safe_str(recovery_info.get('position_qty'))} "
                    f"avg_entry={safe_str(recovery_info.get('avg_entry'))} "
                    f"trade_id={safe_str(recovery_info.get('open_trade_id'))} "
                    f"entry_order_id={safe_str(recovery_info.get('entry_order_id'))} "
                    f"exit_order_id={safe_str(recovery_info.get('exit_order_id'))} "
                    f"startup_fill_count={len(startup_fill_ids)} "
                    f"contradictions={safe_str(recovery_info.get('contradictions'))}"
                ),
            )

            _save_runtime_snapshot(
                state_dir=state_dir,
                state=state,
                symbol=symbol,
                run_id=run_id,
                recovery_state=recovery_state,
                last_fill_poll_ts=last_fill_poll_ts,
                applied_fill_ids=applied_fill_ids,
                recovery_source=recovery_source,
            )

            if not recovery_info.get("ok", False):
                log_event(symbol, "RECOVERY_HALT", safe_str(recovery_info.get("halt_reason")) or "reconcile_on_startup returned RECOVERY_HALT")
                print("[HALT] Startup recovery contradictions detected.")
                return 2

        except Exception as e:
            log_event(symbol, "RECOVERY_FAIL", str(e))
            print(f"[HALT] Recovery failed: {e}")
            print(traceback.format_exc())
            return 2

    needed = max(cfg["MA_TREND_200"], cfg["MA_TREND_50"], cfg["MA_SLOW"], cfg["MA_FAST"]) + 10
    try:
        seeded_1m = await preload_indicator_history(
            indicator_engine=state.strat_1m.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS", 60))},
            needed_candles=needed,
        )
        seeded_5m = await preload_indicator_history(
            indicator_engine=state.strat_5m.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS_5M", cfg.get("TF_5M_SECONDS", 300)))} ,
            needed_candles=needed,
        )
        seeded_1h = await preload_indicator_history(
            indicator_engine=state.strat_1h.ind,
            http=http,
            cfg={**cfg, "CANDLE_SECONDS": int(cfg.get("CANDLE_SECONDS_1H", cfg.get("TF_1H_SECONDS", 3600)))} ,
            needed_candles=needed,
        )
        print(f"[PRELOAD] Seeded 1m={seeded_1m} 5m={seeded_5m} 1h={seeded_1h} (needed~{needed})")
        log_event(symbol, "PRELOAD", f"1m={seeded_1m} 5m={seeded_5m} 1h={seeded_1h} needed~{needed}")
    except Exception as e:
        print(f"[PRELOAD] Failed: {e}")
        log_event(symbol, "PRELOAD_FAIL", str(e))

    if _adapter_mode_enabled(cfg, state):
        print(f"[START] EXECUTION MODE={exec_mode} ADAPTER={safe_str(cfg.get('EXECUTION_ADAPTER', 'PAPER'))} | {symbol}")
    else:
        print(f"[START] PAPER LEDGER (ENGINE MODE) | {symbol}")

    print(f"  feed: {cfg.get('COINBASE_SPOT_URL', '')}")
    print(
        f"  logs: {log_dir} | "
        f"kill={cfg.get('KILL_SWITCH_FILE', 'KILL_SWITCH.txt')} "
        f"pause={cfg.get('PAUSE_FILE', 'PAUSE.txt')}"
    )
    print(
        f"  dry_run={int(dry_run)} "
        f"force_buy={int(force_test_buy)} "
        f"force_sell={int(force_test_sell)} "
        f"force_once={int(force_test_only_once)} "
        f"once={int(once)} "
        f"exec_mode={exec_mode} "
        f"recovery_state={recovery_state} "
        f"recovery_source={recovery_source} "
        f"run_start_ts={run_start_ts}"
    )
    print(
        f"  kill_after_submit={int(_killpoint_enabled(cfg, 'KILL_AFTER_ORDER_SUBMIT'))} "
        f"kill_before_journal={int(_killpoint_enabled(cfg, 'KILL_BEFORE_JOURNAL_WRITE'))} "
        f"kill_after_journal={int(_killpoint_enabled(cfg, 'KILL_AFTER_JOURNAL_WRITE'))}"
    )

    # --- Phase 16: run manifest & mode init ---
    _rm_file = cfg.get("RUNTIME_MODE_FILE", "") or None
    _rm_mode_init, _ = runtime_mode.load_mode(_rm_file)
    _run_manifest: Optional[Dict[str, Any]] = None
    try:
        _run_manifest = create_manifest(
            run_id=run_id,
            cfg=cfg,
            runtime_mode_str=_rm_mode_init,
        )
        save_manifest(_run_manifest, log_dir)
        print(f"  manifest: config_hash={_run_manifest['config_hash'][:12]}... "
              f"code_hash={_run_manifest['code_hash'][:12]}... mode={_rm_mode_init}")
    except Exception as e:
        print(f"  [WARN] manifest write failed: {e}")

    _last_health_ts = 0.0
    _health_interval = float(cfg.get("HEALTH_CHECK_INTERVAL_S", 60))

    fail_count = 0
    did_one = False
    forced_test_consumed = False

    try:
        while True:
            if is_kill_switch_on(cfg):
                msg = "Kill switch file detected. Exiting."
                log_event(symbol, "KILL_SWITCH", msg)
                if cfg.get("DISCORD_WEBHOOK_URL"):
                    maybe_notify_discord(http, cfg["DISCORD_WEBHOOK_URL"], "KILL SWITCH", msg)
                print("[EXIT] Kill switch file detected.")
                return 0

            paused = is_paused(cfg)

            try:
                tick = await fetch_spot_price(http, cfg)
                fail_count = 0

                try:
                    _clamp_tick_time(state=state, tick=tick, cfg=cfg, symbol=symbol)
                except Exception:
                    pass

                if _adapter_mode_enabled(cfg, state):
                    try:
                        _adapter_update_market_from_tick(state.execution_adapter, symbol, tick)
                    except Exception:
                        pass

            except Exception as e:
                fail_count += 1
                log_event(symbol, "PRICE_FETCH_FAIL", str(e))
                if cfg.get("DISCORD_WEBHOOK_URL"):
                    maybe_notify_discord(http, cfg["DISCORD_WEBHOOK_URL"], "PRICE FETCH FAIL", str(e))
                backoff = min(
                    float(cfg.get("MAX_FAIL_BACKOFF_SECONDS", 60)),
                    float(cfg.get("POLL_MED_SECONDS", 2)) * (2 ** min(fail_count, 6)),
                )
                await asyncio.sleep(backoff)
                continue

            # --- Phase 16: periodic health checks ---
            if cfg.get("HEALTH_CHECK_ENABLED", True) and _adapter_mode_enabled(cfg, state):
                _now_health = time.time()
                if (_now_health - _last_health_ts) >= _health_interval:
                    _last_health_ts = _now_health
                    try:
                        _tick_epoch = float(getattr(tick, "epoch", 0) or 0)
                        _h_report = run_health_checks(
                            last_tick_epoch=_tick_epoch if _tick_epoch > 0 else _now_health,
                            feed_stale_threshold_s=float(cfg.get("HEALTH_FEED_STALE_THRESHOLD_S", 120)),
                            fill_latency_threshold_s=float(cfg.get("HEALTH_FILL_LATENCY_THRESHOLD_S", 30)),
                            consecutive_loss_threshold=int(cfg.get("HEALTH_CONSECUTIVE_LOSS_THRESHOLD", 5)),
                            slippage_p99_bps=float(cfg.get("HEALTH_SLIPPAGE_P99_BPS", 50)),
                            mode_file=_rm_file,
                            auto_escalate=True,
                        )
                        if not _h_report.all_ok:
                            log_health_report(_h_report, log_dir)
                            _failed_names = ", ".join(r.name for r in _h_report.failed)
                            log_event(symbol, "HEALTH_CHECK_FAIL", f"failed=[{_failed_names}]")
                            if _h_report.escalated and cfg.get("DISCORD_WEBHOOK_URL"):
                                maybe_notify_discord(
                                    http, cfg["DISCORD_WEBHOOK_URL"],
                                    "HEALTH ALERT",
                                    f"Mode escalated: {_h_report.prev_mode} -> {_h_report.new_mode}. "
                                    f"Failed: {_failed_names}",
                                )
                    except Exception:
                        pass

            snap = step(state, tick, cfg, paused=paused)

            if forced_test_complete_reason:
                try:
                    setattr(snap, "execution_status", "FORCED_TEST_COMPLETE")
                    setattr(snap, "execution_reason", forced_test_complete_reason)
                    setattr(snap, "risk_blocked_reason", forced_test_complete_reason)
                    setattr(snap, "action", "HOLD")
                    setattr(snap, "action_reason", forced_test_complete_reason)
                except Exception:
                    pass

            try:
                current_action = str(getattr(snap, "action", "") or "").upper()
                ledger_qty_now = _ledger_position_qty(state)
                suppress, suppress_reason = _should_suppress_action_during_recovery(
                    action=current_action,
                    recovery_state=recovery_state,
                    ledger_qty=ledger_qty_now,
                    state=state,
                )
                if suppress:
                    setattr(snap, "execution_status", "RECOVERY_SUPPRESSED")
                    setattr(snap, "execution_reason", suppress_reason)
                    setattr(snap, "risk_blocked_reason", suppress_reason)
                    setattr(snap, "action", "HOLD")
                    setattr(snap, "action_reason", suppress_reason)
                    log_event(symbol, "RECOVERY_SUPPRESS", suppress_reason)
            except Exception:
                pass

            if force_test_only_once and (forced_test_consumed or forced_test_complete_reason):
                try:
                    action_now = str(getattr(snap, "action", "") or "").upper()
                    if action_now in {"BUY", "SELL"}:
                        setattr(snap, "action", "HOLD")
                        setattr(snap, "action_reason", "force_test_only_once_consumed")
                        setattr(snap, "execution_status", "FORCE_TEST_LATCHED")
                except Exception:
                    pass

            terminate_now, terminate_reason = _enforce_force_proof_mode(
                snap=snap,
                state=state,
                cfg=cfg,
                recovery_state=recovery_state,
                paused=paused,
                dry_run=dry_run,
                force_test_buy=force_test_buy,
                force_test_sell=force_test_sell,
                force_test_only_once=force_test_only_once,
                forced_test_consumed=forced_test_consumed,
                forced_test_complete_reason=forced_test_complete_reason,
            )
            if terminate_now:
                log_event(symbol, "FORCE_TEST_VETO", terminate_reason)
                _save_runtime_snapshot(
                    state_dir=state_dir,
                    state=state,
                    symbol=symbol,
                    run_id=run_id,
                    recovery_state=recovery_state,
                    last_fill_poll_ts=last_fill_poll_ts,
                    applied_fill_ids=applied_fill_ids,
                    recovery_source=recovery_source,
                )
                print(f"[EXIT] {terminate_reason}")
                return 0

            submitted_order: Optional[OrderState] = None
            submit_failed = False

            # --- Phase 16: runtime mode gating ---
            if _adapter_mode_enabled(cfg, state) and cfg.get("RUNTIME_MODE_GATING_ENABLED", True):
                _rm_file = cfg.get("RUNTIME_MODE_FILE", "") or None
                _rm_mode, _ = runtime_mode.load_mode(_rm_file)
                _rm_action = str(getattr(snap, "action", "") or "").upper()
                _rm_allowed, _rm_reason = runtime_mode.gate_action(_rm_action, _rm_mode)
                if not _rm_allowed:
                    try:
                        setattr(snap, "action", "HOLD")
                        setattr(snap, "action_reason", _rm_reason)
                        setattr(snap, "execution_status", "MODE_BLOCKED")
                        setattr(snap, "execution_reason", _rm_reason)
                    except Exception:
                        pass
                    log_event(symbol, "RUNTIME_MODE_BLOCK", f"mode={_rm_mode} action={_rm_action} reason={_rm_reason}")

            # --- Phase 16: auto-throttle on BUY ---
            if _adapter_mode_enabled(cfg, state):
                _at_action = str(getattr(snap, "action", "") or "").upper()
                if _at_action == "BUY":
                    try:
                        _at_qty_raw = getattr(snap, "execution_qty", None)
                        if _at_qty_raw is not None:
                            from decimal import Decimal as _Dec
                            _at_qty = _Dec(str(_at_qty_raw))
                            _at_adj, _at_mult, _at_reason = apply_throttle_to_qty(_at_qty, cfg=cfg)
                            if _at_mult < 1.0:
                                snap.execution_qty = _at_adj
                                log_event(symbol, "AUTO_THROTTLE", f"mult={_at_mult:.2f} qty={_at_qty}->{_at_adj} reason={_at_reason}")
                    except Exception:
                        pass

            if _adapter_mode_enabled(cfg, state) and (not paused) and (not dry_run) and (not forced_test_complete_reason):
                try:
                    submitted_order = _submit_adapter_order_from_snapshot(state=state, snap=snap, cfg=cfg)
                    if submitted_order is not None:
                        snap.order_id = str(submitted_order.order_id)
                        snap.execution_status = str(submitted_order.status)
                        prior_reason = safe_str(getattr(snap, "execution_reason", ""))
                        snap.execution_reason = f"{prior_reason} | submitted".strip(" |")

                        submitted_side = str(submitted_order.side).upper()
                        if submitted_side == "BUY":
                            try:
                                setattr(state, "entry_order_id", str(submitted_order.order_id))
                                setattr(state, "exit_order_id", "")
                            except Exception:
                                pass
                            recovery_state = BOT_STATE_ENTERING
                            _set_runtime_recovery_state(state, recovery_state, recovery_source)
                        elif submitted_side == "SELL":
                            try:
                                setattr(state, "exit_order_id", str(submitted_order.order_id))
                            except Exception:
                                pass
                            recovery_state = BOT_STATE_EXITING
                            _set_runtime_recovery_state(state, recovery_state, recovery_source)

                        try:
                            setattr(state, "last_processed_order_id", str(submitted_order.order_id))
                        except Exception:
                            pass
                        try:
                            setattr(state, "last_action_ts", int(getattr(tick, "epoch", 0) or time.time()))
                        except Exception:
                            pass

                        requested_forced_action = _requested_forced_action(force_test_buy, force_test_sell)
                        if force_test_only_once and requested_forced_action in {"BUY", "SELL"}:
                            submitted_side = str(submitted_order.side).upper()
                            if submitted_side == requested_forced_action:
                                _set_forced_request_inflight(
                                    state=state,
                                    requested_action=requested_forced_action,
                                    submitted_order_id=str(submitted_order.order_id),
                                    submitted_at=int(getattr(tick, "epoch", 0) or time.time()),
                                )

                        _persist_submit_transition(
                            cfg=cfg,
                            log_dir=log_dir,
                            run_id=run_id,
                            state=state,
                            order=submitted_order,
                            symbol=symbol,
                            ts_ms=int(getattr(tick, "epoch", 0) or time.time() * 1000),
                        )

                        _save_runtime_snapshot(
                            state_dir=state_dir,
                            state=state,
                            symbol=symbol,
                            run_id=run_id,
                            recovery_state=recovery_state,
                            last_fill_poll_ts=last_fill_poll_ts,
                            applied_fill_ids=applied_fill_ids,
                            recovery_source=recovery_source,
                        )

                        _maybe_abort_at_killpoint(
                            cfg=cfg,
                            key="KILL_AFTER_ORDER_SUBMIT",
                            symbol=symbol,
                            detail=(
                                f"run_id={run_id} order_id={submitted_order.order_id} "
                                f"side={submitted_order.side} status={submitted_order.status}"
                            ),
                        )

                        log_event(
                            symbol,
                            "ORDER_SUBMITTED",
                            (
                                f"client_order_id={submitted_order.client_order_id} "
                                f"order_id={submitted_order.order_id} side={submitted_order.side} "
                                f"qty={submitted_order.qty} type={submitted_order.order_type} "
                                f"status={submitted_order.status}"
                            ),
                        )

                except ControlledAbort:
                    raise
                except Exception as e:
                    submit_failed = True
                    snap.execution_status = "SUBMIT_FAIL"
                    snap.execution_reason = str(e)
                    _clear_forced_request_state(state)
                    try:
                        log_event(symbol, "ORDER_SUBMIT_FAIL", str(e))
                    except Exception:
                        pass

                    if force_test_only_once and not forced_test_complete_reason:
                        forced_test_complete_reason = f"submit_fail:{str(e)}"

            terminate_after_fill_processing = False
            terminal_flat_position_written = False
            positions_written_this_tick = False

            if _adapter_mode_enabled(cfg, state) and (not dry_run):
                try:
                    new_fills = _poll_new_fills(state=state, since_ts=last_fill_poll_ts)
                    fresh_rows: List[Dict[str, Any]] = []

                    for fill in new_fills:
                        fill_id = str(fill.fill_id)
                        if fill_id in applied_fill_ids:
                            continue

                        prior_qty_ledger = _ledger_position_qty(state)
                        prior_qty_adapter = _adapter_symbol_position_qty(state, symbol)
                        prior_qty_effective = prior_qty_adapter if _adapter_mode_enabled(cfg, state) else prior_qty_ledger
                        fill_ts_s = _normalize_epoch_seconds(int(fill.ts or 0))

                        applied_fill_ids.add(fill_id)
                        last_fill_poll_ts = max(last_fill_poll_ts, fill_ts_s)

                        _apply_fill_to_ledger(state=state, fill=fill, cfg=cfg)

                        post_qty_ledger = _ledger_position_qty(state)
                        post_qty_adapter = _adapter_symbol_position_qty(state, symbol)
                        post_qty_effective = post_qty_adapter if _adapter_mode_enabled(cfg, state) else post_qty_ledger

                        try:
                            setattr(state, "last_processed_fill_id", fill_id)
                        except Exception:
                            pass
                        try:
                            setattr(state, "last_processed_order_id", str(fill.order_id))
                        except Exception:
                            pass
                        try:
                            setattr(state, "last_action_ts", fill_ts_s)
                        except Exception:
                            pass

                        if not getattr(snap, "trade_id", "") and getattr(fill, "trade_id", None):
                            snap.trade_id = str(fill.trade_id)
                        if not getattr(snap, "order_id", ""):
                            snap.order_id = str(fill.order_id)
                        if not getattr(snap, "client_order_id", ""):
                            snap.client_order_id = str(fill.client_order_id)
                        snap.execution_status = "FILLED"

                        fill_row = _build_fallback_fill_row(run_id, fill)
                        fresh_rows.append(fill_row)

                        fill_side = str(fill.side).upper()
                        _mark_forced_request_fill_verified(state, fill_side)

                        # Use ledger quantities for close_to_flat detection.
                        # PaperAdapter processes fills synchronously inside place_order
                        # (_try_fill_or_rest), so adapter._positions is already updated
                        # before the fill-back loop sees the fill. prior_qty_effective
                        # would be 0 for the SELL case, making close_to_flat False.
                        # The VirtualLedger is only updated by _apply_fill_to_ledger
                        # (called above), so prior_qty_ledger / post_qty_ledger
                        # correctly capture the open→flat transition.
                        close_to_flat = (
                            fill_side == "SELL"
                            and _is_effectively_open_qty(prior_qty_ledger)
                            and _is_effectively_flat_qty(post_qty_ledger)
                        )

                        open_after_buy = (
                            fill_side == "BUY"
                            and _is_effectively_open_qty(post_qty_effective)
                        )

                        if fill_side == "BUY":
                            if open_after_buy:
                                recovery_state = BOT_STATE_OPEN
                            else:
                                recovery_state = BOT_STATE_ENTERING
                            recovery_source = "canonical_execution"
                            _set_runtime_recovery_state(state, recovery_state, recovery_source)
                            try:
                                setattr(state, "entry_order_id", str(fill.order_id))
                                setattr(state, "exit_order_id", "")
                            except Exception:
                                pass
                            if getattr(fill, "trade_id", None):
                                try:
                                    setattr(state, "open_trade_id", str(fill.trade_id))
                                except Exception:
                                    pass

                            _capture_entry_context_from_fill(state=state, fill=fill, snap=snap, cfg=cfg)

                        elif fill_side == "SELL":
                            if close_to_flat:
                                log_event(
                                    symbol,
                                    "CLOSE_TRANSITION_DETECTED",
                                    (
                                        f"fill_id={fill.fill_id} order_id={fill.order_id} "
                                        f"prior_qty_ledger={prior_qty_ledger} prior_qty_adapter={prior_qty_adapter} "
                                        f"post_qty_ledger={post_qty_ledger} post_qty_adapter={post_qty_adapter}"
                                    ),
                                )

                                _maybe_abort_at_killpoint(
                                    cfg=cfg,
                                    key="KILL_BEFORE_JOURNAL_WRITE",
                                    symbol=symbol,
                                    detail=(
                                        f"run_id={run_id} fill_id={fill.fill_id} "
                                        f"order_id={fill.order_id} "
                                        f"prior_qty={prior_qty_effective} post_qty={post_qty_effective}"
                                    ),
                                )

                                try:
                                    wrote_journal, journal_reason = _maybe_write_closed_trade_journal(
                                        state=state,
                                        log_dir=log_dir,
                                        run_id=run_id,
                                        fill=fill,
                                        snap=snap,
                                        symbol=symbol,
                                        prior_qty=prior_qty_ledger,
                                        post_qty=post_qty_ledger,
                                        run_start_ts=run_start_ts,
                                        startup_fill_ids=startup_fill_ids,
                                    )
                                except Exception as e:
                                    wrote_journal = False
                                    journal_reason = f"journal_exception:{e}"
                                    try:
                                        log_event(symbol, "TRADE_JOURNAL_FAIL", journal_reason)
                                    except Exception:
                                        pass

                                if wrote_journal:
                                    log_event(
                                        symbol,
                                        "TRADE_JOURNAL_APPEND",
                                        (
                                            f"trade_id={safe_str(getattr(fill, 'trade_id', None))} "
                                            f"exit_fill_id={fill.fill_id} "
                                            f"reason={journal_reason}"
                                        ),
                                    )
                                    _maybe_abort_at_killpoint(
                                        cfg=cfg,
                                        key="KILL_AFTER_JOURNAL_WRITE",
                                        symbol=symbol,
                                        detail=(
                                            f"run_id={run_id} fill_id={fill.fill_id} "
                                            f"trade_id={safe_str(getattr(fill, 'trade_id', None))}"
                                        ),
                                    )
                                else:
                                    log_event(
                                        symbol,
                                        "TRADE_JOURNAL_SKIP",
                                        (
                                            f"exit_fill_id={fill.fill_id} "
                                            f"reason={journal_reason} "
                                            f"fill_ts={fill_ts_s} "
                                            f"run_start_ts={run_start_ts}"
                                        ),
                                    )

                                _finalize_runtime_flat_state(
                                    state=state,
                                    symbol=symbol,
                                    fill=fill,
                                    recovery_source="canonical_execution",
                                )
                                recovery_state = BOT_STATE_FLAT
                                recovery_source = "canonical_execution"

                                try:
                                    realized_now = _ledger_realized_pnl(state)
                                    _append_terminal_flat_position_row(
                                        log_dir=log_dir,
                                        run_id=run_id,
                                        symbol=symbol,
                                        mark_px=fill.price,
                                        realized_pnl=realized_now,
                                        now_ts=fill_ts_s,
                                    )
                                    terminal_flat_position_written = True
                                    positions_written_this_tick = True
                                    log_event(
                                        symbol,
                                        "POSITIONS_FLAT_APPEND",
                                        (
                                            f"exit_fill_id={fill.fill_id} "
                                            f"qty=0 realized_pnl={realized_now}"
                                        ),
                                    )
                                except Exception as e:
                                    try:
                                        log_event(symbol, "POSITIONS_FLAT_APPEND_FAIL", str(e))
                                    except Exception:
                                        pass

                                # --- Phase 16: auto-throttle trade result feedback ---
                                try:
                                    _at_pnl = float(getattr(fill, "realized_pnl", 0) or 0)
                                    # If we can't get PnL from fill, try ledger
                                    if _at_pnl == 0:
                                        _at_pnl = float(_ledger_realized_pnl(state) or 0)
                                    _at_slip = float(getattr(fill, "slippage_bps", 0) or 0)
                                    record_trade_result(
                                        is_win=(_at_pnl > 0),
                                        slippage_bps=abs(_at_slip),
                                        cfg=cfg,
                                    )
                                except Exception:
                                    pass

                            else:
                                recovery_state = BOT_STATE_OPEN if _is_effectively_open_qty(post_qty_effective) else BOT_STATE_FLAT
                                recovery_source = "canonical_execution"
                                _set_runtime_recovery_state(state, recovery_state, recovery_source)
                                try:
                                    setattr(state, "exit_order_id", str(fill.order_id))
                                except Exception:
                                    pass

                        _persist_fill_transition(
                            cfg=cfg,
                            log_dir=log_dir,
                            run_id=run_id,
                            state=state,
                            fill=fill,
                            symbol=symbol,
                            recovery_state=recovery_state,
                            wrote_flat_row=terminal_flat_position_written and _is_effectively_flat_qty(post_qty_effective),
                        )

                        log_event(
                            symbol,
                            "FILL_APPLIED",
                            (
                                f"fill_id={fill.fill_id} trade_id={safe_str(fill.trade_id)} "
                                f"order_id={fill.order_id} side={fill.side} "
                                f"qty={fill.qty} px={fill.price} fee={fill.fee} "
                                f"prior_qty_ledger={prior_qty_ledger} prior_qty_adapter={prior_qty_adapter} "
                                f"post_qty_ledger={post_qty_ledger} post_qty_adapter={post_qty_adapter} "
                                f"recovery_state={recovery_state}"
                            ),
                        )

                        should_exit, exit_reason = _should_exit_after_forced_success(
                            cfg=cfg,
                            fill=fill,
                            state=state,
                            force_test_buy=force_test_buy,
                            force_test_sell=force_test_sell,
                        )
                        if should_exit and not forced_test_complete_reason:
                            forced_test_consumed = True
                            forced_test_complete_reason = exit_reason
                            terminate_after_fill_processing = True
                            _clear_forced_request_state(state)
                            log_event(symbol, "FORCED_TEST_COMPLETE", forced_test_complete_reason)

                    if fresh_rows:
                        _maybe_abort_at_killpoint(
                            cfg=cfg,
                            key="KILL_AFTER_FILL_PERSIST",
                            symbol=symbol,
                            detail=f"run_id={run_id} fills_persisted={len(fresh_rows)}",
                        )

                    try:
                        positions = state.execution_adapter.get_positions()
                        if positions:
                            _append_positions_rows(log_dir, run_id, positions, int(getattr(tick, "epoch", 0) or 0))
                            positions_written_this_tick = True
                        elif recovery_state == BOT_STATE_FLAT and terminal_flat_position_written:
                            pass
                    except Exception:
                        pass

                    if positions_written_this_tick:
                        _maybe_abort_at_killpoint(
                            cfg=cfg,
                            key="KILL_AFTER_POSITIONS_WRITE",
                            symbol=symbol,
                            detail=f"run_id={run_id} recovery_state={recovery_state}",
                        )

                    _save_runtime_snapshot(
                        state_dir=state_dir,
                        state=state,
                        symbol=symbol,
                        run_id=run_id,
                        recovery_state=recovery_state,
                        last_fill_poll_ts=last_fill_poll_ts,
                        applied_fill_ids=applied_fill_ids,
                        recovery_source=recovery_source,
                    )

                except ControlledAbort:
                    raise
                except Exception as e:
                    try:
                        log_event(symbol, "FILL_POLL_FAIL", str(e))
                    except Exception:
                        pass

            if getattr(snap, "events", None):
                for ev in snap.events:
                    try:
                        log_event(symbol, ev.name, ev.message)
                    except Exception:
                        pass

                    if getattr(ev, "notify_title", "") and cfg.get("DISCORD_WEBHOOK_URL"):
                        try:
                            maybe_notify_discord(
                                http,
                                cfg["DISCORD_WEBHOOK_URL"],
                                ev.notify_title,
                                ev.notify_body or ev.message,
                            )
                        except Exception:
                            pass

            print(
                f"[{snap.ts}] px={snap.px:.2f} "
                f"sig={safe_str(getattr(snap, 'sig_1m', None))} trend_ok={safe_str(getattr(snap, 'trend_ok_1m', None))} "
                f"score={safe_str(getattr(snap, 'score_1m', None))} "
                f"cash={getattr(snap, 'cash_usd', 0):.2f} qty={safe_str(getattr(snap, 'position_qty', None))} "
                f"exec_qty={safe_str(getattr(snap, 'execution_qty', None))} "
                f"equity={getattr(snap, 'equity_usd', 0):.2f} "
                f"unrl={getattr(snap, 'unrl_pnl_usd', 0):.2f} realized={getattr(snap, 'realized_pnl_usd', 0):.2f} "
                f"hold={safe_str(getattr(snap, 'hold_s', None))}s "
                f"belowN={safe_str(getattr(snap, 'trend_below_count', None))} "
                f"cooldown={safe_str(getattr(snap, 'cooldown_remaining_s', None))}s "
                f"paused={int(bool(paused))} "
                f"action={safe_str(getattr(snap, 'action', None))} "
                f"{safe_str(getattr(snap, 'action_reason', None))} "
                f"{safe_str(getattr(snap, 'risk_blocked_reason', None))} "
                f"next={safe_str(getattr(snap, 'next_poll_s', None))}s "
                f"conf={safe_str(getattr(snap, 'confluence_score', None))} "
                f"exec_mode={safe_str(getattr(snap, 'execution_mode', None))} "
                f"client_order_id={safe_str(getattr(snap, 'client_order_id', None))} "
                f"order_id={safe_str(getattr(snap, 'order_id', None))} "
                f"trade_id={safe_str(getattr(snap, 'trade_id', None))} "
                f"exec_status={safe_str(getattr(snap, 'execution_status', None))} "
                f"recovery_state={recovery_state} "
                f"recovery_source={recovery_source} "
                f"submit_failed={int(bool(submit_failed))} "
                f"{safe_str(getattr(snap, 'confluence_reasons', None))}"
            )

            try:
                log_signal_snapshot(snap, symbol=symbol, price=getattr(snap, "px", None))
            except Exception:
                pass

            did_one = True

            if once and did_one:
                return 0

            if terminate_after_fill_processing and forced_test_complete_reason:
                return 0

            await asyncio.sleep(float(getattr(snap, "next_poll_s", 1.0)))

    except ControlledAbort as e:
        try:
            log_event(symbol, "RUNNER_ABORT", str(e))
        except Exception:
            pass
        print(f"[ABORT] {e}")
        return 99
    except KeyboardInterrupt:
        print("[STOP] KeyboardInterrupt")
        return 0
    except Exception as e:
        try:
            log_event(symbol, "RUNNER_CRASH", str(e))
        except Exception:
            pass
        print(f"[CRASH] {e}")
        print(traceback.format_exc())
        return 1
    finally:
        try:
            _mirror_adapter_state_to_canonical(
                cfg=cfg,
                state=state,
                reason="RUNNER_FINALIZE",
                ts_ms=int(time.time() * 1000),
                symbol=symbol,
                force_flat_row=(recovery_state == BOT_STATE_FLAT),
            )
        except Exception:
            pass
        try:
            _save_runtime_snapshot(
                state_dir=state_dir,
                state=state,
                symbol=symbol,
                run_id=run_id,
                recovery_state=recovery_state,
                last_fill_poll_ts=last_fill_poll_ts,
                applied_fill_ids=applied_fill_ids,
                recovery_source=recovery_source,
            )
        except Exception:
            pass
        # --- Phase 16: finalize run manifest + artifact integrity snapshot ---
        if _run_manifest is not None:
            try:
                finalize_manifest(_run_manifest, status="COMPLETE")
                save_manifest(_run_manifest, log_dir)
            except Exception:
                pass
        try:
            save_integrity_manifest(log_dir)
        except Exception:
            pass
        await _close_http(http)


if __name__ == "__main__":
    # line above: raise SystemExit(asyncio.run(run_live(...)))
    raise SystemExit(asyncio.run(run_live(cfg_path=None, dry_run=None, once=None)))