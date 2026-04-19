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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
CANONICAL_FILLS_PATH = _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"


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
        with open(CANONICAL_FILLS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass  # never let the logger break a trade


def read_fills(limit: int | None = None, strategy: str | None = None) -> list[dict]:
    """Read fills (most recent first). For dashboard + analytics."""
    if not CANONICAL_FILLS_PATH.exists():
        return []
    try:
        lines = CANONICAL_FILLS_PATH.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
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
            break
    return rows


def backfill_from_trade_csvs() -> int:
    """One-shot: seed canonical_fills.jsonl from existing trades.csv files so
    the dashboard has data immediately. Idempotent — skips rows already present
    (matched on strategy+entry_ts+exit_ts).
    """
    if not CANONICAL_FILLS_PATH.exists():
        CANONICAL_FILLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CANONICAL_FILLS_PATH.touch()

    existing_keys = set()
    try:
        for line in CANONICAL_FILLS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                existing_keys.add((r.get("strategy"), r.get("entry_ts"), r.get("exit_ts")))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass

    specs = [
        ("argus_usdjpy",       "argus_flow/logs/usdjpy/trades.csv",   "ts",        True),
        ("argus_gbpusd",       "argus_flow/logs/gbpusd/trades.csv",   "ts",        True),
        ("argus_cadjpy",       "argus_flow/logs/cadjpy/trades.csv",   "ts",        True),
        ("forge_gld_pm_long",  "forge/logs/gld_pm_long/trades.csv",   "ts",        False),
        ("forge_wick_gbpusd",  "forge/logs/wick_gbpusd/trades.csv",   "ts",        False),
        ("forge_nq_overnight", "forge/logs/nq_overnight/trades.csv",  "ts",        False),
        ("forge_jpy_pm_short", "forge/logs/jpy_pm_short/trades.csv",  "ts",        False),
        ("forge_gdx_gld",      "forge/logs/gdx_gld/trades.csv",       "entry_date", False),
    ]
    appended = 0
    for label, path_rel, ts_col, valid_only in specs:
        p = _REPO / path_rel
        if not p.exists():
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if valid_only and str(r.get("experiment_valid", "")).lower() != "true":
                        continue
                    entry_ts = r.get(ts_col) or r.get("entry_ts") or r.get("entry_date") or ""
                    exit_ts = r.get("exit_ts") or r.get("exit_date") or ""
                    key = (label, entry_ts, exit_ts)
                    if key in existing_keys:
                        continue
                    pnl_raw = r.get("pnl_usd")
                    try:
                        pnl = float(pnl_raw) if pnl_raw not in (None, "") else None
                    except ValueError:
                        pnl = None
                    risk_raw = r.get("risk_usd")
                    try:
                        risk = float(risk_raw) if risk_raw not in (None, "") else None
                    except ValueError:
                        risk = None
                    size_raw = r.get("position_size") or r.get("gdx_shares") or r.get("shares") or ""
                    try:
                        size_val = float(size_raw) if size_raw not in (None, "") else None
                    except ValueError:
                        size_val = size_raw
                    row = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "strategy": label,
                        "symbol": r.get("symbol") or "",
                        "direction": r.get("direction") or "",
                        "side": "EXIT",
                        "entry_ts": entry_ts or None,
                        "exit_ts": exit_ts or None,
                        "entry_px": r.get("entry_px") or r.get("gdx_entry") or None,
                        "exit_px": r.get("exit_px") or r.get("gdx_exit") or None,
                        "size": size_val,
                        "risk_usd": risk,
                        "pnl_usd": pnl,
                        "exit_reason": r.get("exit_reason") or "",
                        "broker_anchor_at_fill_usd": None,  # historical — no anchor at time
                        "source": "backfill_from_trade_csv",
                    }
                    with open(CANONICAL_FILLS_PATH, "a", encoding="utf-8") as out:
                        out.write(json.dumps(row, default=str) + "\n")
                    existing_keys.add(key)
                    appended += 1
        except Exception:
            continue
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
