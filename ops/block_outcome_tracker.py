"""ops/block_outcome_tracker — aggregate blocked signals + heuristic outcomes.

The dashboard already shows raw block-reason counts via /api/strategy_efficiency.
This script extends that with a first-cut OUTCOME read: was the block protective
(saved capital) or did it cost alpha?

Heuristic v1 (MVP — no external price feed needed):
  For each blocked signal at time T, look at the same strategy's signals.csv
  for an ENTRY action within the next FOLLOW_THROUGH_MINUTES. If found, the
  gate was a transient filter (block-then-take = working as intended).
  If no follow-through, the gate either correctly killed a no-go signal or
  threw away potential alpha. We can't tell which without realized-fill data,
  but we CAN count.

Output (argus_flow/logs/block_outcomes_latest.json):
  {
    "evaluated_at_utc": "...",
    "window_days": 30,
    "by_strategy": [
      {
        "strategy": "spy_mean_rev",
        "blocked_total": 234,
        "blocked_with_followthrough": 18,    # gate cleared shortly after
        "blocked_no_followthrough": 216,     # gate held (could be save OR miss)
        "follow_through_rate_pct": 7.7,
        "top_reasons": [{"reason": "rsi", "count": 198}, ...]
      },
      ...
    ],
    "fleet_top_reasons": [{"reason": "rsi", "total_count": 612, "strategies": [...]}, ...]
  }

V2 would join against realized price data (yfinance / canonical fills) to
compute actual would-have-traded P&L. This MVP just lays the data layer.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.block_outcome_tracker

Schedule: hourly via managed_truth_loop (cheap — pure CSV scan).
"""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Strategies emit reason strings with the indicator value baked in
# (`NO_TRIGGER_RSI_NEUTRAL_67.6`). Strip trailing `_<num>` so all RSI-neutral
# blocks aggregate as one reason instead of fragmenting into 50 buckets.
_NUMERIC_SUFFIX_RE = re.compile(r"(_\d+(\.\d+)?)+$")


def _normalize_reason(raw: str) -> str:
    return _NUMERIC_SUFFIX_RE.sub("", (raw or "unknown").lower()) or "unknown"

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "block_outcomes_latest.json"

WINDOW_DAYS = 30
FOLLOW_THROUGH_MINUTES = 120  # how long after a block to look for an ENTRY


@dataclass
class BlockedRow:
    ts: datetime
    reason: str  # normalized lower-case (e.g. "rsi", "session", "regime")


@dataclass
class EntryRow:
    ts: datetime


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _classify_action(action: str) -> tuple[str, str | None]:
    """Return (kind, reason).

    kind ∈ {"ENTRY", "BLOCKED", "OTHER"}.
    For BLOCKED, reason is the lowercased gate label (suffix after NO_TRIGGER_).
    """
    a = (action or "").upper().strip()
    if a.startswith("ENTRY_") or a.startswith("ENTER_"):
        return ("ENTRY", None)
    if a.startswith("NO_TRIGGER_"):
        return ("BLOCKED", _normalize_reason(a[len("NO_TRIGGER_"):]))
    if a.startswith("BLOCKED_") or a.startswith("SKIP_") or a.startswith("REJECT_"):
        prefix = a.split("_", 1)[0]
        return ("BLOCKED", _normalize_reason(a[len(prefix) + 1:]))
    return ("OTHER", None)


def _scan_strategy_csv(csv_path: Path, cutoff: datetime) -> tuple[list[BlockedRow], list[EntryRow]]:
    blocked: list[BlockedRow] = []
    entries: list[EntryRow] = []
    try:
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            ts_col = None
            for row in reader:
                if ts_col is None:
                    ts_col = "ts" if "ts" in row else ("timestamp" if "timestamp" in row else None)
                    if ts_col is None:
                        return ([], [])
                ts = _parse_ts(row.get(ts_col, "") or "")
                if not ts or ts < cutoff:
                    continue
                kind, reason = _classify_action(row.get("action", "") or "")
                if kind == "BLOCKED":
                    blocked.append(BlockedRow(ts=ts, reason=reason or "unknown"))
                elif kind == "ENTRY":
                    entries.append(EntryRow(ts=ts))
    except Exception:
        pass
    return (blocked, entries)


def _follow_through_count(blocked: list[BlockedRow], entries: list[EntryRow]) -> int:
    """Count blocked rows that had an ENTRY within FOLLOW_THROUGH_MINUTES after them."""
    if not entries:
        return 0
    entry_times = sorted(e.ts for e in entries)
    n = 0
    j = 0
    blocked_sorted = sorted(blocked, key=lambda b: b.ts)
    for b in blocked_sorted:
        # Advance j past entries that fired before b
        while j < len(entry_times) and entry_times[j] < b.ts:
            j += 1
        if j < len(entry_times) and entry_times[j] - b.ts <= timedelta(minutes=FOLLOW_THROUGH_MINUTES):
            n += 1
    return n


def main() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)

    by_strategy: list[dict] = []
    fleet_reasons: dict[str, dict] = defaultdict(
        lambda: {"total_count": 0, "strategies": []}
    )

    for csv_path in sorted((REPO / "forge" / "logs").glob("*/signals.csv")):
        strat = csv_path.parent.name
        blocked, entries = _scan_strategy_csv(csv_path, cutoff)
        if not blocked and not entries:
            continue

        followed = _follow_through_count(blocked, entries)
        no_follow = len(blocked) - followed

        # Top reasons for this strategy
        reason_counts: dict[str, int] = defaultdict(int)
        for b in blocked:
            reason_counts[b.reason] += 1
        top = sorted(reason_counts.items(), key=lambda x: -x[1])[:5]

        # Roll up to fleet
        for r, c in reason_counts.items():
            fleet_reasons[r]["total_count"] += c
            fleet_reasons[r]["strategies"].append({"strategy": strat, "count": c})

        ft_rate = (followed / len(blocked) * 100) if blocked else 0.0
        by_strategy.append({
            "strategy": strat,
            "blocked_total": len(blocked),
            "entries_total": len(entries),
            "blocked_with_followthrough": followed,
            "blocked_no_followthrough": no_follow,
            "follow_through_rate_pct": round(ft_rate, 1),
            "top_reasons": [{"reason": r, "count": c} for r, c in top],
        })

    by_strategy.sort(key=lambda s: -s["blocked_total"])

    # Fleet-wide top reasons
    fleet_top = sorted(
        [{"reason": r, **v} for r, v in fleet_reasons.items()],
        key=lambda x: -x["total_count"],
    )[:10]
    # Truncate per-reason strategies list to top 3 contributors
    for entry in fleet_top:
        entry["strategies"] = sorted(entry["strategies"], key=lambda s: -s["count"])[:3]

    payload = {
        "evaluated_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "follow_through_window_minutes": FOLLOW_THROUGH_MINUTES,
        "by_strategy": by_strategy,
        "fleet_top_reasons": fleet_top,
        "method_note": (
            "MVP: counts blocks per (strategy, reason) and tags blocks that had "
            "an ENTRY within follow_through_window_minutes. High follow-through "
            "rate = transient gate (good filter). Low rate could be either "
            "save (gate killed bad signal) or miss (gate killed alpha) — V2 "
            "will join against realized price data to disambiguate."
        ),
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Console summary
    print("=" * 76)
    print(f"  BLOCK-OUTCOME MVP  —  {WINDOW_DAYS}d window")
    print("=" * 76)
    print(f"  Strategies with block data: {len(by_strategy)}")
    print()
    for s in by_strategy[:10]:
        print(
            f"  {s['strategy']:25s}  "
            f"blocked={s['blocked_total']:>5}  "
            f"entries={s['entries_total']:>4}  "
            f"FT={s['follow_through_rate_pct']:>5.1f}%  "
            f"top={','.join(r['reason'] for r in s['top_reasons'][:3])}"
        )
    print()
    print(f"  Fleet top reasons:")
    for r in fleet_top[:5]:
        print(f"    {r['reason']:20s}  count={r['total_count']:>5}")
    print()
    print(f"  Output: {OUT_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
