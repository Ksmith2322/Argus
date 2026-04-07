#!/usr/bin/env python3
# trade_journal.py

from __future__ import annotations

import csv
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


# =========================================================
# Canonical schema
# =========================================================

TRADE_JOURNAL_FIELDS: List[str] = [
    # Core identity / lifecycle (required)
    "trade_id",
    "run_id",
    "symbol",
    "entry_time",
    "exit_time",
    "entry_px",
    "exit_px",
    "qty",
    "pnl",
    "entry_reason",
    "exit_reason",
    "entry_order_id",
    "exit_order_id",
    "entry_fill_id",
    "exit_fill_id",
    "state_open_ts",
    "state_closed_ts",
    # Phase 9: trade lifecycle finalization (optional — empty string allowed)
    "entry_fee",        # fee paid on BUY fill (USD)
    "exit_fee",         # fee paid on SELL fill (USD)
    "total_fee",        # entry_fee + exit_fee
    "mae_pct",          # max adverse excursion % from entry (negative, e.g. -0.005)
    "mfe_pct",          # max favorable excursion % from entry (positive, e.g. 0.012)
    "regime_at_entry",  # regime label at time of entry (TRENDING/RANGING/UNKNOWN)
    "trade_duration_s", # seconds from entry_time to exit_time
    "slippage_bps",     # configured slippage in bps (from PAPER_SLIPPAGE_BPS / SLIPPAGE_BPS)
    "entry_mid_px",     # mid price at entry before slippage (tick price)
    "exit_mid_px",      # mid price at exit before slippage (tick price)
]

# Only the original 17 fields are required to be non-empty.
# Phase 9 fields are optional — allowed to be empty on recovered/backfilled rows.
REQUIRED_FIELDS: set = set(TRADE_JOURNAL_FIELDS[:17])
OPTIONAL_TRADE_JOURNAL_FIELDS: set = set(TRADE_JOURNAL_FIELDS[17:])


# =========================================================
# Data model
# =========================================================

@dataclass(frozen=True)
class TradeJournalRow:
    # Required fields
    trade_id: str
    run_id: str
    symbol: str
    entry_time: str
    exit_time: str
    entry_px: str
    exit_px: str
    qty: str
    pnl: str
    entry_reason: str
    exit_reason: str
    entry_order_id: str
    exit_order_id: str
    entry_fill_id: str
    exit_fill_id: str
    state_open_ts: str
    state_closed_ts: str
    # Phase 9 optional fields (default empty for backward compatibility)
    entry_fee: str = ""
    exit_fee: str = ""
    total_fee: str = ""
    mae_pct: str = ""
    mfe_pct: str = ""
    regime_at_entry: str = ""
    trade_duration_s: str = ""
    slippage_bps: str = ""
    entry_mid_px: str = ""
    exit_mid_px: str = ""

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


# =========================================================
# Normalization helpers
# =========================================================

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _normalize_epoch_seconds(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        if isinstance(value, bool):
            return None
        v = int(float(value))
    except Exception:
        return None

    if v >= 100_000_000_000:
        v = v // 1000
    return v


def _norm_decimal(value: Any, places: Optional[int] = None) -> str:
    if value is None or value == "":
        return ""
    dec = Decimal(str(value))
    if places is not None:
        quant = Decimal("1." + ("0" * places))
        dec = dec.quantize(quant)
    return format(dec, "f")


def _norm_ts(value: Any) -> str:
    """
    Accepts:
      - ISO string
      - datetime
      - epoch seconds
      - epoch milliseconds
      - None

    Returns:
      ISO-8601 string or empty string
    """
    if value is None or value == "":
        return ""

    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    s = str(value).strip()
    if not s:
        return ""

    epoch_guess = _normalize_epoch_seconds(s)
    if epoch_guess is not None:
        try:
            return datetime.fromtimestamp(epoch_guess, tz=timezone.utc).isoformat()
        except Exception:
            pass

    return s


def _parse_decimal(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _parse_iso_or_epoch(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    epoch_guess = _normalize_epoch_seconds(value)
    if epoch_guess is not None:
        try:
            return datetime.fromtimestamp(epoch_guess, tz=timezone.utc)
        except Exception:
            return None

    s = str(value).strip()
    if not s:
        return None

    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# =========================================================
# Paths / file creation
# =========================================================

def trade_journal_path(log_dir: str | Path, run_id: str) -> Path:
    return Path(log_dir) / f"trade_journal_{_norm_str(run_id)}.csv"


def ensure_trade_journal(log_dir: str | Path, run_id: str) -> Path:
    path = trade_journal_path(log_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_JOURNAL_FIELDS)
            writer.writeheader()

    return path


def _atomic_rewrite_csv(path: Path, rows: List[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp_trade_journal_",
        suffix=".csv",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_JOURNAL_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({k: _norm_str(row.get(k, "")) for k in TRADE_JOURNAL_FIELDS})
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except Exception:
            pass


# =========================================================
# Row building
# =========================================================

def build_trade_journal_row(
    *,
    trade_id: str,
    run_id: str,
    symbol: str,
    entry_time: Any,
    exit_time: Any,
    entry_px: Any,
    exit_px: Any,
    qty: Any,
    pnl: Any,
    entry_reason: Any,
    exit_reason: Any,
    entry_order_id: Any,
    exit_order_id: Any,
    entry_fill_id: Any,
    exit_fill_id: Any,
    state_open_ts: Any,
    state_closed_ts: Any,
    # Phase 9 optional fields
    entry_fee: Any = "",
    exit_fee: Any = "",
    total_fee: Any = "",
    mae_pct: Any = "",
    mfe_pct: Any = "",
    regime_at_entry: Any = "",
    trade_duration_s: Any = "",
    slippage_bps: Any = "",
    entry_mid_px: Any = "",
    exit_mid_px: Any = "",
) -> TradeJournalRow:
    return TradeJournalRow(
        trade_id=_norm_str(trade_id),
        run_id=_norm_str(run_id),
        symbol=_norm_str(symbol),
        entry_time=_norm_ts(entry_time),
        exit_time=_norm_ts(exit_time),
        entry_px=_norm_decimal(entry_px),
        exit_px=_norm_decimal(exit_px),
        qty=_norm_decimal(qty),
        pnl=_norm_decimal(pnl),
        entry_reason=_norm_str(entry_reason),
        exit_reason=_norm_str(exit_reason),
        entry_order_id=_norm_str(entry_order_id),
        exit_order_id=_norm_str(exit_order_id),
        entry_fill_id=_norm_str(entry_fill_id),
        exit_fill_id=_norm_str(exit_fill_id),
        state_open_ts=_norm_ts(state_open_ts),
        state_closed_ts=_norm_ts(state_closed_ts),
        entry_fee=_norm_decimal(entry_fee) if entry_fee else "",
        exit_fee=_norm_decimal(exit_fee) if exit_fee else "",
        total_fee=_norm_decimal(total_fee) if total_fee else "",
        mae_pct=_norm_decimal(mae_pct) if mae_pct else "",
        mfe_pct=_norm_decimal(mfe_pct) if mfe_pct else "",
        regime_at_entry=_norm_str(regime_at_entry),
        trade_duration_s=_norm_str(trade_duration_s),
        slippage_bps=_norm_str(slippage_bps),
        entry_mid_px=_norm_decimal(entry_mid_px) if entry_mid_px else "",
        exit_mid_px=_norm_decimal(exit_mid_px) if exit_mid_px else "",
    )


def build_trade_journal_row_from_mapping(data: Mapping[str, Any]) -> TradeJournalRow:
    missing = [k for k in REQUIRED_FIELDS if k not in data]
    if missing:
        raise KeyError(f"Missing required trade journal fields: {missing}")

    return build_trade_journal_row(
        trade_id=data["trade_id"],
        run_id=data["run_id"],
        symbol=data["symbol"],
        entry_time=data["entry_time"],
        exit_time=data["exit_time"],
        entry_px=data["entry_px"],
        exit_px=data["exit_px"],
        qty=data["qty"],
        pnl=data["pnl"],
        entry_reason=data["entry_reason"],
        exit_reason=data["exit_reason"],
        entry_order_id=data["entry_order_id"],
        exit_order_id=data["exit_order_id"],
        entry_fill_id=data["entry_fill_id"],
        exit_fill_id=data["exit_fill_id"],
        state_open_ts=data["state_open_ts"],
        state_closed_ts=data["state_closed_ts"],
        # Phase 9 optional — default "" if absent (backward compat with pre-Phase-9 rows)
        entry_fee=data.get("entry_fee", ""),
        exit_fee=data.get("exit_fee", ""),
        total_fee=data.get("total_fee", ""),
        mae_pct=data.get("mae_pct", ""),
        mfe_pct=data.get("mfe_pct", ""),
        regime_at_entry=data.get("regime_at_entry", ""),
        trade_duration_s=data.get("trade_duration_s", ""),
        slippage_bps=data.get("slippage_bps", ""),
        entry_mid_px=data.get("entry_mid_px", ""),
        exit_mid_px=data.get("exit_mid_px", ""),
    )


# =========================================================
# Validation
# =========================================================

def validate_closed_trade_state(
    *,
    entry_fill_id: Any,
    exit_fill_id: Any,
    entry_order_id: Any,
    exit_order_id: Any,
    state_closed_ts: Any,
) -> None:
    if not _norm_str(entry_fill_id):
        raise ValueError("Cannot journal closed trade without entry_fill_id")
    if not _norm_str(exit_fill_id):
        raise ValueError("Cannot journal closed trade without exit_fill_id")
    if not _norm_str(entry_order_id):
        raise ValueError("Cannot journal closed trade without entry_order_id")
    if not _norm_str(exit_order_id):
        raise ValueError("Cannot journal closed trade without exit_order_id")
    if not _norm_ts(state_closed_ts):
        raise ValueError("Cannot journal closed trade without state_closed_ts")


def validate_trade_journal_row(row: Mapping[str, Any]) -> None:
    # Only the original 17 required fields must be non-empty.
    # Phase 9 optional fields are allowed to be absent or empty.
    missing = [k for k in REQUIRED_FIELDS if _norm_str(row.get(k, "")) == ""]
    if missing:
        raise ValueError(f"Trade journal row missing required populated fields: {missing}")

    try:
        qty = Decimal(str(row["qty"]))
        entry_px = Decimal(str(row["entry_px"]))
        exit_px = Decimal(str(row["exit_px"]))
        pnl = Decimal(str(row["pnl"]))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError(f"Invalid numeric trade journal row: {exc}") from exc

    if qty <= 0:
        raise ValueError(f"qty must be > 0, got {qty}")
    if entry_px <= 0:
        raise ValueError(f"entry_px must be > 0, got {entry_px}")
    if exit_px <= 0:
        raise ValueError(f"exit_px must be > 0, got {exit_px}")

    entry_dt = _parse_iso_or_epoch(row.get("entry_time"))
    exit_dt = _parse_iso_or_epoch(row.get("exit_time"))
    state_open_dt = _parse_iso_or_epoch(row.get("state_open_ts"))
    state_closed_dt = _parse_iso_or_epoch(row.get("state_closed_ts"))

    if entry_dt and exit_dt and exit_dt < entry_dt:
        raise ValueError("exit_time cannot be earlier than entry_time")

    if state_open_dt and state_closed_dt and state_closed_dt < state_open_dt:
        raise ValueError("state_closed_ts cannot be earlier than state_open_ts")

    _ = pnl


# =========================================================
# Duplicate protection
# =========================================================

def trade_journal_contains_trade_id(path: str | Path, trade_id: str) -> bool:
    p = Path(path)
    if not p.exists():
        return False

    wanted = _norm_str(trade_id)
    if not wanted:
        return False

    with p.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if _norm_str(row.get("trade_id")) == wanted:
                return True
    return False


def trade_journal_contains_closed_lifecycle(
    path: str | Path,
    *,
    entry_fill_id: Any,
    exit_fill_id: Any,
    entry_order_id: Any,
    exit_order_id: Any,
) -> bool:
    p = Path(path)
    if not p.exists():
        return False

    want_entry_fill_id = _norm_str(entry_fill_id)
    want_exit_fill_id = _norm_str(exit_fill_id)
    want_entry_order_id = _norm_str(entry_order_id)
    want_exit_order_id = _norm_str(exit_order_id)

    if not all([want_entry_fill_id, want_exit_fill_id, want_entry_order_id, want_exit_order_id]):
        return False

    with p.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (
                _norm_str(row.get("entry_fill_id")) == want_entry_fill_id
                and _norm_str(row.get("exit_fill_id")) == want_exit_fill_id
                and _norm_str(row.get("entry_order_id")) == want_entry_order_id
                and _norm_str(row.get("exit_order_id")) == want_exit_order_id
            ):
                return True

    return False


# =========================================================
# Append
# =========================================================

def append_trade_journal_row(
    log_dir: str | Path,
    run_id: str,
    row: TradeJournalRow | Mapping[str, Any],
    *,
    dedupe_on_trade_id: bool = True,
    dedupe_on_lifecycle_ids: bool = True,
) -> Path:
    """
    Append exactly one canonical closed-trade row.

    Rules:
      - file is run-scoped: trade_journal_<run_id>.csv
      - row must already represent a CLOSED trade
      - duplicate trade_id writes are rejected by default
      - duplicate lifecycle writes are also rejected by default
    """
    path = ensure_trade_journal(log_dir, run_id)

    if isinstance(row, TradeJournalRow):
        row_dict = row.to_dict()
    else:
        row_dict = build_trade_journal_row_from_mapping(row).to_dict()

    row_dict["run_id"] = _norm_str(run_id)

    validate_closed_trade_state(
        entry_fill_id=row_dict["entry_fill_id"],
        exit_fill_id=row_dict["exit_fill_id"],
        entry_order_id=row_dict["entry_order_id"],
        exit_order_id=row_dict["exit_order_id"],
        state_closed_ts=row_dict["state_closed_ts"],
    )
    validate_trade_journal_row(row_dict)

    if dedupe_on_trade_id and trade_journal_contains_trade_id(path, row_dict["trade_id"]):
        raise ValueError(f"trade_journal duplicate trade_id refused: {row_dict['trade_id']}")

    if dedupe_on_lifecycle_ids and trade_journal_contains_closed_lifecycle(
        path,
        entry_fill_id=row_dict["entry_fill_id"],
        exit_fill_id=row_dict["exit_fill_id"],
        entry_order_id=row_dict["entry_order_id"],
        exit_order_id=row_dict["exit_order_id"],
    ):
        raise ValueError(
            "trade_journal duplicate closed lifecycle refused: "
            f"entry_fill_id={row_dict['entry_fill_id']} "
            f"exit_fill_id={row_dict['exit_fill_id']} "
            f"entry_order_id={row_dict['entry_order_id']} "
            f"exit_order_id={row_dict['exit_order_id']}"
        )

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_JOURNAL_FIELDS)
        writer.writerow({k: _norm_str(row_dict.get(k, "")) for k in TRADE_JOURNAL_FIELDS})
        f.flush()
        os.fsync(f.fileno())

    return path


# =========================================================
# Upsert / repair helper
# =========================================================

def upsert_trade_journal_row(
    log_dir: str | Path,
    run_id: str,
    row: TradeJournalRow | Mapping[str, Any],
) -> Path:
    """
    Replace an existing trade row by trade_id, else append it.

    Useful for repair flows, but do not use this for normal close-path writes.
    Normal runtime should prefer append_trade_journal_row().
    """
    path = ensure_trade_journal(log_dir, run_id)

    if isinstance(row, TradeJournalRow):
        row_dict = row.to_dict()
    else:
        row_dict = build_trade_journal_row_from_mapping(row).to_dict()

    row_dict["run_id"] = _norm_str(run_id)
    validate_closed_trade_state(
        entry_fill_id=row_dict["entry_fill_id"],
        exit_fill_id=row_dict["exit_fill_id"],
        entry_order_id=row_dict["entry_order_id"],
        exit_order_id=row_dict["exit_order_id"],
        state_closed_ts=row_dict["state_closed_ts"],
    )
    validate_trade_journal_row(row_dict)

    existing = read_trade_journal(path)
    replaced = False
    out_rows: List[Dict[str, str]] = []

    for old in existing:
        if _norm_str(old.get("trade_id")) == _norm_str(row_dict["trade_id"]):
            out_rows.append({k: _norm_str(row_dict.get(k, "")) for k in TRADE_JOURNAL_FIELDS})
            replaced = True
        else:
            out_rows.append({k: _norm_str(old.get(k, "")) for k in TRADE_JOURNAL_FIELDS})

    if not replaced:
        out_rows.append({k: _norm_str(row_dict.get(k, "")) for k in TRADE_JOURNAL_FIELDS})

    _atomic_rewrite_csv(path, out_rows)
    return path


# =========================================================
# Read helpers
# =========================================================

def read_trade_journal(path: str | Path) -> List[Dict[str, str]]:
    p = Path(path)
    if not p.exists():
        return []

    with p.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def iter_trade_journal(path: str | Path) -> Iterable[Dict[str, str]]:
    p = Path(path)
    if not p.exists():
        return iter(())

    def _generator() -> Iterable[Dict[str, str]]:
        with p.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield row

    return _generator()


def latest_trade_journal_row(path: str | Path) -> Optional[Dict[str, str]]:
    rows = read_trade_journal(path)
    if not rows:
        return None
    return rows[-1]


# =========================================================
# PnL helpers
# =========================================================

def compute_realized_pnl(entry_px: Any, exit_px: Any, qty: Any) -> Decimal:
    e = Decimal(str(entry_px))
    x = Decimal(str(exit_px))
    q = Decimal(str(qty))
    return (x - e) * q


def sum_trade_journal_pnl(path: str | Path) -> Decimal:
    total = Decimal("0")
    for row in iter_trade_journal(path):
        pnl = _norm_str(row.get("pnl", "0"))
        if pnl:
            total += Decimal(pnl)
    return total


# =========================================================
# Convenience close-trade constructor
# =========================================================

def row_from_closed_trade(
    *,
    trade_id: str,
    run_id: str,
    symbol: str,
    qty: Any,
    entry_px: Any,
    exit_px: Any,
    entry_time: Any,
    exit_time: Any,
    entry_reason: Any,
    exit_reason: Any,
    entry_order_id: Any,
    exit_order_id: Any,
    entry_fill_id: Any,
    exit_fill_id: Any,
    state_open_ts: Any,
    state_closed_ts: Any = None,
    pnl: Any = None,
    # Phase 9 optional
    entry_fee: Any = "",
    exit_fee: Any = "",
    total_fee: Any = "",
    mae_pct: Any = "",
    mfe_pct: Any = "",
    regime_at_entry: Any = "",
    trade_duration_s: Any = "",
    slippage_bps: Any = "",
    entry_mid_px: Any = "",
    exit_mid_px: Any = "",
) -> TradeJournalRow:
    if state_closed_ts is None:
        state_closed_ts = _utc_now_iso()

    if pnl is None or pnl == "":
        pnl = compute_realized_pnl(entry_px, exit_px, qty)

    return build_trade_journal_row(
        trade_id=trade_id,
        run_id=run_id,
        symbol=symbol,
        entry_time=entry_time,
        exit_time=exit_time,
        entry_px=entry_px,
        exit_px=exit_px,
        qty=qty,
        pnl=pnl,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        entry_fill_id=entry_fill_id,
        exit_fill_id=exit_fill_id,
        state_open_ts=state_open_ts,
        state_closed_ts=state_closed_ts,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fee=total_fee,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        regime_at_entry=regime_at_entry,
        trade_duration_s=trade_duration_s,
        slippage_bps=slippage_bps,
        entry_mid_px=entry_mid_px,
        exit_mid_px=exit_mid_px,
    )


# =========================================================
# Runner bridge
# =========================================================

def row_from_runner_close(
    *,
    run_id: str,
    symbol: Any,
    trade_id: Any,
    entry_fill_id: Any,
    exit_fill_id: Any,
    entry_order_id: Any,
    exit_order_id: Any,
    entry_ts: Any,
    exit_ts: Any,
    entry_px: Any,
    exit_px: Any,
    qty: Any,
    net_pnl: Any,
    entry_reason: Any,
    exit_reason: Any,
    state_open_ts: Any = None,
    state_closed_ts: Any = None,
    # Phase 9 optional
    entry_fee: Any = "",
    exit_fee: Any = "",
    total_fee: Any = "",
    mae_pct: Any = "",
    mfe_pct: Any = "",
    regime_at_entry: Any = "",
    trade_duration_s: Any = "",
    slippage_bps: Any = "",
    entry_mid_px: Any = "",
    exit_mid_px: Any = "",
) -> TradeJournalRow:
    """
    Adapter bridge for runner_live close-boundary emission.

    Use this when runner_live has entry/exit fill truth and wants to emit the
    canonical journal schema expected by reconciliation.
    """
    if state_open_ts is None:
        state_open_ts = entry_ts
    if state_closed_ts is None:
        state_closed_ts = exit_ts

    return row_from_closed_trade(
        trade_id=_norm_str(trade_id),
        run_id=_norm_str(run_id),
        symbol=_norm_str(symbol),
        qty=qty,
        entry_px=entry_px,
        exit_px=exit_px,
        entry_time=entry_ts,
        exit_time=exit_ts,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        entry_fill_id=entry_fill_id,
        exit_fill_id=exit_fill_id,
        state_open_ts=state_open_ts,
        state_closed_ts=state_closed_ts,
        pnl=net_pnl,
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        total_fee=total_fee,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        regime_at_entry=regime_at_entry,
        trade_duration_s=trade_duration_s,
        slippage_bps=slippage_bps,
        entry_mid_px=entry_mid_px,
        exit_mid_px=exit_mid_px,
    )


def append_closed_trade_from_runner(
    *,
    log_dir: str | Path,
    run_id: str,
    symbol: Any,
    trade_id: Any,
    entry_fill_id: Any,
    exit_fill_id: Any,
    entry_order_id: Any,
    exit_order_id: Any,
    entry_ts: Any,
    exit_ts: Any,
    entry_px: Any,
    exit_px: Any,
    qty: Any,
    net_pnl: Any,
    entry_reason: Any,
    exit_reason: Any,
    state_open_ts: Any = None,
    state_closed_ts: Any = None,
    dedupe_on_trade_id: bool = True,
    dedupe_on_lifecycle_ids: bool = True,
) -> Path:
    row = row_from_runner_close(
        run_id=run_id,
        symbol=symbol,
        trade_id=trade_id,
        entry_fill_id=entry_fill_id,
        exit_fill_id=exit_fill_id,
        entry_order_id=entry_order_id,
        exit_order_id=exit_order_id,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_px=entry_px,
        exit_px=exit_px,
        qty=qty,
        net_pnl=net_pnl,
        entry_reason=entry_reason,
        exit_reason=exit_reason,
        state_open_ts=state_open_ts,
        state_closed_ts=state_closed_ts,
    )

    return append_trade_journal_row(
        log_dir=log_dir,
        run_id=run_id,
        row=row,
        dedupe_on_trade_id=dedupe_on_trade_id,
        dedupe_on_lifecycle_ids=dedupe_on_lifecycle_ids,
    )


# =========================================================
# CLI smoke test
# =========================================================

if __name__ == "__main__":
    test_log_dir = Path("ops/logs")
    test_run_id = "smoke_test"

    row = row_from_closed_trade(
        trade_id="T-000001",
        run_id=test_run_id,
        symbol="ETH-USD",
        qty="0.015",
        entry_px="3200.10",
        exit_px="3230.50",
        entry_time=_utc_now_iso(),
        exit_time=_utc_now_iso(),
        entry_reason="FORCE_TEST_BUY",
        exit_reason="FORCE_TEST_SELL",
        entry_order_id="ORD-ENTRY-001",
        exit_order_id="ORD-EXIT-001",
        entry_fill_id="FILL-ENTRY-001",
        exit_fill_id="FILL-EXIT-001",
        state_open_ts=_utc_now_iso(),
        state_closed_ts=_utc_now_iso(),
    )

    out = append_trade_journal_row(test_log_dir, test_run_id, row)
    print(f"Wrote trade journal row -> {out}")
    print(f"Journal pnl total -> {sum_trade_journal_pnl(out)}")