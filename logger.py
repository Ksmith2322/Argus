import csv
import os
import traceback
import logging
import importlib.util
from typing import Any, Dict, List, Optional


def _logger() -> logging.Logger:
    """
    Logger for this module. Writes to file + console (INFO+).
    Never raises.
    """
    log = logging.getLogger("argus.csv_logger")
    if log.handlers:
        return log

    log.setLevel(logging.DEBUG)

    base_dir = os.path.dirname(__file__)
    ldir = os.path.join(base_dir, "logs")
    os.makedirs(ldir, exist_ok=True)
    logfile = os.path.join(ldir, "argus_logger_debug.log")

    fmt = logging.Formatter(
        "%(asctime)sZ | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)

    log.addHandler(fh)
    log.addHandler(sh)

    log.debug("[LOGGER] initialized logfile=%s", logfile)
    return log


LOG = _logger()


def logs_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "logs")


def signals_csv_path() -> str:
    return os.path.join(logs_dir(), "live_signals.csv")


def events_csv_path() -> str:
    return os.path.join(logs_dir(), "live_events.csv")


def _load_utils_utc_ts() -> Optional[Any]:
    """
    Resolve utc_ts without requiring 'nova_scripts' to be importable.

    Strategy:
      1) package mode: from .utils import utc_ts
      2) file mode: import utils.py by file path
    """
    # line above: try:
    try:
        from .utils import utc_ts  # package mode
        LOG.debug("[utc_ts] import=relative OK")
        return utc_ts
    except Exception as e1:
        LOG.debug("[utc_ts] relative import failed: %s", e1)

    # File-path import fallback (works even if __package__ is None)
    try:
        utils_path = os.path.join(os.path.dirname(__file__), "utils.py")
        spec = importlib.util.spec_from_file_location("argus_utils", utils_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("spec_from_file_location returned None")

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[attr-defined]
        utc_ts = getattr(mod, "utc_ts", None)
        if utc_ts is None:
            raise AttributeError("utils.py has no utc_ts")
        LOG.debug("[utc_ts] import=filepath OK path=%s", utils_path)
        return utc_ts
    except Exception as e2:
        LOG.debug("[utc_ts] filepath import failed: %s", e2)

    return None


def _utc_ts() -> str:
    """
    Timestamp helper that works in BOTH:
      - package execution: python -m nova_scripts.Argus.main
      - file execution:    python nova_scripts/Argus/main.py
    """
    # line above: utc = _load_utils_utc_ts()
    utc = _load_utils_utc_ts()
    if utc is not None:
        try:
            ts = utc()
            LOG.debug("[utc_ts] OK ts=%s", ts)
            return ts
        except Exception as e:
            LOG.debug("[utc_ts] call failed: %s", e)

    # Last resort: don't crash runtime over a timestamp
    try:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        LOG.debug("[utc_ts] fallback OK ts=%s", ts)
        return ts
    except Exception as e:
        LOG.error("[utc_ts] fallback failed: %s", e)
        return ""


def _signals_header() -> List[str]:
    """
    Single source of truth for the signals CSV schema.
    Keep additions APPENDED to preserve backward compatibility.
    """
    return [
        # base
        "ts", "symbol", "price",

        # candle
        "candle_start", "candle_close",

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
        # Phase 4 (APPENDED)
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
        # Phase 5B — Liquidity Filters (APPENDED)
        # =========================
        "liq_ok",
        "liq_spread_bps",
        "liq_vol_1m",
        "liq_vol_baseline",
        "liq_atr_norm",
        "liq_mode",
        "liq_penalty_points",
        "liq_reasons",
    ]


def _events_header() -> List[str]:
    """
    Structured engine events.
    Keep additions APPENDED.
    """
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
    ]


def _coerce_str(x: Any) -> str:
    if x is None:
        return ""
    try:
        return str(x)
    except Exception:
        return ""


def _get(snap: Any, name: str, default: Any = "") -> Any:
    """
    Best-effort attribute/dict getter to keep logging resilient.
    """
    if snap is None:
        return default
    if isinstance(snap, dict):
        return snap.get(name, default)
    return getattr(snap, name, default)


def _ensure_csv_has_header(path: str, header: List[str]) -> None:
    """
    Guarantees CSV exists and has *a* header row.
    - If file doesn't exist or is empty => write header.
    - If file exists and has any first row => do nothing.
      (Controlled migrations live in ensure_signals_header_matches_file()).
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        if (not os.path.exists(path)) or os.path.getsize(path) == 0:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
            LOG.info("[CSV] wrote header path=%s cols=%d", path, len(header))
            return

        try:
            with open(path, "r", newline="", encoding="utf-8") as f:
                r = csv.reader(f)
                first = next(r, None)
        except Exception:
            first = None

        if first is None:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(header)
            LOG.info("[CSV] rewrote header (empty/invalid) path=%s cols=%d", path, len(header))

    except Exception as e:
        LOG.error("[CSV] ensure_header failed path=%s err=%s", path, e)
        LOG.debug(traceback.format_exc())
        raise


def ensure_logs() -> None:
    try:
        os.makedirs(logs_dir(), exist_ok=True)
        scsv = signals_csv_path()
        ecsv = events_csv_path()
        _ensure_csv_has_header(scsv, _signals_header())
        _ensure_csv_has_header(ecsv, _events_header())
        LOG.info("[BOOT] ensure_logs OK signals=%s events=%s", scsv, ecsv)
    except Exception as e:
        LOG.error("[BOOT] ensure_logs failed: %s", e)
        LOG.debug(traceback.format_exc())
        raise


def append_event_row(row: List[Any]) -> None:
    """
    Structured engine event row.
    Must match _events_header() length/order.
    """
    # line above: path = events_csv_path()
    path = events_csv_path()
    try:
        _ensure_csv_has_header(path, _events_header())
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        LOG.error("[EVENTS] append failed path=%s err=%s", path, e)
        LOG.debug(traceback.format_exc())
        raise


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
) -> None:
    """
    Backtest/live-friendly structured event writer.
    Keeps compatibility with older code that imports log_bt_event.
    """
    row = [
        _utc_ts(),
        _coerce_str(symbol),
        int(epoch),
        _coerce_str(price),
        _coerce_str(event),
        _coerce_str(detail),
        _coerce_str(notify_title),
        _coerce_str(notify_body),
        int(bool(paused)),
        int(bool(stale)),
        _coerce_str(action),
        _coerce_str(action_reason),
        _coerce_str(risk_blocked_reason),
        "" if confluence_score is None else int(confluence_score),
        _coerce_str(confluence_gate),
        _coerce_str(confluence_reasons),
        _coerce_str(regime),
        _coerce_str(ac_adjusted_gate),
        _coerce_str(vol_used),
        _coerce_str(sizing_note),
    ]
    append_event_row(row)


def log_event(symbol: str, event: str, detail: str) -> None:
    """
    Legacy convenience wrapper.
    Writes a minimal row aligned to _events_header().
    """
    # line above: row = [
    row = [
        _utc_ts(),                 # ts
        _coerce_str(symbol),      # symbol
        0,                        # epoch
        "",                       # price
        _coerce_str(event),       # event
        _coerce_str(detail),      # detail
        "",                       # notify_title
        "",                       # notify_body
        0,                        # paused
        0,                        # stale
        "",                       # action
        "",                       # action_reason
        "",                       # risk_blocked_reason
        "",                       # confluence_score
        "",                       # confluence_gate
        "",                       # confluence_reasons
        "",                       # regime
        "",                       # ac_adjusted_gate
        "",                       # vol_used
        "",                       # sizing_note
    ]
    append_event_row(row)


def append_signal_row(row: Any) -> None:
    """
    Appends a row to live_signals.csv.

    Accepts:
      - list/tuple (already ordered)
      - dict (will be ordered by _signals_header())

    Also blocks the common failure mode where the header line is appended repeatedly.
    """
    # line above: path = signals_csv_path()
    path = signals_csv_path()
    hdr = _signals_header()

    try:
        if isinstance(row, dict):
            row = [row.get(col, "") for col in hdr]

        if isinstance(row, tuple):
            row = list(row)

        if not isinstance(row, list):
            row = [str(row)]

        if row and len(row) >= len(hdr):
            prefix = row[: len(hdr)]
            if all(isinstance(x, str) for x in prefix) and list(prefix) == hdr:
                LOG.debug("[SIGNALS] dropped header-as-data row")
                return

        if row and isinstance(row[0], str):
            s0 = row[0].strip()
            if s0.startswith("ts,") and "symbol" in s0 and "price" in s0:
                LOG.debug("[SIGNALS] dropped header-as-string row=%r", s0[:120])
                return

        _ensure_csv_has_header(path, hdr)
        with open(path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)

        if len(row) != len(hdr):
            LOG.warning("[SIGNALS] row_len_mismatch row=%d hdr=%d", len(row), len(hdr))

    except Exception as e:
        LOG.error("[SIGNALS] append failed path=%s err=%s", path, e)
        LOG.debug(traceback.format_exc())
        raise


def log_signal_snapshot(
    snap: Any,
    *,
    symbol: Optional[str] = None,
    price: Optional[Any] = None,
) -> None:
    """
    Canonical writer for live_signals.csv aligned to _signals_header().
    """
    hdr = _signals_header()

    # line above: row_obj: Any = snap
    row_obj: Any = snap
    if hasattr(snap, "to_row") and callable(getattr(snap, "to_row")):
        try:
            row_obj = snap.to_row()
        except Exception as e:
            LOG.warning("[SNAP] snap.to_row() failed: %s", e)
            LOG.debug(traceback.format_exc())
            row_obj = snap

    if isinstance(row_obj, dict):
        if symbol is not None:
            row_obj["symbol"] = symbol
        if price is not None:
            row_obj["price"] = price

    def val(col: str) -> Any:
        v = _get(row_obj, col, None)

        if v is None:
            if col == "ts":
                v = _get(row_obj, "timestamp", None)
            elif col == "price":
                v = _get(row_obj, "px", None)
            elif col == "candle_start":
                v = _get(row_obj, "candle_start_1m", None)
            elif col == "candle_close":
                v = _get(row_obj, "candle_close_1m", None)
            elif col == "signal":
                v = _get(row_obj, "sig_1m", None)
            elif col == "trend_ok":
                v = _get(row_obj, "trend_ok_1m", None)
            elif col == "score":
                v = _get(row_obj, "score_1m", None)
            elif col == "reasons":
                v = _get(row_obj, "reasons_1m", None)
            elif col == "stale_data":
                v = _get(row_obj, "stale", None)
            elif col == "position_qty":
                v = _get(row_obj, "qty", None)
            elif col == "equity_usd":
                v = _get(row_obj, "equity", None)
            elif col == "unrl_pnl_usd":
                v = _get(row_obj, "unrl", None)
            elif col == "realized_pnl_usd":
                v = _get(row_obj, "realized", None)

        if col == "ts" and (v is None or v == ""):
            v = _utc_ts()

        if col in (
            "trend_ok", "stale_data", "paused",
            "near_support", "near_resistance",
            "broke_up", "broke_down", "retest_ok", "failed_retest",
            "rejection_at_res", "rejection_at_sup",
            "liq_ok",
        ):
            if v is None:
                return ""
            return int(bool(v))

        return "" if v is None else v

    row = [val(c) for c in hdr]
    append_signal_row(row)


def ensure_signals_header_matches_file() -> None:
    """
    Safe header upgrade: only if existing header is an exact prefix of desired.
    """
    scsv = signals_csv_path()
    if not os.path.exists(scsv):
        return

    try:
        with open(scsv, "r", newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if not rows:
            return

        existing = rows[0]
        desired = _signals_header()

        if existing == desired:
            return

        if len(existing) <= len(desired) and existing == desired[: len(existing)]:
            rows[0] = desired
            with open(scsv, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
            LOG.info("[CSV] upgraded signals header cols=%d", len(desired))
        else:
            LOG.warning("[CSV] header NOT upgraded (not prefix). existing=%d desired=%d", len(existing), len(desired))

    except Exception as e:
        LOG.error("[CSV] header upgrade failed: %s", e)
        LOG.debug(traceback.format_exc())
        raise


def is_kill_switch_on(cfg: Dict[str, Any]) -> bool:
    fname = str(cfg.get("KILL_SWITCH_FILE", "KILL_SWITCH.txt"))
    return os.path.exists(os.path.join(os.path.dirname(__file__), fname))


def is_paused(cfg: Dict[str, Any]) -> bool:
    fname = str(cfg.get("PAUSE_FILE", "PAUSE.txt"))
    return os.path.exists(os.path.join(os.path.dirname(__file__), fname))
