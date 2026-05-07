"""Persistent pending-fills queue — root fix for the orphan-fill problem.

Background (5/4-5/7 audit): every restart-during-pending-fill creates an
orphan position. ib_insync's `execDetailsEvent` only fires for orders
submitted in the *current* TCP session. If the runner submits an order,
TWS fills it, but the runner disconnects/restarts before the fill callback
fires, the execution is lost from local state. Broker has the position;
runner thinks FLAT. Recurring failure pattern across the week.

Current band-aid: argus_flow/runner_unified.py adopts orphans on
reconcile. But orphans still happen — the queue here prevents them.

Design:
  - Before submitting any order, write a JSON line to
    `argus_flow/logs/_pending_fills.jsonl`:
      {"ts": ..., "client_id": ..., "order_id": ..., "strategy": ...,
       "symbol": ..., "direction": "long|short", "size": N,
       "est_entry_px": ..., "stop_px": ..., "target_px": ...}
  - On confirmed fill, remove the entry by order_id.
  - On runner startup, call `reconcile_pending(ib)` which:
      1. Reads current pending entries
      2. Queries broker for open positions matching pending entries
      3. If broker has a matching position → mark fill, return (orphan_resolved)
      4. If no match → entry was either never filled or the broker reported
         it differently. Log + age out (24h).

This module is intentionally process-safe via atomic file writes (write
to .tmp, rename). Multiple runners can share the queue safely.

Usage from a runner:

    from helio.pending_fills import write_pending, clear_pending, reconcile_pending

    # Before submitting:
    write_pending(client_id=109, order_id=str(oid), strategy="forge_multi_orb",
                  symbol="QQQ", direction="long", size=14,
                  est_entry_px=685.0, stop_px=684.0, target_px=687.0)

    # On confirmed fill:
    clear_pending(order_id=str(oid))

    # On startup (after IB connect):
    resolved = reconcile_pending(ib, strategy="forge_multi_orb")
    for r in resolved:
        log.info("ADOPTED ORPHAN FROM PENDING QUEUE: %s", r)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
QUEUE_PATH = REPO / "argus_flow" / "logs" / "_pending_fills.jsonl"

# Entries older than this are considered stale (broker won't have execution
# history that far back). Cleared during reconcile.
MAX_PENDING_AGE_S = 24 * 60 * 60  # 24 hours


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """Atomic JSONL write: write to .tmp then rename. Survives a crash mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass
        raise


def _read_all() -> list[dict]:
    if not QUEUE_PATH.exists():
        return []
    out: list[dict] = []
    try:
        for line in QUEUE_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


def write_pending(
    *,
    client_id: int,
    order_id: str,
    strategy: str,
    symbol: str,
    direction: str,
    size: float,
    est_entry_px: float,
    stop_px: float = 0.0,
    target_px: float = 0.0,
    extra: Optional[dict] = None,
) -> None:
    """Append a pending-fill record. Called BEFORE the order is submitted to
    the broker so it survives any subsequent crash.
    """
    record = {
        "ts": _now_iso(),
        "client_id": int(client_id),
        "order_id": str(order_id),
        "strategy": str(strategy),
        "symbol": str(symbol),
        "direction": str(direction),
        "size": float(size),
        "est_entry_px": float(est_entry_px),
        "stop_px": float(stop_px),
        "target_px": float(target_px),
    }
    if extra:
        record["extra"] = extra
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Append-only — no atomic rewrite needed for a new entry
    with QUEUE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


def clear_pending(*, order_id: Optional[str] = None,
                  symbol: Optional[str] = None,
                  strategy: Optional[str] = None) -> int:
    """Remove pending-fill records matching the criteria. Returns count removed.

    Call after a confirmed fill (with order_id) to tidy up. Or pass symbol/
    strategy to clear a runner's stale entries on shutdown.
    """
    entries = _read_all()
    keep = []
    removed = 0
    for e in entries:
        match = True
        if order_id is not None and str(e.get("order_id")) != str(order_id):
            match = False
        if symbol is not None and str(e.get("symbol", "")).upper() != str(symbol).upper():
            match = False
        if strategy is not None and str(e.get("strategy", "")) != str(strategy):
            match = False
        if match:
            removed += 1
        else:
            keep.append(e)
    if removed:
        _atomic_write_lines(QUEUE_PATH, [json.dumps(e) for e in keep])
    return removed


def list_pending() -> list[dict]:
    """All currently-pending entries. Useful for diagnostics + dashboard."""
    return _read_all()


def _is_stale(entry: dict, now: datetime) -> bool:
    try:
        ts = datetime.fromisoformat(str(entry["ts"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() > MAX_PENDING_AGE_S
    except Exception:
        return True  # treat unparseable as stale


def reconcile_pending(
    ib,
    *,
    strategy: Optional[str] = None,
) -> list[dict]:
    """On runner startup, check pending-fill records against broker positions.

    For each pending entry whose strategy matches (or all if strategy=None):
      - If broker has a matching position (symbol + direction + non-zero qty),
        mark "resolved" — the fill happened during the gap. Return for adoption.
      - If no broker position, leave the entry (it may still be pending) until
        it ages out at MAX_PENDING_AGE_S.
      - Stale entries (>24h) are removed regardless.

    Returns list of resolved entries (caller adopts these into local state).
    """
    if not QUEUE_PATH.exists():
        return []

    try:
        positions = ib.positions()
    except Exception:
        # Can't query broker — leave queue as-is, return nothing
        return []

    # Index positions by symbol for fast lookup
    by_symbol: dict[str, float] = {}
    for p in positions:
        sym = str(p.contract.symbol).upper()
        by_symbol[sym] = by_symbol.get(sym, 0.0) + float(p.position)

    entries = _read_all()
    now = datetime.now(timezone.utc)
    resolved: list[dict] = []
    keep: list[dict] = []

    for e in entries:
        if strategy is not None and e.get("strategy") != strategy:
            keep.append(e)
            continue
        if _is_stale(e, now):
            # Drop stale entries — broker won't have history that far back
            continue
        sym = str(e.get("symbol", "")).upper()
        direction = str(e.get("direction", "")).lower()
        broker_qty = by_symbol.get(sym, 0.0)
        broker_dir = "long" if broker_qty > 0 else ("short" if broker_qty < 0 else "flat")

        if broker_qty != 0 and broker_dir == direction:
            # Match found — fill happened during the gap. Adopt + remove.
            e["resolved_at"] = _now_iso()
            e["broker_qty_observed"] = broker_qty
            resolved.append(e)
        else:
            # No matching position — leave entry until it ages out
            keep.append(e)

    if resolved or len(keep) != len(entries):
        _atomic_write_lines(QUEUE_PATH, [json.dumps(e) for e in keep])

    return resolved


def cleanup_stale() -> int:
    """Remove entries older than MAX_PENDING_AGE_S. Returns count removed."""
    entries = _read_all()
    now = datetime.now(timezone.utc)
    keep = [e for e in entries if not _is_stale(e, now)]
    removed = len(entries) - len(keep)
    if removed:
        _atomic_write_lines(QUEUE_PATH, [json.dumps(e) for e in keep])
    return removed
