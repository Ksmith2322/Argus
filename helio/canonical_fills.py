"""Canonical fills logger — one JSONL file for every closed trade across
the fleet. Single source of truth for PnL reconciliation.

Each runner calls `write_fill()` once per closed trade (exit-fill path). The
log is append-only. Trade CSVs remain per-strategy for strategy-specific
fields; this log is fleet-wide for cross-strategy analytics and dashboard.

Design goal (blueprint §18.8 fill-derived reconciliation): future dashboards
and reports should be able to derive fleet PnL by reading this one file
instead of merging 8 trades.csv files with inconsistent schemas.

Usage:
    from helio.canonical_fills import write_fill
    write_fill(
        strategy="forge_gld_pm_long",
        symbol="GLD",
        direction="long",
        side="EXIT",
        entry_ts="2026-04-16T19:30:00+00:00",
        exit_ts="2026-04-17T13:30:00+00:00",
        entry_px=440.16,
        exit_px=441.98,
        size=109,
        risk_usd=100.0,
        pnl_usd=199.15,
        exit_reason="target",
    )
"""
from __future__ import annotations

import csv
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Intra-process write lock — serialises threads within one Python process.
_WRITE_LOCK = threading.Lock()


@contextmanager
def _cross_process_file_lock(path: Path):
    """Best-effort cross-process exclusive lock on `path`. Uses
    msvcrt.locking on Windows (SetFilePointer + LockFileEx under the
    hood), fcntl.flock on POSIX. Falls back to no lock if neither is
    available (shouldn't happen on supported platforms).

    The lock is held against a dedicated lockfile (`path + .lock`) —
    we can't reliably lock the append-mode file handle itself on
    Windows because the handle position changes between lock + write.

    Caught by test_canonical_fills_multiprocess: without this lock,
    4 subprocesses × 25 writes each lose ~17% of rows on Windows.
    """
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if os.name == "nt":
        # Windows: hold an exclusive handle to the lockfile for the
        # entire critical section. We create with O_CREAT (no O_EXCL —
        # subsequent acquirers will wait on msvcrt.locking below) and
        # then acquire a byte-range lock on offset 0. msvcrt.LK_LOCK
        # blocks up to ~10s internally; we wrap it in a retry loop so
        # we can wait longer.
        import msvcrt
        import time as _time

        # Open once, then spin on msvcrt.locking.
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        acquired = False
        deadline = _time.time() + 60.0
        while _time.time() < deadline:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                _time.sleep(0.002)
        if not acquired:
            # DO NOT proceed without the lock — concurrent writers would
            # race and some writes would silently drop. Raise loudly so
            # the caller's try/except can log the fact; the record is
            # also copied to a fallback jsonl for later reconciliation.
            try:
                os.close(fd)
            except OSError:
                pass
            raise TimeoutError(
                f"canonical_fills lock timeout after 60s on {lock_path} — "
                f"did not proceed to avoid a silent-drop race. Caller must log "
                f"and the failover path will pick up the record."
            )
        try:
            yield
        finally:
            try:
                if acquired:
                    # msvcrt.LK_UNLCK needs the file pointer at 0
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
    else:
        # POSIX: fcntl.flock is robust and blocks atomically.
        f = open(lock_path, "a+b")
        try:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except ImportError:
                pass
            yield
        finally:
            f.close()

_REPO = Path(__file__).resolve().parents[1]
CANONICAL_FILLS_PATH = _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
# Failover log for records that couldn't acquire the main lock. A reconciler
# should periodically drain this into the primary ledger.
CANONICAL_FAILOVER_PATH = _REPO / "argus_flow" / "logs" / "canonical_fills_failover.jsonl"

# Reader-glob supports rotation: canonical_fills_YYYYMM.jsonl etc.
# _CANONICAL_GLOB pattern matches archives. Readers use _iter_canonical_paths
# to glob current + archived; writers rotate via rotate_if_needed().
_CANONICAL_GLOB = "canonical_fills*.jsonl"
# Rotation trigger: when the current file exceeds this size, rename it to
# canonical_fills_YYYYMM.jsonl and start a fresh one. 5 MB keeps individual
# archives comfortably reading in-memory; dashboard endpoints load only the
# current file plus the latest archive for live views.
_ROTATION_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB


def _norm_backfill_ts(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        return text


def _norm_backfill_symbol(value) -> str | None:
    symbol = str(value or "").strip().upper()
    if not symbol:
        return None
    if symbol == "NQ":
        return "MNQ"
    return symbol


def _backfill_dedup_key(strat, entry_ts, exit_ts, symbol) -> tuple:
    if str(strat).startswith("argus_"):
        logical_ts = exit_ts or entry_ts
    else:
        logical_ts = entry_ts
    return (strat, _norm_backfill_ts(logical_ts), _norm_backfill_symbol(symbol))


def _iter_canonical_paths() -> list[Path]:
    """Return all canonical fill files (current + any rotated archives),
    oldest-first by filename. A rotation that writes
    canonical_fills_202604.jsonl is picked up automatically."""
    log_dir = CANONICAL_FILLS_PATH.parent
    if not log_dir.exists():
        return []
    # Sort by name: canonical_fills.jsonl sorts BEFORE canonical_fills_202604.jsonl
    # so put the unnumbered (current) file LAST for most-recent-last semantics.
    paths = sorted(log_dir.glob(_CANONICAL_GLOB))
    current = CANONICAL_FILLS_PATH
    archived = [p for p in paths if p != current]
    return archived + ([current] if current.exists() else [])


def rotate_if_needed() -> Path | None:
    """If canonical_fills.jsonl is over the rotation threshold, rename it
    to canonical_fills_YYYYMM.jsonl and start fresh. Returns the archive
    path if rotation occurred, else None. Never raises — a rotation failure
    must not break the writer.

    Idempotent: if a stamp for this month already exists (rare edge after
    clock skew), the rotation is skipped rather than overwriting it.
    """
    try:
        if not CANONICAL_FILLS_PATH.exists():
            return None
        size = CANONICAL_FILLS_PATH.stat().st_size
        if size < _ROTATION_SIZE_BYTES:
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m")
        archive = CANONICAL_FILLS_PATH.parent / f"canonical_fills_{stamp}.jsonl"
        if archive.exists():
            # Month stamp already used — don't overwrite. Next month's
            # rotation will succeed naturally.
            return None
        CANONICAL_FILLS_PATH.rename(archive)
        # Touch a fresh empty current file so subsequent appends land cleanly
        CANONICAL_FILLS_PATH.touch()
        return archive
    except Exception:
        return None


def _current_anchor() -> float | None:
    try:
        from helio.fleet_sizing import get_sizing_anchor_usd
        return float(get_sizing_anchor_usd())
    except Exception:
        return None


def write_fill(
    *,
    strategy: str,
    symbol: str,
    direction: str,
    side: str,  # "ENTRY" or "EXIT"
    entry_ts: str | None = None,
    exit_ts: str | None = None,
    entry_px: float | None = None,
    exit_px: float | None = None,
    size: float | int | None = None,
    risk_usd: float | None = None,
    pnl_usd: float | None = None,
    exit_reason: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one fill/close record to the canonical fills log.

    Never raises — failures are silently swallowed so a broken log path can't
    crash a live strategy. Trade CSVs remain the strategy-specific source.
    """
    try:
        row: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy,
            "symbol": symbol,
            "direction": direction,
            "side": side,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_px": entry_px,
            "exit_px": exit_px,
            "size": size,
            "risk_usd": risk_usd,
            "pnl_usd": pnl_usd,
            "exit_reason": exit_reason,
            "broker_anchor_at_fill_usd": _current_anchor(),
        }
        if extra:
            row["extra"] = extra
        CANONICAL_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Serialise rotation + append under BOTH:
        #   - intra-process thread lock (stops thread races in one process)
        #   - cross-process file lock (stops races between runner processes)
        try:
            with _WRITE_LOCK, _cross_process_file_lock(CANONICAL_FILLS_PATH):
                rotate_if_needed()
                with open(CANONICAL_FILLS_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, default=str) + "\n")
        except TimeoutError:
            # Lock acquisition timed out. Don't silently drop — write to the
            # failover jsonl so a reconciler can fold it back later.
            _write_failover(row, reason="lock_timeout")
    except Exception:
        pass  # never let the logger break a trade


def write_fill_typed(fill, extra: dict[str, Any] | None = None) -> None:
    """Write a helio.domain.Fill object to the canonical log.

    Additive twin of write_fill(). Phase 2 runners can create a Fill with
    their existing state, pass it here, and get the same behaviour. The
    on-disk shape matches write_fill() exactly — extra arg is included only
    when non-None (same asymmetry as the kwargs API). Never raises.

    This closes the writer side of the domain module: read_fills, reconcile,
    morning_brief, and dashboard already consume Fill; runners will produce
    it once Phase 2 migrations begin.
    """
    try:
        # Duck-type rather than import — avoids a circular import risk if
        # helio.domain ever depends on something in helio.canonical_fills.
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "strategy": getattr(fill, "strategy", ""),
            "symbol": getattr(fill, "symbol", ""),
            "direction": getattr(fill, "direction", ""),
            "side": getattr(fill, "side", ""),
            "entry_ts": getattr(fill, "entry_ts", None),
            "exit_ts": getattr(fill, "exit_ts", None),
            "entry_px": getattr(fill, "entry_px", None),
            "exit_px": getattr(fill, "exit_px", None),
            "size": getattr(fill, "size", None),
            "risk_usd": getattr(fill, "risk_usd", None),
            "pnl_usd": getattr(fill, "pnl_usd", None),
            "exit_reason": getattr(fill, "exit_reason", None),
            "broker_anchor_at_fill_usd": _current_anchor(),
        }
        if extra:
            row["extra"] = extra
        CANONICAL_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Both intra-process AND cross-process locks required — see write_fill()
        try:
            with _WRITE_LOCK, _cross_process_file_lock(CANONICAL_FILLS_PATH):
                rotate_if_needed()
                with open(CANONICAL_FILLS_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, default=str) + "\n")
        except TimeoutError:
            _write_failover(row, reason="lock_timeout")
    except Exception:
        pass  # same invariant as write_fill: never break a live trade


def _write_failover(row: dict, reason: str) -> None:
    """Last-resort append to the failover log when the main lock is unobtainable.

    The failover file has no lock (append-only, one row per line, duplicates
    tolerated). A reconciler should periodically drain records from failover
    into the main ledger when the main lock is available again.
    """
    try:
        CANONICAL_FAILOVER_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {**row, "_failover_reason": reason,
                   "_failover_ts": datetime.now(timezone.utc).isoformat()}
        with open(CANONICAL_FAILOVER_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        # If even the failover fails, we've exhausted options. The trade is
        # still logged in the per-strategy CSV by the runner's _close path;
        # reconciler can catch it later via CSV-vs-canonical diff.
        pass


def read_fills(limit: int | None = None, strategy: str | None = None) -> list[dict]:
    """Read fills (most recent first). Reads across the current file AND any
    rotated archives (canonical_fills_*.jsonl). For dashboard + analytics."""
    paths = _iter_canonical_paths()
    if not paths:
        return []
    rows: list[dict] = []
    # Iterate newest-to-oldest file, and within each file newest-to-oldest line
    for path in reversed(paths):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if strategy and r.get("strategy") != strategy:
                continue
            rows.append(r)
            if limit and len(rows) >= limit:
                return rows
    return rows


def backfill_from_trade_csvs() -> int:
    """One-shot: seed canonical_fills.jsonl from existing trades.csv files so
    the dashboard has data immediately. Idempotent — skips rows already present
    (matched on strategy+entry_ts+exit_ts).
    """
    if not CANONICAL_FILLS_PATH.exists():
        CANONICAL_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CANONICAL_FILLS_PATH.touch()

    # Collect dedup keys across current AND any rotated archives — otherwise
    # after rotation, backfill would re-append rows that live in an archive.
    # Normalise empty strings to None so the key computed from a stored JSONL
    # row (where exit_ts was persisted as None) matches the key computed from
    # a CSV reread (where the empty column reads as "").
    #
    # Include symbol in the key: a multi-symbol strategy (e.g. forge_jpy_pm_short
    # trading USDJPY+CADJPY) can open two rows at the same entry_ts with no
    # exit_ts yet. Without the symbol discriminator, the second row gets
    # collapsed as a duplicate and never reaches canonical.
    def _norm_ts(value) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace("Z", "+00:00").replace(" ", "T")
        try:
            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            return text

    def _norm_symbol(value) -> str | None:
        symbol = str(value or "").strip().upper()
        if not symbol:
            return None
        if symbol == "NQ":
            return "MNQ"
        return symbol

    def _key(strat, ets, xts, sym) -> tuple:
        return _backfill_dedup_key(strat, ets, xts, sym)

    existing_keys = set()
    for path in _iter_canonical_paths():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    existing_keys.add(_key(r.get("strategy"),
                                          r.get("entry_ts"),
                                          r.get("exit_ts"),
                                          r.get("symbol")))
                except json.JSONDecodeError:
                    continue
        except Exception:
            continue

    # 5th element = symbol_hint. For single-symbol strategies the live
    # writer stamps a concrete symbol (e.g. "GLD") on canonical rows; if the
    # CSV schema lacks a symbol column, the backfill would otherwise write
    # symbol="" and the dedup key (which now includes symbol) would treat
    # live and backfilled rows as distinct → duplicate insertion. `None`
    # means "read from CSV symbol column" (multi-symbol strategies).
    specs = [
        ("argus_usdjpy",       "argus_flow/logs/usdjpy/trades.csv",   "ts",        True,  "USDJPY"),
        ("argus_gbpusd",       "argus_flow/logs/gbpusd/trades.csv",   "ts",        True,  "GBPUSD"),
        ("argus_cadjpy",       "argus_flow/logs/cadjpy/trades.csv",   "ts",        True,  "CADJPY"),
        ("forge_gld_pm_long",  "forge/logs/gld_pm_long/trades.csv",   "ts",        False, "GLD"),
        ("forge_wick_gbpusd",  "forge/logs/wick_gbpusd/trades.csv",   "ts",        False, "GBPUSD"),
        ("forge_nq_overnight", "forge/logs/nq_overnight/trades.csv",  "ts",        False, "MNQ"),
        ("forge_jpy_pm_short", "forge/logs/jpy_pm_short/trades.csv",  "ts",        False, None),
        ("forge_gdx_gld",      "forge/logs/gdx_gld/trades.csv",       "entry_date", False, None),
    ]
    appended = 0
    # Backfill must share the same write critical section as live writers so a
    # nightly backfill can't race with a live close on Windows. Hold the
    # intra-process + cross-process locks for the whole batch, and call
    # rotate_if_needed() up front (matches write_fill*).
    with _WRITE_LOCK, _cross_process_file_lock(CANONICAL_FILLS_PATH):
        rotate_if_needed()
        try:
            out = open(CANONICAL_FILLS_PATH, "a", encoding="utf-8")
        except Exception:
            return appended
        try:
            for label, path_rel, ts_col, valid_only, symbol_hint in specs:
                p = _REPO / path_rel
                if not p.exists():
                    continue
                try:
                    from helio.domain import Fill  # lazy circular-safe
                    with open(p, encoding="utf-8") as f:
                        for r in csv.DictReader(f):
                            if valid_only and str(r.get("experiment_valid", "")).lower() != "true":
                                continue
                            # Skip rows without a realised pnl — they represent
                            # open/timed-out trades, not closed fills. This
                            # matches helio.reconciliation._read_strategy_csv;
                            # without this filter, canonical picks up orphans
                            # that reconcile flags as EXTRA_IN_CANONICAL.
                            if r.get("pnl_usd") in (None, ""):
                                continue
                            entry_ts = r.get(ts_col) or r.get("entry_ts") or r.get("entry_date") or ""
                            exit_ts = r.get("exit_ts") or r.get("exit_date") or ""
                            symbol = r.get("symbol") or symbol_hint or ""
                            key = _key(label, entry_ts, exit_ts, symbol)
                            if key in existing_keys:
                                continue

                            raw = {
                                "strategy": label,
                                "symbol": symbol,
                                "direction": r.get("direction") or "",
                                "side": "EXIT",
                                "entry_ts": entry_ts or None,
                                "exit_ts": exit_ts or None,
                                "entry_px": r.get("entry_px") or r.get("gdx_entry"),
                                "exit_px": r.get("exit_px") or r.get("gdx_exit"),
                                "size": (r.get("position_size") or r.get("gdx_shares")
                                          or r.get("shares") or None),
                                "risk_usd": r.get("risk_usd"),
                                "pnl_usd": r.get("pnl_usd"),
                                "exit_reason": r.get("exit_reason") or "",
                                "broker_anchor_at_fill_usd": None,
                                "source": "backfill_from_trade_csv",
                            }
                            fill = Fill.from_canonical_row(raw)
                            row = fill.to_canonical_row()
                            row["ts"] = datetime.now(timezone.utc).isoformat()
                            out.write(json.dumps(row, default=str) + "\n")
                            existing_keys.add(key)
                            appended += 1
                except Exception:
                    continue
        finally:
            try:
                out.close()
            except Exception:
                pass
    return appended


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--backfill":
        n = backfill_from_trade_csvs()
        print(f"Backfilled {n} rows into {CANONICAL_FILLS_PATH}")
    else:
        rows = read_fills(limit=20)
        print(f"{len(rows)} recent fills:")
        for r in rows:
            print(f"  {r.get('ts')[:19]}  {r.get('strategy')} {r.get('symbol')} {r.get('side')}  pnl=${r.get('pnl_usd')}")
