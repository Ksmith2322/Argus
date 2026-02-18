#!/usr/bin/env python3
# io_logs.py
# line above: import csv
import csv
import os
from typing import Any, Dict, List, Optional, Union
from utils import utc_ts


# =========================
# Paths
# =========================
def logs_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "logs")


def signals_csv_path() -> str:
    return os.path.join(logs_dir(), "live_signals.csv")


def events_csv_path() -> str:
    return os.path.join(logs_dir(), "live_events.csv")


# =========================
# Canonical Schemas
# =========================
def _signals_header() -> List[str]:
    """
    Single source of truth for the signals CSV schema.

    MUST-HAVES for Phase 5 closure:
      - candle_volume_1m
      - liquidity proof fields (spread_bps, vol_1m, baseline, atr_norm)
      - all session fields (Phase 5C)
      - deterministic fill: every column always exists; blanks for missing
    """
    return [
        # base
        "ts", "symbol", "price", "epoch",

        # candle (include V for liquidity baseline proof)
        "candle_start", "candle_close", "candle_volume_1m",

        # indicators
        "ma_fast", "ma_slow", "ma50", "ma200",

        # signal
        "signal", "trend_ok",
        "score", "reasons",

        # Phase 3 ledger
        "cash_usd", "position_qty", "avg_entry_px",
        "equity_usd", "exposure_usd",
        "unrl_pnl_usd", "realized_pnl_usd",

        # Shadow-style exit levels
        "hold_s", "peak_price",
        "take_profit", "stop_loss", "trail_stop",

        # Distances + trend invalidation
        "dist_ma200_pct", "dist_tp_pct", "dist_sl_pct", "dist_trail_pct",
        "trend_below_count",

        # MFE / MAE
        "mfe_pct", "mae_pct",

        # Ops / action
        "stale_data",
        "action", "action_reason",
        "cooldown_remaining_s",
        "risk_blocked_reason",
        "paused", "next_poll_s",

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
        # Phase 5A â€” Market Structure
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
        # Phase 5B â€” Liquidity Filters (proof fields)
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
        # Phase 5C â€” Session Behavior (full fields)
        # =========================
        "session",
        "session_labels",
        "session_bonus_points",
        "session_risk_mult",
        "session_reason",
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

        # Phase 5B/5C (append-only)
        "liq_ok",
        "liq_spread_bps",
        "session",
    ]


# =========================
# CSV primitives (NO manual joining, ever)
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
    # prevent multiline CSV corruption
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
    Use ensure_signals_header_matches_file() for safe append-only upgrades.
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


def ensure_logs() -> None:
    os.makedirs(logs_dir(), exist_ok=True)
    _ensure_csv_has_header(signals_csv_path(), _signals_header())
    _ensure_csv_has_header(events_csv_path(), _events_header())
    # safe append-only schema upgrade for signals (optional but helpful)
    ensure_signals_header_matches_file()


# =========================
# Events logging
# =========================
def append_event_row(row: List[Any]) -> None:
    path = events_csv_path()
    hdr = _events_header()
    _ensure_csv_has_header(path, hdr)

    try:
        # force width stability
        out = ["" for _ in hdr]
        for i in range(min(len(row), len(hdr))):
            out[i] = row[i]

        with open(path, "a", newline="", encoding="utf-8") as f:
            _csv_writer(f).writerow([_coerce_str(x) for x in out])
    except Exception:
        return


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
    # accept schema drift (Phase 5+ additions) without crashing
    **kwargs: Any,
) -> None:
    hdr = _events_header()

    rowd: Dict[str, Any] = {
        "ts": utc_ts(),
        "symbol": symbol,
        "epoch": int(epoch),
        "price": price,
        "event": event,
        "detail": detail,
        "notify_title": notify_title,
        "notify_body": notify_body,
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
    }

    # Merge any new fields safely: only hdr fields will be written
    for k, v in kwargs.items():
        rowd[k] = "" if v is None else v

    row = [_coerce_str(rowd.get(col, "")) for col in hdr]
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

        # Aliases for common schema drift (explicit and cheap)
        if out is None:
            if col == "price":
                out = _get(snap, "px", None)

            elif col == "epoch":
                out = _get(snap, "ts_epoch", None)
                if out is None:
                    out = _get(snap, "tick_epoch", None)

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
                out = _get(snap, "equity", None)
            elif col == "unrl_pnl_usd":
                out = _get(snap, "unrl", None)
            elif col == "realized_pnl_usd":
                out = _get(snap, "realized", None)

            elif col == "dist_ma200_pct":
                out = _get(snap, "dist_ma200", None)
            elif col == "dist_tp_pct":
                out = _get(snap, "dist_tp", None)
            elif col == "dist_sl_pct":
                out = _get(snap, "dist_sl", None)
            elif col == "dist_trail_pct":
                out = _get(snap, "dist_trail", None)

            # Session field aliases (Phase 5C drift)
            elif col == "session_bonus_points":
                out = _get(snap, "session_bonus", None)
            elif col == "session_reason":
                out = _get(snap, "session_reasons", None)

        # Default timestamp (always present)
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
        # --------- LINE ABOVE: try:
        if isinstance(row, dict):
            row = [row.get(col, "") for col in hdr]
        elif isinstance(row, tuple):
            row = list(row)
        elif not isinstance(row, list):
            row = [row]

        # Drop accidental header-as-data
        if row and len(row) >= len(hdr):
            if [str(x) for x in row[: len(hdr)]] == hdr:
                return

        # Force width stability
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


def ensure_signals_header_matches_file() -> None:
    """
    Safe header upgrade (append-only).
    Upgrades ONLY if existing header is an exact prefix of desired header.
    """
    scsv = signals_csv_path()
    if not os.path.exists(scsv):
        return

    try:
        with open(scsv, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except Exception:
        return

    if not rows:
        return

    existing = rows[0]
    desired = _signals_header()

    if existing == desired:
        return

    # only upgrade if existing is exact prefix (safe append-only upgrades)
    if len(existing) <= len(desired) and existing == desired[: len(existing)]:
        rows[0] = desired
        try:
            with open(scsv, "w", newline="", encoding="utf-8") as f:
                _csv_writer(f).writerows(rows)
        except Exception:
            return


# =========================
# Ops toggles
# =========================
def is_kill_switch_on(cfg: Dict[str, Any]) -> bool:
    fname = cfg.get("KILL_SWITCH_FILE", "KILL_SWITCH")
    return os.path.exists(os.path.join(os.path.dirname(__file__), str(fname)))


def is_paused(cfg: Dict[str, Any]) -> bool:
    fname = cfg.get("PAUSE_FILE", "PAUSE")
    return os.path.exists(os.path.join(os.path.dirname(__file__), str(fname)))

