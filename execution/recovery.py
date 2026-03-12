#!/usr/bin/env python3
# execution/recovery.py

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Recovery state model
# ---------------------------------------------------------------------------

BOT_STATE_FLAT = "FLAT"
BOT_STATE_ENTERING = "ENTERING"
BOT_STATE_OPEN = "OPEN"
BOT_STATE_EXITING = "EXITING"
BOT_STATE_RECOVERY_HALT = "RECOVERY_HALT"

ORDER_SIDE_BUY = "BUY"
ORDER_SIDE_SELL = "SELL"

ORDER_STATUS_NEW = "NEW"
ORDER_STATUS_ACCEPTED = "ACCEPTED"
ORDER_STATUS_OPEN = "OPEN"
ORDER_STATUS_PARTIALLY_FILLED = "PARTIALLY_FILLED"
ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_CANCELED = "CANCELED"
ORDER_STATUS_CANCELLED = "CANCELLED"
ORDER_STATUS_REJECTED = "REJECTED"

Q8 = Decimal("0.00000001")
ZERO = Decimal("0")
EPS_QTY = Decimal("0.00000001")
EPS_PX = Decimal("0.00000001")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SnapshotState:
    run_id: str = ""
    session_id: str = ""
    symbol: str = ""
    bot_state: str = BOT_STATE_FLAT
    position_qty: Decimal = Decimal("0")
    avg_entry: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    open_trade_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    last_processed_fill_id: Optional[str] = None
    last_processed_order_id: Optional[str] = None
    cooldown_until: Optional[str] = None
    last_action_ts: Optional[str] = None
    state_version: int = 1
    applied_fill_ids: List[str] = field(default_factory=list)
    closed_trade_keys: List[str] = field(default_factory=list)
    open_trade_ctx: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRecord:
    order_id: str
    client_order_id: str
    trade_id: Optional[str]
    side: str
    status: str
    qty: Decimal
    filled_qty: Decimal
    price: Decimal
    ts: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FillRecord:
    fill_id: str
    order_id: str
    client_order_id: str
    trade_id: Optional[str]
    side: str
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    ts: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionRecord:
    run_id: str
    ts: str
    symbol: str
    qty: Decimal
    avg_entry_px: Decimal
    mark_px: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    side: str
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountRecord:
    ts: str
    cash: Decimal
    realized_pnl: Decimal = Decimal("0")
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecoveryIssue:
    severity: str
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass
class RecoveryResult:
    ok: bool
    bot_state: str
    position_qty: Decimal
    avg_entry: Decimal
    cash: Decimal
    realized_pnl: Decimal
    open_trade_id: Optional[str]
    entry_order_id: Optional[str]
    exit_order_id: Optional[str]
    last_processed_fill_id: Optional[str]
    last_processed_order_id: Optional[str]
    source_of_truth: str
    orders_path: Optional[str]
    fills_path: Optional[str]
    positions_path: Optional[str]
    account_path: Optional[str]
    snapshot_path: Optional[str]
    pending_entry_order_id: Optional[str] = None
    pending_exit_order_id: Optional[str] = None
    halt_reason: Optional[str] = None
    contradictions: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    issues: List[RecoveryIssue] = field(default_factory=list)

    def to_log_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "bot_state": self.bot_state,
            "position_qty": str(self.position_qty),
            "avg_entry": str(self.avg_entry),
            "cash": str(self.cash),
            "realized_pnl": str(self.realized_pnl),
            "open_trade_id": self.open_trade_id,
            "entry_order_id": self.entry_order_id,
            "exit_order_id": self.exit_order_id,
            "last_processed_fill_id": self.last_processed_fill_id,
            "last_processed_order_id": self.last_processed_order_id,
            "source_of_truth": self.source_of_truth,
            "orders_path": self.orders_path,
            "fills_path": self.fills_path,
            "positions_path": self.positions_path,
            "account_path": self.account_path,
            "snapshot_path": self.snapshot_path,
            "pending_entry_order_id": self.pending_entry_order_id,
            "pending_exit_order_id": self.pending_exit_order_id,
            "halt_reason": self.halt_reason,
            "contradictions": list(self.contradictions),
            "notes": list(self.notes),
            "issues": [i.to_log_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def reconcile_on_startup(cfg: Dict[str, Any]) -> RecoveryResult:
    """
    Rebuild runtime truth on startup using strict precedence:

        canonical execution truth:
            fills.csv
            + positions/account snapshots as refinement/cross-check
            + orders.csv for pending intent
        snapshot JSON fallback only if canonical execution truth is absent

    Principles:
    - fills are primary execution truth
    - positions/account refine and cross-check canonical truth, but do not outrank fills
    - orders describe active/pending order intent, but stale order rows must not outrank
      canonical terminal-flat execution truth
    - snapshot is advisory fallback, not a peer truth surface
    - only material canonical contradictions halt recovery
    """

    symbol = _nullable_str(cfg.get("PRODUCT_ID")) or _nullable_str(cfg.get("SYMBOL")) or ""

    snapshot_path = _resolve_snapshot_path(cfg)
    orders_path = _resolve_orders_path(cfg)
    fills_path = _resolve_fills_path(cfg)
    positions_path = _resolve_positions_path(cfg)
    account_path = _resolve_account_path(cfg)

    snapshot = _load_snapshot(snapshot_path)
    orders = _load_orders(orders_path)
    fills = _load_fills(fills_path)
    positions = _load_positions(positions_path, symbol=symbol or None)
    accounts = _load_accounts(account_path)

    issues: List[RecoveryIssue] = []
    notes: List[str] = []

    fills_truth = _build_truth_from_fills(fills)
    orders_truth = _build_truth_from_orders(orders)
    positions_truth = _build_truth_from_positions(positions)
    account_truth = _build_truth_from_accounts(accounts)

    # -----------------------------------------------------------------------
    # Canonical contradiction checks
    # -----------------------------------------------------------------------

    _check_canonical_consistency(
        fills_truth=fills_truth,
        positions_truth=positions_truth,
        orders_truth=orders_truth,
        snapshot=snapshot,
        issues=issues,
        notes=notes,
    )

    # Normalize stale pending orders when canonical execution truth is terminal.
    _normalize_orders_against_canonical_terminal_truth(
        fills_truth=fills_truth,
        positions_truth=positions_truth,
        orders_truth=orders_truth,
        notes=notes,
    )

    # -----------------------------------------------------------------------
    # Resolve strongest truth
    # -----------------------------------------------------------------------

    position_qty = Decimal("0")
    avg_entry = Decimal("0")
    cash = Decimal("0")
    realized_pnl = Decimal("0")
    open_trade_id: Optional[str] = None
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    last_processed_fill_id: Optional[str] = None
    last_processed_order_id: Optional[str] = None
    pending_entry_order_id: Optional[str] = orders_truth["pending_entry_order_id"]
    pending_exit_order_id: Optional[str] = orders_truth["pending_exit_order_id"]
    source_of_truth = "defaults"
    bot_state = BOT_STATE_FLAT

    has_canonical_execution_truth = fills_truth["has_fills"] or positions_truth["has_positions"]
    has_orders = orders_truth["has_orders"]
    has_snapshot_state = _snapshot_has_meaningful_state(snapshot)

    if has_canonical_execution_truth:
        source_of_truth = "canonical_execution"

        # Primary truth from fills.
        if fills_truth["has_fills"]:
            position_qty = fills_truth["position_qty"]
            avg_entry = fills_truth["avg_entry"]
            open_trade_id = fills_truth["open_trade_id"]
            entry_order_id = fills_truth["entry_order_id"]
            exit_order_id = fills_truth["exit_order_id"]
            last_processed_fill_id = fills_truth["last_fill_id"]
            realized_pnl = fills_truth["realized_pnl"]

        # Positions refine when fresh, but do not outrank fills.
        if positions_truth["has_positions"]:
            if not fills_truth["has_fills"]:
                position_qty = positions_truth["position_qty"]
                avg_entry = positions_truth["avg_entry"]
                realized_pnl = positions_truth["realized_pnl"]
                notes.append("no fills found; restored position state from canonical positions snapshot")
            else:
                if positions_truth["is_fresh_vs_fills"]:
                    if _is_zero(positions_truth["position_qty"]):
                        position_qty = ZERO
                        avg_entry = ZERO
                        open_trade_id = None
                        notes.append("fresh canonical positions snapshot confirmed terminal flat state")
                    elif _same_decimal(positions_truth["position_qty"], fills_truth["position_qty"]):
                        avg_entry = positions_truth["avg_entry"] if positions_truth["position_qty"] > ZERO else ZERO
                        notes.append("fresh canonical positions snapshot refined avg_entry")
                    realized_pnl = positions_truth["realized_pnl"]
                    notes.append("restored realized_pnl from canonical positions snapshot")
                else:
                    notes.append("positions snapshot is older than latest fill; fills truth kept as primary")

        # Account refines cash; realized_pnl from account is advisory only.
        if account_truth["has_accounts"]:
            cash = account_truth["cash"]
            notes.append("restored cash from canonical account snapshot")
            if _is_zero(realized_pnl) and not _is_zero(account_truth["realized_pnl"]):
                realized_pnl = account_truth["realized_pnl"]
                notes.append("positions/fills realized_pnl absent; refined realized_pnl from account snapshot")
        elif not _is_zero(snapshot.cash):
            cash = snapshot.cash
            notes.append("no canonical account artifact found; restored cash from snapshot")
        else:
            cash = ZERO
            notes.append("no canonical account artifact found; cash defaulted to 0")

        if not open_trade_id and fills_truth["has_fills"] and fills_truth["position_qty"] > ZERO:
            open_trade_id = fills_truth["open_trade_id"]

        if position_qty > ZERO:
            if pending_exit_order_id:
                bot_state = BOT_STATE_EXITING
                if not exit_order_id:
                    exit_order_id = pending_exit_order_id
                notes.append("canonical open qty plus pending exit order -> startup state=EXITING")
            else:
                bot_state = BOT_STATE_OPEN
                notes.append("canonical execution truth implies OPEN")
        else:
            # Canonical terminal flat truth wins over stale pending exit rows.
            if pending_entry_order_id:
                bot_state = BOT_STATE_ENTERING
                if not entry_order_id:
                    entry_order_id = pending_entry_order_id
                notes.append("flat inventory plus pending entry order -> startup state=ENTERING")
            else:
                bot_state = BOT_STATE_FLAT
                notes.append("canonical execution truth implies FLAT")

            open_trade_id = None
            avg_entry = ZERO

        last_processed_order_id = orders_truth["last_order_id"] or snapshot.last_processed_order_id

    elif has_orders:
        source_of_truth = "orders"

        if account_truth["has_accounts"]:
            cash = account_truth["cash"]
            notes.append("restored cash from account artifact under orders-only recovery")
            realized_pnl = account_truth["realized_pnl"]
        else:
            cash = snapshot.cash
            realized_pnl = snapshot.realized_pnl

        last_processed_fill_id = snapshot.last_processed_fill_id
        last_processed_order_id = orders_truth["last_order_id"] or snapshot.last_processed_order_id

        if pending_entry_order_id and pending_exit_order_id:
            issues.append(
                RecoveryIssue(
                    severity="fatal",
                    code="ORDERS_SIMULTANEOUS_PENDING_ENTRY_AND_EXIT",
                    message="simultaneous pending entry and pending exit orders detected",
                    details={
                        "pending_entry_order_id": pending_entry_order_id,
                        "pending_exit_order_id": pending_exit_order_id,
                    },
                )
            )
        elif pending_entry_order_id:
            bot_state = BOT_STATE_ENTERING
            entry_order_id = pending_entry_order_id
            open_trade_id = orders_truth["pending_trade_id"] or snapshot.open_trade_id
            notes.append("no canonical fills/positions found; pending entry reconstructed from orders")
        elif pending_exit_order_id:
            if snapshot.position_qty > ZERO:
                bot_state = BOT_STATE_EXITING
                position_qty = _q8(snapshot.position_qty)
                avg_entry = _q8(snapshot.avg_entry)
                open_trade_id = snapshot.open_trade_id or _trade_id_from_ctx(snapshot.open_trade_ctx)
                entry_order_id = snapshot.entry_order_id
                exit_order_id = pending_exit_order_id
                notes.append("orders-only pending exit restored using snapshot open position fallback")
            else:
                issues.append(
                    RecoveryIssue(
                        severity="fatal",
                        code="ORDERS_PENDING_EXIT_WITHOUT_POSITION",
                        message="pending exit order found but no open position exists in canonical truth or snapshot",
                        details={"pending_exit_order_id": pending_exit_order_id},
                    )
                )
        else:
            if snapshot.position_qty > ZERO:
                source_of_truth = "snapshot"
                position_qty = _q8(snapshot.position_qty)
                avg_entry = _q8(snapshot.avg_entry)
                cash = account_truth["cash"] if account_truth["has_accounts"] else snapshot.cash
                realized_pnl = account_truth["realized_pnl"] if account_truth["has_accounts"] else _q8(snapshot.realized_pnl)
                open_trade_id = snapshot.open_trade_id or _trade_id_from_ctx(snapshot.open_trade_ctx)
                entry_order_id = snapshot.entry_order_id
                exit_order_id = snapshot.exit_order_id
                bot_state = BOT_STATE_OPEN
                notes.append("orders history had no active order; restored OPEN from snapshot fallback")
            else:
                bot_state = BOT_STATE_FLAT
                notes.append("orders history exists but no active pending order and no open snapshot")

    elif has_snapshot_state:
        source_of_truth = "snapshot"
        position_qty = _q8(snapshot.position_qty)
        avg_entry = _q8(snapshot.avg_entry if snapshot.position_qty > ZERO else ZERO)
        cash = account_truth["cash"] if account_truth["has_accounts"] else _q8(snapshot.cash)
        realized_pnl = account_truth["realized_pnl"] if account_truth["has_accounts"] else _q8(snapshot.realized_pnl)
        open_trade_id = snapshot.open_trade_id or _trade_id_from_ctx(snapshot.open_trade_ctx)
        entry_order_id = snapshot.entry_order_id or _nullable_str(snapshot.open_trade_ctx.get("entry_order_id"))
        exit_order_id = snapshot.exit_order_id
        last_processed_fill_id = snapshot.last_processed_fill_id
        last_processed_order_id = snapshot.last_processed_order_id
        bot_state = BOT_STATE_OPEN if snapshot.position_qty > ZERO else BOT_STATE_FLAT
        notes.append("no canonical execution artifacts found; restored from snapshot fallback")

    else:
        source_of_truth = "defaults"
        position_qty = ZERO
        avg_entry = ZERO
        cash = account_truth["cash"] if account_truth["has_accounts"] else ZERO
        realized_pnl = account_truth["realized_pnl"] if account_truth["has_accounts"] else (
            positions_truth["realized_pnl"] if positions_truth["has_positions"] else ZERO
        )
        bot_state = BOT_STATE_FLAT
        notes.append("no canonical execution or snapshot evidence of open state; default FLAT")

    # -----------------------------------------------------------------------
    # Canonical terminal-flat normalization
    # -----------------------------------------------------------------------

    if _canonical_truth_is_terminal_flat(fills_truth=fills_truth, positions_truth=positions_truth):
        bot_state = BOT_STATE_FLAT
        position_qty = ZERO
        avg_entry = ZERO
        open_trade_id = None
        pending_exit_order_id = None
        notes.append("canonical terminal-flat truth overrides stale transitional metadata")

    # -----------------------------------------------------------------------
    # Final consistency / halt checks
    # -----------------------------------------------------------------------

    _check_final_recovery_state(
        bot_state=bot_state,
        position_qty=position_qty,
        avg_entry=avg_entry,
        pending_entry_order_id=pending_entry_order_id,
        pending_exit_order_id=pending_exit_order_id,
        open_trade_id=open_trade_id,
        issues=issues,
        notes=notes,
    )

    fatal_issues = [i for i in issues if i.severity.lower() == "fatal"]
    contradictions = [f"{i.code}: {i.message}" for i in fatal_issues]

    if fatal_issues:
        return RecoveryResult(
            ok=False,
            bot_state=BOT_STATE_RECOVERY_HALT,
            position_qty=_q8(position_qty),
            avg_entry=_q8(avg_entry),
            cash=_q8(cash),
            realized_pnl=_q8(realized_pnl),
            open_trade_id=open_trade_id,
            entry_order_id=entry_order_id,
            exit_order_id=exit_order_id,
            last_processed_fill_id=last_processed_fill_id,
            last_processed_order_id=last_processed_order_id,
            source_of_truth=source_of_truth,
            orders_path=orders_path,
            fills_path=fills_path,
            positions_path=positions_path,
            account_path=account_path,
            snapshot_path=snapshot_path,
            pending_entry_order_id=pending_entry_order_id,
            pending_exit_order_id=pending_exit_order_id,
            halt_reason="; ".join(contradictions),
            contradictions=contradictions,
            notes=notes,
            issues=issues,
        )

    return RecoveryResult(
        ok=True,
        bot_state=bot_state,
        position_qty=_q8(position_qty),
        avg_entry=_q8(avg_entry),
        cash=_q8(cash),
        realized_pnl=_q8(realized_pnl),
        open_trade_id=open_trade_id,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        last_processed_fill_id=last_processed_fill_id,
        last_processed_order_id=last_processed_order_id,
        source_of_truth=source_of_truth,
        orders_path=orders_path,
        fills_path=fills_path,
        positions_path=positions_path,
        account_path=account_path,
        snapshot_path=snapshot_path,
        pending_entry_order_id=pending_entry_order_id,
        pending_exit_order_id=pending_exit_order_id,
        contradictions=[],
        notes=notes,
        issues=issues,
    )


# ---------------------------------------------------------------------------
# Optional helper for runner logging
# ---------------------------------------------------------------------------

def format_recovery_log_lines(result: RecoveryResult) -> List[str]:
    lines = [
        f"[RECOVERY] ok={result.ok} state={result.bot_state} source={result.source_of_truth}",
        f"[RECOVERY] qty={result.position_qty} avg_entry={result.avg_entry} cash={result.cash} realized_pnl={result.realized_pnl}",
        f"[RECOVERY] open_trade_id={result.open_trade_id} entry_order_id={result.entry_order_id} exit_order_id={result.exit_order_id}",
        f"[RECOVERY] last_fill_id={result.last_processed_fill_id} last_order_id={result.last_processed_order_id}",
        f"[RECOVERY] snapshot={result.snapshot_path}",
        f"[RECOVERY] orders={result.orders_path}",
        f"[RECOVERY] fills={result.fills_path}",
        f"[RECOVERY] positions={result.positions_path}",
        f"[RECOVERY] account={result.account_path}",
    ]

    if result.pending_entry_order_id:
        lines.append(f"[RECOVERY] pending_entry_order_id={result.pending_entry_order_id}")

    if result.pending_exit_order_id:
        lines.append(f"[RECOVERY] pending_exit_order_id={result.pending_exit_order_id}")

    for note in result.notes:
        lines.append(f"[RECOVERY][NOTE] {note}")

    for issue in result.issues:
        lines.append(
            f"[RECOVERY][{issue.severity.upper()}][{issue.code}] {issue.message}"
        )
        if issue.details:
            lines.append(f"[RECOVERY][DETAILS][{issue.code}] {json.dumps(issue.details, sort_keys=True)}")

    for c in result.contradictions:
        lines.append(f"[RECOVERY][CONTRADICTION] {c}")

    if result.halt_reason:
        lines.append(f"[RECOVERY][HALT] {result.halt_reason}")

    return lines


# ---------------------------------------------------------------------------
# Artifact path resolution
# ---------------------------------------------------------------------------

def _resolve_snapshot_path(cfg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        cfg.get("RUNTIME_STATE_PATH"),
        cfg.get("RUNTIME_STATE_LATEST_JSON"),
        cfg.get("CHECKPOINT_PATH"),
        _join_if(cfg.get("OPS_LOG_DIR"), "runtime_state_latest.json"),
        _join_if(cfg.get("LOG_DIR"), "runtime_state_latest.json"),
        _join_if(cfg.get("RUN_LOG_DIR"), "runtime_state_latest.json"),
    ]
    return _first_existing_or_first_nonempty(candidates)


def _resolve_orders_path(cfg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        cfg.get("LIVE_ORDERS_CSV"),
        cfg.get("ORDERS_CSV"),
        cfg.get("EXEC_ORDERS_CSV"),
        _join_if(cfg.get("OPS_LOG_DIR"), "orders.csv"),
        _join_if(cfg.get("OPS_LOG_DIR"), "orders_latest.csv"),
        _join_if(cfg.get("LOG_DIR"), "orders.csv"),
        _join_if(cfg.get("LOG_DIR"), "orders_latest.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "orders.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "orders_latest.csv"),
    ]
    return _first_existing_or_first_nonempty(candidates)


def _resolve_fills_path(cfg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        cfg.get("LIVE_FILLS_CSV"),
        cfg.get("FILLS_CSV"),
        cfg.get("EXEC_FILLS_CSV"),
        _join_if(cfg.get("OPS_LOG_DIR"), "fills.csv"),
        _join_if(cfg.get("OPS_LOG_DIR"), "fills_latest.csv"),
        _join_if(cfg.get("LOG_DIR"), "fills.csv"),
        _join_if(cfg.get("LOG_DIR"), "fills_latest.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "fills.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "fills_latest.csv"),
    ]
    return _first_existing_or_first_nonempty(candidates)


def _resolve_positions_path(cfg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        cfg.get("LIVE_POSITIONS_CSV"),
        cfg.get("POSITIONS_CSV"),
        cfg.get("EXEC_POSITIONS_CSV"),
        _join_if(cfg.get("OPS_LOG_DIR"), "positions.csv"),
        _join_if(cfg.get("OPS_LOG_DIR"), "positions_latest.csv"),
        _join_if(cfg.get("LOG_DIR"), "positions.csv"),
        _join_if(cfg.get("LOG_DIR"), "positions_latest.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "positions.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "positions_latest.csv"),
    ]
    return _first_existing_or_first_nonempty(candidates)


def _resolve_account_path(cfg: Dict[str, Any]) -> Optional[str]:
    candidates = [
        cfg.get("LIVE_ACCOUNT_CSV"),
        cfg.get("ACCOUNT_CSV"),
        cfg.get("EXEC_ACCOUNT_CSV"),
        _join_if(cfg.get("OPS_LOG_DIR"), "account.csv"),
        _join_if(cfg.get("OPS_LOG_DIR"), "account_latest.csv"),
        _join_if(cfg.get("LOG_DIR"), "account.csv"),
        _join_if(cfg.get("LOG_DIR"), "account_latest.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "account.csv"),
        _join_if(cfg.get("RUN_LOG_DIR"), "account_latest.csv"),
    ]
    return _first_existing_or_first_nonempty(candidates)


def _join_if(base: Optional[str], name: str) -> Optional[str]:
    if not base:
        return None
    return os.path.join(base, name)


def _first_existing_or_first_nonempty(paths: Iterable[Optional[str]]) -> Optional[str]:
    nonempty = [p for p in paths if p]
    for p in nonempty:
        if os.path.exists(p):
            return p
    return nonempty[0] if nonempty else None


# ---------------------------------------------------------------------------
# Snapshot loading
# ---------------------------------------------------------------------------

def _load_snapshot(path: Optional[str]) -> SnapshotState:
    if not path or not os.path.exists(path):
        return SnapshotState()

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return SnapshotState()

    applied_fill_ids = raw.get("applied_fill_ids") or []
    closed_trade_keys = raw.get("closed_trade_keys") or []
    open_trade_ctx = raw.get("open_trade_ctx") if isinstance(raw.get("open_trade_ctx"), dict) else {}

    return SnapshotState(
        run_id=str(raw.get("run_id", "")),
        session_id=str(raw.get("session_id", "")),
        symbol=str(raw.get("symbol", "")),
        bot_state=str(raw.get("bot_state", BOT_STATE_FLAT)).strip().upper() or BOT_STATE_FLAT,
        position_qty=_to_decimal(raw.get("position_qty", "0")),
        avg_entry=_to_decimal(raw.get("avg_entry", "0")),
        cash=_to_decimal(raw.get("cash", "0")),
        realized_pnl=_to_decimal(raw.get("realized_pnl", "0")),
        open_trade_id=_nullable_str(raw.get("open_trade_id")),
        entry_order_id=_nullable_str(raw.get("entry_order_id")),
        exit_order_id=_nullable_str(raw.get("exit_order_id")),
        last_processed_fill_id=_nullable_str(raw.get("last_processed_fill_id")),
        last_processed_order_id=_nullable_str(raw.get("last_processed_order_id")),
        cooldown_until=_nullable_str(raw.get("cooldown_until")),
        last_action_ts=_nullable_str(raw.get("last_action_ts")),
        state_version=_safe_int(raw.get("state_version", 1), 1),
        applied_fill_ids=[str(x) for x in applied_fill_ids if str(x).strip()],
        closed_trade_keys=[str(x) for x in closed_trade_keys if str(x).strip()],
        open_trade_ctx=open_trade_ctx,
    )


# ---------------------------------------------------------------------------
# Orders loading
# ---------------------------------------------------------------------------

def _load_orders(path: Optional[str]) -> List[OrderRecord]:
    if not path or not os.path.exists(path):
        return []

    rows: List[OrderRecord] = []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                order_id = _pick(raw, "order_id", "id", "exec_order_id")
                if not order_id:
                    continue

                rows.append(
                    OrderRecord(
                        order_id=order_id,
                        client_order_id=_pick(raw, "client_order_id", "client_id", "clordid") or order_id,
                        trade_id=_pick(raw, "trade_id", "open_trade_id"),
                        side=_normalize_side(_pick(raw, "side")),
                        status=_normalize_order_status(_pick(raw, "status", "order_status")),
                        qty=_to_decimal(_pick(raw, "qty", "order_qty", "quantity")),
                        filled_qty=_to_decimal(_pick(raw, "filled_qty", "cum_qty", "executed_qty")),
                        price=_to_decimal(_pick(raw, "price", "limit_price", "avg_price")),
                        ts=_pick(raw, "ts", "timestamp", "created_at", "event_ts") or "",
                        raw=dict(raw),
                    )
                )
    except Exception:
        return []

    rows.sort(key=lambda r: _ts_key(r.ts))
    return rows


# ---------------------------------------------------------------------------
# Fills loading
# ---------------------------------------------------------------------------

def _load_fills(path: Optional[str]) -> List[FillRecord]:
    if not path or not os.path.exists(path):
        return []

    rows: List[FillRecord] = []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                fill_id = _pick(raw, "fill_id", "id", "exec_id", "trade_execution_id")
                order_id = _pick(raw, "order_id", "exec_order_id", "parent_order_id")

                if not fill_id:
                    fill_id = _fill_fingerprint(raw)

                rows.append(
                    FillRecord(
                        fill_id=fill_id,
                        order_id=order_id or "",
                        client_order_id=_pick(raw, "client_order_id", "client_id", "clordid") or "",
                        trade_id=_pick(raw, "trade_id", "open_trade_id"),
                        side=_normalize_side(_pick(raw, "side")),
                        qty=_to_decimal(_pick(raw, "qty", "fill_qty", "last_qty", "quantity")),
                        price=_to_decimal(_pick(raw, "price", "fill_price", "last_px", "avg_price")),
                        fee=_to_decimal(_pick(raw, "fee", "commission")),
                        ts=_pick(raw, "ts", "timestamp", "filled_at", "event_ts") or "",
                        raw=dict(raw),
                    )
                )
    except Exception:
        return []

    deduped: Dict[str, FillRecord] = {}
    for row in rows:
        deduped[row.fill_id] = row

    out = list(deduped.values())
    out.sort(key=lambda r: _ts_key(r.ts))
    return out


# ---------------------------------------------------------------------------
# Positions loading
# ---------------------------------------------------------------------------

def _load_positions(path: Optional[str], symbol: Optional[str] = None) -> List[PositionRecord]:
    if not path or not os.path.exists(path):
        return []

    rows: List[PositionRecord] = []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                row_symbol = _pick(raw, "symbol", "product_id") or ""
                if symbol and row_symbol and row_symbol.strip().upper() != symbol.strip().upper():
                    continue

                ts = _pick(raw, "ts", "timestamp", "time", "event_ts") or ""
                rows.append(
                    PositionRecord(
                        run_id=_pick(raw, "run_id") or "",
                        ts=ts,
                        symbol=row_symbol,
                        qty=_to_decimal(_pick(raw, "qty", "position_qty")),
                        avg_entry_px=_to_decimal(_pick(raw, "avg_entry_px", "avg_entry", "entry_px")),
                        mark_px=_to_decimal(_pick(raw, "mark_px", "price", "px")),
                        unrealized_pnl=_to_decimal(_pick(raw, "unrealized_pnl")),
                        realized_pnl=_to_decimal(_pick(raw, "realized_pnl")),
                        side=_nullable_str(_pick(raw, "side")) or "",
                        raw=dict(raw),
                    )
                )
    except Exception:
        return []

    rows.sort(key=lambda r: _ts_key(r.ts))
    return rows


# ---------------------------------------------------------------------------
# Account loading
# ---------------------------------------------------------------------------

def _load_accounts(path: Optional[str]) -> List[AccountRecord]:
    if not path or not os.path.exists(path):
        return []

    rows: List[AccountRecord] = []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for raw in reader:
                rows.append(
                    AccountRecord(
                        ts=_pick(raw, "ts", "timestamp", "time", "event_ts") or "",
                        cash=_to_decimal(_pick(raw, "cash", "cash_balance")),
                        realized_pnl=_to_decimal(_pick(raw, "realized_pnl")),
                        raw=dict(raw),
                    )
                )
    except Exception:
        return []

    rows.sort(key=lambda r: _ts_key(r.ts))
    return rows


# ---------------------------------------------------------------------------
# Truth builders
# ---------------------------------------------------------------------------

def _build_truth_from_fills(fills: List[FillRecord]) -> Dict[str, Any]:
    """
    Rebuild current position from fills only.

    Assumptions:
    - one open position max
    - buys increase inventory
    - sells reduce inventory
    - avg entry uses weighted average on buys
    - sells do not mutate avg entry except when position goes flat
    """

    qty = ZERO
    avg_entry = ZERO
    realized_pnl = ZERO
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    open_trade_id: Optional[str] = None
    last_fill_id: Optional[str] = None
    last_ts: Optional[str] = None
    last_side: Optional[str] = None

    for fill in fills:
        if fill.qty <= ZERO:
            continue

        if fill.side not in (ORDER_SIDE_BUY, ORDER_SIDE_SELL):
            continue

        last_fill_id = fill.fill_id
        last_ts = fill.ts or last_ts
        last_side = fill.side

        if fill.side == ORDER_SIDE_BUY:
            notional_before = qty * avg_entry
            notional_add = fill.qty * fill.price
            qty = qty + fill.qty
            if qty > ZERO:
                avg_entry = (notional_before + notional_add) / qty
            if fill.order_id:
                entry_order_id = fill.order_id
            if fill.trade_id:
                open_trade_id = fill.trade_id

        elif fill.side == ORDER_SIDE_SELL:
            close_qty = fill.qty if fill.qty <= qty else qty
            realized_pnl += ((fill.price - avg_entry) * close_qty) - fill.fee
            qty = qty - fill.qty
            if fill.order_id:
                exit_order_id = fill.order_id

            if qty <= ZERO:
                qty = ZERO
                avg_entry = ZERO
                open_trade_id = None

    return {
        "has_fills": len(fills) > 0,
        "position_qty": _q8(qty),
        "avg_entry": _q8(avg_entry),
        "realized_pnl": _q8(realized_pnl),
        "open_trade_id": open_trade_id,
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "last_fill_id": last_fill_id,
        "last_ts": last_ts,
        "last_side": last_side,
    }


def _build_truth_from_positions(positions: List[PositionRecord]) -> Dict[str, Any]:
    if not positions:
        return {
            "has_positions": False,
            "position_qty": ZERO,
            "avg_entry": ZERO,
            "realized_pnl": ZERO,
            "last_ts": None,
            "is_fresh_vs_fills": False,
        }

    last = positions[-1]
    qty = _q8(last.qty)
    avg = _q8(last.avg_entry_px if qty > ZERO else ZERO)
    realized = _q8(last.realized_pnl)

    return {
        "has_positions": True,
        "position_qty": qty,
        "avg_entry": avg,
        "realized_pnl": realized,
        "last_ts": last.ts,
        "is_fresh_vs_fills": False,
    }


def _build_truth_from_accounts(accounts: List[AccountRecord]) -> Dict[str, Any]:
    if not accounts:
        return {
            "has_accounts": False,
            "cash": ZERO,
            "realized_pnl": ZERO,
            "last_ts": None,
        }

    last = accounts[-1]
    return {
        "has_accounts": True,
        "cash": _q8(last.cash),
        "realized_pnl": _q8(last.realized_pnl),
        "last_ts": last.ts,
    }


def _build_truth_from_orders(orders: List[OrderRecord]) -> Dict[str, Any]:
    """
    Derive pending order state from orders.

    Rules:
    - FILLED/CANCELED/REJECTED are terminal
    - NEW/ACCEPTED/OPEN/PARTIALLY_FILLED are active
    - latest active BUY -> pending entry
    - latest active SELL -> pending exit
    """

    active_statuses = {
        ORDER_STATUS_NEW,
        ORDER_STATUS_ACCEPTED,
        ORDER_STATUS_OPEN,
        ORDER_STATUS_PARTIALLY_FILLED,
    }
    terminal_statuses = {
        ORDER_STATUS_FILLED,
        ORDER_STATUS_CANCELED,
        ORDER_STATUS_CANCELLED,
        ORDER_STATUS_REJECTED,
    }

    pending_entry_order_id: Optional[str] = None
    pending_exit_order_id: Optional[str] = None
    pending_trade_id: Optional[str] = None
    pending_entry_ts: Optional[str] = None
    pending_exit_ts: Optional[str] = None
    last_order_id: Optional[str] = None
    last_order_ts: Optional[str] = None

    latest_by_order_id: Dict[str, OrderRecord] = {}
    for row in orders:
        latest_by_order_id[row.order_id] = row
        last_order_id = row.order_id
        last_order_ts = row.ts

    for row in reversed(list(latest_by_order_id.values())):
        if row.status in terminal_statuses:
            continue
        if row.status not in active_statuses:
            continue

        if row.side == ORDER_SIDE_BUY and pending_entry_order_id is None:
            pending_entry_order_id = row.order_id
            pending_entry_ts = row.ts
            if row.trade_id:
                pending_trade_id = row.trade_id

        elif row.side == ORDER_SIDE_SELL and pending_exit_order_id is None:
            pending_exit_order_id = row.order_id
            pending_exit_ts = row.ts
            if row.trade_id:
                pending_trade_id = row.trade_id

    return {
        "has_orders": len(orders) > 0,
        "pending_entry_order_id": pending_entry_order_id,
        "pending_exit_order_id": pending_exit_order_id,
        "pending_trade_id": pending_trade_id,
        "pending_entry_ts": pending_entry_ts,
        "pending_exit_ts": pending_exit_ts,
        "last_order_id": last_order_id,
        "last_order_ts": last_order_ts,
    }


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------

def _check_canonical_consistency(
    *,
    fills_truth: Dict[str, Any],
    positions_truth: Dict[str, Any],
    orders_truth: Dict[str, Any],
    snapshot: SnapshotState,
    issues: List[RecoveryIssue],
    notes: List[str],
) -> None:
    fills_ts = fills_truth.get("last_ts")
    positions_ts = positions_truth.get("last_ts")

    if fills_truth["has_fills"] and positions_truth["has_positions"]:
        positions_truth["is_fresh_vs_fills"] = _ts_key(positions_ts or "") >= _ts_key(fills_ts or "")

        if positions_truth["is_fresh_vs_fills"]:
            if not _same_decimal(fills_truth["position_qty"], positions_truth["position_qty"]):
                issues.append(
                    RecoveryIssue(
                        severity="fatal",
                        code="CANONICAL_QTY_MISMATCH",
                        message="fresh positions snapshot disagrees with fills-derived qty",
                        details={
                            "fills_position_qty": str(fills_truth["position_qty"]),
                            "positions_position_qty": str(positions_truth["position_qty"]),
                            "fills_last_ts": fills_ts,
                            "positions_last_ts": positions_ts,
                        },
                    )
                )

            if fills_truth["position_qty"] > ZERO and positions_truth["avg_entry"] > ZERO:
                if not _same_decimal(fills_truth["avg_entry"], positions_truth["avg_entry"]):
                    issues.append(
                        RecoveryIssue(
                            severity="fatal",
                            code="CANONICAL_AVG_ENTRY_MISMATCH",
                            message="fresh positions snapshot disagrees with fills-derived avg_entry",
                            details={
                                "fills_avg_entry": str(fills_truth["avg_entry"]),
                                "positions_avg_entry": str(positions_truth["avg_entry"]),
                                "fills_last_ts": fills_ts,
                                "positions_last_ts": positions_ts,
                            },
                        )
                    )
        else:
            notes.append("positions snapshot older than latest fill; mismatch checks downgraded to advisory")

    # Snapshot disagreements are advisory unless canonical truth is absent.
    if fills_truth["position_qty"] > ZERO and _is_zero(snapshot.position_qty):
        notes.append("snapshot says flat but canonical fills imply open position; canonical truth wins")

    if _is_zero(fills_truth["position_qty"]) and snapshot.position_qty > ZERO:
        notes.append("snapshot says open but canonical fills imply flat; canonical truth wins")

    # Only fatal if there is no canonical terminal-flat closure to explain the stale order row.
    if (
        orders_truth["pending_exit_order_id"]
        and _is_zero(fills_truth["position_qty"])
        and _is_zero(positions_truth["position_qty"])
        and not _canonical_truth_is_terminal_flat(fills_truth=fills_truth, positions_truth=positions_truth)
    ):
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="PENDING_EXIT_WITHOUT_CANONICAL_POSITION",
                message="pending exit order exists but no open position found in canonical execution truth",
                details={"pending_exit_order_id": orders_truth["pending_exit_order_id"]},
            )
        )

    if (
        orders_truth["pending_entry_order_id"]
        and orders_truth["pending_exit_order_id"]
        and not _canonical_truth_is_terminal_flat(fills_truth=fills_truth, positions_truth=positions_truth)
    ):
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="SIMULTANEOUS_PENDING_ENTRY_AND_EXIT",
                message="simultaneous pending entry and pending exit orders detected",
                details={
                    "pending_entry_order_id": orders_truth["pending_entry_order_id"],
                    "pending_exit_order_id": orders_truth["pending_exit_order_id"],
                },
            )
        )


def _normalize_orders_against_canonical_terminal_truth(
    *,
    fills_truth: Dict[str, Any],
    positions_truth: Dict[str, Any],
    orders_truth: Dict[str, Any],
    notes: List[str],
) -> None:
    if not _canonical_truth_is_terminal_flat(fills_truth=fills_truth, positions_truth=positions_truth):
        return

    if orders_truth.get("pending_exit_order_id"):
        notes.append(
            "suppressed stale pending exit order because canonical execution truth already proves terminal flat closure"
        )
        orders_truth["pending_exit_order_id"] = None
        orders_truth["pending_exit_ts"] = None

    if orders_truth.get("pending_entry_order_id") and fills_truth.get("last_side") == ORDER_SIDE_SELL:
        notes.append(
            "suppressed stale pending entry order because latest canonical lifecycle already closed terminally flat"
        )
        orders_truth["pending_entry_order_id"] = None
        orders_truth["pending_entry_ts"] = None


def _check_final_recovery_state(
    *,
    bot_state: str,
    position_qty: Decimal,
    avg_entry: Decimal,
    pending_entry_order_id: Optional[str],
    pending_exit_order_id: Optional[str],
    open_trade_id: Optional[str],
    issues: List[RecoveryIssue],
    notes: List[str],
) -> None:
    if position_qty < ZERO:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="NEGATIVE_POSITION_QTY",
                message="negative position_qty detected",
                details={"position_qty": str(position_qty)},
            )
        )

    if position_qty > ZERO and avg_entry <= ZERO:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="OPEN_POSITION_NON_POSITIVE_AVG_ENTRY",
                message="open position has non-positive avg_entry",
                details={
                    "position_qty": str(position_qty),
                    "avg_entry": str(avg_entry),
                },
            )
        )

    if bot_state == BOT_STATE_OPEN and position_qty <= ZERO:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="STATE_OPEN_WITHOUT_POSITION",
                message="bot_state OPEN but recovered qty <= 0",
                details={"position_qty": str(position_qty)},
            )
        )

    if bot_state == BOT_STATE_EXITING and position_qty <= ZERO:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="STATE_EXITING_WITHOUT_POSITION",
                message="bot_state EXITING but recovered qty <= 0",
                details={"position_qty": str(position_qty)},
            )
        )

    if bot_state == BOT_STATE_ENTERING and pending_entry_order_id is None:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="STATE_ENTERING_WITHOUT_PENDING_ENTRY",
                message="bot_state ENTERING but no pending entry order exists",
                details={},
            )
        )

    if bot_state == BOT_STATE_EXITING and pending_exit_order_id is None:
        issues.append(
            RecoveryIssue(
                severity="fatal",
                code="STATE_EXITING_WITHOUT_PENDING_EXIT",
                message="bot_state EXITING but no pending exit order exists",
                details={},
            )
        )

    if bot_state in (BOT_STATE_OPEN, BOT_STATE_EXITING) and open_trade_id is None:
        notes.append(
            "open state recovered without explicit open_trade_id; degraded but usable if adapter truth remains coherent"
        )


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _pick(raw: Dict[str, Any], *keys: str) -> Optional[str]:
    for k in keys:
        v = raw.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _nullable_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _to_decimal(v: Any) -> Decimal:
    if v is None:
        return ZERO
    if isinstance(v, Decimal):
        return v
    s = str(v).strip()
    if not s:
        return ZERO
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return ZERO


def _q8(v: Decimal) -> Decimal:
    try:
        return v.quantize(Q8, rounding=ROUND_DOWN)
    except Exception:
        return Decimal("0.00000000")


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _normalize_side(side: Optional[str]) -> str:
    s = (side or "").strip().upper()
    if s in ("BUY", "B", "BOT"):
        return ORDER_SIDE_BUY
    if s in ("SELL", "S", "SLD"):
        return ORDER_SIDE_SELL
    return s


def _normalize_order_status(status: Optional[str]) -> str:
    s = (status or "").strip().upper().replace(" ", "_")
    aliases = {
        "ACKED": ORDER_STATUS_ACCEPTED,
        "ACCEPT": ORDER_STATUS_ACCEPTED,
        "WORKING": ORDER_STATUS_OPEN,
        "PARTIAL": ORDER_STATUS_PARTIALLY_FILLED,
        "PART_FILLED": ORDER_STATUS_PARTIALLY_FILLED,
        "CANCELLED": ORDER_STATUS_CANCELLED,
        "CANCELED": ORDER_STATUS_CANCELED,
        "REJECT": ORDER_STATUS_REJECTED,
        "DONE": ORDER_STATUS_FILLED,
    }
    return aliases.get(s, s)


def _ts_key(ts: str) -> Tuple[int, str]:
    if not ts:
        return (0, "")
    try:
        dt = _parse_ts(ts)
        return (int(dt.timestamp()), ts)
    except Exception:
        return (0, ts)


def _parse_ts(ts: str) -> datetime:
    s = ts.strip()
    if s.replace(".", "", 1).isdigit():
        raw = float(s)
        if raw > 1_000_000_000_000:
            raw = raw / 1000.0
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fill_fingerprint(raw: Dict[str, Any]) -> str:
    """
    Deterministic fallback fill identity if upstream does not persist fill_id.
    """
    pieces = [
        _pick(raw, "order_id", "exec_order_id", "parent_order_id") or "",
        _normalize_side(_pick(raw, "side")),
        _pick(raw, "qty", "fill_qty", "last_qty", "quantity") or "",
        _pick(raw, "price", "fill_price", "last_px", "avg_price") or "",
        _pick(raw, "ts", "timestamp", "filled_at", "event_ts") or "",
    ]
    return "|".join(pieces)


def _snapshot_has_meaningful_state(snapshot: SnapshotState) -> bool:
    return any(
        [
            snapshot.position_qty != ZERO,
            snapshot.avg_entry != ZERO,
            snapshot.cash != ZERO,
            snapshot.realized_pnl != ZERO,
            snapshot.open_trade_id is not None,
            snapshot.entry_order_id is not None,
            snapshot.exit_order_id is not None,
            snapshot.last_processed_fill_id is not None,
            snapshot.last_processed_order_id is not None,
            bool(snapshot.applied_fill_ids),
            bool(snapshot.closed_trade_keys),
            bool(snapshot.open_trade_ctx),
        ]
    )


def _trade_id_from_ctx(ctx: Dict[str, Any]) -> Optional[str]:
    if not isinstance(ctx, dict):
        return None
    return _nullable_str(ctx.get("trade_id"))


def _same_decimal(a: Decimal, b: Decimal, eps: Decimal = EPS_QTY) -> bool:
    try:
        return abs(_q8(a) - _q8(b)) <= eps
    except Exception:
        return False


def _is_zero(v: Decimal, eps: Decimal = EPS_QTY) -> bool:
    try:
        return abs(_q8(v)) <= eps
    except Exception:
        return False


def _canonical_truth_is_terminal_flat(
    *,
    fills_truth: Dict[str, Any],
    positions_truth: Dict[str, Any],
) -> bool:
    fills_flat = fills_truth["has_fills"] and _is_zero(fills_truth["position_qty"])
    positions_flat = positions_truth["has_positions"] and _is_zero(positions_truth["position_qty"])

    # Strongest acceptance:
    # - fills say flat, or
    # - positions say flat, or
    # - both agree flat
    #
    # This is intentional: closed/flat canonical truth must win over stale transitional metadata.
    return fills_flat or positions_flat


# ---------------------------------------------------------------------------
# Optional persistence helper
# ---------------------------------------------------------------------------

def write_reconcile_report(path: str, result: RecoveryResult) -> None:
    dir_name = os.path.dirname(path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    payload = result.to_log_dict()
    payload["written_at"] = datetime.now(timezone.utc).isoformat()

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    os.replace(tmp, path)