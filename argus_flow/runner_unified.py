"""Unified Multi-Instrument Paper Trading Runner via IBKR TWS.

Single process, single IB connection, manages ALL instruments simultaneously.
Loads every config from argus_flow/configs/*.json, creates the appropriate
contract (Forex / Future / Crypto-as-CFD), subscribes to market data, and
runs each strategy in lock-step inside one event loop.

Usage:
    python -m argus_flow.runner_unified
    python -m argus_flow.runner_unified --configs argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/mnq_vol_burst_paper_v1.json
    python -m argus_flow.runner_unified --exclude eth_range btc_range
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import uuid
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from argus_flow.ops.broker_truth import (
    atomic_write_json,
    fleet_snapshot_path,
    load_process_snapshots,
    merge_fleet_snapshots,
    process_snapshot_path,
    runner_broker_state_path,
)
from argus_flow.ops.fleet_registry import (
    infer_stage,
    load_existing_registry_entry,
    normalize_stage,
    resolve_log_dir,
    resolve_risk_policy,
    stage_account,
    stage_execution_mode,
)
from argus_flow.ops.fleet_registry import validate_risk_policy_for_execution
from argus_flow.ops.trade_artifact_schema import ensure_trade_csv_schema
from argus_flow.schemas import signal_header, build_signal_row
from argus_flow.sizing import (
    DEFAULT_JPY_PIP_VALUE_PER_UNIT_USD,
    fx_notional_per_unit_usd,
    fx_pip_value_per_unit_usd,
    fx_units_for_risk,
    futures_contracts_for_risk,
)
from ops.process_lock import ProcessLock, ProcessLockError, build_runner_lock_name

load_dotenv()


def _ensure_import_event_loop() -> None:
    """Ensure ib_insync/eventkit sees a default loop during module import."""
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            asyncio.get_event_loop_policy().get_event_loop()
            return
        except RuntimeError:
            pass

    asyncio.set_event_loop(asyncio.new_event_loop())


_ensure_import_event_loop()

try:
    from ib_insync import IB, Forex, Future, MarketOrder, StopOrder, LimitOrder, util
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

# ── Logging ──────────────────────────────────────────────────
_LOG_DIR = Path("argus_flow/logs")
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_log_handlers = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler
    # Rotate at 5MB, keep 3 backups (max 20MB total)
    _file_handler = RotatingFileHandler(
        _LOG_DIR / "runner_unified.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setLevel(logging.INFO)
    _log_handlers.append(_file_handler)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    handlers=_log_handlers,
)
log = logging.getLogger("unified")

# ── Global connection settings from .env ─────────────────────
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
# 2026-05-18: default 7497 (paper). Previous 7496 (live) was the "port
# split-brain" risk Codex flagged — a missing env var would silently
# route argus FX entries to the live account. Real-money runs MUST set
# IBKR_PORT=7496 explicitly + flip real_money_allowlist.global_enabled.
IBKR_PORT = int(os.getenv("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

REPO = Path(__file__).resolve().parents[1]
CONFIGS_DIR = Path("argus_flow/configs")
LOGS_ROOT = Path("argus_flow/logs")
HASHES_FILE = CONFIGS_DIR / "hashes.json"
# DEFAULT_ACCOUNT_EQUITY_USD used to fall back to a hardcoded 10000 at module
# load if broker couldn't be read. That was the silent-fallback pattern we
# removed on 2026-04-23. Now:
#   - Try to read broker at module load.
#   - If successful: snapshot the value (for book-keeping init only).
#   - If broker is cold at load time: leave it at 0.0. Call sites that need
#     a real number (trade sizing) MUST call get_sizing_anchor_usd() per-trade
#     and handle BrokerEquityUnavailableError. The 0.0 sentinel exists only
#     for init-time bookkeeping paths that would otherwise crash on import.
#   - Env var ARGUS_DEFAULT_ACCOUNT_EQUITY_USD is still honored as an
#     explicit override (useful for tests / backtest replay).
_override_env = os.getenv("ARGUS_DEFAULT_ACCOUNT_EQUITY_USD", "")
if _override_env:
    DEFAULT_ACCOUNT_EQUITY_USD = float(_override_env)
else:
    try:
        from helio.fleet_sizing import get_initial_capital_usd as _fleet_anchor
        DEFAULT_ACCOUNT_EQUITY_USD = float(_fleet_anchor())
    except Exception:
        # Broker cold at import. Set to 0.0 sentinel — callers that need a
        # real number should call get_sizing_anchor_usd() live (which will
        # raise BrokerEquityUnavailableError if still unavailable at that
        # point, letting the runner enter READ_ONLY).
        DEFAULT_ACCOUNT_EQUITY_USD = 0.0
GOVERNOR_MODEL_PATH = Path("argus_flow/data/fx_governor.pkl")
AUTO_GROUP_CLIENT_ID_BASE = 1000
AUTO_GROUP_CLIENT_ID_SPAN = 8000

# FX market hours (UTC): Sunday 21:00 → Friday 21:00
# Close positions 15 min before Friday close to avoid weekend gap risk
FX_FRIDAY_CLOSE_MINUTE = 20 * 60 + 45  # 20:45 UTC Friday (15 min before 21:00 close)
FX_FRIDAY_HARD_CLOSE_MINUTE = 20 * 60 + 55  # 20:55 UTC — emergency flatten if still open


def _fx_market_open(now: datetime) -> bool:
    """Return True if FX market is currently open (Sunday 21:00 → Friday 21:00 UTC)."""
    wd = now.weekday()  # 0=Mon .. 6=Sun
    utc_minutes = now.hour * 60 + now.minute
    if wd == 4 and utc_minutes >= 21 * 60:  # Friday after 21:00
        return False
    if wd == 5:  # Saturday
        return False
    if wd == 6 and utc_minutes < 21 * 60:  # Sunday before 21:00
        return False
    return True


# ═════════════════════════════════════════════════════════════
# BarBuffer — rolling 1-minute bar window
# ═════════════════════════════════════════════════════════════
class BarBuffer:
    """Rolling buffer of 1-minute OHLCV bars."""

    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.bars: list[dict] = []

    def add(self, bar: dict) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            self.bars = self.bars[-self.maxlen :]

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.bars) if self.bars else pd.DataFrame()

    def last(self):
        """Return the most recent bar, or None if buffer empty."""
        return self.bars[-1] if self.bars else None

    def __len__(self) -> int:
        return len(self.bars)


# ═════════════════════════════════════════════════════════════
# State — per-instrument position persistence
# ═════════════════════════════════════════════════════════════
class State:
    """Tracks current position and trade lifecycle for one instrument."""

    def __init__(self, state_file: Path):
        self.file = state_file
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.position = "FLAT"  # FLAT | LONG | SHORT
        self.entry_price = 0.0
        self.entry_time: Optional[datetime] = None
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time: Optional[datetime] = None
        self.last_signal_time: Optional[datetime] = None
        self.trade_count = 0
        self.pnl_pips = 0.0    # FX
        self.pnl_points = 0.0  # Futures / bps-based
        self.pnl_usd = 0.0
        self.direction_str = ""  # "long" or "short" for logging
        self.restored_this_session = False  # True if loaded from file
        self.had_zero_stops = False  # True if stop/target were 0 at any point
        self.pyramid_adds = 0        # Number of scale-in adds this trade
        self.avg_entry_price = 0.0   # Volume-weighted average entry price
        self.position_size = 0.0
        self.entry_risk_usd = 0.0
        self.sizing_policy = ""
        self.account_equity_at_entry = 0.0
        self.entry_regime = ""
        # Profit floor state machine (BUILT but not deployed — behind config flag)
        self.hard_stop_price = 0.0        # Original catastrophic stop (never changes)
        self.profit_floor_price = 0.0     # Dynamic floor (0 = inactive)
        self.profit_floor_state = "DISARMED"  # DISARMED | ARMED_10 | ARMED_15
        self.max_favorable_pips = 0.0     # Running MFE for this trade
        # Real execution order tracking
        self.entry_pending: bool = False
        self.exit_pending: bool = False
        self.entry_order_id: str = ""
        self.stop_order_id: str = ""
        self.target_order_id: str = ""
        self.exit_order_id: str = ""
        self.entry_fill_px: float = 0.0
        self.exit_fill_px: float = 0.0
        self.entry_submitted_ts: str = ""
        self.exit_submitted_ts: str = ""

    def save(self) -> None:
        """Atomic state write: write to temp file, flush, then rename."""
        if self.position == "FLAT" and not self.entry_pending and not self.exit_pending:
            self.clear_trade_state()
        data = json.dumps({
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "timeout_time": self.timeout_time.isoformat() if self.timeout_time else None,
            "trade_count": self.trade_count,
            "pnl_pips": self.pnl_pips,
            "pnl_points": self.pnl_points,
            "pnl_usd": self.pnl_usd,
            "pyramid_adds": self.pyramid_adds,
            "avg_entry_price": self.avg_entry_price,
            "position_size": self.position_size,
            "entry_risk_usd": self.entry_risk_usd,
            "sizing_policy": self.sizing_policy,
            "account_equity_at_entry": self.account_equity_at_entry,
            "entry_regime": self.entry_regime,
            "entry_pending": self.entry_pending,
            "exit_pending": self.exit_pending,
            "entry_order_id": self.entry_order_id,
            "stop_order_id": self.stop_order_id,
            "target_order_id": self.target_order_id,
            "exit_order_id": self.exit_order_id,
            "entry_fill_px": self.entry_fill_px,
            "exit_fill_px": self.exit_fill_px,
            "entry_submitted_ts": self.entry_submitted_ts,
            "exit_submitted_ts": self.exit_submitted_ts,
        }, indent=2)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(data)
        for _ in range(3):
            try:
                tmp.replace(self.file)  # atomic on same filesystem
                return
            except PermissionError:
                time.sleep(0.05)

        self.file.write_text(data)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def clear_trade_state(self) -> None:
        """Reset trade-specific fields while preserving cumulative PnL and counters."""
        self.position = "FLAT"
        self.entry_price = 0.0
        self.entry_time = None
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time = None
        self.direction_str = ""
        self.hard_stop_price = 0.0
        self.profit_floor_price = 0.0
        self.profit_floor_state = "DISARMED"
        self.max_favorable_pips = 0.0
        self.pyramid_adds = 0
        self.avg_entry_price = 0.0
        self.position_size = 0.0
        self.entry_risk_usd = 0.0
        self.sizing_policy = ""
        self.account_equity_at_entry = 0.0
        self.entry_regime = ""
        self.entry_pending = False
        self.exit_pending = False
        self.entry_order_id = ""
        self.stop_order_id = ""
        self.target_order_id = ""
        self.exit_order_id = ""
        self.entry_fill_px = 0.0
        self.exit_fill_px = 0.0
        self.entry_submitted_ts = ""
        self.exit_submitted_ts = ""

    def load(self, sym_label: str) -> None:
        if not self.file.exists():
            return
        try:
            d = json.loads(self.file.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning(f"[{sym_label}] Corrupt state file — starting FLAT")
            return
        self.position = d.get("position", "FLAT")
        self.entry_price = d.get("entry_price", 0.0)
        self.stop_price = d.get("stop_price", 0.0)
        self.target_price = d.get("target_price", 0.0)
        self.trade_count = d.get("trade_count", 0)
        self.pnl_pips = d.get("pnl_pips", 0.0)
        self.pnl_points = d.get("pnl_points", 0.0)
        self.pnl_usd = d.get("pnl_usd", 0.0)
        self.pyramid_adds = d.get("pyramid_adds", 0)
        self.avg_entry_price = d.get("avg_entry_price", 0.0)
        self.position_size = d.get("position_size", 0.0)
        self.entry_risk_usd = d.get("entry_risk_usd", 0.0)
        self.sizing_policy = d.get("sizing_policy", "")
        self.account_equity_at_entry = d.get("account_equity_at_entry", 0.0)
        self.entry_regime = d.get("entry_regime", "")
        self.entry_pending = d.get("entry_pending", False)
        self.exit_pending = d.get("exit_pending", False)
        self.entry_order_id = d.get("entry_order_id", "")
        self.stop_order_id = d.get("stop_order_id", "")
        self.target_order_id = d.get("target_order_id", "")
        self.exit_order_id = d.get("exit_order_id", "")
        self.entry_fill_px = d.get("entry_fill_px", 0.0)
        self.exit_fill_px = d.get("exit_fill_px", 0.0)
        self.entry_submitted_ts = d.get("entry_submitted_ts", "")
        self.exit_submitted_ts = d.get("exit_submitted_ts", "")
        if d.get("entry_time"):
            try:
                self.entry_time = datetime.fromisoformat(d["entry_time"])
            except (ValueError, TypeError):
                pass
        if d.get("timeout_time"):
            try:
                self.timeout_time = datetime.fromisoformat(d["timeout_time"])
            except (ValueError, TypeError):
                pass
        # Safety: if restored in-position but stops are zero, force FLAT locally.
        # WARNING: This is a LOCAL-ONLY override. If broker has a real position,
        # this creates a phantom mismatch that reconciliation MUST catch.
        # The runner will log this as had_zero_stops=True for trade validity.
        if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
            log.warning(
                f"[{sym_label}] Restored {self.position} but stop/target=0 — forcing FLAT (LOCAL ONLY). "
                f"If broker has a real position, reconciliation will flag PHANTOM mismatch."
            )
            self.had_zero_stops = True
            self.clear_trade_state()
        # Safety: if restored in-position but timeout missing, mark invalid
        if self.position != "FLAT" and self.timeout_time is None:
            log.warning(f"[{sym_label}] Restored {self.position} but timeout_time missing — trade validity compromised")
            self.had_zero_stops = True  # triggers invalid flag on close
        # Safety: validate internal consistency of in-position state
        if self.position != "FLAT":
            if self.entry_price <= 0:
                log.error(f"[{sym_label}] CORRUPT STATE: {self.position} but entry_price={self.entry_price} — forcing FLAT")
                self.had_zero_stops = True
                self.clear_trade_state()
            elif self.position_size <= 0:
                log.error(f"[{sym_label}] CORRUPT STATE: {self.position} but position_size={self.position_size} — forcing FLAT")
                self.had_zero_stops = True
                self.clear_trade_state()
        if self.position == "FLAT":
            self.clear_trade_state()
        if self.position != "FLAT":
            self.restored_this_session = True
            log.info(
                f"[{sym_label}] Restored {self.position} entry={self.entry_price} "
                f"stop={self.stop_price} target={self.target_price} "
                f"trades={self.trade_count}"
            )


class AccountEquityTracker:
    """Caches account equity so sizing/risk checks don't spam the API."""

    STALE_THRESHOLD_S = 600  # warn if equity hasn't refreshed in 10 minutes
    _consecutive_failures: int = 0

    def __init__(self, ib: IB, account: str | None = None, refresh_sec: int = 60):
        self.ib = ib
        self.account = account
        self.refresh_sec = refresh_sec
        self._equity_usd = DEFAULT_ACCOUNT_EQUITY_USD
        self._last_refresh = 0.0
        self._last_successful_refresh = 0.0
        self._consecutive_failures = 0

    @property
    def equity_usd(self) -> float:
        return self._equity_usd

    @property
    def is_stale(self) -> bool:
        if self._last_successful_refresh == 0.0:
            return False  # never refreshed yet
        return (time.time() - self._last_successful_refresh) > self.STALE_THRESHOLD_S

    def refresh(self, force: bool = False) -> float:
        now = time.time()
        if not force and now - self._last_refresh < self.refresh_sec:
            return self._equity_usd

        try:
            rows = self.ib.accountSummary(account=self.account or "")
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                log.warning(
                    f"ACCOUNT_EQUITY refresh failed {self._consecutive_failures}x: {exc} "
                    f"(last success {now - self._last_successful_refresh:.0f}s ago)"
                )
            self._last_refresh = now
            return self._equity_usd

        best = None
        for row in rows:
            if getattr(row, "tag", "") != "NetLiquidation":
                continue
            currency = getattr(row, "currency", "") or ""
            if currency not in ("USD", "BASE", ""):
                continue
            try:
                best = float(row.value)
                break
            except (TypeError, ValueError):
                continue

        if best is None:
            for row in rows:
                if getattr(row, "tag", "") != "NetLiquidation":
                    continue
                try:
                    best = float(row.value)
                    break
                except (TypeError, ValueError):
                    continue

        if best and best > 0:
            self._equity_usd = best
            self._last_successful_refresh = now
            self._consecutive_failures = 0
        self._last_refresh = now
        return self._equity_usd


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_broker_positions(ib: IB) -> tuple[dict, bool, str | None]:
    """Fetch normalized broker positions from IBKR."""
    try:
        positions = {}
        for p in ib.positions():
            key = _normalize_ib_key(p.contract)
            qty = _safe_float(getattr(p, "position", 0))
            positions[key] = {
                "qty": qty,
                "direction": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT"),
                "avg_cost": _safe_float(getattr(p, "avgCost", 0)),
                "account": getattr(p, "account", ""),
            }
        return positions, True, None
    except Exception as exc:
        return {}, False, str(exc)


def _fetch_account_snapshot(
    ib: IB,
    account: str | None,
    fallback_equity_usd: float,
) -> dict:
    """Fetch a compact USD-denominated account summary snapshot."""
    snapshot = {
        "account_id": account,
        "net_liquidation_usd": fallback_equity_usd,
        "buying_power_usd": 0.0,
        "available_funds_usd": 0.0,
        "total_cash_usd": 0.0,
        "excess_liquidity_usd": 0.0,
        "maint_margin_req_usd": 0.0,
        "init_margin_req_usd": 0.0,
    }
    try:
        rows = ib.accountSummary(account=account or "")
    except Exception as exc:
        snapshot["error"] = str(exc)
        return snapshot

    tags: dict[str, float] = {}
    for row in rows:
        currency = (getattr(row, "currency", "") or "").upper()
        if currency not in ("", "USD", "BASE"):
            continue
        tag = getattr(row, "tag", "")
        if not tag or tag in tags:
            continue
        try:
            tags[tag] = float(row.value)
        except (TypeError, ValueError):
            continue

    snapshot.update({
        "net_liquidation_usd": tags.get("NetLiquidation", fallback_equity_usd),
        "buying_power_usd": tags.get("BuyingPower", 0.0),
        "available_funds_usd": tags.get("AvailableFunds", 0.0),
        "total_cash_usd": tags.get("TotalCashValue", 0.0),
        "excess_liquidity_usd": tags.get("ExcessLiquidity", 0.0),
        "maint_margin_req_usd": tags.get("MaintMarginReq", tags.get("FullMaintMarginReq", 0.0)),
        "init_margin_req_usd": tags.get("InitMarginReq", tags.get("FullInitMarginReq", 0.0)),
    })
    return snapshot


def _fetch_open_orders(ib: IB) -> tuple[list[dict], str | None]:
    """Fetch normalized open orders from IBKR."""
    orders: list[dict] = []
    try:
        for trade in ib.openTrades():
            contract = getattr(trade, "contract", None)
            order = getattr(trade, "order", None)
            status = getattr(trade, "orderStatus", None)
            if contract is None or order is None:
                continue
            orders.append({
                "symbol_key": _normalize_ib_key(contract),
                "action": getattr(order, "action", "") or "",
                "order_type": getattr(order, "orderType", "") or "",
                "quantity": _safe_float(getattr(order, "totalQuantity", 0)),
                "status": getattr(status, "status", "") or "",
                "filled": _safe_float(getattr(status, "filled", 0)),
                "remaining": _safe_float(getattr(status, "remaining", 0)),
                "limit_price": _safe_float(getattr(order, "lmtPrice", 0)),
                "stop_price": _safe_float(getattr(order, "auxPrice", 0)),
                "order_id": int(_safe_float(getattr(order, "orderId", 0))),
                "perm_id": int(_safe_float(getattr(order, "permId", 0))),
                "account": getattr(order, "account", "") or "",
            })
    except Exception as exc:
        return [], str(exc)
    return orders, None


def _write_broker_truth_artifacts(
    ib: IB,
    account: str | None,
    instruments: list,
    runtime_mode: str,
    equity_tracker: AccountEquityTracker | None,
    recon_results: dict | None = None,
    broker_positions: dict | None = None,
    broker_ok: bool | None = None,
    position_error: str | None = None,
    account_snapshot: dict | None = None,
    open_orders: list[dict] | None = None,
    open_orders_error: str | None = None,
) -> dict:
    """Persist fleet and per-runner broker/account truth artifacts."""
    if broker_positions is None or broker_ok is None:
        broker_positions, broker_ok, position_error = _fetch_broker_positions(ib)
    if account_snapshot is None:
        fallback_equity = equity_tracker.equity_usd if equity_tracker else DEFAULT_ACCOUNT_EQUITY_USD
        account_snapshot = _fetch_account_snapshot(ib, account, fallback_equity)
    if open_orders is None and open_orders_error is None:
        open_orders, open_orders_error = _fetch_open_orders(ib)

    now_iso = datetime.now(timezone.utc).isoformat()
    broker_connected = bool(ib.isConnected() and broker_ok)

    if recon_results is None:
        recon_results = {}
        for inst in instruments:
            recon_results[inst.symbol] = {
                "result": getattr(inst, "_reconciliation", ReconcileResult.BROKER_UNAVAILABLE),
                "detail": getattr(inst, "_reconciliation_detail", ""),
                "local_position": inst.state.position,
                "broker_position": getattr(inst, "_broker_position", "FLAT"),
                "broker_qty": getattr(inst, "_broker_qty", 0.0),
                "ib_key": _runner_to_ib_key(inst),
            }

    fleet_snapshot = {
        "timestamp": now_iso,
        "source": "runner_unified",
        "source_pid": os.getpid(),
        "runtime_mode": runtime_mode,
        "broker_connected": broker_connected,
        "account": account_snapshot,
        "positions": broker_positions,
        "open_orders": open_orders or [],
        "reconciliation": recon_results,
    }
    if position_error:
        fleet_snapshot["position_error"] = position_error
    if open_orders_error:
        fleet_snapshot["open_orders_error"] = open_orders_error

    atomic_write_json(process_snapshot_path(os.getpid()), fleet_snapshot)
    merged_snapshot = merge_fleet_snapshots(load_process_snapshots(max_age_s=600))
    if merged_snapshot:
        atomic_write_json(fleet_snapshot_path(), merged_snapshot)

    for inst in instruments:
        ib_key = _runner_to_ib_key(inst)
        broker_info = broker_positions.get(ib_key, {"qty": 0.0, "direction": "FLAT", "avg_cost": 0.0})
        inst_orders = [o for o in (open_orders or []) if o.get("symbol_key") == ib_key]
        local_open_risk = inst.current_open_risk_usd()
        local_unrealized_pnl = inst.current_unrealized_pnl_usd()
        runner_snapshot = {
            "timestamp": now_iso,
            "source": "runner_unified",
            "source_pid": os.getpid(),
            "runtime_mode": runtime_mode,
            "broker_connected": broker_connected,
            "account": account_snapshot,
            "runner": {
                "symbol": inst.symbol,
                "deployment_stage": inst.deployment_stage,
                "ib_key": ib_key,
                "local_position": inst.state.position,
                "position_size": inst.state.position_size,
                "entry_price": inst.state.entry_price if inst.state.position != "FLAT" else None,
                "stop_price": inst.state.stop_price if inst.state.position != "FLAT" else None,
                "target_price": inst.state.target_price if inst.state.position != "FLAT" else None,
                "entry_risk_usd": inst.state.entry_risk_usd,
                "open_risk_usd": local_open_risk,
                "unrealized_pnl_usd": local_unrealized_pnl,
                "trade_count": inst.state.trade_count,
                "quarantined": getattr(inst, "_quarantined", False),
                "entries_blocked": getattr(inst, "_entries_blocked", False),
                "entry_block_reason": getattr(inst, "_entries_block_reason", ""),
            },
            "broker": {
                "position": broker_info.get("direction", "FLAT"),
                "qty": broker_info.get("qty", 0.0),
                "avg_cost": broker_info.get("avg_cost", 0.0),
                "open_orders": inst_orders,
            },
            "reconciliation": {
                "result": getattr(inst, "_reconciliation", ReconcileResult.BROKER_UNAVAILABLE),
                "detail": getattr(inst, "_reconciliation_detail", ""),
                "requires_manual_review": getattr(inst, "_reconciliation", "") in (
                    ReconcileResult.LOCAL_FLAT_BROKER_OPEN,
                    ReconcileResult.UNRESOLVED,
                    "RECON_DRIFT",
                ),
            },
        }
        atomic_write_json(runner_broker_state_path(inst.log_dir), runner_snapshot)

    return fleet_snapshot


def _write_heartbeat_files(
    instruments: list,
    runtime_mode: str,
    ib: IB,
    equity_tracker: AccountEquityTracker,
    now: datetime,
) -> None:
    """Persist per-runner heartbeat files used by oversight surfaces."""
    for inst in instruments:
        hb_file = inst.log_dir / "heartbeat.json"
        try:
            # Keep the local state artifact present whenever the runner is alive
            # enough to emit a heartbeat so dashboard/oversight surfaces never
            # fall back to inferred runtime state.
            inst.state.save()
            # Persist recent bars for dashboard candlestick chart
            recent_bars = [
                {
                    "t": b.get("ts", ""),
                    "o": b["open"],
                    "h": b["high"],
                    "l": b["low"],
                    "c": b["close"],
                    "v": float(b.get("volume", 0) or 0.0),
                    "n": int(b.get("ticks", 0) or 0),
                }
                for b in inst.buf.bars[-120:]  # last 2 hours of 1-min bars
            ] if inst.buf.bars else []

            atomic_write_json(hb_file, {
                "ts": now.isoformat(),
                "pid": os.getpid(),
                "session_id": getattr(inst, '_session_id', ''),
                "instrument": inst.symbol,
                "deployment_stage": inst.deployment_stage,
                "runtime_mode": runtime_mode,
                "position": inst.state.position,
                "broker_position": getattr(inst, '_broker_position', 'FLAT'),
                "broker_qty": getattr(inst, '_broker_qty', 0.0),
                "entries_blocked": getattr(inst, '_entries_blocked', False),
                "entry_block_reason": getattr(inst, '_entries_block_reason', ""),
                "quarantined": getattr(inst, '_quarantined', False),
                "consecutive_errors": getattr(inst, '_consecutive_errors', 0),
                "reconciliation": getattr(inst, '_reconciliation', ''),
                "last_bar_ts": str(inst.current_bar_minute) if inst.current_bar_minute else None,
                "broker_connected": ib.isConnected(),
                "account_equity_usd": equity_tracker.equity_usd,
                "open_risk_usd": inst.current_open_risk_usd(),
                "unrealized_pnl_usd": inst.current_unrealized_pnl_usd(),
                "recent_bars": recent_bars,
                "entry_price": inst.state.entry_price if inst.state.position != "FLAT" else None,
                "stop_price": inst.state.stop_price if inst.state.position != "FLAT" else None,
                "target_price": inst.state.target_price if inst.state.position != "FLAT" else None,
            })
        except Exception:
            pass


def _sync_entry_block_state(inst) -> None:
    """Combine control-plane and reconciliation blocks into one runtime flag."""
    control_blocked = bool(getattr(inst, "_control_blocked", False))
    recon_blocked = bool(getattr(inst, "_recon_blocked", False))
    inst._entries_blocked = control_blocked or recon_blocked
    if control_blocked:
        inst._entries_block_reason = str(getattr(inst, "_control_block_reason", "") or "CONTROL_BLOCK")
    elif recon_blocked:
        inst._entries_block_reason = str(getattr(inst, "_recon_block_reason", "") or "RECON_BLOCK")
    else:
        inst._entries_block_reason = ""


def _load_governor():
    """Load the FX governor model if available. Returns (model, metadata) or (None, None)."""
    if not GOVERNOR_MODEL_PATH.exists():
        return None, None
    try:
        import pickle
        with open(GOVERNOR_MODEL_PATH, "rb") as f:
            artifact = pickle.load(f)
        log.info(f"Governor model loaded: {artifact.get('training_stats', {}).get('n_trades', '?')} trades, "
                 f"{len(artifact.get('feature_names', []))} features")
        return artifact, artifact.get("training_stats", {})
    except Exception as e:
        log.warning(f"Governor model load failed: {e}")
        return None, None


def _governor_score(artifact: dict, features: dict, direction: str, symbol: str) -> float | None:
    """Score a signal using the governor model. Returns P(win) or None on error."""
    try:
        import math as _math
        import numpy as _np
        model = artifact["model"]
        imputer = artifact["imputer"]
        encoders = artifact["encoders"]
        numeric_features = artifact["numeric_features"]
        categorical_features = artifact["categorical_features"]

        hour = features.get("hour", 12)
        hour_f = float(hour) if hour else 12.0

        # Build feature vector matching training order
        row_numeric = []
        for f in numeric_features:
            if f == "hour_sin":
                row_numeric.append(_math.sin(2 * _math.pi * hour_f / 24))
            elif f == "hour_cos":
                row_numeric.append(_math.cos(2 * _math.pi * hour_f / 24))
            else:
                val = features.get(f)
                row_numeric.append(float(val) if val is not None and val != "" else _np.nan)

        row_cat = []
        for f in categorical_features:
            if f == "direction":
                val = direction or "unknown"
            elif f == "symbol":
                val = symbol.upper()
            else:
                val = str(features.get(f, "unknown") or "unknown")
            le = encoders.get(f)
            if le is not None and val in le.classes_:
                row_cat.append(le.transform([val])[0])
            else:
                row_cat.append(0)  # unknown category -> 0

        X = _np.array([row_numeric + row_cat], dtype=float)
        X = imputer.transform(X)
        proba = model.predict_proba(X)[0, 1]  # P(win)
        return float(proba)
    except Exception:
        return None


def _config_short_hash(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_config_for_client_id(path_str: str | Path) -> tuple[Path, dict] | None:
    path = Path(path_str)
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def resolve_client_id(
    config_paths: Optional[list[str]] = None,
    explicit_client_id: Optional[int] = None,
    default_client_id: Optional[int] = None,
) -> tuple[int, str]:
    """Resolve a stable IBKR client ID for this runner instance."""
    fallback = int(default_client_id or IBKR_CLIENT_ID)
    if explicit_client_id is not None:
        return int(explicit_client_id), "cli"

    loaded = []
    for raw_path in config_paths or []:
        cfg_pair = _load_config_for_client_id(raw_path)
        if cfg_pair is not None:
            loaded.append(cfg_pair)

    if len(loaded) == 1:
        cfg_client_id = loaded[0][1].get("ibkr_client_id")
        if isinstance(cfg_client_id, int) and cfg_client_id > 0:
            return int(cfg_client_id), f"config:{loaded[0][0].name}"
        return fallback, "env"

    if len(loaded) > 1:
        signature = "|".join(sorted(path.name.lower() for path, _ in loaded))
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()
        derived = AUTO_GROUP_CLIENT_ID_BASE + (
            int(digest[:8], 16) % AUTO_GROUP_CLIENT_ID_SPAN
        )
        return int(derived), "auto-group"

    return fallback, "env"


def _validate_config_registry(cfg_files: list[Path]) -> bool:
    if not HASHES_FILE.exists():
        log.error(f"FATAL: {HASHES_FILE} missing. Config hash freeze cannot be enforced.")
        return False

    try:
        expected = json.loads(HASHES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error(f"FATAL: could not read config hash registry: {exc}")
        return False

    missing = []
    mismatched = []
    for cfg_path in cfg_files:
        if cfg_path.name == HASHES_FILE.name:
            continue
        expected_hash = expected.get(cfg_path.name)
        actual_hash = _config_short_hash(cfg_path)
        if expected_hash is None:
            missing.append(cfg_path.name)
        elif expected_hash != actual_hash:
            mismatched.append((cfg_path.name, expected_hash, actual_hash))

    for name in missing:
        log.error(f"FATAL: config '{name}' missing from hashes.json")
    for name, exp, got in mismatched:
        log.error(f"FATAL: config '{name}' hash mismatch expected={exp} actual={got}")

    return not missing and not mismatched


# ═════════════════════════════════════════════════════════════
# Feature computation
# ═════════════════════════════════════════════════════════════
def compute_features_fx(buf: BarBuffer) -> Optional[dict]:
    """Compute range/accel features for FX strategies."""
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))
    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    last_close = float(pre["close"].iloc[-1])
    range_pct = (pre["high"].max() - pre["low"].min()) / last_close if last_close != 0 else 0.0
    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    current_px = float(pre["close"].iloc[-1])
    dist_from_low = (current_px - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    # Use last bar timestamp for hour, not wall-clock (avoids drift on stall/reconnect)
    try:
        bar_hour = int(str(df["ts"].iloc[-1])[11:13])
    except (ValueError, IndexError):
        bar_hour = datetime.now(timezone.utc).hour

    regime = classify_regime(buf)

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "dist_from_low": dist_from_low,
        "hour": bar_hour,
        "price": current_px,
        "regime": regime["regime"],
        "trend_strength": regime["trend_strength"],
        "efficiency_ratio": regime["efficiency_ratio"],
        "regime_confidence": regime["regime_confidence"],
    }


def compute_features_futures(buf: BarBuffer) -> Optional[dict]:
    """Compute range/accel + vol_burst features for futures strategies."""
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))
    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    last_close = float(pre["close"].iloc[-1])
    range_pct = (pre["high"].max() - pre["low"].min()) / last_close if last_close != 0 else 0.0
    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

    # Volume burst z-score (futures-specific)
    vol_burst_z = 0.0
    if "volume" in df.columns and df["volume"].sum() > 0:
        vol_5m = df["volume"].iloc[-5:].sum()
        vol_hist = df["volume"].rolling(5).sum().iloc[-ctx_win:-5]
        if len(vol_hist) > 10 and vol_hist.mean() > 0:
            vol_burst_z = (vol_5m - vol_hist.mean()) / vol_hist.mean()

    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    current_px = float(pre["close"].iloc[-1])
    dist_from_low = (current_px - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    # Use last bar timestamp for hour, not wall-clock
    try:
        bar_hour = int(str(df["ts"].iloc[-1])[11:13])
    except (ValueError, IndexError):
        bar_hour = datetime.now(timezone.utc).hour

    regime = classify_regime(buf)

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "vol_burst_z": vol_burst_z,
        "dist_from_low": dist_from_low,
        "hour": bar_hour,
        "price": current_px,
        "regime": regime["regime"],
        "trend_strength": regime["trend_strength"],
        "efficiency_ratio": regime["efficiency_ratio"],
        "regime_confidence": regime["regime_confidence"],
    }


# ═════════════════════════════════════════════════════════════
# Regime classification
# ═════════════════════════════════════════════════════════════
def classify_regime(buf: BarBuffer) -> dict:
    """Classify current market regime from bar buffer.

    Returns dict with:
        regime: TRENDING | RANGING | CHOPPY
        trend_strength: float (-1 to +1, negative = downtrend)
        volatility_rank: float (0 to 1, percentile of current vol vs history)
        regime_confidence: float (0 to 1, how clearly the regime is defined)
        regime_suitable_for: list of strategy types that fit this regime
    """
    df = buf.to_df()
    if len(df) < 120:
        return {"regime": "UNKNOWN", "trend_strength": 0, "volatility_rank": 0.5,
                "efficiency_ratio": 0, "regime_confidence": 0, "regime_suitable_for": ["range_accel"]}

    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    # ── Trend detection via linear regression slope ──
    # Use last 60 bars (~1 hour) for trend
    recent = closes.iloc[-60:]
    x = np.arange(len(recent))
    slope = np.polyfit(x, recent.values, 1)[0]
    # Normalize slope by ATR to make it comparable across assets
    atr = (highs.iloc[-60:] - lows.iloc[-60:]).mean()
    trend_strength = (slope * 60) / atr if atr > 0 else 0  # slope over 60 bars, normalized
    trend_strength = max(-1.0, min(1.0, trend_strength))   # clamp to [-1, 1]

    # ── Volatility rank (current vs historical) ──
    bar_ranges = highs - lows
    recent_vol = bar_ranges.iloc[-30:].mean()
    hist_vol = bar_ranges.mean()
    vol_ratio = recent_vol / hist_vol if hist_vol > 0 else 1.0
    volatility_rank = max(0.0, min(1.0, vol_ratio / 2.0))  # 0-1 scale, 0.5 = normal

    # ── Choppiness index (Kaufman efficiency ratio) ──
    # direction / total_path — high = trending, low = choppy
    window = min(60, len(closes) - 1)
    direction = abs(float(closes.iloc[-1]) - float(closes.iloc[-window - 1]))
    total_path = closes.diff().abs().iloc[-window:].sum()
    efficiency = direction / total_path if total_path > 0 else 0

    # ── Classify ──
    abs_trend = abs(trend_strength)
    if efficiency > 0.35 and abs_trend > 0.3:
        regime = "TRENDING"
        confidence = min(1.0, efficiency * 1.5)
        suitable = ["momentum", "breakout", "trend_follow"]
    elif efficiency < 0.15 or volatility_rank > 0.7:
        regime = "CHOPPY"
        confidence = min(1.0, (1 - efficiency) * 0.8)
        suitable = ["mean_revert", "scalp"]  # range_accel is risky here
    else:
        regime = "RANGING"
        confidence = min(1.0, (0.35 - efficiency) / 0.2) if efficiency < 0.35 else 0.5
        suitable = ["range_accel", "mean_revert"]

    return {
        "regime": regime,
        "trend_strength": round(trend_strength, 4),
        "volatility_rank": round(volatility_rank, 4),
        "efficiency_ratio": round(efficiency, 4),
        "regime_confidence": round(confidence, 3),
        "regime_suitable_for": suitable,
    }


# ═════════════════════════════════════════════════════════════
# Trigger checks
# ═════════════════════════════════════════════════════════════
def _in_session(hour: int, start: int, end: int) -> bool:
    """Check if hour is within session window, supporting wrap-around (e.g. 22-08)."""
    if start <= end:
        return start <= hour <= end
    else:
        # Wrap-around: e.g. 22-08 means 22,23,0,1,...,8
        return hour >= start or hour <= end


def check_trigger_fx(features: dict, cfg: dict) -> Optional[str]:
    """FX trigger: range_pct + range_accel + vol_z + session + blocked hours.

    2026-05-21: paper_stress_multiplier wired here for range_accel strategies
    (GBPUSD). Mirrors the MTF-side wiring in InstrumentRunner.__init__ so the
    full FX trio (USDJPY mtf, CADJPY mtf, GBPUSD range_accel) responds to the
    same activity-multiplication knob. The multiplier only scales the barrier
    threshold (range_pct_min) — the dist thresholds route direction, not
    whether to enter, so they're left alone."""
    trigger = cfg.get("trigger", {})
    stress_mult = cfg.get("paper_stress_multiplier", 1.0)
    base_range_pct_min = trigger.get("range_pct_min", 0.0012)
    if stress_mult != 1.0:
        from helio.paper_stress import apply as _stress_apply
        symbol_for_log = cfg.get("symbol") or "fx"
        eff_range_pct_min = _stress_apply(
            base_range_pct_min, stress_mult,
            strategy=f"argus_{symbol_for_log}", knob="range_pct_min",
        )
    else:
        eff_range_pct_min = base_range_pct_min
    if features["range_pct"] < eff_range_pct_min:
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    vol_z_min = trigger.get("vol_z_min")
    if vol_z_min is not None and features.get("vol_z", 0) <= vol_z_min:
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 0), trigger.get("session_end_utc", 23)):
        return None
    # Blocked hours: e.g. [[15, 19]] blocks 15:00-18:59 UTC (NY dead zone)
    for block in trigger.get("blocked_hours_utc", []):
        if len(block) >= 2 and block[0] <= h < block[1]:
            return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    # MTF-strategy configs (e.g. CADJPY, USDJPY) use string direction like "both" / "long_only".
    # Those configs run through the MTF engine path in live, but the walkforward harness calls
    # this function directly. Fall through to default thresholds when direction isn't a dict.
    if not isinstance(direction_cfg, dict):
        direction_cfg = {}
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade (was: hidden long bias)


def check_trigger_futures(features: dict, cfg: dict) -> Optional[str]:
    """Futures trigger — dispatches by strategy field in config."""
    strategy = cfg.get("strategy", "vol_burst")
    if strategy.startswith("range_accel"):
        return _check_trigger_futures_range(features, cfg)
    return _check_trigger_futures_vol_burst(features, cfg)


def _check_trigger_futures_vol_burst(features: dict, cfg: dict) -> Optional[str]:
    """Original vol_burst: volume spike + range accel + session."""
    trigger = cfg.get("trigger", {})
    if features.get("vol_burst_z", 0) <= trigger.get("vol_burst_z_min", 1.0):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade


def _check_trigger_futures_range(features: dict, cfg: dict) -> Optional[str]:
    """Range-accel for futures: same logic as FX but uses futures features."""
    trigger = cfg.get("trigger", {})
    if features["range_pct"] < trigger.get("range_pct_min", 0.0008):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    vol_z_min = trigger.get("vol_z_min")
    if vol_z_min is not None and features.get("vol_z", 0) <= vol_z_min:
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade


# ═════════════════════════════════════════════════════════════
# InstrumentRunner — one per config
# ═════════════════════════════════════════════════════════════
class InstrumentRunner:
    """Manages one instrument's strategy within the unified process."""

    def __init__(self, config: dict, contract, ticker, log_dir: Path, config_path: str):
        self.cfg = config
        self.contract = contract
        self.ticker = ticker
        self.config_path = config_path

        self.symbol: str = config["symbol"]
        self.instrument_type: str = config.get("instrument_type", "forex")
        self.strategy: str = config.get("strategy", "unknown")
        self.label = f"{self.symbol}:{self.strategy}"
        self.deployment = config.get("deployment", {}) if isinstance(config.get("deployment", {}), dict) else {}
        self.deployment_stage = infer_stage(config, Path(config_path))
        self.risk_policy = resolve_risk_policy(config, Path(config_path), stage=self.deployment_stage)
        registry_entry = load_existing_registry_entry(Path(config_path).name)
        if isinstance(registry_entry, dict):
            registry_stage = normalize_stage(registry_entry.get("current_stage"))
            if registry_stage:
                self.deployment_stage = registry_stage
            registry_risk = registry_entry.get("risk_policy", {}) if isinstance(registry_entry.get("risk_policy", {}), dict) else {}
            merged_risk = dict(self.risk_policy)
            merged_risk.update({k: v for k, v in registry_risk.items() if v is not None})
            merged_risk["stage"] = self.deployment_stage
            self.risk_policy = merged_risk
        # Validate model equity for paper/watcher stages
        equity_error = validate_risk_policy_for_execution(config, Path(config_path), stage=self.deployment_stage)
        if equity_error:
            raise ValueError(f"[{self.label}] {equity_error}")

        self.stage_account = stage_account(self.deployment_stage)
        self.execution_mode = stage_execution_mode(self.deployment_stage)
        self.trade_enabled = self.execution_mode in ("paper", "real")
        self._entries_blocked = False
        self._entries_block_reason = ""
        self._control_blocked = False
        self._control_block_reason = ""
        self._recon_blocked = False
        self._recon_block_reason = ""

        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.buf = BarBuffer(maxlen=300)
        self.state = State(log_dir / "state.json")
        self.signal_log = log_dir / "signals.csv"
        self.trade_log = log_dir / "trades.csv"

        self.current_bar_minute: Optional[datetime] = None
        self.current_bar: dict = {}

        # Risk params
        risk = config.get("risk", {})
        self.timeout_min = risk.get("timeout_minutes", 60)
        self.min_gap = risk.get("min_signal_gap_minutes", 15)

        # FX-specific
        self.stop_pips = risk.get("stop_pips", 0)
        self.target_pips = risk.get("target_pips", 0)
        self.lot_size = risk.get("lot_size", 20000)
        self.min_lot_size = int(risk.get("min_lot_size", 1000))
        self.max_lot_size = risk.get("max_lot_size")

        # Futures/bps-specific
        self.stop_bps = risk.get("stop_bps", 0)
        self.target_bps = risk.get("target_bps", 0)
        self.num_contracts = risk.get("num_contracts", 1)
        self.min_contracts = int(risk.get("min_contracts", 1))
        self.max_contracts = risk.get("max_contracts")
        # Use config risk_pct for sizing (not registry active_risk_pct which is a fleet policy)
        self.risk_pct = float(risk.get("risk_pct", 0) or self.risk_policy.get("configured_risk_pct", 0) or 0)
        self.risk_pct = min(self.risk_pct, 0.10)  # Hard cap at 10% per-trade risk
        self.dynamic_position_sizing = self.risk_pct > 0

        # Derived: pip size for FX
        self.pip_size = 0.01 if "JPY" in self.symbol.upper() else 0.0001

        # Uses pips (FX) or bps (futures/crypto)?
        self.uses_pips = self.instrument_type == "forex" and self.stop_pips > 0

        # Multi-timeframe evaluation: only fire signals on higher-TF bar closes
        self.eval_timeframe = config.get("eval_timeframe", "1m")  # 1m|5m|15m|30m|1h
        self._eval_tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(self.eval_timeframe, 1)
        self._last_eval_tf_boundary: Optional[datetime] = None

        # Strategy variant: controls exit management style
        self.strategy_variant = config.get("strategy_variant", "default")
        # Trailing stop: lock in profits by trailing N pips behind peak
        self._trailing_pips = float(risk.get("trailing_pips", 0))
        self._trailing_active = self._trailing_pips > 0
        self._peak_favorable_pips: float = 0.0
        # Breakeven stop: move stop to entry+1pip after N pips favorable
        self._breakeven_trigger_pips = float(risk.get("breakeven_trigger_pips", 0))
        self._breakeven_activated = False

        # Hour filter: only trade during profitable hours (backtested +144%)
        # Default profitable hours from sweep: late NY session + Asian open
        self._profitable_hours: set[int] | None = None
        hour_cfg = config.get("hour_filter", {})
        if hour_cfg.get("enabled", True):
            default_hours = [0, 2, 5, 10, 12, 20, 21, 22, 23]  # removed 1,14 (toxic in live data), 9 already excluded
            self._profitable_hours = set(hour_cfg.get("hours", default_hours))

        # Entry confirmation: wait for N confirming bars before entering
        # DISABLED by default — backtested well individually but stacked with other gates
        # it cuts volume by 55% and total PnL by 55%. Only use if explicitly enabled.
        self._confirm_bars_long = int(config.get("entry_confirm_bars", 0))
        self._confirm_bars_short = int(config.get("entry_confirm_bars_short", self._confirm_bars_long + 1 if self._confirm_bars_long > 0 else 0))
        self._confirm_bars = self._confirm_bars_long  # default, overridden per direction below
        self._pending_entry: dict | None = None  # {"direction", "features", "bars_confirmed", "trigger_price"}

        # Pyramiding / scale-in (opt-in, defaults OFF — does not affect cohort)
        pyramid = config.get("pyramid", {})
        self.pyramid_enabled = pyramid.get("enabled", False)
        self.pyramid_trigger_pips = pyramid.get("trigger_pips", 0)   # FX: add when price moves N pips in our favor
        self.pyramid_trigger_bps = pyramid.get("trigger_bps", 0)     # Futures: add when price moves N bps in our favor
        self.pyramid_max_adds = pyramid.get("max_adds", 1)           # Max scale-in entries (1 = double position)
        self.pyramid_move_stop_breakeven = pyramid.get("move_stop_breakeven", True)  # Move stop to avg entry on add

        # Futures multiplier
        self.multiplier = 1.0
        if hasattr(contract, "multiplier") and contract.multiplier:
            try:
                self.multiplier = float(contract.multiplier)
            except (ValueError, TypeError):
                pass

        # Per-instrument logger
        self._log = logging.getLogger(f"inst.{self.symbol.lower()}")

        # Track last feature eval minute to avoid double-evaluation
        self._last_eval_minute: Optional[datetime] = None

        # Fill deduplication — persisted to disk for dedup across restarts
        self._processed_fill_ids: set[str] = set()
        self._fill_ids_file = log_dir / "processed_fill_ids.json"
        self._load_processed_fill_ids()

        # Advanced features: multi-timeframe, spread gate, session scoring, sequencing
        from argus_flow.advanced_features import MultiTimeframeBuffer, SpreadTracker, SessionScorer
        self._mtf = MultiTimeframeBuffer()
        from argus_flow.mtf_analysis import MTFAnalysisEngine
        self._mtf_engine = MTFAnalysisEngine(self.symbol, config)

        # MTF Trend Strategy engine (4H/1H/5M)
        self._mtf_strategy = None
        self._ai_overlay = None
        mtf_cfg = config.get("mtf", {})
        if mtf_cfg.get("enabled") and config.get("strategy") == "mtf_trend":
            from argus_flow.strategies.mtf_engine import MTFStrategyEngine
            from helio.paper_stress import apply as _stress_apply
            _stress_mult = mtf_cfg.get("paper_stress_multiplier", 1.0)
            _trend_eff = _stress_apply(
                mtf_cfg.get("min_trend_strength", 0.5),
                _stress_mult,
                strategy=f"argus_{self.symbol}", knob="min_trend_strength",
            )
            _conf_eff = _stress_apply(
                mtf_cfg.get("min_confidence", 0.5),
                _stress_mult,
                strategy=f"argus_{self.symbol}", knob="min_confidence",
            )
            self._mtf_strategy = MTFStrategyEngine(
                symbol=self.symbol,
                pip_size=self.pip_size,
                trend_ema_fast=mtf_cfg.get("trend_ema_fast", 8),
                trend_ema_slow=mtf_cfg.get("trend_ema_slow", 21),
                rsi_period=mtf_cfg.get("rsi_period", 14),
                min_trend_strength=_trend_eff,
            )
            self._mtf_min_confidence = _conf_eff
            self._log.info(f"MTF Trend Strategy enabled (4H/1H/5M, min_conf={self._mtf_min_confidence})")
            # AI Overlay — adaptive ensemble voter layer on top of MTF signals
            from argus_flow.strategies.ai_overlay import AdaptiveOverlay
            self._ai_overlay = AdaptiveOverlay(
                symbol=self.symbol,
                pip_size=self.pip_size,
                state_dir=log_dir,
            )
            _ai_disabled_at_boot = os.environ.get("ARGUS_DISABLE_AI_OVERLAY", "").strip() in ("1", "true", "yes")
            if _ai_disabled_at_boot:
                self._log.warning("AI Overlay DISABLED via ARGUS_DISABLE_AI_OVERLAY env (signals pass MTF directly)")
            else:
                self._log.info("AI Overlay enabled (15 voters, adaptive weights)")
            # LLM reasoning moved to nightly analysis (ops/nightly_analysis.py)
            # Real-time LLM gating disabled — adds latency, governor is better
            self._llm_reasoner = None
        self._bars_since_last_trade: int = 999
        self._consecutive_losses: int = 0
        self._spread_tracker = SpreadTracker(window=120)
        self._session_scorer = SessionScorer(self.symbol)
        trigger_cfg = config.get("trigger", {})
        self._max_spread_ratio = float(trigger_cfg.get("max_spread_ratio", 1.5))
        self._mtf_enabled = bool(trigger_cfg.get("mtf_enabled", True))
        self._spread_gate_enabled = bool(trigger_cfg.get("spread_gate_enabled", True))
        self._news_filter_enabled = bool(trigger_cfg.get("news_filter_enabled", True))
        self._sequencing_gap_minutes = float(trigger_cfg.get("sequencing_gap_minutes", 10))

    # ── Price extraction ─────────────────────────────────────
    def _get_mid(self) -> Optional[float]:
        """Extract mid price from ticker. FX: bid/ask mid. Futures: last or delayed."""
        t = self.ticker
        if self.instrument_type == "forex":
            bid = getattr(t, "bid", None)
            ask = getattr(t, "ask", None)
            if bid and bid > 0 and ask and ask > 0:
                return (bid + ask) / 2
            last = getattr(t, "last", None)
            if last and last > 0:
                return last
            return None
        else:
            # Futures / Crypto: prefer last, then delayedLast, then bid/ask mid
            last = getattr(t, "last", None) or getattr(t, "delayedLast", None)
            if last and last > 0:
                return float(last)
            bid = getattr(t, "bid", None) or getattr(t, "delayedBid", None)
            ask = getattr(t, "ask", None) or getattr(t, "delayedAsk", None)
            if bid and bid > 0 and ask and ask > 0:
                return (bid + ask) / 2
            return None

    def _load_processed_fill_ids(self) -> None:
        """Load previously processed fill IDs from disk for dedup across restarts."""
        if self._fill_ids_file.exists():
            try:
                data = json.loads(self._fill_ids_file.read_text())
                if isinstance(data, list):
                    self._processed_fill_ids = set(data[-1000:])
            except (json.JSONDecodeError, OSError):
                pass

    def _persist_fill_id(self, fill_id: str) -> None:
        """Persist fill ID to disk, keeping bounded to last 1000."""
        self._processed_fill_ids.add(fill_id)
        bounded = list(self._processed_fill_ids)[-1000:]
        try:
            self._fill_ids_file.write_text(json.dumps(bounded))
        except OSError:
            pass

    def _check_ticker_staleness(self) -> bool:
        """Return True if ticker data is fresh, False if stale. Logs warning on staleness."""
        t = self.ticker
        tick_time = getattr(t, "time", None)
        if tick_time is None:
            return True  # no timestamp available, can't check
        try:
            if isinstance(tick_time, (int, float)):
                last_tick = datetime.fromtimestamp(tick_time, tz=timezone.utc)
            else:
                last_tick = tick_time if tick_time.tzinfo else tick_time.replace(tzinfo=timezone.utc)
            age_s = (datetime.now(timezone.utc) - last_tick).total_seconds()
            _stale_threshold = int(os.getenv("STALE_TICK_SECONDS", "120"))
            if age_s > _stale_threshold:
                if not getattr(self, '_stale_warned', False):
                    self._log.warning(
                        f"STALE TICKER: {self.symbol} last tick {age_s:.0f}s ago — "
                        f"blocking entries until fresh data resumes"
                    )
                    self._stale_warned = True
                return False
            if getattr(self, '_stale_warned', False):
                self._log.info(f"Ticker {self.symbol} fresh again (age={age_s:.0f}s)")
                self._stale_warned = False
        except Exception:
            pass
        return True

    def _get_volume(self) -> float:
        """Get INCREMENTAL volume since last read (not cumulative session volume).

        IBKR ticker.volume is cumulative for the session. We track the previous
        value and return the delta, which gives per-bar volume when sampled once
        per bar close.
        """
        t = self.ticker
        raw_vol = getattr(t, "volume", None) or getattr(t, "delayedVolume", None)
        raw_vol = float(raw_vol) if raw_vol and raw_vol > 0 else 0.0

        prev = getattr(self, '_prev_cumulative_vol', 0.0)
        self._prev_cumulative_vol = raw_vol

        if prev <= 0 or raw_vol < prev:
            # First read or session reset — can't compute delta
            return 0.0
        return raw_vol - prev

    # ── CSV helpers ──────────────────────────────────────────
    def _get_account_equity(self) -> float:
        # 2026-04-17: anchor is now dynamic (broker equity), not hardcoded $10K.
        # Paper/watcher/real all pull from the central helper so going live is
        # literally a broker-account swap — no sizing math changes.
        raw = self.risk_policy.get("model_start_equity_usd", 0)
        if isinstance(raw, str) and raw.strip().lower() == "fleet_anchor":
            try:
                from helio.fleet_sizing import get_sizing_anchor_usd
                return float(get_sizing_anchor_usd())
            except Exception:
                return DEFAULT_ACCOUNT_EQUITY_USD
        # Explicit numeric override in config (escape hatch for testing)
        try:
            modeled = float(raw or 0)
        except (TypeError, ValueError):
            modeled = 0.0
        if modeled > 0:
            return modeled
        # Live broker equity for real stage, or if paper lacks an override
        tracker = getattr(self, "_equity_tracker", None)
        if tracker is not None and tracker.equity_usd:
            return tracker.equity_usd
        try:
            from helio.fleet_sizing import get_sizing_anchor_usd
            return float(get_sizing_anchor_usd())
        except Exception:
            return DEFAULT_ACCOUNT_EQUITY_USD

    def _reference_usd_jpy(self) -> float | None:
        if self.symbol.upper() == "USDJPY":
            mid = self._get_mid()
            return float(mid) if mid and mid > 0 else None
        for inst in getattr(self, "_all_instruments", []):
            if inst.symbol.upper() != "USDJPY":
                continue
            mid = inst._get_mid()
            if mid and mid > 0:
                return float(mid)
        return None

    def _pip_value_per_unit_usd(self, reference_price: float | None = None) -> float:
        return fx_pip_value_per_unit_usd(
            self.symbol,
            quote_price=reference_price,
            usd_jpy_price=self._reference_usd_jpy(),
        )

    def _risk_usd_for_size(self, entry_price: float, stop_price: float, size: float) -> float:
        if size <= 0:
            return 0.0
        if self.uses_pips:
            stop_distance_pips = abs(entry_price - stop_price) / self.pip_size if self.pip_size > 0 else 0.0
            return stop_distance_pips * self._pip_value_per_unit_usd(entry_price) * size
        stop_distance_pts = abs(entry_price - stop_price)
        return stop_distance_pts * self.multiplier * size

    def _effective_risk_pct(self) -> float:
        """Dynamic tier-based risk_pct for this symbol's Argus strategy.
        Falls back to the config's static self.risk_pct if fleet_sizing is
        unavailable — keeps Argus safe even if the central config breaks.
        """
        try:
            from helio.fleet_sizing import get_allocation_factor, get_effective_risk_pct
            label = f"argus_{self.symbol.lower()}"
            return float(get_effective_risk_pct(label)["risk_pct"]) * float(get_allocation_factor(label))
        except Exception:
            return self.risk_pct

    def _resolve_position_size(self, entry_price: float, stop_price: float) -> tuple[float, float, str]:
        equity_usd = self._get_account_equity()
        # Resolve tier-based risk_pct each call so Argus auto-promotes as
        # measured live performance earns it (matches Forge tier behavior).
        effective_risk = self._effective_risk_pct()
        if effective_risk <= 0:
            self._log.warning(
                "ALLOCATION_BLOCK: effective_risk_pct<=0 symbol=%s stage=%s",
                self.symbol,
                self.deployment_stage,
            )
            return 0.0, 0.0, "allocation_factor_zero"
        if equity_usd < 100:
            self._log.warning(f"LOW_EQUITY_DEBUG: equity={equity_usd} stage={self.deployment_stage} model={self.risk_policy.get('model_start_equity_usd')} risk_pct={effective_risk}")

        if self.dynamic_position_sizing and equity_usd > 0:
            if self.uses_pips:
                actual_stop_pips = (
                    abs(entry_price - stop_price) / self.pip_size
                    if self.pip_size > 0 else self.stop_pips
                )
                size = fx_units_for_risk(
                    equity_usd=equity_usd,
                    risk_pct=effective_risk,
                    stop_pips=actual_stop_pips if actual_stop_pips > 0 else self.stop_pips,
                    symbol=self.symbol,
                    min_units=self.min_lot_size,
                    max_units=self.max_lot_size,
                    quote_price=entry_price,
                    usd_jpy_price=self._reference_usd_jpy(),
                )
            else:
                actual_stop_bps = (
                    abs(entry_price - stop_price) / entry_price * 10000.0
                    if entry_price > 0 else self.stop_bps
                )
                size = futures_contracts_for_risk(
                    equity_usd=equity_usd,
                    risk_pct=effective_risk,
                    entry_price=entry_price,
                    stop_bps=actual_stop_bps if actual_stop_bps > 0 else self.stop_bps,
                    multiplier=self.multiplier,
                    min_contracts=self.min_contracts,
                    max_contracts=self.max_contracts,
                )
            policy = "dynamic"
        else:
            size = self.lot_size if self.uses_pips else self.num_contracts
            policy = "fixed"

        size = float(size or 0)

        # Hard cap: never exceed max_lot_size / max_contracts
        if self.uses_pips and self.max_lot_size and size > self.max_lot_size:
            self._log.warning(f"SIZE_CAP: {size} > max_lot_size {self.max_lot_size}")
            size = float(self.max_lot_size)
        elif not self.uses_pips and self.max_contracts and size > self.max_contracts:
            self._log.warning(f"SIZE_CAP: {size} > max_contracts {self.max_contracts}")
            size = float(self.max_contracts)

        # Notional cap (replaced the old SIZE_CAP_10X 2026-04-17): enforce
        # a percent-of-anchor ceiling on total notional so sizing auto-scales
        # with broker equity instead of the fixed lot_size config. This keeps
        # risk math anchored to what a real funded account could actually
        # hold (e.g., stocks capped at 2x equity per Reg T margin).
        try:
            from helio.fleet_sizing import max_notional_usd, get_sizing_anchor_usd
            asset_class = "fx" if self.uses_pips else "micro_future"
            cap_usd = max_notional_usd(asset_class)
            if cap_usd > 0 and entry_price > 0:
                if self.uses_pips:
                    notional_per_unit = fx_notional_per_unit_usd(
                        self.symbol,
                        quote_price=entry_price,
                        usd_jpy_price=self._reference_usd_jpy(),
                    )
                    cap_units = cap_usd / max(notional_per_unit, 1e-9)
                else:
                    cap_units = cap_usd / (float(entry_price) * float(self.multiplier or 1))
                if size > cap_units:
                    self._log.warning(f"NOTIONAL_CAP: size {size} > {asset_class} cap {cap_units:.0f} (anchor=${get_sizing_anchor_usd():,.0f})")
                    size = float(cap_units)
        except Exception as _e:
            # Safety fallback: retain the original 10x lot_size guard if
            # fleet_sizing is unavailable at this callsite.
            base_size = self.lot_size if self.uses_pips else self.num_contracts
            if base_size > 0 and size > base_size * 10:
                self._log.warning(f"SIZE_CAP_10X (fallback): {size} > 10x base {base_size} — capping")
                size = float(base_size * 10)

        risk_usd = self._risk_usd_for_size(entry_price, stop_price, size)
        return size, risk_usd, policy

    def _hydrate_legacy_state_size(self) -> None:
        s = self.state
        if s.position == "FLAT" or s.position_size > 0:
            return
        fallback = float(self.lot_size if self.uses_pips else self.num_contracts)
        s.position_size = fallback
        s.entry_risk_usd = self._risk_usd_for_size(s.entry_price, s.stop_price, fallback)
        s.sizing_policy = s.sizing_policy or "legacy_fixed"

    def _hydrate_trade_history_from_journal(self) -> None:
        """Recover cumulative closed-trade history if state lags the journal."""
        if not self.trade_log.exists():
            return
        try:
            with open(self.trade_log, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError:
            return
        if not rows:
            return

        serials = []
        pnl_total = 0.0
        pnl_usd_total = 0.0
        pnl_field = "pnl_pips" if self.uses_pips else "pnl_pts"

        for row in rows:
            try:
                serials.append(int(float(row.get("trade_num", 0) or 0)))
            except (TypeError, ValueError):
                pass
            try:
                pnl_total += float(row.get(pnl_field, 0) or 0)
            except (TypeError, ValueError):
                pass
            try:
                pnl_usd_total += float(row.get("pnl_usd", 0) or 0)
            except (TypeError, ValueError):
                pass

        if not serials:
            return

        journal_trade_count = max(serials)
        s = self.state
        if s.trade_count < journal_trade_count:
            s.trade_count = journal_trade_count

        if self.uses_pips:
            s.pnl_pips = pnl_total
        else:
            s.pnl_points = pnl_total
        s.pnl_usd = pnl_usd_total

    def current_open_risk_usd(self) -> float:
        s = self.state
        if s.position == "FLAT":
            return 0.0
        self._hydrate_legacy_state_size()
        return self._risk_usd_for_size(s.entry_price, s.stop_price, s.position_size)

    def current_unrealized_pnl_usd(self, current_price: float | None = None) -> float:
        s = self.state
        if s.position == "FLAT":
            return 0.0
        self._hydrate_legacy_state_size()
        px = current_price if current_price is not None else self._get_mid()
        if px is None or px <= 0:
            return 0.0
        if self.uses_pips:
            if s.position == "LONG":
                pnl_units = (px - s.entry_price) / self.pip_size
            else:
                pnl_units = (s.entry_price - px) / self.pip_size
            return pnl_units * self._pip_value_per_unit_usd(px) * s.position_size
        if s.position == "LONG":
            pnl_points = px - s.entry_price
        else:
            pnl_points = s.entry_price - px
        return pnl_points * self.multiplier * s.position_size

    _SIGNAL_ROTATE_BYTES = 50 * 1024 * 1024  # 50MB

    def _ensure_signal_header(self) -> None:
        if not self.signal_log.exists():
            with open(self.signal_log, "w", newline="") as f:
                csv.writer(f).writerow(signal_header(self.instrument_type))

    def _rotate_signal_log_if_needed(self) -> None:
        try:
            if self.signal_log.exists() and self.signal_log.stat().st_size > self._SIGNAL_ROTATE_BYTES:
                rotated = self.signal_log.with_name(
                    f"signals_rotated_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.csv"
                )
                self.signal_log.rename(rotated)
                self._log.info(f"Rotated signals.csv -> {rotated.name}")
                self._ensure_signal_header()
        except OSError as exc:
            self._log.warning(f"Signal log rotation failed: {exc}")

    def _log_signal(self, features: dict, direction: Optional[str], action: str) -> None:
        self._rotate_signal_log_if_needed()
        self._ensure_signal_header()
        features["ts"] = datetime.now(timezone.utc).isoformat()
        # Governor scoring (LOG_ONLY — stamps P(win) without blocking)
        gov = getattr(self, '_governor', None)
        if gov is not None and direction:
            score = _governor_score(gov, features, direction, self.symbol)
            if score is not None:
                features["gov_score"] = round(score, 3)
                features["gov_action"] = "TAKE" if score >= 0.5 else "SKIP"
        with open(self.signal_log, "a", newline="") as f:
            csv.writer(f).writerow(build_signal_row(
                features, direction, action, self.instrument_type,
                getattr(self, '_config_hash', ''), getattr(self, '_session_id', ''),
            ))
            f.flush()
            os.fsync(f.fileno())
        # Log blocked signals as shadow opportunities for counterfactual analysis
        if "BLOCKED" in action or action == "MAINTENANCE_BLACKOUT":
            self._log_opportunity(features, direction, action)

    def _log_opportunity(self, features: dict, direction: Optional[str], block_reason: str) -> None:
        """Emit lightweight opportunity event for shadow accounting."""
        opp_file = self.log_dir / "opportunities.jsonl"
        try:
            risk = self.cfg.get("risk", {})
            opp = {
                "opportunity_id": f"{self.symbol}_{features.get('ts', '')}_{direction}",
                "ts": features.get("ts", ""),
                "symbol": self.symbol,
                "direction": direction or "",
                "price": features.get("price", 0),
                "block_reason": block_reason,
                "regime": features.get("regime", ""),
                "efficiency_ratio": features.get("efficiency_ratio", 0),
                "range_pct": features.get("range_pct", 0),
                "hour": features.get("hour", 0),
                "stop_pips": risk.get("stop_pips", risk.get("stop_bps", 0)),
                "target_pips": risk.get("target_pips", risk.get("target_bps", 0)),
                "timeout_minutes": risk.get("timeout_minutes", 75),
                "config_hash": getattr(self, '_config_hash', ''),
                "session_id": getattr(self, '_session_id', ''),
                "entry_spread": features.get("entry_spread", 0),
            }
            with open(opp_file, "a") as f:
                f.write(json.dumps(opp) + "\n")
        except Exception:
            pass  # never let opportunity logging break the runner

    def _evaluate_validity(self) -> tuple[bool, str]:
        """Determine if this trade is experimentally valid.

        Taxonomy (ordered by priority):
        - restored_from_file: state loaded from disk (first trade after restart)
        - zero_stops_during_trade: stop/target were 0 at any point
        - timeout_restoration_failure: timeout_time missing after restore
        - reconnect_during_session: IBKR reconnect occurred (session-wide)
        - repeated_tick_failures: runner had N+ consecutive errors during trade
        """
        s = self.state
        if s.restored_this_session:
            return False, "restored_from_file"
        if s.had_zero_stops:
            return False, "zero_stops_or_timeout_missing"
        if getattr(self, '_reconnected', False):
            return False, "reconnect_during_session"
        if getattr(self, '_consecutive_errors', 0) >= 3:
            return False, "repeated_tick_failures"
        return True, ""

    def _ensure_trade_header(self) -> None:
        ensure_trade_csv_schema(self.trade_log, self.uses_pips)

    def _log_trade(self, exit_price: float, exit_reason: str, now: datetime) -> tuple[float, float]:
        """Log closed trade, return PnL in pips/points and USD."""
        s = self.state
        self._hydrate_legacy_state_size()
        dur = (now - s.entry_time).total_seconds() / 60 if s.entry_time else 0

        if self.uses_pips:
            if s.position == "LONG":
                pnl = (exit_price - s.entry_price) / self.pip_size
            else:
                pnl = (s.entry_price - exit_price) / self.pip_size
        else:
            if s.position == "LONG":
                pnl = exit_price - s.entry_price
            else:
                pnl = s.entry_price - exit_price

        # Evaluate experiment validity
        valid, invalid_reason = self._evaluate_validity()
        runtime_epoch = int(time.time() - self._runtime_start) if hasattr(self, '_runtime_start') else 0

        if self.uses_pips:
            pnl_usd = pnl * self._pip_value_per_unit_usd(exit_price) * s.position_size
        else:
            pnl_usd = pnl * self.multiplier * s.position_size

        validity_fields = [
            str(valid).lower(), invalid_reason or "",
            getattr(self, '_config_hash', ''), getattr(self, '_session_id', ''),
            str(runtime_epoch), getattr(self, '_git_sha', ''),
        ]

        # Entry-feature + execution quality fields (v4 schema)
        ef = getattr(self, '_entry_features', {})
        signal_mid = ef.get("signal_mid", s.entry_price)
        slippage = abs(s.entry_price - signal_mid) / self.pip_size if self.uses_pips and signal_mid else 0
        signal_ts = ef.get("signal_ts", "")
        fill_latency_ms = 0
        if signal_ts and s.entry_time:
            try:
                from datetime import datetime as _dt
                sig_dt = _dt.fromisoformat(signal_ts)
                fill_latency_ms = int((s.entry_time - sig_dt).total_seconds() * 1000)
            except Exception:
                pass

        entry_feature_fields = [
            f"{ef.get('mtf_score', 0):.3f}",
            ef.get("mtf_alignment", ""),
            f"{ef.get('session_score', 0):.3f}",
            ef.get("session_label", ""),
            f"{ef.get('spread_ratio', 0):.3f}",
            f"{ef.get('entry_spread', 0):.2f}" if 'entry_spread' in ef else "0",
            f"{ef.get('entry_bid', 0):.5f}" if ef.get('entry_bid') else "0",
            f"{ef.get('entry_ask', 0):.5f}" if ef.get('entry_ask') else "0",
            f"{ef.get('conviction_score', 0):.3f}",
            ef.get("bias_4h", ""),
            f"{signal_mid:.5f}" if signal_mid else "0",
            str(fill_latency_ms),
            f"{slippage:.2f}",
        ]

        self._ensure_trade_header()
        with open(self.trade_log, "a", newline="") as f:
            w = csv.writer(f)
            if self.uses_pips:
                w.writerow([
                    now.isoformat(), s.position.lower(),
                    f"{s.entry_price:.5f}",
                    f"{exit_price:.5f}", f"{pnl:.2f}",
                    exit_reason, f"{dur:.1f}", s.trade_count,
                    f"{pnl_usd:.2f}", f"{s.position_size:.0f}", f"{s.entry_risk_usd:.2f}",
                    s.sizing_policy or "fixed", s.entry_regime or "",
                ] + validity_fields + entry_feature_fields)
            else:
                if self.instrument_type == "future":
                    ep = f"{s.entry_price:.2f}"
                    xp = f"{exit_price:.2f}"
                else:
                    ep = f"{s.entry_price:.5f}"
                    xp = f"{exit_price:.5f}"
                w.writerow([
                    now.isoformat(), s.position.lower(),
                    ep, xp, f"{pnl:.2f}", f"{pnl_usd:.2f}",
                    exit_reason, f"{dur:.1f}", s.trade_count,
                    f"{s.position_size:.0f}", f"{s.entry_risk_usd:.2f}", s.sizing_policy or "fixed",
                    s.entry_regime or "",
                ] + validity_fields + entry_feature_fields)

        # Dual-write to canonical fills so Argus trades flow to the fleet-wide
        # log live, not nightly. Gate on experiment validity to match the
        # existing backfill filter (argus valid_filter=True), otherwise
        # reconciliation would immediately flag DRIFT between canonical and CSV.
        # Internal try/except swallows any broken log path — cannot break
        # paper trading here. Matches the pattern used by the forge runners.
        if valid:
            try:
                from helio.canonical_fills import write_fill_typed
                from helio.domain import Fill
                write_fill_typed(Fill(
                    strategy=f"argus_{self.symbol.lower()}",
                    symbol=self.symbol,
                    direction=s.position.lower() if s.position else "",
                    side="EXIT",
                    entry_ts=s.entry_time.isoformat() if s.entry_time else None,
                    exit_ts=now.isoformat(),
                    entry_px=float(s.entry_price),
                    exit_px=float(exit_price),
                    size=float(s.position_size),
                    risk_usd=float(s.entry_risk_usd or 0.0),
                    pnl_usd=round(pnl_usd, 2),
                    exit_reason=exit_reason,
                ))
            except Exception:
                pass  # never let canonical log break paper trading

        # Clear entry features after trade close
        self._entry_features = {}

        # Reset validity flags after trade closes
        s.restored_this_session = False
        s.had_zero_stops = False

        return pnl, pnl_usd

    def _recent_win_rate(self, lookback: int = 10) -> float:
        """Win rate of last N trades from trade log (for AI overlay)."""
        trades_file = self.log_dir / "trades.csv"
        if not trades_file.exists():
            return 0.5
        try:
            with open(trades_file, "r") as f:
                rows = list(csv.DictReader(f))
            if not rows:
                return 0.5
            recent = rows[-lookback:]
            wins = sum(1 for r in recent if float(r.get("pnl_pips", r.get("pnl_pts", "0"))) > 0)
            return wins / len(recent)
        except Exception:
            return 0.5

    def _ai_overlay_learn(self, pnl_pips: float) -> None:
        """Notify the AI overlay of a closed trade so it can update weights."""
        if self._ai_overlay is not None:
            try:
                self._ai_overlay.learn(pnl_pips)
                self._bars_since_last_trade = 0
                # Re-pool weights across all symbols after learning
                pool = getattr(self, '_ai_pool', None)
                overlays = getattr(self, '_ai_overlays', None)
                if pool and overlays:
                    pool.update(overlays)
                    pool.sync_to_overlays(overlays, blend=0.3)
            except Exception as e:
                self._log.warning(f"AI overlay learn error: {e}")

    # ── Feature & trigger dispatch ───────────────────────────
    def _compute_features(self) -> Optional[dict]:
        if self.instrument_type in ("future", "crypto"):
            return compute_features_futures(self.buf)
        return compute_features_fx(self.buf)

    def _check_trigger(self, features: dict) -> Optional[str]:
        strategy = self.cfg.get("strategy", "range_accel")
        # Advanced strategies: FVG, liquidity sweep, volume profile
        if strategy == "fvg":
            return self._check_trigger_fvg(features)
        elif strategy == "liquidity_sweep":
            return self._check_trigger_sweep(features)
        elif strategy == "volume_profile":
            return self._check_trigger_vp(features)
        # Default: range-based triggers
        if self.instrument_type in ("future", "crypto"):
            return check_trigger_futures(features, self.cfg)
        return check_trigger_fx(features, self.cfg)

    def _check_trigger_fvg(self, features: dict) -> Optional[str]:
        """Fair Value Gap strategy: enter on retrace to fill 3-candle imbalance."""
        from argus_flow.strategies.fvg_detector import detect_fvgs, find_fvg_fill_entries
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        if len(df) < 60:
            return None
        df = df.reset_index(drop=True)
        fvgs = detect_fvgs(df, min_displacement_mult=trigger.get("min_displacement_mult", 1.5))
        entries = find_fvg_fill_entries(
            df, fvgs, max_wait_bars=trigger.get("max_wait_bars", 60),
            session_start=trigger.get("session_start_utc", 8),
            session_end=trigger.get("session_end_utc", 20),
        )
        if entries:
            return entries[-1]["direction"]  # latest signal
        return None

    def _check_trigger_sweep(self, features: dict) -> Optional[str]:
        """Liquidity sweep: enter opposite direction after wick beyond swing point."""
        from argus_flow.strategies.liquidity_sweep import find_swing_points, detect_sweeps
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        if len(df) < 60:
            return None
        df = df.reset_index(drop=True)
        sh, sl = find_swing_points(df, lookback=trigger.get("swing_lookback", 20))
        sweeps = detect_sweeps(
            df, sh, sl, wick_ratio_min=trigger.get("wick_ratio_min", 0.3),
            session_start=trigger.get("session_start_utc", 8),
            session_end=trigger.get("session_end_utc", 20),
        )
        if sweeps:
            return sweeps[-1]["direction"]
        return None

    def _check_trigger_vp(self, features: dict) -> Optional[str]:
        """Volume profile: mean-revert from VAH/VAL toward POC."""
        from argus_flow.strategies.volume_profile import calculate_volume_profile
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        lookback = trigger.get("vp_lookback", 240)
        if len(df) < lookback:
            return None
        df = df.reset_index(drop=True)
        profile = calculate_volume_profile(df, len(df) - lookback, len(df))
        if not profile:
            return None
        price = features["price"]
        buffer = trigger.get("entry_buffer_pct", 0.0002)
        vah = profile["vah"]
        val = profile["val"]
        if price >= vah * (1 - buffer):
            return "short"  # at value area high, mean-revert down
        elif price <= val * (1 + buffer):
            return "long"   # at value area low, mean-revert up
        return None

    @staticmethod
    def _regime_compatible(regime: str, strategy: str) -> bool:
        """Check if the current regime suits the strategy type."""
        compatibility = {
            "range_accel":      {"RANGING", "UNKNOWN"},
            "T4_full_stack":    {"RANGING", "TRENDING", "UNKNOWN"},
            "range_accel_NY":   {"RANGING", "UNKNOWN"},
            "momentum":         {"TRENDING", "UNKNOWN"},
            "vol_burst":        {"TRENDING", "CHOPPY", "UNKNOWN"},
            "fvg":              {"RANGING", "TRENDING", "UNKNOWN"},
            "liquidity_sweep":  {"RANGING", "CHOPPY", "UNKNOWN"},
            "volume_profile":   {"RANGING", "UNKNOWN"},
        }
        allowed = compatibility.get(strategy, {"RANGING", "TRENDING", "UNKNOWN"})
        return regime in allowed

    # ── Stop/target computation ──────────────────────────────
    def _compute_atr(self) -> float:
        """Compute 14-period ATR from bar buffer."""
        df = self.buf.to_df()
        if len(df) < 14:
            return 0.0
        highs = df["high"].astype(float).iloc[-14:]
        lows = df["low"].astype(float).iloc[-14:]
        closes = df["close"].astype(float).iloc[-15:-1]  # previous closes
        if len(closes) < 14:
            return float((highs - lows).mean())
        tr = pd.concat([
            highs - lows,
            (highs - closes).abs(),
            (lows - closes).abs(),
        ], axis=1).max(axis=1)
        return float(tr.mean())

    def _compute_stops(self, entry_px: float, direction: str) -> tuple[float, float]:
        """Return (stop_price, target_price) for a new entry.

        Uses ATR-scaled stops if atr_stop_mult is in config, otherwise fixed pip/bps.
        """
        risk_cfg = self.cfg.get("risk", {})
        atr_stop_mult = risk_cfg.get("atr_stop_mult", 0)
        atr_target_mult = risk_cfg.get("atr_target_mult", 0)

        if atr_stop_mult > 0:
            atr = self._compute_atr()
            if atr > 0:
                stop_dist = atr * atr_stop_mult
                target_dist = atr * (atr_target_mult if atr_target_mult > 0 else atr_stop_mult * 2)
            else:
                # Fallback to fixed if ATR can't be computed
                stop_dist = self.stop_pips * self.pip_size if self.uses_pips else entry_px * (self.stop_bps / 10000)
                target_dist = self.target_pips * self.pip_size if self.uses_pips else entry_px * (self.target_bps / 10000)
        elif self.uses_pips:
            stop_dist = self.stop_pips * self.pip_size
            target_dist = self.target_pips * self.pip_size
        else:
            stop_dist = entry_px * (self.stop_bps / 10000)
            target_dist = entry_px * (self.target_bps / 10000)

        if direction == "long":
            return entry_px - stop_dist, entry_px + target_dist
        else:
            return entry_px + stop_dist, entry_px - target_dist

    def _check_trailing_stop(self, mid: float) -> None:
        """Dynamic stop management: R-based trailing + config-based trailing/breakeven.

        Supports three modes (can stack):
        1. R-based: move to BE after 1R, trail at +0.5R after 1.5R (always active)
        2. Pip-based trailing: trail N pips behind peak favorable (from config trailing_pips)
        3. Breakeven trigger: move to entry+1pip after N pips favorable (from config breakeven_trigger_pips)
        """
        s = self.state
        if s.entry_price == 0 or s.stop_price == 0:
            return

        risk_dist = abs(s.entry_price - s.stop_price)
        pip = self.pip_size

        # Track peak favorable movement
        if s.position == "LONG":
            favorable_pips = (mid - s.entry_price) / pip
        elif s.position == "SHORT":
            favorable_pips = (s.entry_price - mid) / pip
        else:
            return

        self._peak_favorable_pips = max(self._peak_favorable_pips, favorable_pips)

        # --- Breakeven trigger (config-based) ---
        if self._breakeven_trigger_pips > 0 and not self._breakeven_activated:
            if self._peak_favorable_pips >= self._breakeven_trigger_pips:
                be_stop = s.entry_price + pip if s.position == "LONG" else s.entry_price - pip
                if (s.position == "LONG" and be_stop > s.stop_price) or \
                   (s.position == "SHORT" and be_stop < s.stop_price):
                    self._log.info(f"BREAKEVEN: triggered at {self._peak_favorable_pips:.1f} pips favorable")
                    s.stop_price = be_stop
                    self._breakeven_activated = True

        # --- Pip-based trailing (config-based) ---
        if self._trailing_active and self._peak_favorable_pips > self._trailing_pips:
            if s.position == "LONG":
                trail_stop = s.entry_price + (self._peak_favorable_pips - self._trailing_pips) * pip
                if trail_stop > s.stop_price:
                    s.stop_price = trail_stop
            elif s.position == "SHORT":
                trail_stop = s.entry_price - (self._peak_favorable_pips - self._trailing_pips) * pip
                if trail_stop < s.stop_price:
                    s.stop_price = trail_stop

        # --- R-based trailing (always active as fallback) ---
        if risk_dist > 0:
            favorable_r = favorable_pips * pip / risk_dist if risk_dist > 0 else 0
            if s.position == "LONG":
                if favorable_r >= 1.5:
                    new_stop = s.entry_price + risk_dist * 0.5
                    if new_stop > s.stop_price:
                        s.stop_price = new_stop
                elif favorable_r >= 1.0:
                    if s.stop_price < s.entry_price:
                        self._log.info(f"TRAILING_R: stop moved to breakeven {s.entry_price:.5f}")
                        s.stop_price = s.entry_price
            elif s.position == "SHORT":
                if favorable_r >= 1.5:
                    new_stop = s.entry_price - risk_dist * 0.5
                    if new_stop < s.stop_price:
                        s.stop_price = new_stop
                elif favorable_r >= 1.0:
                    if s.stop_price > s.entry_price:
                        self._log.info(f"TRAILING_R: stop moved to breakeven {s.entry_price:.5f}")
                        s.stop_price = s.entry_price

    # ── Pyramiding / scale-in ────────────────────────────────
    def _check_pyramid(self, mid: float, now: datetime) -> None:
        """Check if we should add to the current position (scale-in on strength)."""
        if not self.pyramid_enabled:
            return
        s = self.state
        if s.pyramid_adds >= self.pyramid_max_adds:
            return

        # Compute how far price has moved in our favor
        if self.uses_pips:
            trigger_dist = self.pyramid_trigger_pips * self.pip_size
        else:
            trigger_dist = s.entry_price * (self.pyramid_trigger_bps / 10000)

        if trigger_dist <= 0:
            return

        if s.position == "LONG":
            favorable_move = mid - s.entry_price
        else:  # SHORT
            favorable_move = s.entry_price - mid

        if favorable_move < trigger_dist:
            return

        self._hydrate_legacy_state_size()
        existing_entries = s.pyramid_adds + 1
        add_size = s.position_size / existing_entries if existing_entries > 0 else 0.0
        if add_size <= 0:
            return

        if hasattr(self, "_risk_mgr"):
            add_risk_usd = self._risk_usd_for_size(mid, s.stop_price, add_size)
            allowed, reason = self._risk_mgr.can_enter(
                self.symbol,
                s.position.lower(),
                getattr(self, "_all_instruments", []),
                candidate_risk_usd=add_risk_usd,
            )
            if not allowed:
                self._log.info(f"PYRAMID_BLOCK {s.position} | reason={reason}")
                return

        # Scale-in: add to position
        old_entry = s.entry_price
        # Average entry: equal-weight average of original + add
        n_entries = s.pyramid_adds + 1  # entries so far (original + prior adds)
        s.avg_entry_price = (old_entry * n_entries + mid) / (n_entries + 1)
        s.pyramid_adds += 1
        s.position_size += add_size

        # Move stop to breakeven (avg entry) if configured
        if self.pyramid_move_stop_breakeven and s.avg_entry_price > 0:
            if s.position == "LONG":
                new_stop = s.avg_entry_price - (1 * self.pip_size if self.uses_pips else 0)
                s.stop_price = max(s.stop_price, new_stop)  # only tighten, never loosen
            else:
                new_stop = s.avg_entry_price + (1 * self.pip_size if self.uses_pips else 0)
                s.stop_price = min(s.stop_price, new_stop)  # only tighten, never loosen

        s.save()
        self._log.info(
            f"PYRAMID ADD #{s.pyramid_adds} {s.position} @ {mid:.5f} "
            f"avg_entry={s.avg_entry_price:.5f} "
            f"new_stop={s.stop_price:.5f} size={s.position_size:.0f}"
        )

    # ── Real execution helpers (gated behind execution_mode == "real") ──
    def _submit_real_entry(self, direction: str, size: float, stop_px: float, target_px: float) -> bool:
        """Submit a market order for real entry. Returns True on success.

        Pre-trade guards (mirrors helio/ibkr_execution.py:submit_bracket):
          0. Real-money boundary (allowlist + global flag) — 2026-05-18: was MISSING
             until Codex audit; argus FX could submit live-account orders unchecked.
          1. Fleet kill-switch (HALT.flag)
          2. FX IdealPro minimum ($25K USD-equivalent)
          3. Cluster exposure cap (per-instrument + macro cluster + total notional)
        """
        s = self.state
        ib = getattr(self, '_ib', None)
        if ib is None:
            self._log.error("REAL_ENTRY FAILED: no IB reference on runner")
            return False

        # Guard 0: real-money boundary. No-op for paper connections but
        # absolute must-check for real connections — strategy_label fed
        # so the boundary can attribute (allowlist matches per-strategy).
        try:
            from helio.real_money import (
                enforce_real_money_boundary, AccountBoundaryViolationError,
            )
            from helio.ibkr_execution import _fx_usd_notional
            est_notional = _fx_usd_notional(self.symbol, float(size), float(stop_px))
            strategy_label = f"argus_{self.symbol.lower().replace('.', '').replace('/', '')}"
            enforce_real_money_boundary(
                ib,
                strategy_label=strategy_label,
                notional_usd=est_notional if est_notional > 0 else None,
            )
        except AccountBoundaryViolationError as exc:
            self._log.error(
                f"REAL_MONEY_BOUNDARY refused entry {direction} {size} {self.symbol}: {exc}"
            )
            return False
        except Exception as exc:
            # Fail closed: any other boundary-check error blocks the trade
            # rather than allowing it through unchecked.
            self._log.error(
                f"REAL_MONEY_BOUNDARY check raised unexpected error "
                f"({type(exc).__name__}: {exc}); failing closed and refusing entry"
            )
            return False

        # Guard 1: fleet halt
        try:
            from helio.ibkr_execution import is_fleet_halted
            halted, halt_reason = is_fleet_halted()
            if halted:
                self._log.warning(f"FLEET_HALTED: refusing entry {direction} {size} {self.symbol}. Reason: {halt_reason}")
                return False
        except Exception as exc:
            # Fail closed: if we can't tell whether the fleet is halted, refuse.
            # Codex audit X5 doctrine — guards must fail closed.
            self._log.error(
                f"halt check failed, REFUSING entry {direction} {size} {self.symbol}: {exc}"
            )
            return False

        # Guard 2: FX IdealPro min — argus only trades FX, so always check
        try:
            from helio.ibkr_execution import _fx_usd_notional, IDEALPRO_MIN_USD
            entry_px_est = float(stop_px)  # rough estimate; for FX, stop is within ~10 pips of entry
            usd_notional = _fx_usd_notional(self.symbol, float(size), entry_px_est)
            if 0 < usd_notional < IDEALPRO_MIN_USD:
                self._log.warning(
                    f"FX_BELOW_IDEALPRO_MIN: refusing {direction} {size} {self.symbol} "
                    f"(~${usd_notional:,.0f} < ${IDEALPRO_MIN_USD:,} IdealPro min) — would route as odd-lot"
                )
                return False
        except Exception as exc:
            # Fail closed (Codex X5)
            self._log.error(
                f"FX min check failed, REFUSING entry {direction} {size} {self.symbol}: {exc}"
            )
            return False

        # Guard 3: cluster exposure cap
        try:
            from helio.cluster_exposure import would_breach_cluster_cap
            entry_px_est = float(stop_px)
            from helio.ibkr_execution import _fx_usd_notional as _est
            est_notional = _est(self.symbol, float(size), entry_px_est)
            if est_notional > 0:
                breach = would_breach_cluster_cap(self.symbol, direction, est_notional)
                if breach:
                    self._log.warning(
                        f"CLUSTER_CAP_BREACH: {breach} would exceed cap on "
                        f"{direction} {size} {self.symbol} (~${est_notional:,.0f}). Refusing entry."
                    )
                    return False
        except Exception as exc:
            # Fail closed (Codex X5)
            self._log.error(
                f"cluster cap check failed, REFUSING entry {direction} {size} {self.symbol}: {exc}"
            )
            return False

        # Guard 4: broker-position check (2026-05-19 incident follow-up).
        # If broker already shows a position in this instrument, that's an
        # orphan we don't know about. Submitting a new entry would either
        # double our exposure (if same direction) or partially offset it
        # (if opposite, briefly creating a smaller-net position with weird
        # P&L tracking). Refuse and let reconciliation adopt the orphan
        # first; next signal cycle can re-evaluate.
        broker_qty = self._read_broker_position()
        if broker_qty is None:
            # Broker unreachable. Fail closed.
            self._log.error(
                f"REAL_ENTRY ABORTED: broker position query returned None — "
                f"cannot verify clean entry state for {direction} {size} {self.symbol}"
            )
            return False
        if abs(broker_qty) > 0:
            self._log.warning(
                f"REAL_ENTRY ABORTED: broker already has {broker_qty} {self.symbol} — "
                f"refusing entry {direction} {size}. Reconciliation will adopt the orphan."
            )
            return False

        try:
            action = "BUY" if direction == "long" else "SELL"
            # ib_insync MarketOrder: totalQuantity must be positive AND integer
            # for IBKR FX (IdealPro). 2026-05-01: order 39/40 cancelled with
            # Error 10318 "doesn't support fractional quantity trading" because
            # the sizing layer produced 23300.509...; round to int here.
            qty = int(abs(size))
            if qty <= 0:
                self._log.warning(f"REAL_ENTRY ABORTED: rounded size={qty} (was {size}); skipping")
                return False

            # 2026-05-21 BUGFIX: FX entries must use LimitOrder + TIF=GTC,
            # not MarketOrder which defaults to TIF=DAY and triggers
            # Error 10349 ("Order TIF was set to DAY based on order preset").
            # Same bug class as the 2026-05-19 exit-path fix
            # (_build_exit_order) and the 2026-05-20 flatten_eod_executor
            # fix; the entry path in _submit_real_entry was missed in both
            # sweeps. Caught by the golden-trace recorder on the first
            # live entry after activation (USDJPY orderId=13, 2026-05-21
            # 18:05Z). TWS auto-retried the rejected order which masked
            # the bug but the trace caught the rejection -> retry pattern.
            # Pattern matches _build_exit_order: wide LMT (5% buffer)
            # with JPY-aware decimals, GTC TIF, outsideRth.
            is_cash = getattr(self.contract, "secType", "") == "CASH"
            if is_cash:
                ref_px = self._get_mid() or float(stop_px) or 1.0
                buffer = 1.05 if action == "BUY" else 0.95
                pair_tags = " ".join([
                    str(getattr(self, "symbol", "")),
                    str(getattr(self.contract, "symbol", "")),
                    str(getattr(self.contract, "currency", "")),
                    str(getattr(self.contract, "localSymbol", "")),
                ]).upper()
                is_jpy = "JPY" in pair_tags
                decimals = 3 if is_jpy else 5
                lmt = round(float(ref_px) * buffer, decimals)
                order = LimitOrder(action, qty, lmt)
                order.outsideRth = True
                order.tif = "GTC"
            else:
                order = MarketOrder(action, qty)
            order.account = getattr(self, 'stage_account', '') or ''
            # 2026-05-21 BUGFIX: pre-allocate orderId + write state BEFORE
            # placeOrder so any inbound execDetails for this order finds
            # state populated. See _pre_allocate_order_id for full reasoning.
            entry_pre_id = self._pre_allocate_order_id(ib)
            if entry_pre_id is not None:
                order.orderId = entry_pre_id
                s.entry_order_id = str(entry_pre_id)
                s.entry_pending = True
                s.entry_submitted_ts = datetime.now(timezone.utc).isoformat()
                s.save()
                trade = ib.placeOrder(self.contract, order)
            else:
                # Fallback: post-placeOrder write (legacy pattern with race)
                trade = ib.placeOrder(self.contract, order)
                s.entry_order_id = str(getattr(trade.order, 'orderId', ''))
                s.entry_pending = True
                s.entry_submitted_ts = datetime.now(timezone.utc).isoformat()
                s.save()

            # 2026-05-20 BUGFIX: persist pending-entry to the crash-recovery
            # queue. helio.pending_fills exists and is wired into forge's
            # submit_bracket, but argus's _submit_real_entry was never
            # connected — argus_flow/logs/_pending_fills.jsonl was 0 bytes.
            # Without this, a runner crash during the 60s pending window
            # falls back to synthetic-stop orphan adoption (the path that's
            # been producing 2-5× wider stops on every adopted entry).
            try:
                from helio.pending_fills import write_pending
                client_id = int(getattr(getattr(ib, 'client', None), 'clientId', 0) or 0)
                est_entry_px = self._get_mid() or float(stop_px) or 1.0
                write_pending(
                    client_id=client_id,
                    order_id=s.entry_order_id,
                    strategy=f"argus_{self.symbol.lower().replace('.', '').replace('/', '')}",
                    symbol=self.symbol,
                    direction=direction,
                    size=float(qty),
                    est_entry_px=float(est_entry_px),
                    stop_px=float(stop_px),
                    target_px=float(target_px),
                )
            except Exception as exc:
                # Don't block the trade on a queue-write failure — the
                # queue is a crash-recovery backstop, not a guard
                self._log.warning(f"pending_fills write failed (non-fatal): {exc}")

            self._log.info(
                f"REAL_ENTRY SUBMITTED {action} {size} {self.symbol} "
                f"orderId={s.entry_order_id} stop={stop_px} target={target_px}"
            )
            # Stash planned stops for bracket submission after fill
            self._pending_stop_px = stop_px
            self._pending_target_px = target_px
            self._pending_direction = direction
            self._pending_size = size
            return True
        except Exception as exc:
            self._log.error(f"REAL_ENTRY FAILED: {exc}", exc_info=True)
            return False

    def _submit_bracket_orders(self, stop_px: float, target_px: float) -> bool:
        """After entry fills, place protective stop and target limit orders."""
        s = self.state
        ib = getattr(self, '_ib', None)
        if ib is None:
            self._log.error("BRACKET FAILED: no IB reference on runner")
            return False
        try:
            size = s.position_size
            # Closing action is opposite of position
            close_action = "SELL" if s.position == "LONG" else "BUY"

            # OCA group — when one bracket leg fills, the other auto-cancels.
            # Mirrors the fix in helio/ibkr_execution.py (2026-04-27): without
            # OCA, both stop AND target can fill on the same bar = unintended
            # opposite-direction position.
            oca_group = f"oca_{self.symbol}_{s.entry_order_id}_{int(time.time() * 1000) % 1_000_000}"

            # 2026-05-20 BUGFIX: JPY-aware decimal rounding on bracket prices.
            # Same fix as _build_exit_order. round(price, 5) on a JPY pair
            # produces a sub-tick price that IBKR rejects with Warning 110.
            # ib_insync Forex: contract.symbol = base (e.g. "USD"),
            # contract.currency = quote (e.g. "JPY"), self.symbol = full
            # pair (e.g. "USDJPY"). Check all three to be safe.
            pair_tags = " ".join([
                str(getattr(self, "symbol", "")),
                str(getattr(self.contract, "symbol", "")),
                str(getattr(self.contract, "currency", "")),
                str(getattr(self.contract, "localSymbol", "")),
            ]).upper()
            is_jpy = "JPY" in pair_tags
            price_decimals = 3 if is_jpy else 5

            # Stop order (protective) — OCA leg A
            stop_order = StopOrder(close_action, abs(size), round(stop_px, price_decimals))
            stop_order.ocaGroup = oca_group
            stop_order.ocaType = 1  # cancel all remaining orders with block
            stop_order.account = getattr(self, 'stage_account', '') or ''
            # 2026-05-21 BUGFIX: pre-allocate orderId + write state BEFORE
            # placeOrder. See _pre_allocate_order_id.
            stop_pre_id = self._pre_allocate_order_id(ib)
            if stop_pre_id is not None:
                stop_order.orderId = stop_pre_id
                s.stop_order_id = str(stop_pre_id)
                stop_trade = ib.placeOrder(self.contract, stop_order)
            else:
                stop_trade = ib.placeOrder(self.contract, stop_order)
                s.stop_order_id = str(getattr(stop_trade.order, 'orderId', ''))

            # Target limit order — OCA leg B
            # 2026-05-20 BUGFIX: half-armed-bracket race. If the target
            # placeOrder throws (Error 110 price-decimal, throttle, network
            # blip), the stop is already LIVE at the broker but this
            # function returns False — caller then issues an emergency MKT
            # exit ALONGSIDE the live OCA stop, producing a LONG→SHORT
            # cascade similar to the 5/19 incident. Fix: wrap target
            # placement in inner try; on failure, explicitly cancel the
            # already-live stop before propagating the error.
            try:
                limit_order = LimitOrder(close_action, abs(size), round(target_px, price_decimals))
                limit_order.ocaGroup = oca_group
                limit_order.ocaType = 1
                limit_order.account = getattr(self, 'stage_account', '') or ''
                # 2026-05-21 BUGFIX: pre-allocate orderId + write state BEFORE
                # placeOrder. See _pre_allocate_order_id.
                tgt_pre_id = self._pre_allocate_order_id(ib)
                if tgt_pre_id is not None:
                    limit_order.orderId = tgt_pre_id
                    s.target_order_id = str(tgt_pre_id)
                    target_trade = ib.placeOrder(self.contract, limit_order)
                else:
                    target_trade = ib.placeOrder(self.contract, limit_order)
                    s.target_order_id = str(getattr(target_trade.order, 'orderId', ''))
            except Exception as target_exc:
                self._log.error(
                    f"BRACKET HALF-ARMED: target placeOrder failed ({target_exc}); "
                    f"cancelling already-live stop {s.stop_order_id} to avoid "
                    f"stop-alone + emergency-MKT cascade."
                )
                self._cancel_order_by_id(s.stop_order_id, "bracket_target_failed")
                s.stop_order_id = ""
                s.save()
                return False

            s.save()
            self._log.info(
                f"BRACKET SUBMITTED {self.symbol} oca={oca_group} "
                f"stop_orderId={s.stop_order_id} @ {stop_px:.5f} | "
                f"target_orderId={s.target_order_id} @ {target_px:.5f}"
            )
            return True
        except Exception as exc:
            self._log.error(f"BRACKET FAILED: {exc}", exc_info=True)
            return False

    def _dump_exit_forensics(self, order_id: str, stage: str) -> None:
        """Capture full TWS state of a stuck exit order before we cancel/retry.

        Background (2026-05-18): recurring argus_gbpusd EXIT FAILED cascade
        (5/13, 5/15, 5/18) shows the LMT+outsideRth exit order sits pending
        >30s without filling. Cancel-and-retry chain runs through IOC then
        GTC, each also timing out. We've had ZERO visibility into WHAT TWS
        was doing — runner only logs 'EXIT TIMEOUT' / 'EXIT STUCK' / 'EXIT
        FAILED' with no order status, no whyHeld, no trade.log.

        This writes the full forensic record to
        `argus_flow/logs/unfilled_orders.jsonl` (same path as the
        helio.ibkr_execution forensics from 2026-05-16) so each retry stage
        captures orderStatus + trade.log at that moment.
        """
        if not order_id:
            return
        ib = getattr(self, '_ib', None)
        if ib is None:
            return
        try:
            # Locate the trade via ib.trades() (covers both open and closed)
            target_trade = None
            for t in ib.trades():
                if str(getattr(t.order, 'orderId', '')) == str(order_id):
                    target_trade = t
                    break
            if target_trade is None:
                self._log.warning(
                    f"EXIT_FORENSICS: orderId={order_id} not found in ib.trades()"
                )
                return
            # Reuse the existing forensics dump from helio.ibkr_execution
            from helio.ibkr_execution import _dump_unfilled_forensics
            _dump_unfilled_forensics(target_trade, order_id=str(order_id))
            # Also log a one-line summary inline so the diagnosis is in runner.log
            os_obj = getattr(target_trade, 'orderStatus', None)
            status = getattr(os_obj, 'status', '?') if os_obj else '?'
            why_held = getattr(os_obj, 'whyHeld', '') if os_obj else ''
            filled = getattr(os_obj, 'filled', 0) if os_obj else 0
            remaining = getattr(os_obj, 'remaining', 0) if os_obj else 0
            log_entries = getattr(target_trade, 'log', None) or []
            last_msg = ""
            for entry in reversed(log_entries):
                msg = str(getattr(entry, 'message', '') or '').strip()
                err = int(getattr(entry, 'errorCode', 0) or 0)
                if err or msg:
                    last_msg = f"err={err}:{msg[:120]}" if err else msg[:140]
                    break
            self._log.warning(
                f"EXIT_FORENSICS stage={stage} order={order_id} status={status} "
                f"filled={filled} remaining={remaining} whyHeld='{why_held}' "
                f"last_log='{last_msg}'"
            )
        except Exception as exc:
            self._log.warning(f"_dump_exit_forensics failed (non-fatal): {exc}")

    def _build_exit_order(self, close_action: str, qty: int, ref_px: float, tif: str = "DAY"):
        """Build the right exit order for this instrument type.

        FX (CASH on IdealPro): wide LimitOrder + outsideRth=True + TIF=GTC.
        MarketOrder on FX can stall around session boundaries — the symptom
        we saw 2026-05-08 (orders 296/299/303/307 timing out with "EXIT
        TIMEOUT" → "EXIT STUCK" → "EXIT FAILED: MANUAL BROKER CHECK
        REQUIRED"). LMT with a 5% adverse buffer fills at NBBO without
        sitting in the queue. TIF=GTC (overriding the DAY default for CASH)
        avoids the speculative end-of-broker-day expiry race observed in
        the argus_gbpusd EXIT FAILED cascade — DAY-flagged FX exits that
        sat past the broker's daily roll boundary would silently expire.
        Codex audit 2026-05-18 flagged this. The retry escalation already
        ends in GTC; this change collapses the retry chain by starting
        there for CASH.

        STK/FUT/ETF: MarketOrder is correct (RTH-only routing handles
        itself), TIF=DAY is the right default for these.

        2026-05-20 BUGFIX: JPY pairs use 0.001 minimum tick size on
        IdealPro (3 decimals), not 0.00005 like EUR/USD-class pairs
        (5 decimals). Rounding the LMT to 5 decimals on a JPY pair
        produces a sub-tick price that IBKR rejects with Warning 110
        "The price does not conform to the minimum price variation for
        this contract". Caught live 2026-05-19 18:36 UTC: CADJPY exit
        cascaded through all 3 retry stages with lmtPrice=109.87177 /
        109.75983 — every retry got Warning 110 and the order stayed
        PendingSubmit. Position remained stuck until manual intervention.
        """
        sec_type = getattr(self.contract, "secType", "") or ""
        if sec_type == "CASH":
            buffer = 1.05 if close_action == "BUY" else 0.95
            # JPY-aware tick rounding (caught 2026-05-19 on CADJPY).
            # JPY pairs: 3 decimals. Other FX: 5 decimals.
            # IMPORTANT: on ib_insync Forex contracts, contract.symbol is the
            # BASE currency (e.g. "USD" for USDJPY) and contract.currency is
            # the QUOTE (e.g. "JPY"). The full pair lives in self.symbol on
            # the runner OR contract.localSymbol ("USD.JPY"). Earlier draft
            # of this fix checked contract.symbol and missed JPY pairs
            # because contract.symbol="USD". This version checks all three.
            pair_tags = " ".join([
                str(getattr(self, "symbol", "")),
                str(getattr(self.contract, "symbol", "")),
                str(getattr(self.contract, "currency", "")),
                str(getattr(self.contract, "localSymbol", "")),
            ]).upper()
            is_jpy = "JPY" in pair_tags
            decimals = 3 if is_jpy else 5
            lmt = round(float(ref_px or 1.0) * buffer, decimals)
            order = LimitOrder(close_action, qty, lmt)
            order.outsideRth = True
            # FX: force GTC unless caller explicitly asked for a non-DAY tif
            # (e.g., last-resort retry that already specified GTC). DAY is
            # the historical default that triggered the EXIT FAILED cascade.
            effective_tif = "GTC" if tif == "DAY" else tif
        else:
            order = MarketOrder(close_action, qty)
            effective_tif = tif
        order.tif = effective_tif
        return order

    def _pre_allocate_order_id(self, ib) -> int | None:
        """Pre-allocate an orderId from ib_insync's client BEFORE placeOrder.

        2026-05-21: eliminates the placeOrder -> state-write race. The
        original pattern was:

            trade = ib.placeOrder(contract, order)
            s.entry_order_id = str(trade.order.orderId)
            s.save()

        Between placeOrder returning and s.save() completing, ib_insync's
        event loop could process incoming TWS messages — including an
        execDetails for this very order. The fill handler would look up
        state by order_id and find it missing. By pre-allocating the
        orderId, setting it on the order object, and writing state
        BEFORE placeOrder, the race window is eliminated.

        Returns the pre-allocated int orderId on success, or None if
        ib.client.getReqId() isn't available — caller should fall back
        to the post-placeOrder pattern in that case.
        """
        try:
            return int(ib.client.getReqId())
        except (AttributeError, TypeError, ValueError) as exc:
            self._log.warning(
                f"pre_allocate_order_id failed ({exc}); "
                f"falling back to post-placeOrder pattern (race window present)"
            )
            return None

    def _read_broker_position(self) -> float | None:
        """Query the broker for this instrument's current position.

        Returns the signed quantity (positive=long, negative=short, 0=flat),
        or None if the broker is unreachable / no IB reference. Used by every
        exit-submission path to verify intent matches reality before placing
        an order — see the 2026-05-19 cascade-double-sell post-mortem
        (project_2026_05_19_cadjpy_jpy_decimal_bugs.md). Without this guard,
        a fill that races our local state update produces a duplicate exit
        order that REVERSES the position.
        """
        ib = getattr(self, "_ib", None)
        if ib is None:
            return None
        try:
            our_local = str(getattr(self.contract, "localSymbol", "")).upper()
            our_sym = str(getattr(self.contract, "symbol", "")).upper()
            our_curr = str(getattr(self.contract, "currency", "")).upper()
            for p in ib.positions():
                pc = p.contract
                if str(getattr(pc, "localSymbol", "")).upper() == our_local and our_local:
                    return float(p.position)
                if (str(pc.symbol).upper() == our_sym
                    and str(getattr(pc, "currency", "")).upper() == our_curr
                    and our_sym):
                    return float(p.position)
            return 0.0  # not in positions list = flat
        except Exception as exc:
            self._log.warning(f"broker position query failed (non-fatal): {exc}")
            return None

    def _broker_state_allows_exit(self, close_action: str, qty: int) -> tuple[bool, str]:
        """Verify the proposed exit makes sense against broker truth.

        Returns (ok, reason). Refuses the exit when:
          - Broker shows flat — our prior exit must have filled; emitting
            another would create a new opposite-direction position (the
            2026-05-19 CADJPY incident).
          - Broker direction is opposite our local belief — local state
            drifted; reconcile before submitting more orders.

        Caller should clear local state on refusal and let the next
        reconciliation cycle handle the broker truth."""
        broker_qty = self._read_broker_position()
        if broker_qty is None:
            # 2026-05-20 BUGFIX: fail CLOSED on broker-unreachable. The
            # previous "passthrough" was itself fail-open — comment claimed
            # downstream submit_bracket / cluster_exposure would catch it,
            # but _submit_real_exit calls ib.placeOrder directly, bypassing
            # those layers entirely. A network blip during a cascade retry
            # would silently produce a duplicate exit — exactly the 5/19
            # catastrophe caused by network failure instead of order race.
            # Better to under-trade for one cycle than double-exit.
            return False, "broker_unreachable_fail_closed"
        if broker_qty == 0.0:
            return False, f"broker_flat_already (intended {close_action} {qty})"
        if close_action == "SELL" and broker_qty < 0:
            return False, f"broker_short_{broker_qty:.0f}_SELL_would_deepen"
        if close_action == "BUY" and broker_qty > 0:
            return False, f"broker_long_{broker_qty:.0f}_BUY_would_deepen"
        # Direction matches but magnitude may differ. Caller should resize
        # to the actual broker quantity, not the local belief. Return ok
        # but with the magnitude info.
        if abs(broker_qty) < qty:
            return True, f"resize_needed_broker_has_{broker_qty:.0f}_local_thinks_{qty}"
        return True, "ok"

    def _submit_real_exit(self, reason: str, mid: float) -> bool:
        """Cancel existing bracket orders and submit a market exit."""
        s = self.state
        qty = abs(s.position_size)
        if qty <= 0:
            self._log.error("Cannot exit: position_size is 0")
            s.clear_trade_state()
            s.save()
            return False
        ib = getattr(self, '_ib', None)
        if ib is None:
            self._log.error("REAL_EXIT FAILED: no IB reference on runner")
            return False

        # 2026-05-19 BUGFIX: verify broker truth before submitting an exit.
        # The cascade race observed today doubled CADJPY (LONG → SHORT)
        # and quadrupled USDJPY (30K → 120K) because exits were submitted
        # on stale local state. This guard short-circuits the runaway
        # when broker shows the position is already gone.
        close_action = "SELL" if s.position == "LONG" else "BUY"
        ok, why = self._broker_state_allows_exit(close_action, int(qty))
        if not ok:
            self._log.error(
                f"EXIT_ABORTED reason={reason} {close_action} {qty} {self.symbol}: {why}. "
                f"Clearing local state to reconverge with broker on next cycle."
            )
            s.exit_pending = False
            s.position = "FLAT"
            s.position_size = 0
            s.clear_trade_state()
            s.save()
            return False
        if why != "ok" and "resize_needed" in why:
            broker_qty = self._read_broker_position() or 0.0
            new_qty = int(abs(broker_qty))
            self._log.warning(
                f"EXIT_RESIZE local={qty} -> broker={new_qty} reason={reason} {self.symbol}"
            )
            qty = new_qty

        try:
            # Cancel existing stop and target orders
            self._cancel_order_by_id(s.stop_order_id, "stop")
            self._cancel_order_by_id(s.target_order_id, "target")

            ref_px = mid if mid and mid > 0 else (s.entry_price or 0)
            order = self._build_exit_order(close_action, int(qty), ref_px, tif="DAY")
            order.account = getattr(self, 'stage_account', '') or ''
            # 2026-05-21 BUGFIX: pre-allocate orderId + write state BEFORE
            # placeOrder. See _pre_allocate_order_id.
            exit_pre_id = self._pre_allocate_order_id(ib)
            if exit_pre_id is not None:
                order.orderId = exit_pre_id
                s.exit_order_id = str(exit_pre_id)
                s.exit_pending = True
                s.exit_submitted_ts = datetime.now(timezone.utc).isoformat()
                s.save()
                trade = ib.placeOrder(self.contract, order)
            else:
                trade = ib.placeOrder(self.contract, order)
                s.exit_order_id = str(getattr(trade.order, 'orderId', ''))
                s.exit_pending = True
                s.exit_submitted_ts = datetime.now(timezone.utc).isoformat()
                s.save()
            self._log.info(
                f"REAL_EXIT SUBMITTED {close_action} {qty} {self.symbol} "
                f"orderId={s.exit_order_id} reason={reason}"
            )
            self._pending_exit_reason = reason
            return True
        except Exception as exc:
            self._log.error(f"REAL_EXIT FAILED: {exc}", exc_info=True)
            return False

    def _cancel_order_by_id(self, order_id: str, label: str) -> bool:
        """Cancel an open order by ID. Verifies cancellation after attempt. Returns True if confirmed."""
        if not order_id:
            return True
        ib = getattr(self, '_ib', None)
        if ib is None:
            return False
        try:
            for trade in ib.openTrades():
                if str(getattr(trade.order, 'orderId', '')) == order_id:
                    ib.cancelOrder(trade.order)
                    self._log.info(f"CANCEL REQUESTED {label} order {order_id}")
                    # Brief wait then verify cancellation
                    try:
                        ib.sleep(2)
                        still_open = any(
                            str(getattr(t.order, 'orderId', '')) == order_id
                            for t in ib.openTrades()
                        )
                        if still_open:
                            self._log.warning(
                                f"CANCEL UNCONFIRMED: {label} order {order_id} still in open trades — "
                                f"orphaned order may fill later"
                            )
                            return False
                        else:
                            self._log.info(f"CANCEL CONFIRMED {label} order {order_id}")
                            return True
                    except Exception:
                        return False
            self._log.debug(f"Cancel {label} order {order_id}: not found in open trades (may already be done)")
            return True
        except Exception as exc:
            self._log.warning(f"Cancel {label} order {order_id} failed: {exc}")
            return False

    def adopt_orphan_position(self, broker_dir: str, broker_qty: float,
                              broker_avg_cost: float) -> None:
        """Adopt a broker-side position into local state.

        Called from reconcile_instruments() when LOCAL_FLAT_BROKER_OPEN is
        detected — the runner restarted between order-submission and fill,
        so ib_insync's execDetailsEvent never fired for this fill (the new
        connection only sees future fills, not past ones from the prior
        session).

        Sets local position to match broker, derives synthetic stop/target
        from configured stop_pips/target_pips. Does NOT submit bracket
        orders to the broker — those would risk firing immediately if
        price has moved past the synthetic stop. Argus manages the adopted
        position via time-stop and price monitoring; on next exit the
        normal close path applies.

        2026-05-04: introduced after Friday's argus FX restart cycle left
        orphan positions that argus didn't know about, blocking entries
        via RECON_DRIFT permanently.

        2026-05-20 BUGFIX: sanity-check broker_qty + broker_avg_cost. On
        5/19 IBKR's position-update stream delivered two corrupted snapshots
        (position=-1.0 avgCost=-202.13) — an odd-lot arithmetic glitch.
        The runner blindly adopted both, submitted real LimitOrder against
        negative prices, and one filled at 115.54 with odd-lot warning.
        Reject obviously-bad values rather than acting on them.
        """
        # Sanity checks — refuse to adopt corrupted values
        if broker_avg_cost <= 0:
            self._log.error(
                f"ADOPT_ORPHAN REFUSED: broker_avg_cost={broker_avg_cost} "
                f"for {self.symbol} (non-positive). IBKR position-update "
                f"glitch. Local state unchanged; next reconcile will retry."
            )
            return
        if abs(broker_qty) < 1:
            # FX min lot is 1000; futures min is 1 contract. Anything below
            # 1 unit is an odd-lot arithmetic artifact, not a real position.
            self._log.error(
                f"ADOPT_ORPHAN REFUSED: broker_qty={broker_qty} for "
                f"{self.symbol} (sub-lot). Likely IBKR odd-lot glitch. "
                f"Local state unchanged."
            )
            return
        # Sanity: avg_cost must be within ±20% of last known mid
        last_mid = self._get_mid() or 0.0
        if last_mid > 0 and abs(broker_avg_cost - last_mid) / last_mid > 0.20:
            self._log.error(
                f"ADOPT_ORPHAN REFUSED: broker_avg_cost={broker_avg_cost} "
                f"diverges >20% from last_mid={last_mid} for {self.symbol}. "
                f"Likely IBKR position-update glitch."
            )
            return

        s = self.state
        s.position = broker_dir.upper()
        s.entry_price = broker_avg_cost
        s.avg_entry_price = broker_avg_cost
        s.position_size = abs(broker_qty)
        s.entry_time = datetime.now(timezone.utc)  # We don't know the real entry time
        s.timeout_time = (s.entry_time.replace(second=0, microsecond=0)
                          + timedelta(minutes=self.timeout_min))
        s.last_signal_time = s.entry_time

        # Synthetic stop/target from config (FX uses pips; futures use bps)
        if self.uses_pips and self.pip_size > 0 and self.stop_pips > 0:
            stop_dist = self.stop_pips * self.pip_size
            target_dist = self.target_pips * self.pip_size if self.target_pips > 0 else stop_dist * 2
            if broker_dir.upper() == "LONG":
                s.stop_price = broker_avg_cost - stop_dist
                s.target_price = broker_avg_cost + target_dist
            else:  # SHORT
                s.stop_price = broker_avg_cost + stop_dist
                s.target_price = broker_avg_cost - target_dist
            s.hard_stop_price = s.stop_price
        # else: non-FX adoption — leave stop/target zero, time-stop only

        s.entry_pending = False
        s.entry_fill_px = broker_avg_cost
        s.trade_count += 1
        s.account_equity_at_entry = self._get_account_equity()
        s.entry_regime = "ADOPTED_ORPHAN"
        s.sizing_policy = "adopted"
        s.entry_risk_usd = self._risk_usd_for_size(broker_avg_cost, s.stop_price, s.position_size) \
            if s.stop_price > 0 else 0.0
        s.save()

        self._log.warning(
            f"ADOPTED ORPHAN: {broker_dir.upper()} qty={s.position_size} @ {broker_avg_cost:.5f}  "
            f"stop={s.stop_price:.5f}  target={s.target_price:.5f}  "
            f"(synthetic stop/target from config; runner will manage via time-stop and price-watch)"
        )

    def _on_fill(self, trade, fill) -> None:
        """Callback when an order fills. Routes to entry/exit handling."""
        # ── Fill deduplication ──
        fill_id = f"{fill.execution.execId}" if hasattr(fill, 'execution') else str(id(fill))
        if fill_id in self._processed_fill_ids:
            self._log.warning(f"Duplicate fill ignored: {fill_id}")
            return
        self._persist_fill_id(fill_id)

        s = self.state
        order_id = str(getattr(trade.order, 'orderId', ''))
        fill_px = float(getattr(fill, 'price', 0) or 0)
        fill_qty = float(getattr(fill, 'shares', 0) or 0)
        now = datetime.now(timezone.utc)

        self._log.info(
            f"FILL RECEIVED orderId={order_id} px={fill_px} qty={fill_qty} "
            f"symbol={self.symbol}"
        )

        # ── Entry fill (including subsequent partial fills on same order) ──
        if order_id == s.entry_order_id and (s.entry_pending or s.position != "FLAT"):
            s.entry_pending = False
            s.entry_fill_px = fill_px
            direction = getattr(self, '_pending_direction', 'long')
            size = getattr(self, '_pending_size', 0.0)
            stop_px = getattr(self, '_pending_stop_px', 0.0)
            target_px = getattr(self, '_pending_target_px', 0.0)

            s.position = direction.upper()
            s.entry_price = fill_px
            s.avg_entry_price = fill_px
            s.pyramid_adds = 0
            s.entry_time = now
            s.stop_price = stop_px
            s.target_price = target_px
            s.timeout_time = now.replace(second=0, microsecond=0) + timedelta(minutes=self.timeout_min)
            s.last_signal_time = now
            s.trade_count += 1
            s.direction_str = direction
            # Handle partial fills: accumulate if position already open, else first fill
            if s.position_size > 0 and s.entry_price > 0:
                # Subsequent partial fill — accumulate
                old_qty = s.position_size
                s.entry_price = ((old_qty * s.entry_price) + (fill_qty * fill_px)) / (old_qty + fill_qty)
                s.avg_entry_price = s.entry_price
                s.position_size = old_qty + fill_qty
                self._log.info(
                    f"PARTIAL FILL ACCUMULATED: +{fill_qty} -> total={s.position_size} "
                    f"avg_entry={s.entry_price:.5f}"
                )
            else:
                # First fill
                s.position_size = fill_qty
            # Reset trailing/breakeven state for new position
            self._peak_favorable_pips = 0.0
            self._breakeven_activated = False
            if fill_qty != size and size > 0:
                self._log.warning(
                    f"PARTIAL FILL: requested={size} filled={fill_qty} "
                    f"({fill_qty/size:.0%}) — position smaller than intended"
                )
            s.entry_risk_usd = self._risk_usd_for_size(fill_px, stop_px, fill_qty)
            s.sizing_policy = getattr(self, '_pending_sizing_policy', 'dynamic')
            s.account_equity_at_entry = self._get_account_equity()
            s.entry_regime = getattr(self, "_pending_entry_regime", "")
            s.save()

            self._log.info(
                f"ENTRY FILLED {direction.upper()} @ {fill_px:.5f} "
                f"size={size} stop={stop_px:.5f} target={target_px:.5f}"
            )

            # 2026-05-20 BUGFIX: clear from the pending-fills queue now that
            # we've confirmed the fill. Paired with write_pending in
            # _submit_real_entry. Without this clear, the queue accumulates
            # already-filled entries and reconcile_pending sees them as
            # "still pending" on next runner restart, producing spurious
            # ADOPTED ORPHAN events.
            try:
                from helio.pending_fills import clear_pending
                if s.entry_order_id:
                    clear_pending(order_id=s.entry_order_id)
            except Exception as exc:
                self._log.warning(f"pending_fills clear failed (non-fatal): {exc}")

            # Dual-write an ENTRY row to canonical_fills. Mirrors the EXIT
            # dual-write in _log_trade and the submit_bracket ENTRY dual-write
            # on the forge side — without this, the fleet ledger has no entry
            # anchor and orphan detection can't distinguish "took the entry,
            # still open" from "never entered". Stamps lineage_id so the row
            # is attributable by intent (Codex X3+X7). Wrapped in try/except
            # so a broken canonical log cannot break paper trading.
            try:
                from helio.canonical_fills import write_fill_typed
                from helio.domain import Fill, make_lineage_id
                try:
                    import os as _os
                    ib = getattr(self, "_ib", None)
                    client_id = getattr(getattr(ib, "client", None), "clientId", None) if ib else None
                    session_id = (
                        f"c{client_id}p{_os.getpid()}" if client_id is not None
                        else f"p{_os.getpid()}"
                    )
                except Exception:
                    session_id = None
                lineage = make_lineage_id(
                    strategy=f"argus_{self.symbol.lower()}",
                    session_id=session_id,
                    entry_order_id=str(s.entry_order_id) if s.entry_order_id else None,
                )
                write_fill_typed(Fill(
                    strategy=f"argus_{self.symbol.lower()}",
                    symbol=self.symbol,
                    direction=direction.lower(),
                    side="ENTRY",
                    entry_ts=now.isoformat(),
                    exit_ts=None,
                    entry_px=float(fill_px),
                    exit_px=None,
                    size=float(fill_qty),
                    risk_usd=float(s.entry_risk_usd or 0.0),
                    pnl_usd=None,
                    exit_reason=None,
                    lineage_id=lineage,
                ))
            except Exception:
                pass  # never let canonical log break paper trading

            # Submit protective bracket orders
            ok = self._submit_bracket_orders(stop_px, target_px)
            if not ok:
                self._log.critical("BRACKET ORDERS FAILED — position unhedged, submitting emergency exit")
                self._submit_real_exit("bracket_failure", fill_px)
            return

        # ── Exit fill (explicit market exit) ──
        if order_id == s.exit_order_id and s.exit_pending:
            s.exit_pending = False
            s.exit_fill_px = fill_px
            self._exit_retry_count = 0  # reset escalation
            exit_reason = getattr(self, '_pending_exit_reason', 'exit')
            self._finalize_real_exit(fill_px, exit_reason, now)
            return

        # ── Stop fill ──
        if order_id == s.stop_order_id and s.position != "FLAT":
            # 2026-05-21 BUGFIX: partial-fill state-ordering race. Previously
            # ANY stop fill (even partial) triggered _finalize_real_exit
            # which clears trade state — but OCA-cancels the target leg,
            # leaving the remaining position OPEN with no bracket protection.
            # Now: broker-truth check before finalizing. If broker still has
            # position, this was partial — log and wait. _check_bracket_health
            # will re-arm a bracket on the remaining qty within 60s.
            broker_pos = self._read_broker_position()
            if broker_pos is not None and abs(broker_pos) > 0.5:
                self._log.warning(
                    f"PARTIAL_STOP_FILL: stop filled qty={fill_qty} but broker "
                    f"shows {self.symbol} pos={broker_pos} (still open). "
                    f"Waiting for closure. bracket_health will re-arm protection."
                )
                return
            # Stop filled fully — cancel the target order
            self._cancel_order_by_id(s.target_order_id, "target")
            self._finalize_real_exit(fill_px, "stop", now)
            return

        # ── Target fill ──
        if order_id == s.target_order_id and s.position != "FLAT":
            # 2026-05-21 BUGFIX: same partial-fill race as stop above.
            broker_pos = self._read_broker_position()
            if broker_pos is not None and abs(broker_pos) > 0.5:
                self._log.warning(
                    f"PARTIAL_TARGET_FILL: target filled qty={fill_qty} but broker "
                    f"shows {self.symbol} pos={broker_pos} (still open). "
                    f"Waiting for closure. bracket_health will re-arm protection."
                )
                return
            # Target filled fully — cancel the stop order
            self._cancel_order_by_id(s.stop_order_id, "stop")
            self._finalize_real_exit(fill_px, "target", now)
            return

        self._log.warning(
            f"UNMATCHED FILL orderId={order_id} px={fill_px} — "
            f"entry_oid={s.entry_order_id} stop_oid={s.stop_order_id} "
            f"target_oid={s.target_order_id} exit_oid={s.exit_order_id}"
        )

    def _finalize_real_exit(self, fill_px: float, exit_reason: str, now: datetime) -> None:
        """Common exit finalization after a real fill (stop, target, or explicit exit)."""
        s = self.state
        pnl, pnl_usd = self._log_trade(fill_px, exit_reason, now)

        if self.uses_pips:
            s.pnl_pips += pnl
            unit = "pip"
            total = s.pnl_pips
        else:
            s.pnl_points += pnl
            unit = "pts"
            total = s.pnl_points
        s.pnl_usd += pnl_usd

        if hasattr(self, '_risk_mgr'):
            stop_size = self.stop_pips if self.uses_pips else self.stop_bps
            pnl_r = pnl / stop_size if stop_size > 0 else pnl
            self._risk_mgr.record_trade_pnl(self.symbol, pnl_r)

        self._log.info(
            f"REAL EXIT {s.position} @ {fill_px:.5f} reason={exit_reason} "
            f"pnl={pnl:+.2f}{unit} (${pnl_usd:+.2f}) total={total:+.2f}{unit} "
            f"trades={s.trade_count}"
        )

        # AI overlay learning — update voter weights from trade outcome
        if self.uses_pips:
            self._ai_overlay_learn(pnl)
            self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0

        # Flatten state
        s.clear_trade_state()
        s.save()

    _BRACKET_CHECK_INTERVAL_S = 60  # check bracket health every 60s

    def _check_bracket_health(self, now: datetime) -> None:
        """Verify bracket orders (stop/target) are still alive in IBKR.

        TWS restarts cancel all open orders. If stop/target vanish while
        in position, resubmit them immediately.
        """
        last_check = getattr(self, '_last_bracket_check', 0.0)
        if time.time() - last_check < self._BRACKET_CHECK_INTERVAL_S:
            return
        self._last_bracket_check = time.time()

        s = self.state
        if not s.stop_order_id and not s.target_order_id:
            return  # no brackets to check

        ib = getattr(self, '_ib', None)
        if ib is None:
            return

        try:
            open_ids = {str(getattr(t.order, 'orderId', '')) for t in ib.openTrades()}
        except Exception:
            return

        stop_alive = s.stop_order_id in open_ids if s.stop_order_id else True
        target_alive = s.target_order_id in open_ids if s.target_order_id else True

        if not stop_alive or not target_alive:
            missing = []
            if not stop_alive:
                missing.append(f"stop({s.stop_order_id})")
            if not target_alive:
                missing.append(f"target({s.target_order_id})")
            self._log.warning(
                f"BRACKET ORPHANED: {', '.join(missing)} not found in open orders — "
                f"resubmitting brackets for {s.position} position"
            )
            ok = self._submit_bracket_orders(s.stop_price, s.target_price)
            if not ok:
                self._log.critical(
                    f"BRACKET RESUBMIT FAILED — position {s.position} UNHEDGED. "
                    f"Submitting emergency exit."
                )
                mid = self._get_mid()
                if mid:
                    self._submit_real_exit("bracket_orphaned", mid)

    def _check_order_timeouts(self, now: datetime) -> None:
        """Cancel stuck orders after timeout. Entry: 60s, Exit: 30s."""
        s = self.state
        if s.entry_pending and s.entry_submitted_ts:
            # 2026-05-20 BUGFIX: ENTRY-side broker-truth check. Mirror of the
            # exit-side fix from 5/19. The 60s entry timeout was firing AFTER
            # the order had already filled but the execDetails callback hadn't
            # yet arrived (instant-fill races on FX), causing the runner to:
            #   1. Issue a cancel (which fails: order already filled at broker)
            #   2. Clear local state INCLUDING _pending_stop_px/_pending_target_px
            #   3. Later adopt via reconciliation as orphan with SYNTHETIC stops
            #      (2-5× wider than the strategy designed)
            # Observed 24× across 5/19+5/20. Every FX entry hit this path.
            # The fix: query broker FIRST. If position exists, the order
            # filled — adopt it with the strategy-intended stops still in
            # memory at self._pending_stop_px / self._pending_target_px.
            try:
                submitted = datetime.fromisoformat(s.entry_submitted_ts)
                if (now - submitted).total_seconds() > 60:
                    broker_qty = self._read_broker_position()
                    if broker_qty is not None and abs(broker_qty) > 0:
                        # Order filled — execDetails callback was lost. Don't
                        # cancel-and-clear; adopt the position with the
                        # strategy-intended stop/target stashed on self.
                        direction = "long" if broker_qty > 0 else "short"
                        pending_stop = getattr(self, '_pending_stop_px', 0.0)
                        pending_target = getattr(self, '_pending_target_px', 0.0)
                        self._log.warning(
                            f"ENTRY_RACE_RESOLVED: broker has {broker_qty} {self.symbol} — "
                            f"prior entry must have filled. Adopting with intended "
                            f"stop={pending_stop} target={pending_target}."
                        )
                        s.position = direction.upper()
                        s.position_size = abs(broker_qty)
                        s.entry_price = self._get_mid() or s.entry_price or 0.0
                        s.entry_time = now
                        s.entry_pending = False
                        s.stop_price = pending_stop or s.stop_price
                        s.target_price = pending_target or s.target_price
                        s.save()
                        # Submit protective brackets at the intended levels
                        if pending_stop and pending_target:
                            self._submit_bracket_orders(pending_stop, pending_target)
                        return
                    # No broker position → genuine timeout, real cancel + clear
                    self._log.warning(
                        f"ENTRY TIMEOUT: order {s.entry_order_id} pending >60s — cancelling"
                    )
                    self._cancel_order_by_id(s.entry_order_id, "entry_timeout")
                    s.clear_trade_state()
                    s.save()
            except (ValueError, TypeError):
                pass

        if s.exit_pending and s.exit_submitted_ts:
            # 2026-05-19 BUGFIX: cascade race. Caught live when CADJPY's IOC
            # retry (order 1930) filled instantly but the GTC retry fired 60s
            # later without checking, double-selling. Same pattern bit
            # USDJPY (30K → 60K → 120K) + GBPUSD (22K → 45K → 90K) over a
            # 2-hour window. The execDetails callback raced our local state
            # update. Defensive: query broker via the centralized helper
            # before each retry. If broker is flat, our exit succeeded —
            # clear local state and skip retrying.
            broker_qty = self._read_broker_position()
            if broker_qty == 0.0:
                self._log.info(
                    f"EXIT_RACE_RESOLVED: broker shows {self.symbol} FLAT — "
                    f"prior exit must have filled. Clearing local state, skipping retry."
                )
                s.exit_pending = False
                s.position = "FLAT"
                s.position_size = 0
                self._exit_retry_count = 0
                s.save()
                return  # exit the timeout-check entirely

            try:
                submitted = datetime.fromisoformat(s.exit_submitted_ts)
                elapsed = (now - submitted).total_seconds()
                exit_retry_count = getattr(self, '_exit_retry_count', 0)
                if elapsed > 30 and exit_retry_count == 0:
                    self._log.warning(
                        f"EXIT TIMEOUT: order {s.exit_order_id} pending >30s — "
                        f"cancelling and retrying with aggressive MKT"
                    )
                    # 2026-05-18: capture forensics BEFORE cancel so we know what
                    # TWS thinks (orderStatus.whyHeld, trade.log error codes).
                    # Recurring argus_gbpusd EXIT FAILED cascade has no visibility
                    # past "EXIT TIMEOUT" — this surfaces the actual TWS state.
                    self._dump_exit_forensics(s.exit_order_id, stage="initial_timeout_30s")
                    self._cancel_order_by_id(s.exit_order_id, "exit_timeout")
                    # 2026-05-20 BUGFIX: re-check broker AFTER the cancel
                    # sleep. _cancel_order_by_id sleeps 2s and returns
                    # "confirmed" if the order is no longer in openTrades —
                    # but a fill during that 2s window ALSO removes it from
                    # openTrades. Without this re-check, we'd submit a
                    # duplicate exit on a position that's already closed.
                    broker_qty = self._read_broker_position()
                    if broker_qty == 0.0:
                        self._log.info(
                            f"EXIT_FILLED_DURING_CANCEL: broker shows {self.symbol} "
                            f"FLAT after cancel — prior exit filled in 2s sleep window. "
                            f"Skipping IOC retry."
                        )
                        s.exit_pending = False
                        s.position = "FLAT"
                        s.position_size = 0
                        self._exit_retry_count = 0
                        s.save()
                        return
                    # Retry with the right order type for this instrument
                    # (FX => LimitOrder + outsideRth, equity/futures => MarketOrder).
                    # Equity/futures still use IOC tif here for urgency; FX
                    # cannot use IOC (IdealPro rejects it on FX) so the helper
                    # downgrades CASH to DAY.
                    ib = getattr(self, '_ib', None)
                    if ib is not None and s.position != "FLAT":
                        close_action = "SELL" if s.position == "LONG" else "BUY"
                        ref_px = s.entry_price or s.target_price or 0
                        sec_type = getattr(self.contract, "secType", "") or ""
                        retry_tif = "DAY" if sec_type == "CASH" else "IOC"
                        order = self._build_exit_order(
                            close_action, int(abs(s.position_size)), ref_px, tif=retry_tif,
                        )
                        order.account = getattr(self, 'stage_account', '') or ''
                        # 2026-05-21 BUGFIX: pre-allocate orderId. See
                        # _pre_allocate_order_id.
                        retry_pre_id = self._pre_allocate_order_id(ib)
                        if retry_pre_id is not None:
                            order.orderId = retry_pre_id
                            s.exit_order_id = str(retry_pre_id)
                            s.exit_submitted_ts = now.isoformat()
                            s.save()
                            trade = ib.placeOrder(self.contract, order)
                        else:
                            trade = ib.placeOrder(self.contract, order)
                            s.exit_order_id = str(getattr(trade.order, 'orderId', ''))
                            s.exit_submitted_ts = now.isoformat()
                            s.save()
                        self._exit_retry_count = 1
                        self._log.info(
                            f"EXIT RETRY (IOC MKT) orderId={s.exit_order_id}"
                        )
                elif elapsed > 60 and exit_retry_count == 1:
                    # Tertiary: IOC failed too. Try GTC market order.
                    self._log.error(
                        f"EXIT STUCK: IOC retry also failed after 60s — "
                        f"submitting GTC MKT as last resort"
                    )
                    self._dump_exit_forensics(s.exit_order_id, stage="ioc_retry_60s")
                    self._cancel_order_by_id(s.exit_order_id, "exit_stuck")
                    # 2026-05-20 BUGFIX: same fill-during-cancel re-check as
                    # the 30s arm above.
                    broker_qty = self._read_broker_position()
                    if broker_qty == 0.0:
                        self._log.info(
                            f"EXIT_FILLED_DURING_CANCEL: broker shows {self.symbol} "
                            f"FLAT after cancel — IOC retry must have filled in 2s "
                            f"sleep window. Skipping GTC retry."
                        )
                        s.exit_pending = False
                        s.position = "FLAT"
                        s.position_size = 0
                        self._exit_retry_count = 0
                        s.save()
                        return
                    ib = getattr(self, '_ib', None)
                    if ib is not None and s.position != "FLAT":
                        close_action = "SELL" if s.position == "LONG" else "BUY"
                        ref_px = s.entry_price or s.target_price or 0
                        order = self._build_exit_order(
                            close_action, int(abs(s.position_size)), ref_px, tif="GTC",
                        )
                        order.account = getattr(self, 'stage_account', '') or ''
                        # 2026-05-21 BUGFIX: pre-allocate orderId. See
                        # _pre_allocate_order_id.
                        gtc_pre_id = self._pre_allocate_order_id(ib)
                        if gtc_pre_id is not None:
                            order.orderId = gtc_pre_id
                            s.exit_order_id = str(gtc_pre_id)
                            s.exit_submitted_ts = now.isoformat()
                            s.save()
                            trade = ib.placeOrder(self.contract, order)
                        else:
                            trade = ib.placeOrder(self.contract, order)
                            s.exit_order_id = str(getattr(trade.order, 'orderId', ''))
                            s.exit_submitted_ts = now.isoformat()
                            s.save()
                        self._exit_retry_count = 2
                        self._log.info(f"EXIT RETRY (GTC MKT) orderId={s.exit_order_id}")
                elif elapsed > 120 and exit_retry_count >= 2:
                    # All retries exhausted — force FLAT locally + incident
                    self._log.critical(
                        f"EXIT FAILED: all retries exhausted after 120s. "
                        f"Forcing local FLAT — MANUAL BROKER CHECK REQUIRED"
                    )
                    self._dump_exit_forensics(s.exit_order_id, stage="gtc_retry_120s_failed")
                    _write_incident(self, "EXIT_FAILED",
                                    f"All exit retries exhausted, forced local FLAT",
                                    s.position, {"direction": s.position, "qty": s.position_size})
                    s.clear_trade_state()
                    s.save()
                    self._exit_retry_count = 0
            except (ValueError, TypeError):
                pass

    # ── Main tick (called every second) ──────────────────────
    def tick(self, now: datetime) -> None:
        """Process one tick: update bar, check exits, evaluate signals."""
        mid = self._get_mid()
        if mid is None or mid <= 0:
            return

        # Update spread tracker with every tick
        t = self.ticker
        _bid = getattr(t, "bid", None) or getattr(t, "delayedBid", None)
        _ask = getattr(t, "ask", None) or getattr(t, "delayedAsk", None)
        if _bid and _ask and _bid > 0 and _ask > 0:
            self._spread_tracker.update(float(_bid), float(_ask))

        vol = self._get_volume()
        bar_minute = now.replace(second=0, microsecond=0)
        new_bar_closed = False

        # ── Bar construction ─────────────────────────────────
        if self.current_bar_minute is None:
            self.current_bar_minute = bar_minute
            self.current_bar = {
                "open": mid, "high": mid, "low": mid, "close": mid, "volume": vol, "ticks": 1,
            }
        elif bar_minute > self.current_bar_minute:
            # Close prior bar, push to buffer
            self.current_bar["ts"] = str(self.current_bar_minute)
            self.buf.add(self.current_bar)
            self._mtf.on_bar_close(self.current_bar)
            self._mtf_engine.on_bar({"t": self.current_bar.get("ts"), **self.current_bar})
            # Feed MTF Trend Strategy engine (resamples 1m -> 5m/1H/4H internally)
            if self._mtf_strategy is not None:
                self._mtf_strategy.update_bar(self.current_bar)
            self._bars_since_last_trade += 1
            new_bar_closed = True
            self.current_bar_minute = bar_minute
            self.current_bar = {
                "open": mid, "high": mid, "low": mid, "close": mid, "volume": vol, "ticks": 1,
            }
        else:
            self.current_bar["high"] = max(self.current_bar["high"], mid)
            self.current_bar["low"] = min(self.current_bar["low"], mid)
            self.current_bar["close"] = mid
            self.current_bar["ticks"] = self.current_bar.get("ticks", 0) + 1
            if vol > 0:
                self.current_bar["volume"] = self.current_bar.get("volume", 0) + vol  # accumulate, not overwrite

        # ── Order timeout check (any real-order venue: paper-account or live) ────────
        # 2026-04-24: "paper" stage now submits real orders to the IBKR paper
        # account, so order lifecycle (timeouts, bracket health, pending gating)
        # must run for paper too. Previously gated only on execution_mode=="real".
        s = self.state
        if self.execution_mode in ("real", "paper"):
            self._check_order_timeouts(now)
            # Check if bracket orders (stop/target) are still alive
            if s.position != "FLAT" and not s.exit_pending:
                self._check_bracket_health(now)
            # While orders are pending, skip normal paper stop/target checks
            if s.entry_pending or s.exit_pending:
                return

        # ── Position management (every tick) ─────────────────
        if s.position != "FLAT":
            exit_reason = None

            if s.position == "LONG":
                if mid <= s.stop_price:
                    exit_reason = "stop"
                elif mid >= s.target_price:
                    exit_reason = "target"
            elif s.position == "SHORT":
                if mid >= s.stop_price:
                    exit_reason = "stop"
                elif mid <= s.target_price:
                    exit_reason = "target"

            if s.timeout_time and now >= s.timeout_time:
                exit_reason = "timeout"

            # Pre-IBKR-maintenance flatten: IBKR drops the socket at 23:45 UTC
            # nightly. Any position open through that disconnect taints the next
            # trade as `restored_from_file`. For paper QA we'd rather close
            # 15min early than burn a cohort slot on a tainted entry. Real
            # configs should NOT flatten — they have proper reconnect handling.
            if (not exit_reason
                and self.deployment_stage in ("paper", "watcher")
                and self.instrument_type == "forex"
                and now.hour == 23 and now.minute >= 30):
                exit_reason = "pre_disconnect_flatten"

            # ── Trailing stop: move stop to breakeven after 1R ──
            if s.position != "FLAT" and not exit_reason:
                self._check_trailing_stop(mid)

            if exit_reason and self.execution_mode in ("real", "paper") and not s.exit_pending:
                # Market-hours guard: don't submit orders into closed FX market
                if self.instrument_type == "forex" and not _fx_market_open(now):
                    if not getattr(self, '_market_closed_warned', False):
                        self._log.warning(
                            f"EXIT DEFERRED: {exit_reason} for {self.symbol} — "
                            f"FX market closed, will execute at open"
                        )
                        self._market_closed_warned = True
                    return  # defer until market reopens
                self._market_closed_warned = False
                # Real execution: submit exit order, don't log trade yet
                self._submit_real_exit(exit_reason, mid)
                return  # wait for fill callback
            elif exit_reason:
                # Paper execution: immediate fill simulation
                pnl, pnl_usd = self._log_trade(mid, exit_reason, now)
                if self.uses_pips:
                    s.pnl_pips += pnl
                    unit = "pip"
                    total = s.pnl_pips
                else:
                    s.pnl_points += pnl
                    unit = "pts"
                    total = s.pnl_points
                s.pnl_usd += pnl_usd
                # Record trade PnL in R-multiples for daily risk limits
                if hasattr(self, '_risk_mgr'):
                    stop_size = self.stop_pips if self.uses_pips else self.stop_bps
                    pnl_r = pnl / stop_size if stop_size > 0 else pnl
                    self._risk_mgr.record_trade_pnl(self.symbol, pnl_r)
                self._log.info(
                    f"EXIT {s.position} @ {mid} reason={exit_reason} "
                    f"pnl={pnl:+.2f}{unit} (${pnl_usd:+.2f}) total={total:+.2f}{unit} "
                    f"trades={s.trade_count}"
                )
                # AI overlay learning — update voter weights from trade outcome
                if self.uses_pips:
                    self._ai_overlay_learn(pnl)
                    self._consecutive_losses = self._consecutive_losses + 1 if pnl < 0 else 0
                s.clear_trade_state()
                s.save()
            else:
                # ── Pyramiding: scale-in on confirmed move ────
                self._check_pyramid(mid, now)
            return  # don't evaluate new signals while in position

        # ── Signal evaluation (once per new bar, when FLAT) ──
        if not new_bar_closed:
            # Also allow eval if we haven't evaluated this minute yet
            if self._last_eval_minute == bar_minute:
                return
        if len(self.buf) < 60:
            return

        # Higher-TF eval gate: only evaluate on timeframe boundary
        if self._eval_tf_minutes > 1 and bar_minute is not None:
            tf_boundary = bar_minute.replace(minute=(bar_minute.minute // self._eval_tf_minutes) * self._eval_tf_minutes, second=0, microsecond=0)
            if tf_boundary == self._last_eval_tf_boundary:
                return
            self._last_eval_tf_boundary = tf_boundary

        self._last_eval_minute = bar_minute
        features = self._compute_features()
        if features is None:
            return

        # MTF Trend Strategy: use 4H/1H/5M engine instead of range_accel trigger
        if self._mtf_strategy is not None:
            mtf_signal = self._mtf_strategy.evaluate(mid)
            if mtf_signal is not None and mtf_signal.confidence >= self._mtf_min_confidence:
                direction = mtf_signal.direction
                features["mtf_trend_4h"] = mtf_signal.trend_4h
                features["mtf_setup_1h"] = mtf_signal.setup_1h
                features["mtf_trigger_5m"] = mtf_signal.trigger_5m
                features["mtf_confidence"] = mtf_signal.confidence
                features["mtf_reason"] = mtf_signal.reason
                features["mtf_support"] = mtf_signal.support
                features["mtf_resistance"] = mtf_signal.resistance
                self._log.info(f"MTF SIGNAL: {direction} {mtf_signal.reason}")

                # ── AI Overlay gate ──────────────────────────────
                # 2026-05-22: env-var bypass. The overlay was calibrated
                # against tighter MTF candidates; once paper_stress_multiplier
                # loosens MTF thresholds the overlay rejects most candidates
                # (3-of-3 SKIPs observed 2026-05-22). Set
                # ARGUS_DISABLE_AI_OVERLAY=1 to short-circuit during the
                # exercise window; re-enable + retune post-5/31 reset.
                _ai_disabled = os.environ.get("ARGUS_DISABLE_AI_OVERLAY", "").strip() in ("1", "true", "yes")
                if self._ai_overlay is not None and direction is not None and not _ai_disabled:
                    from argus_flow.strategies.ai_overlay import MarketState
                    ai_state = MarketState(
                        price=mid,
                        atr_14=features.get("atr_14", 0.0),
                        rsi_14=features.get("rsi_14", 50.0),
                        spread_pips=getattr(self, '_last_spread_pips', 1.0),
                        volume_ratio=features.get("vol_z", 0.0) + 1.0,
                        dist_from_high_20=1.0 - features.get("dist_from_low", 0.5),
                        dist_from_low_20=features.get("dist_from_low", 0.5),
                        ema_8_slope=features.get("trend_strength", 0.0),
                        ema_21_slope=features.get("efficiency_ratio", 0.0),
                        hour=features.get("hour", 12),
                        day_of_week=now.weekday(),
                        bars_since_last_trade=getattr(self, '_bars_since_last_trade', 999),
                        recent_win_rate=self._recent_win_rate(),
                        recent_pnl=self.state.pnl_pips,
                        consecutive_losses=getattr(self, '_consecutive_losses', 0),
                        trend_strength_4h=abs(mtf_signal.ema_8_4h - mtf_signal.ema_21_4h) / self.pip_size if self.pip_size > 0 else 0.0,
                        rsi_1h=mtf_signal.rsi_1h,
                        confidence_mtf=mtf_signal.confidence,
                    )
                    overlay_decision = self._ai_overlay.evaluate(direction, ai_state)
                    features["ai_action"] = overlay_decision.action
                    features["ai_consensus"] = overlay_decision.consensus_score
                    features["ai_confidence"] = overlay_decision.confidence
                    features["ai_for"] = overlay_decision.voters_for
                    features["ai_against"] = overlay_decision.voters_against
                    if overlay_decision.action == "SKIP":
                        self._log.info(
                            f"AI OVERLAY SKIP: consensus={overlay_decision.consensus_score:+.2f} "
                            f"({overlay_decision.voters_for}v{overlay_decision.voters_against}) | {overlay_decision.reason}"
                        )
                        self._log_signal(features, direction, "AI_OVERLAY_SKIP")
                        direction = None
                    elif direction and getattr(self, '_llm_reasoner', None) is not None:
                        # LLM second opinion (non-blocking, optional)
                        try:
                            llm_opinion = self._llm_reasoner.evaluate(
                                direction=direction,
                                market_state={
                                    "price": mid, "rsi_14": features.get("rsi_14", 50),
                                    "rsi_1h": mtf_signal.rsi_1h, "atr_14": features.get("atr_14", 0),
                                    "spread_pips": getattr(self, '_last_spread_pips', 1.0),
                                    "volume_ratio": features.get("vol_z", 0) + 1.0,
                                    "hour": features.get("hour", 12), "day_of_week": now.weekday(),
                                    "recent_wr": self._recent_win_rate(),
                                    "consecutive_losses": getattr(self, '_consecutive_losses', 0),
                                },
                                mtf_info={
                                    "trend_4h": mtf_signal.trend_4h, "setup_1h": mtf_signal.setup_1h,
                                    "trigger_5m": mtf_signal.trigger_5m, "confidence": mtf_signal.confidence,
                                },
                                overlay_info={
                                    "consensus": overlay_decision.consensus_score,
                                    "voters_for": overlay_decision.voters_for,
                                    "voters_against": overlay_decision.voters_against,
                                    "top_reasons": overlay_decision.reason[:100],
                                },
                            )
                            features["llm_action"] = llm_opinion.action
                            features["llm_confidence"] = llm_opinion.confidence
                            features["llm_latency_ms"] = llm_opinion.latency_ms
                            if llm_opinion.available and llm_opinion.action == "SKIP" and llm_opinion.confidence >= 70:
                                self._log.info(f"LLM VETO: {llm_opinion.reasoning}")
                                self._log_signal(features, direction, "LLM_VETO")
                                direction = None
                        except Exception as _llm_err:
                            pass  # LLM failure never blocks trading
            else:
                direction = None
                if mtf_signal:
                    features["mtf_blocked"] = f"low_conf={mtf_signal.confidence:.0%}"
        else:
            direction = self._check_trigger(features)

        # ── Hour filter gate (backtested +144% PnL) ──────────
        if direction and self._profitable_hours is not None:
            current_hour = now.hour
            if current_hour not in self._profitable_hours:
                self._log_signal(features, direction, "HOUR_FILTERED")
                direction = None

        # ── Entry confirmation gate (backtested +37% PF) ────
        # Shorts need extra confirmation (live data: shorts PF 0.52)
        _required_confirms = self._confirm_bars_short if direction == "short" else self._confirm_bars_long
        if direction and _required_confirms > 0:
            if self._pending_entry is None:
                # First signal — start confirmation countdown
                self._pending_entry = {
                    "direction": direction,
                    "bars_confirmed": 0,
                    "trigger_price": mid,
                }
                self._log_signal(features, direction, "PENDING_CONFIRM")
                direction = None  # don't enter yet
            else:
                pe = self._pending_entry
                if pe["direction"] != direction:
                    # Direction changed — reset
                    self._pending_entry = {
                        "direction": direction,
                        "bars_confirmed": 0,
                        "trigger_price": mid,
                    }
                    self._log_signal(features, direction, "PENDING_CONFIRM_RESET")
                    direction = None
                else:
                    # Check if price confirms: moving in the entry direction
                    prev_close = float(self.buf.last()["close"]) if self.buf.last() else mid
                    confirmed = False
                    if direction == "long" and mid > pe["trigger_price"]:
                        confirmed = True
                    elif direction == "short" and mid < pe["trigger_price"]:
                        confirmed = True

                    if confirmed:
                        pe["bars_confirmed"] += 1
                        pe["trigger_price"] = mid
                        if pe["bars_confirmed"] >= _required_confirms:
                            # Confirmed — allow entry to proceed
                            self._pending_entry = None
                            # direction stays set
                        else:
                            self._log_signal(features, direction, f"CONFIRMING_{pe['bars_confirmed']}/{_required_confirms}")
                            direction = None
                    else:
                        # Price didn't confirm — kill the pending
                        self._pending_entry = None
                        self._log_signal(features, direction, "CONFIRM_FAILED")
                        direction = None
        elif not direction:
            # No trigger — clear any pending confirmation
            self._pending_entry = None

        # ── Stale ticker gate ────────────────────────────────
        if direction and not self._check_ticker_staleness():
            self._log_signal(features, direction, "STALE_TICKER_BLOCKED")
            direction = None

        # ── Gate decision tracking (for brain visualization) ──
        _trigger_fired = direction is not None
        _gate_log = {
            "ts": now.isoformat(),
            "symbol": self.symbol,
            "trigger": "PASS" if _trigger_fired else "NO_SIGNAL",
            "direction": direction or "",
            "session": "PASS",
            "range_pct": "PASS" if _trigger_fired else "FAIL",
            "regime": "PASS",
            "maintenance": "PASS",
            "mtf": "PASS",
            "spread": "PASS",
            "news": "PASS",
            "sequencing": "PASS",
            "conviction": features.get("conviction_score", 0) if _trigger_fired else 0,
            "sizing": "PASS",
            "risk_gate": "PASS",
            "final": "PENDING",
        }

        # ── Regime gate ──────────────────────────────────────
        # Stamps every signal with regime. In GATE mode, blocks entries
        # when regime doesn't match strategy type.
        regime = features.get("regime", "UNKNOWN")
        regime_mode = self.cfg.get("regime_gate", "LOG_ONLY")  # LOG_ONLY | GATE
        if direction and regime_mode == "GATE":
            strategy = self.cfg.get("strategy", "range_accel")
            regime_ok = self._regime_compatible(regime, strategy)
            if not regime_ok:
                self._log.info(
                    f"REGIME_BLOCK {direction.upper()} | regime={regime} "
                    f"strategy={strategy} eff={features.get('efficiency_ratio', 0):.3f} "
                    f"trend={features.get('trend_strength', 0):.3f}"
                )
                self._log_signal(features, direction, "REGIME_BLOCKED")
                _gate_log["regime"] = "FAIL"
                direction = None

        # ── Maintenance blackout (TWS restart window) ────
        # Block new entries during TWS restart period to avoid tainted trades.
        # Conservative window: 01:30-04:30 UTC covers both CDT and CST restart times.
        if direction:
            h_utc = now.hour
            m_utc = now.minute
            utc_minutes = h_utc * 60 + m_utc
            blackout_start = 3 * 60 + 0    # 03:00 UTC (actual TWS restart)
            blackout_end = 3 * 60 + 45     # 03:45 UTC (was 01:30-04:30, blocked 3hrs unnecessarily)
            if blackout_start <= utc_minutes <= blackout_end:
                self._log.info(f"MAINTENANCE_BLACKOUT: {direction.upper()} blocked during TWS restart window")
                self._log_signal(features, direction, "MAINTENANCE_BLACKOUT")
                _gate_log["maintenance"] = "FAIL"
                direction = None

        # ── Advanced gates (multi-timeframe, spread, news, session, sequencing) ──
        if direction:
            from argus_flow.advanced_features import check_news_filter, check_entry_sequencing, compute_conviction_score

            # 1. Multi-timeframe confirmation (LOG_ONLY — shadow mode)
            # Stamps features but does NOT block entries until proven with 60+ trades
            mtf_data = self._mtf.get_multi_trend()
            features["mtf_alignment"] = mtf_data.get("alignment", "neutral")
            features["mtf_score"] = mtf_data.get("alignment_score", 0)
            features["bias_4h"] = mtf_data.get("timeframes", {}).get("4h", {}).get("direction", "neutral")
            if self._mtf_enabled:
                mtf_ok, mtf_reason = self._mtf.check_entry_alignment(direction)
                if not mtf_ok:
                    # Shadow log — would have blocked, but LOG_ONLY
                    self._log.info(f"MTF_SHADOW_BLOCK {direction.upper()} | {mtf_reason}")
                    features["mtf_shadow_blocked"] = True
                    _gate_log["mtf"] = "SHADOW"

        # 1b. New MTF Analysis Engine — HARD GATE (blocks counter-trend entries)
        if direction:
            try:
                mtf_result = self._mtf_engine.analyze(mid)
                features["mtf_bias"] = mtf_result.directional_bias
                features["mtf_confidence"] = round(mtf_result.confidence, 3)
                features["mtf_consensus"] = round(mtf_result.trend_consensus_score, 3)
                features["mtf_4h"] = mtf_result.trend_4h.direction
                features["mtf_1h"] = mtf_result.trend_1h.direction
                features["mtf_rsi"] = round(mtf_result.rsi_14, 1)
                features["mtf_bb_pos"] = round(mtf_result.bb_position, 2)
                features["mtf_vwap_pos"] = mtf_result.vwap_position
                features["mtf_at_support"] = mtf_result.at_support
                features["mtf_at_resistance"] = mtf_result.at_resistance
                features["mtf_patterns"] = len(mtf_result.active_patterns)
                features["mtf_breakout"] = mtf_result.breakout.is_breakout

                # Smart gate: S/R filter + RSI confirmation (backtested +105% improvement)
                # Block longs not near support / shorts not near resistance (S/R filter)
                # Block counter-4H-trend when RSI doesn't confirm reversal (RSI confirm)
                blocked = False
                block_reason = ""

                # S/R filter: longs should be near support + BB lower half, shorts near resistance + BB upper half
                if direction == "long" and not mtf_result.at_support and mtf_result.bb_position > 0.3:
                    blocked = True
                    block_reason = "MTF_LONG_NOT_AT_SUPPORT"
                elif direction == "short" and not mtf_result.at_resistance and mtf_result.bb_position < 0.7:
                    blocked = True
                    block_reason = "MTF_SHORT_NOT_AT_RESISTANCE"

                # RSI confirmation: block counter-trend if 4H strong AND RSI doesn't confirm reversal
                if not blocked:
                    if (direction == "short" and mtf_result.trend_4h.direction == "up"
                            and mtf_result.trend_4h.strength >= 0.6 and mtf_result.rsi_14 < 60):
                        blocked = True
                        block_reason = "MTF_SHORT_COUNTER_TREND_NO_RSI"
                    elif (direction == "long" and mtf_result.trend_4h.direction == "down"
                            and mtf_result.trend_4h.strength >= 0.6 and mtf_result.rsi_14 > 40):
                        blocked = True
                        block_reason = "MTF_LONG_COUNTER_TREND_NO_RSI"

                if blocked:
                    self._log.info(
                        f"MTF_BLOCK {direction.upper()} | {block_reason} "
                        f"bias={mtf_result.directional_bias} conf={mtf_result.confidence:.2f} "
                        f"4h={mtf_result.trend_4h.direction} 1h={mtf_result.trend_1h.direction}"
                    )
                    self._log_signal(features, direction, f"MTF_BLOCKED_{block_reason}")
                    _gate_log["mtf_engine"] = "BLOCKED"
                    direction = None
                else:
                    _gate_log["mtf_engine"] = "PASS"
            except Exception as _mtf_err:
                self._log.warning(f"MTF engine error: {_mtf_err}")
                _gate_log["mtf_engine"] = "ERROR"

        if direction:
            # 2. Spread gate
            if self._spread_gate_enabled:
                spread_ok, spread_reason = self._spread_tracker.check_entry(self._max_spread_ratio)
                features["spread_ratio"] = self._spread_tracker.spread_ratio
                features["spread_current"] = self._spread_tracker.current_spread
                if not spread_ok:
                    self._log.info(f"SPREAD_BLOCK {direction.upper()} | {spread_reason}")
                    self._log_signal(features, direction, "SPREAD_BLOCKED")
                    _gate_log["spread"] = "FAIL"
                    direction = None

        if direction:
            # 3. News/economic calendar filter
            if self._news_filter_enabled:
                news_ok, news_reason = check_news_filter(self.symbol, now)
                if not news_ok:
                    self._log.info(f"NEWS_BLOCK {direction.upper()} | {news_reason}")
                    self._log_signal(features, direction, "NEWS_BLOCKED")
                    _gate_log["news"] = "FAIL"
                    direction = None

        if direction:
            # 4. Adaptive session scoring (log-only by default, set min_score > 0 to gate)
            session_score = self._session_scorer.score_hour(now.hour)
            features["session_score"] = session_score["score"]
            features["session_label"] = session_score["label"]

        if direction:
            # 5. Correlation-aware entry sequencing
            if self._sequencing_gap_minutes > 0 and hasattr(self, '_all_instruments'):
                seq_ok, seq_reason = check_entry_sequencing(
                    self.symbol, direction, getattr(self, '_all_instruments', []),
                    min_gap_minutes=self._sequencing_gap_minutes,
                )
                if not seq_ok:
                    self._log.info(f"SEQ_BLOCK {direction.upper()} | {seq_reason}")
                    self._log_signal(features, direction, "SEQUENCING_BLOCKED")
                    _gate_log["sequencing"] = "FAIL"
                    direction = None

        # Min gap between signals
        if direction and s.last_signal_time:
            gap = (now - s.last_signal_time).total_seconds() / 60
            if gap < self.min_gap:
                direction = None

        # Block new entries if reconciliation requires recovery
        if direction and self.trade_enabled and getattr(self, '_entries_blocked', False):
            block_reason = str(getattr(self, "_entries_block_reason", "") or "ENTRY_BLOCKED")
            self._log.warning(f"Entry BLOCKED ({direction}) -- {block_reason}")
            self._log_signal(features, direction, f"RUNTIME_BLOCKED_{block_reason}")
            _gate_log["risk_gate"] = "FAIL"
            direction = None

        # Write gate decision log for brain visualization
        if _trigger_fired:
            _gate_log["final"] = "ENTRY" if direction else "BLOCKED"
            _gate_log["conviction"] = features.get("conviction_score", 0)
            try:
                _gf = self.log_dir / "gate_decisions.json"
                atomic_write_json(_gf, _gate_log)
            except Exception:
                pass

        if direction and not self.trade_enabled:
            s.last_signal_time = now
            self._log_signal(features, direction, "ENTRY")
            self._log.info(
                f"{self.deployment_stage.upper()} OBSERVE {direction.upper()} @ {mid} "
                f"| execution_mode={self.execution_mode} | no trade executed"
            )
            return

        # Compute conviction score (LOG_ONLY — equal-weighted, not used for sizing yet)
        if direction:
            mtf_data = self._mtf.get_multi_trend()
            features["conviction_score"] = compute_conviction_score(features, direction, mtf_data)

        entry_plan = None
        if direction:
            entry_px = mid
            stop_px, target_px = self._compute_stops(entry_px, direction)
            size, risk_usd, sizing_policy = self._resolve_position_size(entry_px, stop_px)
            if size <= 0:
                eq = self._get_account_equity()
                self._log.warning(
                    f"SIZING_DEBUG: size=0 entry={entry_px} stop={stop_px} "
                    f"equity={eq} stage={self.deployment_stage} "
                    f"risk_pct={self.risk_pct} stop_pips={self.stop_pips} "
                    f"lot={self.lot_size} min={self.min_lot_size} max={self.max_lot_size} "
                    f"policy={sizing_policy} usd_jpy_ref={self._reference_usd_jpy()}"
                )

            # Drawdown-scaled sizing: linear ramp-down with floor at 0.2x.
            # Hard pauses are enforced in the portfolio risk gate so a manual
            # RESET_DRAWDOWN/session reset can clear them before sizing is zeroed.
            if hasattr(self, '_risk_mgr') and self._risk_mgr._peak_pnl > 0 and not self._risk_mgr._drawdown_pause:
                dd = (self._risk_mgr._peak_pnl - self._risk_mgr._current_pnl) / abs(self._risk_mgr._peak_pnl)
                if dd > 0 and self._risk_mgr.max_drawdown_pct > 0:
                    dd_mult = max(0.2, 1.0 - (dd / self._risk_mgr.max_drawdown_pct))
                    size = int(size * dd_mult)
                    if dd_mult < 1.0:
                        self._log.info(f"DD_RAMP: dd={dd:.1%} mult={dd_mult:.2f} size={size}")

            # Minimum trade size floor
            min_size = self.min_lot_size if self.uses_pips else 1
            if 0 < size < min_size:
                self._log.info(f"SIZE_BELOW_FLOOR: {size} < min {min_size}")
                self._log_signal(features, direction, "SIZE_BELOW_FLOOR")
                _gate_log["sizing"] = "FAIL"
                size = 0

            if size <= 0:
                self._log.info(f"SIZE_BLOCK {direction.upper()} | size=0 policy={sizing_policy}")
                self._log_signal(features, direction, "RISK_BLOCKED_SIZE_ZERO")
                _gate_log["sizing"] = "FAIL"
                direction = None
            else:
                entry_plan = {
                    "entry_px": entry_px,
                    "stop_px": stop_px,
                    "target_px": target_px,
                    "size": size,
                    "risk_usd": risk_usd,
                    "sizing_policy": sizing_policy,
                }

        # ── Portfolio risk gate ─────────────────────────────
        if direction and hasattr(self, '_risk_mgr'):
            allowed, reason = self._risk_mgr.can_enter(
                self.symbol,
                direction,
                getattr(self, '_all_instruments', []),
                candidate_risk_usd=entry_plan["risk_usd"] if entry_plan else 0.0,
            )
            if not allowed:
                self._log.info(f"RISK_BLOCK {direction.upper()} | reason={reason}")
                self._log_signal(features, direction, f"RISK_BLOCKED_{reason}")
                _gate_log["risk_gate"] = "FAIL"
                direction = None

        if direction:
            entry_px = entry_plan["entry_px"]
            stop_px = entry_plan["stop_px"]
            target_px = entry_plan["target_px"]

            if self.execution_mode in ("real", "paper"):
                # 2026-04-24: "paper" stage now also submits real orders (to the
                # IBKR paper account, port 7497). Previously "paper" fell through
                # to the immediate-fill simulation below — which never hit the
                # broker, leaving the real account balance flat.
                if s.entry_pending:
                    self._log.warning("Entry already pending — skipping duplicate")
                    return
                self._pending_sizing_policy = entry_plan["sizing_policy"]
                self._pending_entry_regime = features.get("regime", "")
                ok = self._submit_real_entry(
                    direction, entry_plan["size"], stop_px, target_px,
                )
                if not ok:
                    self._log.warning(f"REAL_ENTRY SUBMIT FAILED — staying FLAT")
                    s.clear_trade_state()
                    self._log_signal(features, direction, "REAL_ENTRY_FAILED")
                    return
                # Don't set position yet — _on_fill will handle it
                s.last_signal_time = now
                self._log_signal(features, direction, "ENTRY_SUBMITTED")
                self._log.info(
                    f"ENTRY SUBMITTED {direction.upper()} @ ~{entry_px} "
                    f"stop={stop_px} target={target_px} "
                    f"size={entry_plan['size']:.0f} risk=${entry_plan['risk_usd']:.2f}"
                )
                return

            # Simulation fallback — retained for execution_mode="observe" (watcher stage)
            # or any future mode that should track locally without hitting a broker.
            s.position = direction.upper()
            s.entry_price = entry_px
            s.avg_entry_price = entry_px
            s.pyramid_adds = 0
            s.entry_time = now
            s.stop_price = stop_px
            s.target_price = target_px
            s.timeout_time = now.replace(second=0, microsecond=0) + timedelta(minutes=self.timeout_min)
            s.last_signal_time = now
            s.trade_count += 1
            s.direction_str = direction
            s.position_size = entry_plan["size"]
            s.entry_risk_usd = entry_plan["risk_usd"]
            s.sizing_policy = entry_plan["sizing_policy"]
            # Reset trailing/breakeven state for new position
            self._peak_favorable_pips = 0.0
            self._breakeven_activated = False
            s.account_equity_at_entry = self._get_account_equity()
            s.entry_regime = features.get("regime", "")

            # Capture entry features for trade-close stamping + execution quality
            self._entry_features = {
                "mtf_score": features.get("mtf_score", 0),
                "mtf_alignment": features.get("mtf_alignment", "neutral"),
                "session_score": features.get("session_score", 0),
                "session_label": features.get("session_label", ""),
                "spread_ratio": features.get("spread_ratio", 0),
                "conviction_score": features.get("conviction_score", 0),
                "bias_4h": features.get("bias_4h", "neutral"),
                "signal_mid": mid,
                "signal_ts": now.isoformat(),
            }
            s.save()

            # Capture spread at entry for toxicity analysis
            t = self.ticker
            bid = getattr(t, "bid", None) or getattr(t, "delayedBid", None)
            ask = getattr(t, "ask", None) or getattr(t, "delayedAsk", None)
            if bid and ask and bid > 0 and ask > 0:
                spread_pips = (ask - bid) / self.pip_size if self.uses_pips else (ask - bid)
                features["entry_spread"] = round(spread_pips, 2)
                features["entry_bid"] = bid
                features["entry_ask"] = ask
            else:
                features["entry_spread"] = 0
                features["entry_bid"] = 0
                features["entry_ask"] = 0

            self._log_signal(features, direction, "ENTRY")

            extra = ""
            if not self.uses_pips:
                extra = f" vol_burst={features.get('vol_burst_z', 0):.2f}"
            spread_str = f" spread={features.get('entry_spread', 0):.1f}pip" if self.uses_pips else ""
            self._log.info(
                f"ENTRY {direction.upper()} @ {entry_px} "
                f"stop={stop_px} target={target_px} "
                f"size={s.position_size:.0f} risk=${s.entry_risk_usd:.2f} policy={s.sizing_policy} "
                f"rng={features['range_pct']:.4f} accel={features['range_accel']:.3f}"
                f" regime={features.get('regime', '?')} eff={features.get('efficiency_ratio', 0):.3f}"
                f"{spread_str}{extra}"
            )
        else:
            # Periodic NO_TRIGGER log every 5 minutes
            if now.minute % 5 == 0 and now.second < 2:
                self._log_signal(features, None, "NO_TRIGGER")

    # ── Seed historical bars ───────────────────────────��─────
    def seed(self, ib: IB) -> None:
        """Load 5D of 1-min history to bootstrap feature computation."""
        what = "MIDPOINT" if self.instrument_type == "forex" else "TRADES"
        try:
            bars = ib.reqHistoricalData(
                self.contract,
                endDateTime="",
                durationStr="5 D",
                barSizeSetting="1 min",
                whatToShow=what,
                useRTH=False,
            )
        except Exception as e:
            self._log.warning(f"Historical data request failed: {e}")
            bars = []
        seed_rows = []
        for b in bars:
            raw_volume = getattr(b, "volume", 0)
            try:
                volume = float(raw_volume)
            except (TypeError, ValueError):
                volume = 0.0
            if volume < 0:
                volume = 0.0
            raw_ticks = getattr(b, "barCount", 0)
            try:
                ticks = int(raw_ticks)
            except (TypeError, ValueError):
                ticks = 0
            row = {
                "ts": str(b.date),
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": volume,
                "ticks": max(ticks, 0),
            }
            self.buf.add(row)
            seed_rows.append(row)

        # Seed MTF analysis engine with full history
        if seed_rows:
            try:
                import pandas as _pd
                _seed_df = _pd.DataFrame(seed_rows)
                _seed_df["ts"] = _pd.to_datetime(_seed_df["ts"], utc=True)
                _seed_df = _seed_df.set_index("ts").sort_index()
                self._mtf_engine.on_seed(_seed_df)
            except Exception as _e:
                self._log.warning(f"MTF engine seed failed: {_e}")

        # Seed MTF Trend Strategy engine with historical bars
        if self._mtf_strategy is not None and seed_rows:
            self._mtf_strategy.seed(seed_rows)
            self._log.info(f"MTF Trend Strategy seeded with {len(seed_rows)} bars")

        self._log.info(f"Seeded {len(self.buf)} bars")


# ═════════════════════════════════════════════════════════════
# Contract creation helpers
# ═════════════════════════════════════════════════════════════
def create_contract(cfg: dict):
    """Create ib_insync contract from config dict."""
    itype = cfg.get("instrument_type", "forex")
    sym = cfg["symbol"]

    if itype == "forex":
        return Forex(sym)
    elif itype == "future":
        return Future(
            symbol=sym,
            exchange=cfg.get("exchange", "CME"),
            lastTradeDateOrContractMonth=cfg.get("expiry", ""),
        )
    elif itype == "crypto":
        # Crypto CFDs via IBKR use the Crypto contract type if available,
        # but ib_insync may not have it. Fall back to generic Forex-style.
        # For IBKR paper, crypto pairs are typically not available — use
        # bps-based stops like futures.
        try:
            from ib_insync import Crypto
            return Crypto(sym, currency="USD")
        except ImportError:
            # Fallback: treat as forex pair  sym + "USD"
            return Forex(sym + "USD")
    else:
        raise ValueError(f"Unknown instrument_type: {itype} for {sym}")


def log_dir_for(cfg: dict) -> Path:
    """Derive per-instrument log directory from deployment stage/config metadata."""
    deployment = cfg.get("deployment", {}) if isinstance(cfg.get("deployment", {}), dict) else {}
    raw = str(deployment.get("log_dir", "") or "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else REPO / path
    config_path = Path(str(cfg.get("__config_path__", "") or ""))
    if not config_path.is_absolute():
        config_path = (REPO / config_path).resolve() if str(config_path) else REPO / "argus_flow" / "configs" / f"{cfg['symbol'].lower()}.json"
    return REPO / resolve_log_dir(cfg, config_path)


# =================================================================
# Runtime state enum -- replaces scattered booleans
# =================================================================
class RuntimeMode:
    """Process-level runtime state."""
    BOOTING = "BOOTING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    DEGRADED = "DEGRADED"       # at least one instrument quarantined
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"  # unresolved broker mismatch


class ReconcileResult:
    """Per-instrument broker reconciliation outcome."""
    CLEAN_FLAT = "CLEAN_FLAT"
    CLEAN_OPEN_MATCHED = "CLEAN_OPEN_MATCHED"
    LOCAL_FLAT_BROKER_OPEN = "LOCAL_FLAT_BROKER_OPEN"
    LOCAL_OPEN_BROKER_FLAT = "LOCAL_OPEN_BROKER_FLAT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    UNRESOLVED = "UNRESOLVED"


# =================================================================
# Broker reconciliation gate
# =================================================================
def _normalize_ib_key(contract) -> str:
    """Normalize IB contract to canonical instrument key."""
    sec_type = getattr(contract, "secType", "")
    if sec_type == "CASH":
        return f"{contract.symbol}.{contract.currency}"
    elif sec_type == "FUT":
        return contract.symbol
    return getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")


def _runner_to_ib_key(runner: 'InstrumentRunner') -> str:
    """Convert runner's contract to canonical key for broker matching."""
    return _normalize_ib_key(runner.contract)


def reconcile_instruments(ib, instruments: list) -> dict:
    """Compare local state vs broker truth for all instruments.

    Returns dict mapping runner.symbol -> {result, local_pos, broker_pos, detail}.
    Writes incident artifacts for non-clean results.
    """
    results = {}

    broker_positions, broker_ok, broker_error = _fetch_broker_positions(ib)
    if not broker_ok:
        log.error(f"Reconciliation: failed to fetch broker positions: {broker_error}")

    for inst in instruments:
        ib_key = _runner_to_ib_key(inst)
        local_pos = inst.state.position
        broker_info = broker_positions.get(ib_key, {"qty": 0, "direction": "FLAT"})
        broker_dir = broker_info["direction"]

        if not broker_ok:
            result = ReconcileResult.BROKER_UNAVAILABLE
            detail = "Could not query broker positions"
        elif local_pos == "FLAT" and broker_dir == "FLAT":
            result = ReconcileResult.CLEAN_FLAT
            detail = "Both local and broker flat"
        elif local_pos == broker_dir:
            # Direction matches — also verify qty is nonzero and reasonable
            broker_qty = abs(broker_info.get("qty", 0))
            if broker_qty > 0:
                result = ReconcileResult.CLEAN_OPEN_MATCHED
                detail = f"Both agree: {local_pos} qty={broker_qty}"
            else:
                # Direction matches but qty is 0 — trust broker
                result = ReconcileResult.LOCAL_OPEN_BROKER_FLAT
                detail = f"Direction matches but broker qty=0 — forcing FLAT"
                inst.state.clear_trade_state()
                inst.state.save()
        elif local_pos == "FLAT" and broker_dir in ("LONG", "SHORT"):
            # 2026-05-04: ADOPT the orphan position into local state instead of
            # leaving it as UNRESOLVED forever. The orphan exists because the
            # runner restarted between order-submission and fill, so the fill
            # callback never fired. Adopting lets argus manage the position
            # (time-stop, price-watch) instead of permanently blocking entries.
            try:
                inst.adopt_orphan_position(
                    broker_dir, broker_info["qty"], broker_info.get("avg_cost", 0.0)
                )
                result = ReconcileResult.CLEAN_OPEN_MATCHED
                detail = (f"Adopted orphan: {broker_dir} qty={broker_info['qty']} "
                          f"@ avg {broker_info.get('avg_cost', 0):.5f}")
            except Exception as exc:
                # If adoption fails, fall back to original blocking behavior
                log.error(f"adopt_orphan_position failed for {inst.symbol}: {exc}")
                result = ReconcileResult.LOCAL_FLAT_BROKER_OPEN
                detail = (f"Orphan adoption failed: broker has {broker_dir} "
                          f"qty={broker_info['qty']}, runner FLAT, adopt error={exc}")
        elif local_pos in ("LONG", "SHORT") and broker_dir == "FLAT":
            result = ReconcileResult.LOCAL_OPEN_BROKER_FLAT
            detail = f"Phantom: runner says {local_pos} but broker is FLAT -- forcing local FLAT"
            # Auto-correct: trust broker truth — clear ALL lifecycle state
            inst.state.clear_trade_state()
            inst.state.save()
        else:
            result = ReconcileResult.UNRESOLVED
            detail = f"local={local_pos} broker={broker_dir} -- cannot auto-resolve"

        results[inst.symbol] = {
            "result": result,
            "local_position": local_pos,
            "broker_position": broker_dir,
            "broker_qty": broker_info.get("qty", 0),
            "detail": detail,
            "ib_key": ib_key,
        }

        # Store reconciliation result on the runner
        inst._reconciliation = result
        inst._reconciliation_detail = detail
        inst._broker_position = broker_dir
        inst._broker_qty = broker_info.get("qty", 0)
        inst._broker_avg_cost = broker_info.get("avg_cost", 0.0)

        if result not in (ReconcileResult.CLEAN_FLAT, ReconcileResult.CLEAN_OPEN_MATCHED):
            log.warning(f"[{inst.symbol}] RECONCILE: {result} -- {detail}")
            _write_incident(inst, result, detail, local_pos, broker_info)
        else:
            log.info(f"[{inst.symbol}] RECONCILE: {result}")

    # Check for orphaned broker positions not tracked by any runner.
    # Argus only owns FX (CADJPY/GBPUSD/USDJPY). Forge runners own equities
    # and futures (SPY, GLD, EEM, EFA, etc) — those positions show up at the
    # broker but aren't argus's responsibility. Treat any non-FX broker
    # position as "owned by another runner" (logged but not flagged as
    # UNRESOLVED, which would trip RECOVERY_REQUIRED and block argus entries).
    tracked_keys = {_runner_to_ib_key(inst) for inst in instruments}
    argus_instrument_types = {(inst.cfg or {}).get("instrument_type", "forex") for inst in instruments}
    # Heuristic: argus FX keys look like "AUD.USD", "GBP.USD", "USD.JPY" (cash).
    # Forge equity/ETF keys look like "SPY", "GLD", etc (no dot).
    # Forge futures look like "MNQ", "MYM" (also no dot, but with exchange context).
    def _is_fx_key(k: str) -> bool:
        return "." in str(k) or len(str(k)) == 6  # AUDUSD or USDJPY style
    for key, info in broker_positions.items():
        if key in tracked_keys or info["direction"] == "FLAT":
            continue
        # Non-tracked non-flat: classify
        if "forex" in argus_instrument_types and not _is_fx_key(key):
            # Likely a forge-runner position. Log informationally, do NOT flag UNRESOLVED.
            log.info(f"NON-ARGUS POSITION: broker has {info['direction']} in {key} (owned by another runner — not argus's concern)")
            continue
        # FX-style key not tracked by argus = real orphan (or unmapped argus pair)
        log.warning(f"ORPHAN DETECTED: broker has {info['direction']} in {key} -- not tracked by any runner")
        results[f"_orphan_{key}"] = {
            "result": ReconcileResult.UNRESOLVED,
            "local_position": "NONE",
            "broker_position": info["direction"],
            "broker_qty": info["qty"],
            "detail": f"Untracked broker position in {key}",
            "ib_key": key,
        }

    return results


_written_incident_keys: set = set()   # dedup: one file per (symbol, type) per session


def _write_incident(inst, result: str, detail: str,
                    local_pos: str, broker_info: dict) -> None:
    """Persist an incident artifact for audit trail."""
    dedup_key = f"{inst.symbol}_{result}"
    if dedup_key in _written_incident_keys:
        return
    _written_incident_keys.add(dedup_key)

    incident_dir = inst.log_dir / "incidents"
    incident_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = getattr(inst, '_session_id', 'unknown')
    incident_file = incident_dir / f"incident_{session_id}_{ts}.json"
    incident_file.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instrument": inst.symbol,
        "session_id": session_id,
        "severity": "CRITICAL" if result in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED) else "WARNING",
        "reconciliation_result": result,
        "detail": detail,
        "local_state": {
            "position": local_pos,
            "entry_price": inst.state.entry_price,
            "stop_price": inst.state.stop_price,
            "target_price": inst.state.target_price,
        },
        "broker_state": broker_info,
        "action_taken": "forced_flat" if result == ReconcileResult.LOCAL_OPEN_BROKER_FLAT else "none",
        "requires_manual_review": result in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED),
    }, indent=2))
    log.info(f"  Incident written: {incident_file}")


def _determine_runtime_mode(recon_results: dict) -> str:
    """Determine overall runtime mode from reconciliation results."""
    has_critical = any(
        r["result"] in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED)
        for r in recon_results.values()
    )
    if has_critical:
        return RuntimeMode.RECOVERY_REQUIRED

    has_degraded = any(
        r["result"] == ReconcileResult.BROKER_UNAVAILABLE
        for r in recon_results.values()
    )
    if has_degraded:
        return RuntimeMode.DEGRADED

    return RuntimeMode.READY


# ═════════════════════════════════════════════════════════════
# Portfolio Risk Manager
# ═════════════════════════════════════════════════════════════
class PortfolioRiskManager:
    """Cross-instrument risk guards. Shared by all runners in a process."""

    # Currency exposure map: which currencies each symbol exposes you to
    CURRENCY_MAP = {
        "EURUSD": {"EUR": +1, "USD": -1},
        "GBPUSD": {"GBP": +1, "USD": -1},
        "AUDUSD": {"AUD": +1, "USD": -1},
        "USDJPY": {"USD": +1, "JPY": -1},
        "EURJPY": {"EUR": +1, "JPY": -1},
        "GBPJPY": {"GBP": +1, "JPY": -1},
        "AUDJPY": {"AUD": +1, "JPY": -1},
        "CADJPY": {"CAD": +1, "JPY": -1},
        "USDCHF": {"USD": +1, "CHF": -1},
        "NZDUSD": {"NZD": +1, "USD": -1},
        "EURGBP": {"EUR": +1, "GBP": -1},
        "EURAUD": {"EUR": +1, "AUD": -1},
        "NZDJPY": {"NZD": +1, "JPY": -1},
        "AUDCAD": {"AUD": +1, "CAD": -1},
        "GBPAUD": {"GBP": +1, "AUD": -1},
        "CHFJPY": {"CHF": +1, "JPY": -1},
        "MES": {"USD_EQUITY": +1}, "MNQ": {"USD_EQUITY": +1},
        "MYM": {"USD_EQUITY": +1}, "M2K": {"USD_EQUITY": +1},
        "MGC": {"GOLD": +1}, "MCL": {"OIL": +1}, "NKD": {"JPY_EQUITY": +1},
    }

    # London cluster: these pairs are ~0.97 correlated, treat as one macro trade
    LONDON_CLUSTER = {"GBPUSD", "EURJPY", "GBPJPY", "CADJPY"}
    MAX_LONDON_CLUSTER_POSITIONS = 3

    # Minimum banked PnL (in R) before the drawdown breaker arms. Prevents the
    # "tiny-peak tyranny" where +5R peak → +2R current reads as 60% drawdown and
    # locks entries despite net-positive PnL. Below this floor, we're still in
    # warmup and the breaker is disabled. Floor tuned for paper: at 10R banked
    # we've proven enough edge for % drawdown to be meaningful.
    MIN_PEAK_R_FOR_BREAKER = 10.0

    # Persistent state file for drawdown pause (survives restart)
    _STATE_FILE = REPO / "argus_flow" / "logs" / "_risk" / "portfolio_risk_state.json"

    def __init__(self, max_same_currency: int = 3, max_drawdown_pct: float = 0.03,
                 daily_max_loss: float = 3.0, portfolio_daily_max_loss: float = 10.0,
                 max_total_open_risk_pct: float = 0.05):
        self.max_same_currency = max_same_currency
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_max_loss = daily_max_loss  # per instrument, in R
        self.portfolio_daily_max_loss = portfolio_daily_max_loss  # fleet-wide, in R
        self.max_total_open_risk_pct = max_total_open_risk_pct
        self._peak_pnl: float = 0.0
        self._current_pnl: float = 0.0
        self._account_equity_usd: float = DEFAULT_ACCOUNT_EQUITY_USD
        self._drawdown_pause = False
        self._daily_pnl: dict[str, float] = {}
        self._daily_paused: set[str] = set()
        self._portfolio_daily_paused: bool = False
        self._current_day: str = ""
        self._load_persistent_state()

    # ── Persistent state (survives restart) ──────────────────
    def _load_persistent_state(self) -> None:
        """Restore drawdown pause and peak PnL from disk if available."""
        if not self._STATE_FILE.exists():
            return
        try:
            data = json.loads(self._STATE_FILE.read_text())
            self._drawdown_pause = bool(data.get("drawdown_pause", False))
            self._peak_pnl = float(data.get("peak_pnl", 0.0))
            # Persisted current_pnl used only for startup-reconciliation dd check
            # (in-memory _current_pnl otherwise re-derives from live update()).
            _persisted_current_pnl = float(data.get("current_pnl", 0.0))
            day = data.get("current_day", "")
            if day == datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                self._daily_pnl = {k: float(v) for k, v in data.get("daily_pnl", {}).items()}
                self._daily_paused = set(data.get("daily_paused", []))
                self._portfolio_daily_paused = bool(data.get("portfolio_daily_paused", False))
                self._current_day = day
            if self._drawdown_pause:
                log.warning(f"RISK_MGR: restored DRAWDOWN_PAUSE from disk (peak={self._peak_pnl:.2f}R)")
                # Startup reconciliation: if persisted drawdown is below threshold
                # OR a manual RESET_DRAWDOWN flag is pending, clear the pause now
                # instead of waiting for the first can_enter call. Rationale:
                # when upstream PAUSE_ENTRIES blocks can_enter, the auto-consume
                # path never fires and drawdown_pause sticks across restarts
                # (observed 2026-04-17: 9.4-day-old RESET_DRAWDOWN never consumed).
                reset_file = REPO / "RESET_DRAWDOWN"
                if self._peak_pnl > 0:
                    persisted_dd = (self._peak_pnl - _persisted_current_pnl) / abs(self._peak_pnl)
                    if persisted_dd < self.max_drawdown_pct:
                        self._drawdown_pause = False
                        log.info(
                            f"RISK_MGR: startup reconciliation cleared pause — "
                            f"dd={persisted_dd*100:.2f}% < threshold={self.max_drawdown_pct*100:.2f}%"
                        )
                    elif reset_file.exists():
                        self._drawdown_pause = False
                        try:
                            reset_file.unlink()
                        except OSError:
                            pass
                        log.info("RISK_MGR: startup reconciliation consumed RESET_DRAWDOWN; pause cleared")
                self.save_persistent_state()
        except Exception as exc:
            log.warning(f"RISK_MGR: failed to load persistent state: {exc}")

    def save_persistent_state(self) -> None:
        """Persist drawdown pause and daily limits so they survive restart."""
        self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "drawdown_pause": self._drawdown_pause,
            "peak_pnl": self._peak_pnl,
            "current_pnl": self._current_pnl,
            "current_day": self._current_day,
            "daily_pnl": self._daily_pnl,
            "daily_paused": list(self._daily_paused),
            "portfolio_daily_paused": self._portfolio_daily_paused,
        }
        try:
            atomic_write_json(self._STATE_FILE, data)
        except Exception as exc:
            log.error(f"RISK_MGR: failed to save persistent state: {exc} — state may be stale on restart")

    def update(self, instruments: list) -> None:
        """Update portfolio PnL tracking.

        Normalizes to R-multiples (multiples of initial risk) per instrument
        to avoid mixing pips and points. 1R = one stop-loss distance of PnL.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_day:
            self._current_day = today
            self._daily_pnl.clear()
            self._daily_paused.clear()
            self._portfolio_daily_paused = False
            log.info("RISK_MGR: daily limits reset")
        # Normalize each instrument's PnL by its stop distance to get R-multiples
        total_r = 0.0
        for i in instruments:
            s = i.state
            raw_pnl = s.pnl_pips if i.uses_pips else s.pnl_points
            # Normalize by stop size to get R-units
            stop = i.stop_pips if i.uses_pips else i.stop_bps
            if stop > 0:
                total_r += raw_pnl / stop
            elif raw_pnl != 0:
                # Zero stop = broken config. Don't contaminate R-aggregate.
                log.warning(f"RISK_MGR: {i.symbol} has stop=0, PnL={raw_pnl} excluded from R-total")
                pass  # skip this instrument's contribution
        self._current_pnl = total_r
        self._peak_pnl = max(self._peak_pnl, total_r)

    def record_trade_pnl(self, symbol: str, pnl_r: float) -> None:
        """Record a completed trade's PnL in R-multiples for daily tracking."""
        self._daily_pnl[symbol] = self._daily_pnl.get(symbol, 0.0) + pnl_r

    def set_account_equity(self, equity_usd: float) -> None:
        if equity_usd and equity_usd > 0:
            self._account_equity_usd = equity_usd

    def can_enter(self, symbol: str, direction: str, instruments: list, candidate_risk_usd: float = 0.0) -> tuple[bool, str]:
        """Master gate: check all portfolio-level risk guards."""
        # 1. Drawdown breaker — only arms once peak_pnl clears MIN_PEAK_R floor
        if self._peak_pnl >= self.MIN_PEAK_R_FOR_BREAKER:
            dd = (self._peak_pnl - self._current_pnl) / abs(self._peak_pnl)
            if dd >= self.max_drawdown_pct:
                if not self._drawdown_pause:
                    log.warning(f"DRAWDOWN BREAKER: {dd:.1%} from peak. ALL entries paused.")
                    self._drawdown_pause = True
                return False, "DRAWDOWN_PAUSE"
        # Reset conditions checked even if peak fell below the floor — otherwise
        # a stale pause flag can trap entries forever.
        if self._drawdown_pause:
            reset_file = REPO / "RESET_DRAWDOWN"
            now_utc = datetime.now(timezone.utc)
            session_reset = (now_utc.hour == 0 and now_utc.minute < 2)
            manual_reset = reset_file.exists()
            below_floor = self._peak_pnl < self.MIN_PEAK_R_FOR_BREAKER
            if session_reset or manual_reset or below_floor:
                why = ("session_boundary" if session_reset else
                       "manual_flag" if manual_reset else "below_min_peak_floor")
                log.info(f"DRAWDOWN BREAKER: reset ({why}). Entries resumed.")
                self._drawdown_pause = False
                if manual_reset:
                    try:
                        reset_file.unlink()
                    except OSError:
                        pass
            else:
                return False, "DRAWDOWN_PAUSE"

        # 2. Daily max loss per instrument
        if symbol in self._daily_paused:
            return False, "DAILY_LIMIT"
        daily = self._daily_pnl.get(symbol, 0.0)
        if daily <= -self.daily_max_loss:
            log.warning(f"DAILY LIMIT: {symbol} lost {daily:+.1f} today. Paused.")
            self._daily_paused.add(symbol)
            return False, "DAILY_LIMIT"

        # 3. Portfolio daily max loss (sum of all instrument daily losses)
        total_daily_r = sum(self._daily_pnl.values())
        if total_daily_r <= -self.portfolio_daily_max_loss:
            if not self._portfolio_daily_paused:
                log.warning(f"PORTFOLIO DAILY LIMIT: fleet lost {total_daily_r:+.1f}R today. ALL entries paused.")
                self._portfolio_daily_paused = True
            return False, "PORTFOLIO_DAILY_LIMIT"

        # 4. Total open-risk budget
        if self._account_equity_usd > 0 and self.max_total_open_risk_pct > 0:
            max_open_risk = self._account_equity_usd * self.max_total_open_risk_pct
            current_open_risk = 0.0
            for inst in instruments:
                if getattr(inst.state, "position", "FLAT") == "FLAT":
                    continue
                try:
                    current_open_risk += float(inst.current_open_risk_usd())
                except Exception:
                    pass
            if current_open_risk + candidate_risk_usd > max_open_risk:
                log.info(
                    f"OPEN_RISK_BLOCK: current=${current_open_risk:.2f} "
                    f"candidate=${candidate_risk_usd:.2f} limit=${max_open_risk:.2f}"
                )
                return False, "OPEN_RISK_LIMIT"

        # 5. Correlation / currency exposure
        cmap = self.CURRENCY_MAP.get(symbol.upper(), {})
        if not cmap:
            # Unknown symbol — fail closed, do not silently skip correlation checks
            log.warning(f"CORRELATION FAIL_CLOSED: {symbol} not in CURRENCY_MAP — entry blocked")
            return False, "UNKNOWN_SYMBOL_EXPOSURE"

        exposure: dict[str, int] = {}
        for inst in instruments:
            if inst.state.position == "FLAT":
                continue
            ic = self.CURRENCY_MAP.get(inst.symbol.upper(), {})
            pm = 1 if inst.state.position == "LONG" else -1
            for ccy, dm in ic.items():
                exposure[ccy] = exposure.get(ccy, 0) + (dm * pm)
        new_mult = 1 if direction == "long" else -1
        for ccy, dm in cmap.items():
            new_exp = exposure.get(ccy, 0) + (dm * new_mult)
            if abs(new_exp) > self.max_same_currency:
                log.info(f"CORRELATION BLOCK: {symbol} {direction} -> {ccy}={new_exp:+d} (max={self.max_same_currency})")
                return False, "CORRELATION_LIMIT"

        # 6. London cluster limit (GBPUSD, EURJPY, GBPJPY, CADJPY ≈ 0.97 correlated)
        if symbol.upper() in self.LONDON_CLUSTER:
            cluster_open = sum(
                1 for inst in instruments
                if inst.state.position != "FLAT" and inst.symbol.upper() in self.LONDON_CLUSTER
            )
            if cluster_open >= self.MAX_LONDON_CLUSTER_POSITIONS:
                log.info(f"LONDON_CLUSTER BLOCK: {symbol} {direction} -> {cluster_open} already open (max={self.MAX_LONDON_CLUSTER_POSITIONS})")
                return False, "LONDON_CLUSTER_LIMIT"

        return True, ""


# ═════════════════════════════════════════════════════════════
# Shutdown Modes (3-mode lifecycle)
#   PAUSE_ENTRIES  — block new entries, keep existing positions running
#   GRACEFUL_EXIT  — block entries, let positions exit via stop/target/timeout, then stop
#   KILL_SWITCH    — cancel all orders, market-close all positions immediately, stop
# ═════════════════════════════════════════════════════════════
def _execute_emergency_shutdown(ib, instruments, kill_file):
    """Emergency halt: cancel all orders, close all positions, notify, exit."""

    # 1. Block all entries
    for inst in instruments:
        inst._entries_blocked = True
        inst.trade_enabled = False

    # 2. Cancel all open orders on the IB connection
    try:
        for trade in ib.openTrades():
            try:
                ib.cancelOrder(trade.order)
                ib.sleep(0.1)
            except Exception:
                pass
    except Exception:
        pass

    # 3. Close all open positions via market orders
    for inst in instruments:
        if inst.state.position != "FLAT":
            side = "SELL" if inst.state.position == "LONG" else "BUY"
            qty = inst.state.position_size or getattr(inst, "lot_size", 0)
            if qty > 0:
                try:
                    from ib_insync import MarketOrder
                    flatten = MarketOrder(side, qty)
                    ib.placeOrder(inst.contract, flatten)
                    log.critical(f"EMERGENCY CLOSE {inst.symbol} {side} qty={qty}")
                    ib.sleep(0.5)
                except Exception as e:
                    log.error(f"Emergency close {inst.symbol} failed: {e}")

        # Reset local state
        inst.state.clear_trade_state()
        inst.state.save()

    # 4. Discord notification
    try:
        import urllib.request
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not webhook:
            env_file = REPO / ".env"
            if env_file.exists():
                for line in env_file.read_text().splitlines():
                    if line.startswith("DISCORD_WEBHOOK_URL="):
                        webhook = line.split("=", 1)[1].strip().strip('"')
        if webhook:
            payload = json.dumps({"embeds": [{
                "title": "EMERGENCY SHUTDOWN",
                "description": "Kill switch triggered. All positions closed, all orders cancelled.",
                "color": 16711680,
            }]}).encode()
            req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log.error(f"Discord kill notification failed: {e}")

    # 5. Disconnect
    log.critical("EMERGENCY: Disconnecting from IBKR")
    try:
        ib.disconnect()
    except Exception:
        pass

    log.critical("EMERGENCY SHUTDOWN COMPLETE — exiting")


# ═════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════���══════
def main(config_paths: Optional[list[str]] = None, exclude: Optional[list[str]] = None) -> bool:
    """
    Run all instruments in a single event loop.
    Returns True to signal reconnect, False for clean exit.
    """
    # ── Discover configs ─────────────────────────────────────
    if config_paths:
        cfg_files = [Path(p) for p in config_paths]
    else:
        cfg_files = sorted(CONFIGS_DIR.glob("*_paper_v1.json"))

    if not cfg_files:
        log.error("No config files found!")
        return False

    exclude_set = set(e.lower() for e in (exclude or []))

    if not _validate_config_registry(cfg_files):
        return False

    # ── Banner ───────────────────────────────────────────────
    log.info("=" * 70)
    log.info("ARGUS Unified Multi-Instrument Runner")
    log.info(f"Gateway: {IBKR_HOST}:{IBKR_PORT}  clientId={IBKR_CLIENT_ID}")
    log.info(f"Configs: {len(cfg_files)} files discovered")
    log.info("=" * 70)

    # ── Connect ──────────────────────────────────────────────
    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=15)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return True

    accounts = ib.managedAccounts()
    log.info(f"Connected. Accounts: {accounts}")
    account = accounts[0] if accounts else None
    equity_tracker = AccountEquityTracker(ib, account=account)
    equity_tracker.refresh(force=True)
    log.info(f"Account equity baseline: ${equity_tracker.equity_usd:,.2f}")

    # Request delayed data fallback (needed for futures outside RTH)
    ib.reqMarketDataType(3)

    # ── Build instrument runners ────────���────────────────────
    instruments: list[InstrumentRunner] = []
    skipped = 0

    for cfg_path in cfg_files:
        try:
            cfg = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Skipping {cfg_path.name}: {e}")
            skipped += 1
            continue

        cfg["__config_path__"] = str(cfg_path)

        sym = cfg.get("symbol", "???")

        # Apply exclusion filter
        if sym.lower() in exclude_set or cfg_path.stem.lower() in exclude_set:
            log.info(f"  SKIP {sym} (excluded)")
            skipped += 1
            continue

        # Skip non-instrument files (e.g. hashes.json)
        if "instrument_type" not in cfg:
            continue

        try:
            contract = create_contract(cfg)
        except Exception as e:
            log.warning(f"  SKIP {sym}: contract creation failed: {e}")
            skipped += 1
            continue

        # Qualify contract
        try:
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                log.warning(f"  SKIP {sym}: qualification returned empty")
                skipped += 1
                continue
            contract = qualified[0]
        except Exception as e:
            log.warning(f"  SKIP {sym}: qualification failed: {e}")
            skipped += 1
            continue

        # Subscribe to market data
        try:
            ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
        except Exception as e:
            log.warning(f"  SKIP {sym}: market data subscription failed: {e}")
            skipped += 1
            continue

        ldir = log_dir_for(cfg)
        try:
            runner = InstrumentRunner(cfg, contract, ticker, ldir, str(cfg_path))
        except ValueError as e:
            log.critical(f"CONFIG ERROR: {e}")
            ib.disconnect()
            return False
        runner.state.load(runner.label)
        if not runner.trade_enabled and runner.state.position != "FLAT":
            runner._log.warning(
                f"[{runner.label}] {runner.deployment_stage.upper()} stage is non-trading; "
                f"forcing local state FLAT from restored {runner.state.position}"
            )
            runner.state.clear_trade_state()
        runner._hydrate_trade_history_from_journal()
        runner._hydrate_legacy_state_size()
        runner.state.save()

        # Cohort tracking fields
        runner._config_hash = _config_short_hash(cfg_path)
        runner._session_id = getattr(main, '_session_id', str(uuid.uuid4())[:8])
        runner._runtime_start = getattr(main, '_runtime_start', time.time())
        runner._reconnected = getattr(main, '_is_reconnect', False)
        runner._git_sha = getattr(main, '_git_sha', 'unknown')
        runner._equity_tracker = equity_tracker

        instruments.append(runner)

        itype = cfg.get("instrument_type", "?")
        risk = cfg.get("risk", {})
        if itype == "forex":
            risk_str = f"SL={risk.get('stop_pips', '?')}pip TP={risk.get('target_pips', '?')}pip"
        else:
            risk_str = f"SL={risk.get('stop_bps', '?')}bps TP={risk.get('target_bps', '?')}bps"
        log.info(f"  OK {sym:8s} [{itype:6s}] {cfg.get('strategy', '?'):20s} {risk_str}  -> {contract}")

    if not instruments:
        log.error("No instruments loaded! Check configs and IBKR connection.")
        ib.disconnect()
        return False

    # Check for duplicate log directories (paper/live same symbol is allowed if log dirs differ)
    seen_log_dirs = {}
    for inst in instruments:
        log_key = str(inst.log_dir).lower()
        if log_key in seen_log_dirs:
            log.error(f"FATAL: duplicate log dir '{inst.log_dir}' — configs '{seen_log_dirs[log_key]}' and '{inst.config_path}' would collide")
            ib.disconnect()
            return False
        seen_log_dirs[log_key] = inst.config_path

    log.info(f"Loaded {len(instruments)} instruments ({skipped} skipped)")
    log.info("-" * 70)

    # -- Pending-fills queue reconcile (orphan-fill root fix) ----------
    # Any entry order submitted in a prior session whose fill arrived
    # during the gap is recovered here BEFORE reconcile_instruments runs,
    # so position state is correct when reconcile checks it.
    try:
        from helio.pending_fills import reconcile_pending
        resolved_pending = reconcile_pending(ib)
        for r in resolved_pending:
            log.warning(
                f"PENDING_FILL_RESOLVED: strategy={r.get('strategy')} "
                f"symbol={r.get('symbol')} dir={r.get('direction')} "
                f"size={r.get('size')} broker_qty={r.get('broker_qty_observed')} "
                f"order_id={r.get('order_id')} (filled during prior-session gap)"
            )
    except Exception as exc:
        log.warning(f"reconcile_pending failed (non-fatal): {exc}")

    # -- Broker reconciliation gate ------------------------------------
    log.info("Running broker reconciliation...")
    runtime_mode = RuntimeMode.RECONCILING
    recon_results = reconcile_instruments(ib, instruments)
    runtime_mode = _determine_runtime_mode(recon_results)
    log.info(f"Reconciliation complete. Runtime mode: {runtime_mode}")

    if runtime_mode == RuntimeMode.RECOVERY_REQUIRED:
        log.error("RECOVERY REQUIRED: unresolved broker mismatch detected")
        log.error("Mismatched instruments BLOCKED until manual review. Monitor will continue.")
        # Only block the specific instruments with mismatches (not entire fleet)
        for inst in instruments:
            r = recon_results.get(inst.symbol, {})
            r_result = r.get("result", "") if isinstance(r, dict) else getattr(r, "result", "")
            if str(r_result) in ("LOCAL_FLAT_BROKER_OPEN", "UNRESOLVED", "RECOVERY_REQUIRED"):
                inst._recon_blocked = True
                inst._recon_block_reason = str(r_result)
                _sync_entry_block_state(inst)
                log.error(f"  BLOCKED: {inst.symbol} ({r_result})")
            else:
                inst._recon_blocked = False
                inst._recon_block_reason = ""
                _sync_entry_block_state(inst)
    else:
        for inst in instruments:
            inst._recon_blocked = False
            inst._recon_block_reason = ""
            _sync_entry_block_state(inst)

    _write_broker_truth_artifacts(
        ib,
        account,
        instruments,
        runtime_mode,
        equity_tracker,
        recon_results=recon_results,
    )
    _write_heartbeat_files(
        instruments,
        runtime_mode,
        ib,
        equity_tracker,
        datetime.now(timezone.utc),
    )

    # -- Portfolio risk manager ----------------------------------------
    # Determine drawdown limit based on fleet stage
    any_real = any(inst.execution_mode == "real" for inst in instruments)
    dd_limit = 0.02 if any_real else 0.05  # 2% for prod, 5% for paper (was 3%, triggered too early for 8-pair portfolio)
    _fleet_size = sum(1 for inst in instruments if inst.trade_enabled)
    _currency_limit = 4 if _fleet_size > 10 else (3 if _fleet_size > 6 else 2)
    risk_mgr = PortfolioRiskManager(
        max_same_currency=_currency_limit,  # scale with fleet size (was 2, blocked 50%+ of signals)
        max_drawdown_pct=dd_limit,
        daily_max_loss=5.0,           # per instrument: 5R/day (was 3, too tight for 5m eval)
        portfolio_daily_max_loss=15.0, # fleet-wide: 15R/day for 16-pair fleet (was 10)
        max_total_open_risk_pct=0.30,  # 30% for 16-pair paper fleet (was 20%)
    )
    # Use model equity for paper fleet, broker equity for real
    _model_equities = [inst._get_account_equity() for inst in instruments if inst.trade_enabled]
    _fleet_equity = max(_model_equities) if _model_equities else equity_tracker.equity_usd
    risk_mgr.set_account_equity(_fleet_equity)
    log.info(f"Risk manager equity: ${_fleet_equity:,.2f} (broker=${equity_tracker.equity_usd:,.2f})")
    for inst in instruments:
        inst._risk_mgr = risk_mgr
        inst._all_instruments = instruments  # reference for correlation checks

    # -- Wire IB reference and fill callback for real execution --------
    for inst in instruments:
        inst._ib = ib

    # -- Optional golden-trace recorder (opt-in via GOLDEN_TRACE_PATH env)
    # Captures every broker event to JSONL so live incidents can be
    # converted into deterministic regression tests via helio.trace_replay.
    _trace_path = os.environ.get("GOLDEN_TRACE_PATH", "").strip()
    if _trace_path:
        try:
            from helio.event_recorder import attach_recorder
            _trace_recorder = attach_recorder(ib, _trace_path)
            log.warning(f"GOLDEN_TRACE_RECORDER attached -> {_trace_path}")
        except Exception as e:
            log.warning(f"GOLDEN_TRACE_RECORDER failed to attach: {e}")

    def _route_fill_to_runner(trade, fill):
        """Route IB fill events to the correct InstrumentRunner.

        2026-05-22 BUGFIX: filter was `execution_mode != "real"` which
        skipped paper-stage instruments. But paper-stage runners submit
        real broker orders (to the paper account) and receive real
        execDetails — they just don't risk real money. With the wrong
        filter, every argus paper fill since 4/23 was silently dropped:
          - _on_fill never invoked
          - No 'FILL RECEIVED' log line
          - No canonical_fills.jsonl write
          - Drift detector accumulated unattributed P&L
        Caught by investigating the 5/22 drift trip — runner thought
        realized=-$799, broker showed -$1,223; the $424 gap was all the
        unlogged argus exits. The runner body uses ("real", "paper") in
        5 other places; this dispatcher just hadn't been updated. The
        "observe" mode still doesn't submit orders so still skipped."""
        trade_symbol = getattr(trade.contract, 'symbol', '') or ''
        trade_key = _normalize_ib_key(trade.contract) if trade.contract else ''
        for inst in instruments:
            if inst.execution_mode not in ("real", "paper"):
                continue
            inst_key = _runner_to_ib_key(inst)
            if trade_key == inst_key or trade_symbol == inst.symbol:
                try:
                    inst._on_fill(trade, fill)
                except Exception as exc:
                    inst._log.error(f"Fill callback error: {exc}", exc_info=True)
                break

    ib.execDetailsEvent += _route_fill_to_runner

    def _route_order_status_to_runner(trade):
        """Route IB order status events (Rejected/Cancelled) to correct runner.

        2026-05-22 BUGFIX: same dispatcher gate as _route_fill_to_runner —
        paper-stage instruments also need to receive Rejected/Cancelled
        order status events to clear their state. Previously only "real"
        runners received them; argus paper would have stale entry_pending
        flags after a rejection. Same root cause."""
        status_str = getattr(trade.orderStatus, 'status', '') if trade.orderStatus else ''
        if status_str not in ('Rejected', 'Cancelled'):
            return
        trade_key = _normalize_ib_key(trade.contract) if trade.contract else ''
        order_id = str(getattr(trade.order, 'orderId', ''))
        for inst in instruments:
            if inst.execution_mode not in ("real", "paper"):
                continue
            inst_key = _runner_to_ib_key(inst)
            if trade_key != inst_key:
                continue
            s = inst.state
            if order_id == s.entry_order_id:
                inst._log.critical(
                    f"ORDER {status_str.upper()}: entry order {order_id} — "
                    f"clearing entry_pending, forcing FLAT"
                )
                s.clear_trade_state()
                s.save()
            elif order_id in (s.stop_order_id, s.target_order_id):
                inst._log.critical(
                    f"ORDER {status_str.upper()}: bracket order {order_id} — "
                    f"position {s.position} may be unhedged, resubmitting brackets"
                )
                ok = inst._submit_bracket_orders(s.stop_price, s.target_price)
                if not ok:
                    inst._log.critical("BRACKET RESUBMIT FAILED — submitting emergency exit")
                    mid = inst._get_mid()
                    if mid:
                        inst._submit_real_exit("bracket_rejected", mid)
            elif order_id == s.exit_order_id:
                inst._log.critical(
                    f"ORDER {status_str.upper()}: exit order {order_id} — retrying exit"
                )
                mid = inst._get_mid()
                if mid:
                    inst._submit_real_exit("exit_retry_after_reject", mid)
            break

    ib.orderStatusEvent += _route_order_status_to_runner

    # -- Seed historical data -----------------------------------------
    log.info("Seeding historical bars...")
    for inst in instruments:
        inst.seed(ib)
        ib.sleep(0.5)  # rate-limit historical data requests

    # -- Sizing validation gate ----------------------------------------
    sizing_failures = []
    for inst in instruments:
        if not inst.trade_enabled:
            continue
        mid = inst._get_mid()
        if not mid or mid <= 0:
            log.warning(f"  [{inst.label}] No mid price for sizing validation — skipping")
            continue
        stop_px, _ = inst._compute_stops(mid, "long")
        size, risk_usd, policy = inst._resolve_position_size(mid, stop_px)
        equity = inst._get_account_equity()
        if size <= 0:
            if policy == "allocation_factor_zero":
                log.warning(
                    f"  Sizing disabled {inst.label}: allocation_factor_zero "
                    f"(reconcile/exit management allowed)"
                )
                continue
            log.critical(
                f"FATAL SIZING: [{inst.label}] dry-run size=0 | equity=${equity:,.2f} "
                f"risk_pct={inst.risk_pct} policy={policy} "
                f"model_equity={inst.risk_policy.get('model_start_equity_usd', 'MISSING')} "
                f"stage={inst.deployment_stage}"
            )
            sizing_failures.append(inst.label)
        else:
            log.info(f"  Sizing OK {inst.label}: size={size} risk=${risk_usd:.2f} equity=${equity:,.2f}")
    if sizing_failures:
        log.critical(f"STARTUP ABORTED: {len(sizing_failures)} instrument(s) would never trade: {sizing_failures}")
        ib.disconnect()
        return False

    # -- Main loop ---------------------------------------------------���───────────────
    # -- FX Governor (LOG_ONLY — stamps P(win) on every signal) ──
    gov_artifact, gov_stats = _load_governor()
    for inst in instruments:
        inst._governor = gov_artifact

    # -- AI Weight Pool (cross-pair learning) ─────────────────────
    try:
        from argus_flow.strategies.ai_overlay import AIWeightPool
        _ai_pool = AIWeightPool(state_dir=LOGS_ROOT)
        _ai_overlays = {
            inst.symbol: inst._ai_overlay
            for inst in instruments
            if getattr(inst, '_ai_overlay', None) is not None
        }
        if _ai_overlays:
            _ai_pool.update(_ai_overlays)
            _ai_pool.sync_to_overlays(_ai_overlays, blend=0.3)
            log.info(f"AI weight pool: {len(_ai_overlays)} overlays synced (blend=0.3)")
        # Attach pool + overlay map so learn callbacks can trigger re-pool
        for inst in instruments:
            inst._ai_pool = _ai_pool
            inst._ai_overlays = _ai_overlays
    except Exception as e:
        log.warning(f"AI weight pool init failed (non-fatal): {e}")
        for inst in instruments:
            inst._ai_pool = None
            inst._ai_overlays = {}

    log.info("Starting main loop (Ctrl+C to stop)...")
    heartbeat_interval = 300  # full broker/account heartbeat every 5 min
    heartbeat_file_interval = 60  # lightweight chart/state heartbeat every 1 min
    last_heartbeat = time.time()
    last_heartbeat_file_write = 0.0
    _last_periodic_recon = time.time()  # periodic broker recon every 5 min

    try:
        while True:
            ib.sleep(1)  # process all IB events for all instruments

            # Check kill switch
            kill_file = REPO / "KILL_SWITCH"
            if kill_file.exists():
                log.critical("KILL SWITCH DETECTED — emergency shutdown")
                _execute_emergency_shutdown(ib, instruments, kill_file)
                break

            # Check graceful exit — block entries, wait for all positions to close, then stop
            graceful_file = REPO / "GRACEFUL_EXIT"
            for inst in instruments:
                inst._control_blocked = False
                inst._control_block_reason = ""
            if graceful_file.exists():
                for inst in instruments:
                    inst._control_blocked = True
                    inst._control_block_reason = "GRACEFUL_EXIT"
                all_flat = all(inst.state.position == "FLAT" for inst in instruments)
                if all_flat:
                    log.critical("GRACEFUL EXIT: all positions closed. Shutting down.")
                    risk_mgr.save_persistent_state()
                    for inst in instruments:
                        inst.state.save()
                    try:
                        from argus_flow.ops.discord_alerts import send_discord
                        send_discord(embeds=[{
                            "title": "GRACEFUL EXIT complete",
                            "description": "All positions closed, runner stopped.",
                            "color": 0x00FF88,
                        }])
                    except Exception:
                        pass
                    try:
                        ib.disconnect()
                    except Exception:
                        pass
                    _log_summary(instruments)
                    break

            # Check pause file — block new entries but keep existing positions
            pause_file = REPO / "PAUSE_ENTRIES"
            if pause_file.exists():
                for inst in instruments:
                    inst._control_blocked = True
                    inst._control_block_reason = "PAUSE_ENTRIES"

            now = datetime.now(timezone.utc)

            # ── Friday auto-close: flatten all positions before weekend ──
            if now.weekday() == 4:  # Friday
                utc_min = now.hour * 60 + now.minute
                if utc_min >= FX_FRIDAY_CLOSE_MINUTE:
                    for inst in instruments:
                        if inst.instrument_type != "forex":
                            continue
                        inst._control_blocked = True
                        inst._control_block_reason = "FRIDAY_CLOSE"
                        s = inst.state
                        if s.position != "FLAT" and not s.exit_pending:
                            reason = "friday_close"
                            if utc_min >= FX_FRIDAY_HARD_CLOSE_MINUTE:
                                reason = "friday_hard_close"
                            mid = inst._get_mid()
                            if not getattr(inst, '_friday_close_logged', False):
                                inst._log.warning(
                                    f"FRIDAY CLOSE: flattening {s.position} position before weekend "
                                    f"(mid={mid}, reason={reason})"
                                )
                                inst._friday_close_logged = True
                            if inst.execution_mode in ("real", "paper") and mid:
                                inst._submit_real_exit(reason, mid)
                            elif mid:
                                pnl, pnl_usd = inst._log_trade(mid, reason, now)
                                if inst.uses_pips:
                                    s.pnl_pips += pnl
                                else:
                                    s.pnl_points += pnl
                                s.pnl_usd += pnl_usd
                                s.clear_trade_state()
                                s.save()
                else:
                    for inst in instruments:
                        inst._friday_close_logged = False

            for inst in instruments:
                _sync_entry_block_state(inst)

            # Use model equity for paper fleet, broker equity for real
            equity_tracker.refresh()
            _model_equities = [inst._get_account_equity() for inst in instruments if inst.trade_enabled]
            _fleet_equity = max(_model_equities) if _model_equities else equity_tracker.equity_usd
            risk_mgr.set_account_equity(_fleet_equity)
            risk_mgr.update(instruments)

            for inst in instruments:
                # Skip quarantined runners
                if getattr(inst, '_quarantined', False):
                    continue
                try:
                    inst.tick(now)
                    inst._consecutive_errors = 0  # reset on success
                except Exception as e:
                    inst._consecutive_errors = getattr(inst, '_consecutive_errors', 0) + 1
                    inst._log.error(f"Tick error ({inst._consecutive_errors}x): {e}")
                    if inst._consecutive_errors >= 10:
                        inst._log.error(f"QUARANTINED after {inst._consecutive_errors} consecutive errors")
                        inst._quarantined = True
                        runtime_mode = RuntimeMode.DEGRADED
                        _write_incident(inst, "QUARANTINED", f"{inst._consecutive_errors} consecutive tick errors",
                                        inst.state.position, {"direction": "unknown", "qty": 0})

            # Periodic broker reconciliation (every 5 min, warn-only, no entry blocking)
            if time.time() - _last_periodic_recon >= 300:
                _last_periodic_recon = time.time()
                try:
                    # Use same normalized key as startup reconciliation.
                    # 2026-05-06: capture avg_cost too so we can adopt orphans
                    # mid-session (same fix as startup reconcile_instruments).
                    broker_positions: dict[str, dict] = {}
                    for p in ib.positions():
                        key = _normalize_ib_key(p.contract)
                        broker_positions[key] = {
                            "qty": float(p.position),
                            "avg_cost": float(getattr(p, "avgCost", 0) or 0),
                        }
                    for inst in instruments:
                        ib_key = _runner_to_ib_key(inst)
                        bp_info = broker_positions.get(ib_key, {"qty": 0, "avg_cost": 0})
                        bp = bp_info["qty"]
                        local_pos = inst.state.position
                        broker_flat = (bp == 0)
                        local_flat = (local_pos == "FLAT")
                        # 2026-04-24: paper stage now submits real orders to the
                        # IBKR paper account, so broker positions should match
                        # local state. Old paper-skip branch removed — drift
                        # check now runs for paper too.
                        if broker_flat != local_flat:
                            # 2026-05-06: when broker has position and local says FLAT
                            # (LOCAL_FLAT_BROKER_OPEN), this is the orphan-fill bug
                            # (fill confirmation lost across reconnect/restart). Adopt
                            # the position into local state instead of blocking forever.
                            # The other direction (broker FLAT, local OPEN) is harder
                            # to auto-resolve safely — we still block on that case.
                            if not broker_flat and local_flat:
                                broker_dir = "LONG" if bp > 0 else "SHORT"
                                try:
                                    inst.adopt_orphan_position(
                                        broker_dir, bp, bp_info["avg_cost"]
                                    )
                                    log.warning(
                                        f"RECON_DRIFT RESOLVED: {inst.symbol} adopted "
                                        f"orphan {broker_dir} qty={bp} @ "
                                        f"{bp_info['avg_cost']:.5f}"
                                    )
                                    inst._recon_blocked = False
                                    inst._recon_block_reason = ""
                                    _sync_entry_block_state(inst)
                                    continue
                                except Exception as exc:
                                    log.error(
                                        f"Runtime orphan adoption failed for "
                                        f"{inst.symbol}: {exc} — falling back to BLOCK"
                                    )
                                    # Fall through to block path
                            log.warning(
                                f"RECON_DRIFT: {inst.symbol} local={local_pos} "
                                f"broker_qty={bp} key={ib_key} — BLOCKING ENTRIES"
                            )
                            inst._recon_blocked = True
                            inst._recon_block_reason = "RECON_DRIFT"
                            _sync_entry_block_state(inst)
                            _write_incident(inst, "RECON_DRIFT",
                                            f"local={local_pos} broker_qty={bp}",
                                            local_pos, {"direction": "unknown", "qty": bp})
                        else:
                            if getattr(inst, '_recon_blocked', False):
                                log.info(f"RECON_DRIFT CLEARED: {inst.symbol} local and broker positions agree again")
                            inst._recon_blocked = False
                            inst._recon_block_reason = ""
                            _sync_entry_block_state(inst)
                except Exception as e:
                    log.warning(f"Periodic reconciliation failed: {e}")

            # Atlas regime context (LOG_ONLY during burn-in)
            if time.time() - last_heartbeat_file_write > heartbeat_file_interval:
                try:
                    from forge.atlas.fleet_gate import check_atlas
                    _atlas = check_atlas()
                    if _atlas.available:
                        _atlas.log_recommendation("argus")
                except Exception:
                    pass  # Atlas is optional — never break the runner

            # Lightweight per-instrument heartbeat for dashboard/chart freshness
            if time.time() - last_heartbeat_file_write > heartbeat_file_interval:
                last_heartbeat_file_write = time.time()
                _write_heartbeat_files(
                    instruments,
                    runtime_mode,
                    ib,
                    equity_tracker,
                    now,
                )

            # Periodic full heartbeat (broker/account truth + log)
            if time.time() - last_heartbeat > heartbeat_interval:
                last_heartbeat = time.time()
                broker_positions, broker_ok, broker_error = _fetch_broker_positions(ib) if ib.isConnected() else ({}, False, "ib_disconnected")
                if not broker_ok and broker_error:
                    log.warning(f"Periodic reconciliation failed: {broker_error}")

                for inst in instruments:
                    ib_key = _runner_to_ib_key(inst)
                    broker_info = broker_positions.get(ib_key, {"qty": 0.0, "direction": "FLAT", "avg_cost": 0.0})
                    local_pos = inst.state.position
                    broker_dir = broker_info.get("direction", "FLAT")
                    broker_qty = broker_info.get("qty", 0.0)

                    inst._broker_position = broker_dir
                    inst._broker_qty = broker_qty
                    inst._broker_avg_cost = broker_info.get("avg_cost", 0.0)

                    if not broker_ok:
                        inst._reconciliation = ReconcileResult.BROKER_UNAVAILABLE
                        inst._reconciliation_detail = broker_error or "Could not query broker positions"
                    # 2026-04-24: paper mode now hits the broker (IBKR paper
                    # account), so its reconciliation falls through to the
                    # normal comparison below — same truth check as real mode.
                    elif local_pos == "FLAT" and broker_dir == "FLAT":
                        inst._reconciliation = ReconcileResult.CLEAN_FLAT
                        inst._reconciliation_detail = "Both local and broker flat"
                    elif local_pos == broker_dir:
                        inst._reconciliation = ReconcileResult.CLEAN_OPEN_MATCHED
                        inst._reconciliation_detail = f"Both agree: {local_pos} qty={broker_qty}"
                    else:
                        inst._reconciliation = "RECON_DRIFT"
                        inst._reconciliation_detail = f"local={local_pos} broker={broker_dir} qty={broker_qty}"
                        _write_incident(
                            inst,
                            "RECON_DRIFT",
                            inst._reconciliation_detail,
                            local_pos,
                            broker_info,
                        )

                account_snapshot = _fetch_account_snapshot(ib, account, equity_tracker.equity_usd)
                open_orders, open_orders_error = _fetch_open_orders(ib)
                _write_broker_truth_artifacts(
                    ib,
                    account,
                    instruments,
                    runtime_mode,
                    equity_tracker,
                    broker_positions=broker_positions,
                    broker_ok=broker_ok,
                    position_error=broker_error,
                    account_snapshot=account_snapshot,
                    open_orders=open_orders,
                    open_orders_error=open_orders_error,
                )

                positions = [
                    f"{i.symbol}={i.state.position}"
                    for i in instruments
                    if i.state.position != "FLAT"
                ]
                flat_count = sum(1 for i in instruments if i.state.position == "FLAT")
                pos_str = ", ".join(positions) if positions else "all FLAT"
                log.info(
                    f"HEARTBEAT | {len(instruments)} instruments | "
                    f"{flat_count} flat | {pos_str}"
                )
                risk_mgr.save_persistent_state()

    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)...")
        risk_mgr.save_persistent_state()
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        _log_summary(instruments)
        return False  # clean exit

    except (ConnectionError, OSError, asyncio.CancelledError) as e:
        log.warning(f"Connection lost: {e}. Will reconnect...")
        risk_mgr.save_persistent_state()
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return True  # reconnect

    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
        # Logic/programming errors — do NOT reconnect, fail loudly
        log.critical(f"CODE DEFECT (not a connection issue): {type(e).__name__}: {e}", exc_info=True)
        risk_mgr.save_persistent_state()
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return False  # do NOT reconnect — fix the bug

    except Exception as e:
        log.error(f"Unexpected error (will reconnect): {e}", exc_info=True)
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return True  # reconnect


def _log_summary(instruments: list[InstrumentRunner]) -> None:
    """Print final summary of all instruments."""
    log.info("=" * 70)
    log.info("SESSION SUMMARY")
    log.info(f"{'Symbol':<10} {'Strategy':<20} {'Trades':>6} {'PnL':>12}")
    log.info("-" * 70)
    for inst in instruments:
        s = inst.state
        if inst.uses_pips:
            pnl_str = f"{s.pnl_pips:+.1f} pip"
        else:
            pnl_str = f"{s.pnl_points:+.2f} pts"
        log.info(f"{inst.symbol:<10} {inst.strategy:<20} {s.trade_count:>6} {pnl_str:>12}")
    log.info("=" * 70)


# ═════════════════════════════════════════════════════════════
# Auto-reconnect wrapper
# ═════════════════════════════════════════════════════════════
def run_with_reconnect(
    config_paths: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Outer loop: reconnect on connection failures with exponential backoff."""
    lock = ProcessLock(build_runner_lock_name(
        client_id=IBKR_CLIENT_ID,
        config_paths=config_paths,
        exclude=exclude,
    ))
    try:
        lock.acquire({
            "kind": "runner_unified",
            "client_id": IBKR_CLIENT_ID,
            "configs": sorted(Path(p).name for p in (config_paths or [])),
            "exclude": sorted(exclude or []),
        })
    except ProcessLockError as exc:
        log.error(f"DUPLICATE_RUNNER_BLOCKED: {exc}")
        return

    max_retries = 200
    retry_delay = 10.0

    # Generate session/cohort tracking once for entire session
    main._session_id = str(uuid.uuid4())[:8]
    main._runtime_start = time.time()
    main._is_reconnect = False

    # Capture git sha for cohort auditing (delegates to shared helper —
    # 2026-04-19 migration)
    from helio.strategy_common import git_sha as _git_sha
    main._git_sha = _git_sha()

    try:
        for attempt in range(max_retries):
            if attempt > 0:
                log.info(f"Reconnect attempt {attempt}/{max_retries} in {retry_delay:.0f}s...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 120)  # cap at 2 min
                # DESIGN DOCTRINE (2026-03-25): _is_reconnect is intentionally False here.
                # Bar buffer rebuilds from scratch on reconnect (300 bars seeded).
                # Only the trade open DURING disconnect is tainted (via restored_from_file).
                # Post-reconnect entries on fresh bar data are valid by design.
                # Previous session-wide tainting killed ALL trades after any single blip,
                # making it impossible to accumulate 30 valid trades for promotion.
                # See COHORT_SPEC.md "Trade-scoped with session reset" for full rationale.
                main._session_id = str(uuid.uuid4())[:8]
                main._runtime_start = time.time()
                main._is_reconnect = False
                log.info(f"New session after reconnect: {main._session_id}")
            else:
                retry_delay = 10.0

            should_reconnect = main(config_paths=config_paths, exclude=exclude)

            if should_reconnect is False:
                break  # clean exit
            if should_reconnect is True:
                log.info("Preparing to reconnect...")
                continue
    finally:
        lock.release()

    log.info("Unified runner stopped.")


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════
def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Argus Unified Multi-Instrument Runner — single process, single connection",
    )
    parser.add_argument(
        "--configs", nargs="*", default=None,
        help="Specific config file paths (default: all *_paper_v1.json in configs dir)",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=None,
        help="Symbols or config stems to exclude (e.g., eth_range btc_range sol)",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="IBKR client ID (default: from IBKR_CLIENT_ID env or 1). Use different IDs for parallel runners.",
    )
    args = parser.parse_args()
    global IBKR_CLIENT_ID
    IBKR_CLIENT_ID, client_id_source = resolve_client_id(
        config_paths=args.configs,
        explicit_client_id=args.client_id,
        default_client_id=IBKR_CLIENT_ID,
    )
    log.info(f"Resolved clientId={IBKR_CLIENT_ID} via {client_id_source}")
    run_with_reconnect(config_paths=args.configs, exclude=args.exclude)


if __name__ == "__main__":
    cli()
