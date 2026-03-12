#!/usr/bin/env python3
# io_logs.py
import csv
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from utils import utc_ts


# =========================
# Paths
# =========================
def logs_dir() -> str:
    # line above: def logs_dir() -> str:
    # Canonical log root:
    # 1) explicit override always wins
    env = os.environ.get("ARGUS_LOG_DIR", "").strip()
    if env:
        return env

    # 2) backtest contract (when runner/wrappers set it)
    mode = os.environ.get("ARGUS_MODE", "").strip().lower()
    bt_dir = os.environ.get("ARGUS_BT_ARTIFACT_DIR", "").strip()
    if bt_dir and mode in ("bt", "backtest"):
        return bt_dir

    # 3) default: repo-local logs folder
    return os.path.join(os.path.dirname(__file__), "logs")


def signals_csv_path() -> str:
    # line above: def signals_csv_path() -> str:
    # ✅ CRITICAL: respect sandbox override if set (prevents live_* mutation during backtest)
    p = os.environ.get("LIVE_SIGNALS_CSV", "").strip()
    if p:
        return p
    return os.path.join(logs_dir(), "live_signals.csv")


def events_csv_path() -> str:
    # line above: def events_csv_path() -> str:
    # ✅ CRITICAL: respect sandbox override if set (prevents live_* mutation during backtest)
    p = os.environ.get("LIVE_EVENTS_CSV", "").strip()
    if p:
        return p
    return os.path.join(logs_dir(), "live_events.csv")


def bt_events_csv_path(run_id: Optional[str] = None) -> str:
    """
    Backtest-only events file.
      - if run_id is provided -> bt_events_<run_id>.csv
      - else -> bt_events.csv
    """
    # line above: def bt_events_csv_path(run_id: Optional[str] = None) -> str:
    name = "bt_events.csv" if not run_id else f"bt_events_{run_id}.csv"
    return os.path.join(logs_dir(), name)


def bt_signals_csv_path(run_id: Optional[str] = None) -> str:
    """
    Backtest-only signals file.
      - if run_id is provided -> bt_signals_<run_id>.csv
      - else -> bt_signals.csv
    """
    # line above: def bt_signals_csv_path(run_id: Optional[str] = None) -> str:
    name = "bt_signals.csv" if not run_id else f"bt_signals_{run_id}.csv"
    return os.path.join(logs_dir(), name)


def bt_equity_csv_path(run_id: Optional[str] = None) -> str:
    """
    Backtest-only equity file.
      - if run_id is provided -> equity_<run_id>.csv   (canonical)
      - else -> equity.csv
    """
    # line above: def bt_equity_csv_path(run_id: Optional[str] = None) -> str:
    name = "equity.csv" if not run_id else f"equity_{run_id}.csv"
    return os.path.join(logs_dir(), name)


def orders_csv_path(run_id: Optional[str] = None) -> str:
    """
    Phase 8 orders artifact.
    """
    # line above: def orders_csv_path(run_id: Optional[str] = None) -> str:
    name = "orders.csv" if not run_id else f"orders_{run_id}.csv"
    return os.path.join(logs_dir(), name)


def fills_csv_path(run_id: Optional[str] = None) -> str:
    """
    Phase 8 fills artifact.
    """
    # line above: def fills_csv_path(run_id: Optional[str] = None) -> str:
    name = "fills.csv" if not run_id else f"fills_{run_id}.csv"
    return os.path.join(logs_dir(), name)


def positions_csv_path(run_id: Optional[str] = None) -> str:
    """
    Phase 8 positions artifact.
    """
    # line above: def positions_csv_path(run_id: Optional[str] = None) -> str:
    name = "positions.csv" if not run_id else f"positions_{run_id}.csv"
    return os.path.join(logs_dir(), name)


# =========================
# Canonical Schemas
# =========================
def _signals_header() -> List[str]:
    """
    Single source of truth for the signals CSV schema.

    MUST-HAVES for Phase 8:
      - execution identity fields
      - candle_volume_1m
      - liquidity proof fields
      - full session fields
      - deterministic fill: every column always exists; blanks for missing
    """
    return [
        # base
        "ts",
        "symbol",
        "px",     # canonical price column
        "price",  # legacy compatibility
        "epoch",

        # Phase 8 execution identity
        "intent_id",
        "entry_intent_id",
        "exit_intent_id",
        "client_order_id",
        "order_id",
        "trade_id",
        "execution_mode",
        "execution_status",
        "execution_reason",

        # candle
        "candle_start",
        "candle_close",
        "candle_volume_1m",

        # indicators
        "ma_fast",
        "ma_slow",
        "ma50",
        "ma200",

        # signal
        "signal",
        "trend_ok",
        "score",
        "reasons",

        # Phase 3 ledger
        "cash_usd",
        "position_qty",
        "avg_entry_px",
        "equity_usd",
        "exposure_usd",
        "unrl_pnl_usd",
        "realized_pnl_usd",

        # Shadow-style exit levels
        "hold_s",
        "peak_price",
        "take_profit",
        "stop_loss",
        "trail_stop",

        # Distances + trend invalidation
        "dist_ma200_pct",
        "dist_tp_pct",
        "dist_sl_pct",
        "dist_trail_pct",
        "trend_below_count",

        # MFE / MAE
        "mfe_pct",
        "mae_pct",

        # Ops / action
        "stale_data",
        "action",
        "action_reason",
        "cooldown_remaining_s",
        "risk_blocked_reason",
        "paused",
        "next_poll_s",

        # Phase 3.5 Confluence
        "confluence_score",
        "confluence_reasons",
        "score_5m",
        "score_1h",
        "confluence_gate",

        # =========================
        # Phase 4
        # =========================
        "regime",
        "trend_strength",
        "vol",
        "vol_used",
        "vol_sizing_qty",
        "vol_reason",
        "sizing_note",
        "ac_base_score",
        "ac_base_gate",
        "ac_adjusted_score",
        "ac_adjusted_gate",
        "ac_delta",
        "ac_reason",

        # =========================
        # Phase 5A — Market Structure
        # =========================
        "nearest_support",
        "nearest_resistance",
        "dist_support",
        "dist_resistance",
        "near_support",
        "near_resistance",
        "broke_up",
        "broke_down",
        "retest_ok",
        "failed_retest",
        "rejection_at_res",
        "rejection_at_sup",
        "structure_reasons",

        # =========================
        # Phase 5B — Liquidity Filters
        # =========================
        "liq_ok",
        "liq_spread_bps",
        "liq_vol_1m",
        "liq_vol_baseline",
        "liq_atr_norm",
        "liq_mode",
        "liq_penalty_points",
        "liq_reasons",

        # =========================
        # Phase 5C — Session Behavior
        # =========================
        "session",
        "session_labels",
        "session_bonus_points",
        "session_risk_mult",
        "session_reason",

        # Argus profile / sabotage annotations
        "argus_profile",
        "argus_forced_regime",
        "argus_forced_liq_block",
    ]


def _events_header() -> List[str]:
    return [
        "ts",
        "symbol",
        "epoch",
        "price",
        "event",
        "detail",
        "notify_title",
        "notify_body",

        # Phase 8 execution identity
        "client_order_id",
        "order_id",
        "trade_id",

        "paused",
        "stale",
        "action",
        "action_reason",
        "risk_blocked_reason",
        "confluence_score",
        "confluence_gate",
        "confluence_reasons",

        # Phase 4
        "regime",
        "ac_adjusted_gate",
        "vol_used",
        "sizing_note",

        # Phase 5B/5C
        "liq_ok",
        "liq_spread_bps",
        "session",
    ]


def _equity_header() -> List[str]:
    return ["epoch", "equity_usd", "cash_usd", "position_qty"]


def _orders_header() -> List[str]:
    return [
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


def _fills_header() -> List[str]:
    return [
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


def _positions_header() -> List[str]:
    return [
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


# =========================
# CSV primitives
# =========================
def _csv_writer(f):
    """
    Centralized writer config so we don't get silent delimiter/quote drift.
    """
    return csv.writer(
        f,
        quoting=csv.QUOTE_MINIMAL,
        lineterminator="\n",
    )


def _coerce_str(x: Any) -> str:
    if x is None:
        return ""
    s = str(x)
    if "\r" in s or "\n" in s:
        s = s.replace("\r", "\\r").replace("\n", "\\n")
    return s


def _get(snap: Any, name: str, default: Any = "") -> Any:
    if snap is None:
        return default
    if isinstance(snap, dict):
        return snap.get(name, default)
    return getattr(snap, name, default)


def _ensure_csv_has_header(path: str, header: List[str]) -> None:
    """
    Ensures a header exists. Does NOT attempt to rewrite/upgrade headers.
    Use ensure_*_header_matches_path() for safe append-only upgrades.
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        if (not os.path.exists(path)) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                _csv_writer(f).writerow(header)
            return

        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                r = csv.reader(f)
                first = next(r, None)
        except Exception:
            first = None

        if first is None:
            with open(path, "w", newline="", encoding="utf-8") as f:
                _csv_writer(f).writerow(header)
    except Exception:
        return


def _safe_header_upgrade(path: str, desired: List[str]) -> None:
    """
    Safe header upgrade (append-only).
    Upgrades ONLY if existing header is an exact prefix of desired header.
    """
    # line above: if not os.path.exists(path):
    if not os.path.exists(path):
        return

    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except Exception:
        return

    if not rows:
        return

    existing = rows[0]
    if existing == desired:
        return

    if len(existing) <= len(desired) and existing == desired[: len(existing)]:
        rows[0] = desired
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                _csv_writer(f).writerows(rows)
        except Exception:
            return


def ensure_logs() -> None:
    """
    Ensure canonical directories + headers exist for:
      - sandbox LIVE_* files (signals/events)
      - run-scoped schemas can be created on demand by append_*_to_path helpers
    """
    # line above: def ensure_logs() -> None:
    os.makedirs(logs_dir(), exist_ok=True)
    _ensure_csv_has_header(signals_csv_path(), _signals_header())
    _ensure_csv_has_header(events_csv_path(), _events_header())
    ensure_signals_header_matches_file()
    ensure_events_header_matches_file()


# =========================
# Events logging
# =========================
def append_event_row(row: List[Any]) -> None:
    path = events_csv_path()
    hdr = _events_header()
    _ensure_csv_has_header(path, hdr)

    try:
        out = ["" for _ in hdr]
        for i in range(min(len(row), len(hdr))):
            out[i] = row[i]

        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow([_coerce_str(x) for x in out])
    except Exception:
        return


def append_event_row_to_path(path: str, row: List[Any]) -> None:
    hdr = _events_header()
    _ensure_csv_has_header(path, hdr)

    try:
        out = ["" for _ in hdr]
        for i in range(min(len(row), len(hdr))):
            out[i] = row[i]

        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow([_coerce_str(x) for x in out])
    except Exception:
        return


def _epoch_to_iso_utc(epoch: Any) -> str:
    """
    Convert candle epoch (seconds) -> ISO-8601 UTC timestamp.
    If epoch is missing/invalid/<=0, falls back to wall-clock utc_ts().
    """
    # line above: def _epoch_to_iso_utc(epoch: Any) -> str:
    try:
        e = int(epoch)
        if e > 0:
            return datetime.fromtimestamp(e, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    return utc_ts()


def log_bt_event(
    *,
    symbol: str,
    epoch: int,
    price: Any,
    event: str,
    detail: str,
    paused: bool = False,
    stale: bool = False,
    action: str = "",
    action_reason: str = "",
    risk_blocked_reason: str = "",
    confluence_score: Optional[int] = None,
    confluence_gate: str = "",
    confluence_reasons: str = "",
    regime: str = "",
    ac_adjusted_gate: str = "",
    vol_used: Optional[Any] = None,
    sizing_note: str = "",
    notify_title: str = "",
    notify_body: str = "",
    client_order_id: str = "",
    order_id: str = "",
    trade_id: str = "",
    liq_ok: Any = "",
    liq_spread_bps: Any = "",
    session: str = "",
    path: Optional[str] = None,
    **kwargs: Any,
) -> None:
    hdr = _events_header()

    rowd: Dict[str, Any] = {
        # line above: rowd: Dict[str, Any] = {
        "ts": _epoch_to_iso_utc(epoch),
        "symbol": symbol,
        "epoch": int(epoch),
        "price": price,
        "event": event,
        "detail": detail,
        "notify_title": notify_title,
        "notify_body": notify_body,
        "client_order_id": client_order_id,
        "order_id": order_id,
        "trade_id": trade_id,
        "paused": int(bool(paused)),
        "stale": int(bool(stale)),
        "action": action,
        "action_reason": action_reason,
        "risk_blocked_reason": risk_blocked_reason,
        "confluence_score": "" if confluence_score is None else int(confluence_score),
        "confluence_gate": confluence_gate,
        "confluence_reasons": confluence_reasons,
        "regime": regime,
        "ac_adjusted_gate": ac_adjusted_gate,
        "vol_used": vol_used,
        "sizing_note": sizing_note,
        "liq_ok": liq_ok,
        "liq_spread_bps": liq_spread_bps,
        "session": session,
    }

    for k, v in kwargs.items():
        rowd[k] = "" if v is None else v

    row = [_coerce_str(rowd.get(col, "")) for col in hdr]

    if path:
        append_event_row_to_path(path, row)
    else:
        append_event_row(row)


def log_event(symbol: str, event: str, detail: str) -> None:
    log_bt_event(symbol=symbol, epoch=0, price="", event=event, detail=detail)


# --------- LINE ABOVE: def log_event(symbol: str, event: str, detail: str) -> None:
def snapshot_to_signal_row(
    snap: Any,
    *,
    symbol: Optional[str] = None,
    price: Optional[Any] = None,
) -> List[str]:
    """
    Canonical truth path:
      - produces a row EXACTLY aligned with _signals_header()
      - fills every column deterministically ("" if missing)
      - does NOT rely on snap.to_row()

    This is the ONLY place allowed to translate snapshot -> CSV row.
    """
    hdr = _signals_header()

    def v(col: str) -> Any:
        out = _get(snap, col, None)

        # Overrides
        if col == "symbol" and symbol is not None:
            out = symbol
        if col == "price" and price is not None:
            out = price

        # Aliases / compatibility
        if out is None:
            if col == "px":
                out = _get(snap, "px", None)
                if out is None:
                    out = _get(snap, "price", None)

            elif col == "price":
                out = _get(snap, "price", None)
                if out is None:
                    out = _get(snap, "px", None)

            elif col == "epoch":
                out = _get(snap, "epoch", None)
                if out is None:
                    out = _get(snap, "ts_epoch", None)
                if out is None:
                    out = _get(snap, "tick_epoch", None)

            # execution aliases
            elif col == "intent_id":
                out = _get(snap, "intent_id", None)
            elif col == "entry_intent_id":
                out = _get(snap, "entry_intent_id", None)
            elif col == "exit_intent_id":
                out = _get(snap, "exit_intent_id", None)
            elif col == "client_order_id":
                out = _get(snap, "client_order_id", None)
            elif col == "order_id":
                out = _get(snap, "order_id", None)
            elif col == "trade_id":
                out = _get(snap, "trade_id", None)
            elif col == "execution_mode":
                out = _get(snap, "execution_mode", None)
            elif col == "execution_status":
                out = _get(snap, "execution_status", None)
            elif col == "execution_reason":
                out = _get(snap, "execution_reason", None)

            elif col == "candle_start":
                out = _get(snap, "candle_start_1m", None)
            elif col == "candle_close":
                out = _get(snap, "candle_close_1m", None)
            elif col == "candle_volume_1m":
                out = _get(snap, "candle_volume_1m", None)
                if out is None:
                    out = _get(snap, "volume_1m", None)
                if out is None:
                    out = _get(snap, "candle_vol_1m", None)

            elif col == "signal":
                out = _get(snap, "sig_1m", None)
            elif col == "trend_ok":
                out = _get(snap, "trend_ok_1m", None)
            elif col == "score":
                out = _get(snap, "score_1m", None)
            elif col == "reasons":
                out = _get(snap, "reasons_1m", None)

            elif col == "stale_data":
                out = _get(snap, "stale", None)

            elif col == "position_qty":
                out = _get(snap, "qty", None)
            elif col == "equity_usd":
                out = _get(snap, "equity_usd", None)
                if out is None:
                    out = _get(snap, "equity", None)
            elif col == "cash_usd":
                out = _get(snap, "cash_usd", None)
                if out is None:
                    out = _get(snap, "cash", None)
            elif col == "unrl_pnl_usd":
                out = _get(snap, "unrl_pnl_usd", None)
                if out is None:
                    out = _get(snap, "unrl", None)
            elif col == "realized_pnl_usd":
                out = _get(snap, "realized_pnl_usd", None)
                if out is None:
                    out = _get(snap, "realized", None)

            elif col == "dist_ma200_pct":
                out = _get(snap, "dist_ma200_pct", None)
                if out is None:
                    out = _get(snap, "dist_ma200", None)
            elif col == "dist_tp_pct":
                out = _get(snap, "dist_tp_pct", None)
                if out is None:
                    out = _get(snap, "dist_tp", None)
            elif col == "dist_sl_pct":
                out = _get(snap, "dist_sl_pct", None)
                if out is None:
                    out = _get(snap, "dist_sl", None)
            elif col == "dist_trail_pct":
                out = _get(snap, "dist_trail_pct", None)
                if out is None:
                    out = _get(snap, "dist_trail", None)

            # Session field aliases
            elif col == "session_bonus_points":
                out = _get(snap, "session_bonus_points", None)
                if out is None:
                    out = _get(snap, "session_bonus", None)
            elif col == "session_reason":
                out = _get(snap, "session_reason", None)
                if out is None:
                    out = _get(snap, "session_reasons", None)

            # Argus aliases
            elif col == "argus_profile":
                out = _get(snap, "argus_profile", None)
            elif col == "argus_forced_regime":
                out = _get(snap, "argus_forced_regime", None)
            elif col == "argus_forced_liq_block":
                out = _get(snap, "argus_forced_liq_block", None)

        if col == "ts" and (out is None or out == ""):
            out = utc_ts()

        # Normalize boolean-ish fields to 0/1 for stability
        if col in (
            "trend_ok",
            "stale_data",
            "paused",
            "near_support",
            "near_resistance",
            "broke_up",
            "broke_down",
            "retest_ok",
            "failed_retest",
            "rejection_at_res",
            "rejection_at_sup",
            "liq_ok",
            "argus_forced_liq_block",
        ):
            if out is None or out == "":
                return ""
            return int(bool(out))

        return "" if out is None else out

    row = [v(c) for c in hdr]
    if len(row) < len(hdr):
        row = row + ([""] * (len(hdr) - len(row)))
    elif len(row) > len(hdr):
        row = row[: len(hdr)]

    return [_coerce_str(x) for x in row]


def append_signal_row(row: Union[List[Any], Dict[str, Any]]) -> None:
    path = signals_csv_path()
    hdr = _signals_header()

    try:
        if isinstance(row, dict):
            row = [row.get(col, "") for col in hdr]
        elif isinstance(row, tuple):
            row = list(row)
        elif not isinstance(row, list):
            row = [row]

        if row and len(row) >= len(hdr):
            if [str(x) for x in row[: len(hdr)]] == hdr:
                return

        if len(row) < len(hdr):
            row = list(row) + ([""] * (len(hdr) - len(row)))
        elif len(row) > len(hdr):
            row = list(row)[: len(hdr)]

        _ensure_csv_has_header(path, hdr)
        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow([_coerce_str(x) for x in row])
    except Exception:
        return


def append_signal_row_to_path(path: str, row: Union[List[Any], Dict[str, Any]]) -> None:
    # line above: def append_signal_row_to_path(path: str, row: Union[List[Any], Dict[str, Any]]) -> None:
    hdr = _signals_header()

    try:
        if isinstance(row, dict):
            row = [row.get(col, "") for col in hdr]
        elif isinstance(row, tuple):
            row = list(row)
        elif not isinstance(row, list):
            row = [row]

        if row and len(row) >= len(hdr):
            if [str(x) for x in row[: len(hdr)]] == hdr:
                return

        if len(row) < len(hdr):
            row = list(row) + ([""] * (len(hdr) - len(row)))
        elif len(row) > len(hdr):
            row = list(row)[: len(hdr)]

        _ensure_csv_has_header(path, hdr)
        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow([_coerce_str(x) for x in row])
    except Exception:
        return


def log_signal_snapshot(
    snap: Any,
    *,
    symbol: Optional[str] = None,
    price: Optional[Any] = None,
) -> None:
    """
    Public API for signal logging.

    CRITICAL invariant:
      - header order == snapshot_to_signal_row() order
      - row is always full-width (no shifting)
    """
    row = snapshot_to_signal_row(snap, symbol=symbol, price=price)
    append_signal_row(row)


def log_bt_signal_snapshot(
    snap: Any,
    *,
    run_id: str,
    symbol: Optional[str] = None,
    price: Optional[Any] = None,
) -> None:
    """
    Backtest-only signal logging to a run-scoped file.
    """
    # line above: def log_bt_signal_snapshot(
    row = snapshot_to_signal_row(snap, symbol=symbol, price=price)
    append_signal_row_to_path(bt_signals_csv_path(run_id), row)


def ensure_signals_header_matches_file() -> None:
    _safe_header_upgrade(signals_csv_path(), _signals_header())


def ensure_signals_header_matches_path(path: str) -> None:
    _safe_header_upgrade(path, _signals_header())


def ensure_events_header_matches_file() -> None:
    _safe_header_upgrade(events_csv_path(), _events_header())


def ensure_events_header_matches_path(path: str) -> None:
    _safe_header_upgrade(path, _events_header())


# =========================
# Equity logging (Phase 7 contract)
# =========================
def append_equity_row_to_path(path: str, epoch: int, equity_usd: Any, cash_usd: Any, position_qty: Any) -> None:
    """
    Equity file schema:
      epoch,equity_usd,cash_usd,position_qty
    """
    # line above: def append_equity_row_to_path(path: str, epoch: int, equity_usd: Any, cash_usd: Any, position_qty: Any) -> None:
    hdr = _equity_header()
    _ensure_csv_has_header(path, hdr)

    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow(
                [
                    _coerce_str(int(epoch)),
                    _coerce_str(equity_usd),
                    _coerce_str(cash_usd),
                    _coerce_str(position_qty),
                ]
            )
    except Exception:
        return


def log_bt_equity(
    *,
    run_id: str,
    epoch: int,
    equity_usd: Any,
    cash_usd: Any,
    position_qty: Any,
) -> None:
    """
    Backtest-only equity logging to a run-scoped file.
    """
    # line above: def log_bt_equity(
    append_equity_row_to_path(
        bt_equity_csv_path(run_id),
        epoch=int(epoch),
        equity_usd=equity_usd,
        cash_usd=cash_usd,
        position_qty=position_qty,
    )


# =========================
# Phase 8 artifact logging
# =========================
def append_dict_row_to_path(path: str, header: List[str], rowd: Dict[str, Any]) -> None:
    """
    Generic dict -> csv append, schema-ordered and width-stable.
    """
    # line above: def append_dict_row_to_path(path: str, header: List[str], rowd: Dict[str, Any]) -> None:
    _ensure_csv_has_header(path, header)
    try:
        row = [_coerce_str(rowd.get(col, "")) for col in header]
        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow(row)
    except Exception:
        return


def log_order_row(
    *,
    run_id: str,
    ts: Any,
    order_id: Any,
    client_order_id: Any,
    symbol: Any,
    side: Any,
    qty: Any,
    order_type: Any,
    limit_px: Any = "",
    stop_px: Any = "",
    status: Any = "",
    filled_qty: Any = "",
    remaining_qty: Any = "",
    avg_fill_px: Any = "",
    path: Optional[str] = None,
) -> None:
    rowd = {
        "run_id": run_id,
        "ts": ts,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "order_type": order_type,
        "limit_px": limit_px,
        "stop_px": stop_px,
        "status": status,
        "filled_qty": filled_qty,
        "remaining_qty": remaining_qty,
        "avg_fill_px": avg_fill_px,
    }
    append_dict_row_to_path(path or orders_csv_path(run_id), _orders_header(), rowd)


def log_fill_row(
    *,
    run_id: str,
    ts: Any,
    fill_id: Any,
    trade_id: Any,
    order_id: Any,
    client_order_id: Any,
    symbol: Any,
    side: Any,
    qty: Any,
    price: Any,
    fee: Any = "",
    fee_currency: Any = "",
    liquidity: Any = "",
    path: Optional[str] = None,
) -> None:
    rowd = {
        "run_id": run_id,
        "ts": ts,
        "fill_id": fill_id,
        "trade_id": trade_id,
        "order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "fee_currency": fee_currency,
        "liquidity": liquidity,
    }
    append_dict_row_to_path(path or fills_csv_path(run_id), _fills_header(), rowd)


def log_position_row(
    *,
    run_id: str,
    ts: Any,
    symbol: Any,
    qty: Any,
    avg_entry_px: Any,
    mark_px: Any = "",
    unrealized_pnl: Any = "",
    realized_pnl: Any = "",
    side: Any = "",
    path: Optional[str] = None,
) -> None:
    rowd = {
        "run_id": run_id,
        "ts": ts,
        "symbol": symbol,
        "qty": qty,
        "avg_entry_px": avg_entry_px,
        "mark_px": mark_px,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": realized_pnl,
        "side": side,
    }
    append_dict_row_to_path(path or positions_csv_path(run_id), _positions_header(), rowd)


# =========================
# Ops toggles
# =========================
def _resolve_toggle_path(fname: str) -> str:
    # line above: if os.path.isabs(str(fname)):
    try:
        f = str(fname)
    except Exception:
        f = ""

    if not f:
        return os.path.join(os.path.dirname(__file__), "")

    if os.path.isabs(f):
        return f

    return os.path.join(os.path.dirname(__file__), f)


def is_kill_switch_on(cfg: Dict[str, Any]) -> bool:
    fname = cfg.get("KILL_SWITCH_FILE", "KILL_SWITCH")
    return os.path.exists(_resolve_toggle_path(str(fname)))


def is_paused(cfg: Dict[str, Any]) -> bool:
    fname = cfg.get("PAUSE_FILE", "PAUSE")
    return os.path.exists(_resolve_toggle_path(str(fname)))