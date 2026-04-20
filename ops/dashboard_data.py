"""Pure data-source readers extracted from ops/dashboard.py.

These are the read helpers every endpoint uses: report-file freshness,
timestamp parsing, live-vs-backfill cutoffs. The monolith imports them
from here and re-exports under private names for back-compat; new
endpoints should import from this module directly.

Sibling file (not a package) to avoid shadowing ops/dashboard.py — Python
refuses both `ops/dashboard.py` and `ops/dashboard/__init__.py`. When the
full Phase-3 split happens (if ever), this module and dashboard.py move
into a real ops/dashboard/ package together.

Nothing in this module touches FastAPI, HTML, or any strategy state —
pure I/O on known JSON files.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def read_canonical_with_freshness(path_rel: str,
                                   fresh_threshold_s: int = 900) -> dict:
    """Read a canonical report file. Adds `_meta.{source_file, mtime_s_ago,
    fresh}` so UI can show "loaded 42s ago" instead of silently trusting
    stale data.

    path_rel: path relative to repo root
    fresh_threshold_s: default 15 min; caller overrides for longer-lived
        reports (26h for nightly runs).
    """
    path = _REPO / path_rel
    if not path.exists():
        return {
            "error": f"missing {path_rel}",
            "_meta": {"source_file": path_rel, "fresh": False},
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "error": str(e),
            "_meta": {"source_file": path_rel, "fresh": False},
        }
    age = int(time.time() - path.stat().st_mtime)
    data["_meta"] = {
        "source_file": path_rel,
        "mtime_s_ago": age,
        "fresh": age < fresh_threshold_s,
        "fresh_threshold_s": fresh_threshold_s,
    }
    return data


def parse_report_ts(raw: str | None) -> datetime | None:
    """Tolerant ISO/date-string parse with UTC fallback.

    Returns None on any parse failure — callers decide whether to drop
    the row or use a sentinel. Every dashboard endpoint that reads trade
    CSVs goes through this.
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                ts = datetime.strptime(str(raw).split(".")[0], fmt)
                break
            except ValueError:
                ts = None
        else:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def strategy_live_cutoffs() -> dict[str, datetime]:
    """Per-strategy live-trading start timestamps, parsed from
    argus_flow/configs/fleet_sizing.json.

    Dashboard views filter historicals from live display using this. A
    missing or corrupt config yields an empty dict — callers see no
    cutoffs rather than crashing.
    """
    try:
        cfg_path = _REPO / "argus_flow" / "configs" / "fleet_sizing.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, datetime] = {}
    for label, raw in (cfg.get("strategy_live_cutoffs") or {}).items():
        ts = parse_report_ts(raw)
        if ts is not None:
            out[str(label)] = ts
    return out


__all__ = [
    "read_canonical_with_freshness",
    "parse_report_ts",
    "strategy_live_cutoffs",
]
