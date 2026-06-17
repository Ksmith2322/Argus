"""Normalized blocker ledger for skipped trades and safety refusals.

The goal is to distinguish "no edge fired" from "edge fired but an
operational/safety gate blocked execution." Runners append one small CSV row
per meaningful blocker; reports aggregate by strategy and code.
"""
from __future__ import annotations

import csv
import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BLOCKER_LEDGER_PATH = REPO / "argus_flow" / "logs" / "blocker_ledger.csv"

FIELDS = [
    "ts", "strategy", "code", "symbol", "stage", "action", "qty",
    "notional_usd", "reason", "context_json",
]

_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_csv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writeheader()


def record_blocker(
    strategy: str,
    code: str,
    *,
    reason: str = "",
    symbol: str = "",
    stage: str = "",
    action: str = "",
    qty: int | float | None = None,
    notional_usd: int | float | None = None,
    context: dict[str, Any] | None = None,
    ts: str | None = None,
    path: Path | None = None,
) -> None:
    """Append one blocker row. Best-effort by design: a ledger write must not
    break a strategy loop."""
    p = path or BLOCKER_LEDGER_PATH
    row = {
        "ts": ts or _now_iso(),
        "strategy": strategy,
        "code": code,
        "symbol": symbol,
        "stage": stage,
        "action": action,
        "qty": "" if qty is None else qty,
        "notional_usd": "" if notional_usd is None else round(float(notional_usd), 2),
        "reason": reason,
        "context_json": json.dumps(context or {}, sort_keys=True, default=str),
    }
    try:
        with _LOCK:
            _ensure_csv(p)
            with open(p, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
    except Exception:
        return


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_blockers(
    *,
    since: datetime | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    p = path or BLOCKER_LEDGER_PATH
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = _parse_ts(row.get("ts", ""))
                if since is not None and (ts is None or ts < since):
                    continue
                rows.append(row)
    except Exception:
        return []
    return rows


def summarize_blockers(days: int = 30, *, path: Path | None = None) -> dict[str, Any]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = read_blockers(since=since, path=path)
    by_strategy: dict[str, Counter] = defaultdict(Counter)
    by_code = Counter()
    for row in rows:
        strategy = row.get("strategy") or "unknown"
        code = row.get("code") or "UNKNOWN"
        by_strategy[strategy][code] += 1
        by_code[code] += 1
    return {
        "generated_at": _now_iso(),
        "window_days": days,
        "total": len(rows),
        "by_code": dict(by_code.most_common()),
        "by_strategy": {
            strategy: dict(counter.most_common())
            for strategy, counter in sorted(by_strategy.items())
        },
        "recent": rows[-25:],
    }
