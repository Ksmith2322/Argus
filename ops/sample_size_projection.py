"""
Sample-size projection by 2026-05-31 (strategy freeze).

For each strategy in argus_flow/logs/verdict_20260501.json:
  - Count current closed trades from its trades.csv
  - Compute trades-per-day rate over last 30d (calendar days)
  - Compute remaining business days from today -> 2026-05-31 (incl. 5/29 Fri end-of-week, exclusive of weekends)
  - Project n_at_freeze = current_n + rate * days_remaining
  - Bucket: ON_TRACK_30 / ON_TRACK_10 / THIN / DEAD

Output:
  - argus_flow/logs/sample_size_projection.json
  - Console table

Stdlib + (optional) numpy. No pandas dependency required.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
VERDICT_FILE = REPO_ROOT / "argus_flow" / "logs" / "verdict_20260501.json"
OUTPUT_FILE = REPO_ROOT / "argus_flow" / "logs" / "sample_size_projection.json"

FREEZE_DATE = dt.date(2026, 5, 31)
TS_COLUMN_CANDIDATES = ("ts", "timestamp", "entry_ts", "entry_date", "entry_time", "exit_ts", "datetime")


# ---------- path resolution ---------------------------------------------------

def candidate_trade_files(strategy: str) -> list[Path]:
    """Return ordered list of plausible trades.csv paths for `strategy`."""
    candidates: list[Path] = []
    if strategy.startswith("forge_"):
        sub = strategy[len("forge_"):]
        candidates.append(REPO_ROOT / "forge" / "logs" / sub / "trades.csv")
    if strategy.startswith("argus_"):
        sub = strategy[len("argus_"):]
        candidates.append(REPO_ROOT / "argus_flow" / "logs" / sub / "trades.csv")
    # Greek family + bare names: <name>/logs/trades.csv
    candidates.append(REPO_ROOT / strategy / "logs" / "trades.csv")
    # Generic fall-throughs in case naming surprises us
    candidates.append(REPO_ROOT / "forge" / "logs" / strategy / "trades.csv")
    candidates.append(REPO_ROOT / "argus_flow" / "logs" / strategy / "trades.csv")
    # Dedupe preserving order
    seen, out = set(), []
    for p in candidates:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def resolve_trades_csv(strategy: str) -> Optional[Path]:
    for p in candidate_trade_files(strategy):
        if p.is_file():
            return p
    return None


# ---------- timestamp parsing -------------------------------------------------

def parse_ts(value: str) -> Optional[dt.datetime]:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None
    # Normalize trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Try a few common shapes
    for fmt in (
        None,  # fromisoformat
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d",
    ):
        try:
            if fmt is None:
                return dt.datetime.fromisoformat(s)
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def to_utc_date(ts: dt.datetime) -> dt.date:
    if ts.tzinfo is None:
        return ts.date()
    return ts.astimezone(dt.timezone.utc).date()


# ---------- trade counting + rate --------------------------------------------

def read_trades(path: Path) -> list[dt.datetime]:
    """Return list of timestamps for closed trades in `path`."""
    out: list[dt.datetime] = []
    try:
        with path.open("r", newline="", encoding="utf-8-sig", errors="replace") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames:
                return out
            ts_col = next((c for c in TS_COLUMN_CANDIDATES if c in reader.fieldnames), None)
            if ts_col is None:
                return out
            for row in reader:
                if not row:
                    continue
                ts = parse_ts(row.get(ts_col, ""))
                if ts is not None:
                    out.append(ts)
    except FileNotFoundError:
        return out
    except Exception:
        return out
    return out


def trades_per_day_30d(trade_ts: list[dt.datetime], today: dt.date) -> float:
    """Calendar-day rate: trades in last 30d / 30."""
    if not trade_ts:
        return 0.0
    cutoff = today - dt.timedelta(days=30)
    recent = sum(1 for t in trade_ts if to_utc_date(t) >= cutoff)
    return recent / 30.0


# ---------- business-day counting --------------------------------------------

def business_days_between(start: dt.date, end: dt.date) -> int:
    """Inclusive of `end`, exclusive of `start` -- weekdays only.

    Matches numpy.busday_count(start, end+1) behavior with 'forward' weekmask.
    Returns 0 if end <= start.
    """
    if end <= start:
        return 0
    try:
        import numpy as np  # type: ignore
        # numpy busday_count is [start, end) on dates -- shift end by 1 day
        return int(np.busday_count(start.isoformat(), (end + dt.timedelta(days=1)).isoformat()))
    except Exception:
        n = 0
        d = start + dt.timedelta(days=1)
        while d <= end:
            if d.weekday() < 5:  # Mon-Fri
                n += 1
            d += dt.timedelta(days=1)
        return n


# ---------- bucketing ---------------------------------------------------------

def bucket(current_n: int, rate: float, projected: float) -> str:
    if projected >= 30:
        return "ON_TRACK_30"
    if projected >= 10:
        return "ON_TRACK_10"
    if rate <= 0.0 and current_n < 10:
        return "DEAD"
    return "THIN"


# ---------- main --------------------------------------------------------------

def main() -> int:
    if not VERDICT_FILE.is_file():
        print(f"ERROR: verdict file not found: {VERDICT_FILE}", file=sys.stderr)
        return 2

    today = dt.datetime.now(dt.timezone.utc).date()
    days_remaining = business_days_between(today, FREEZE_DATE)

    with VERDICT_FILE.open("r", encoding="utf-8") as fh:
        verdict = json.load(fh)

    rows: list[dict] = []
    for entry in verdict.get("strategies", []):
        strat = entry.get("strategy", "")
        path = resolve_trades_csv(strat)
        trade_ts = read_trades(path) if path else []
        current_n = len(trade_ts)
        rate = trades_per_day_30d(trade_ts, today)
        projected = current_n + rate * days_remaining
        status = bucket(current_n, rate, projected)
        rows.append({
            "strategy": strat,
            "trades_csv": str(path) if path else None,
            "current_n": current_n,
            "rate_per_day_30d": round(rate, 4),
            "days_remaining_business": days_remaining,
            "projected_n_at_freeze": round(projected, 1),
            "status": status,
            "verdict_label": entry.get("verdict"),
        })

    # Aggregate
    n_track_30 = sum(1 for r in rows if r["status"] == "ON_TRACK_30")
    n_track_10 = sum(1 for r in rows if r["status"] in ("ON_TRACK_10", "ON_TRACK_30"))
    n_dead = sum(1 for r in rows if r["status"] == "DEAD")
    n_thin = sum(1 for r in rows if r["status"] == "THIN")

    out = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "today": today.isoformat(),
        "freeze_date": FREEZE_DATE.isoformat(),
        "days_remaining_business": days_remaining,
        "n_strategies": len(rows),
        "summary": {
            "on_track_30": n_track_30,
            "on_track_10_or_better": n_track_10,
            "thin": n_thin,
            "dead": n_dead,
        },
        "strategies": rows,
    }
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)

    # Console table
    headers = ("Strategy", "Cur N", "Rate/d", "Days Rem", "Proj N", "Status", "Verdict")
    widths = (28, 6, 7, 9, 7, 13, 18)

    def fmt_row(cells: tuple) -> str:
        return "  ".join(str(c).ljust(w) for c, w in zip(cells, widths))

    print()
    print(f"Sample-size projection -- today={today.isoformat()} freeze={FREEZE_DATE.isoformat()} "
          f"days_remaining={days_remaining}")
    print("=" * sum(widths) + "=" * (2 * (len(widths) - 1)))
    print(fmt_row(headers))
    print("-" * (sum(widths) + 2 * (len(widths) - 1)))

    # Sort: ON_TRACK_30 -> ON_TRACK_10 -> THIN -> DEAD, then by projected desc
    order = {"ON_TRACK_30": 0, "ON_TRACK_10": 1, "THIN": 2, "DEAD": 3}
    rows_sorted = sorted(rows, key=lambda r: (order.get(r["status"], 9), -r["projected_n_at_freeze"]))
    for r in rows_sorted:
        print(fmt_row((
            r["strategy"][:28],
            r["current_n"],
            f"{r['rate_per_day_30d']:.3f}",
            r["days_remaining_business"],
            f"{r['projected_n_at_freeze']:.1f}",
            r["status"],
            (r["verdict_label"] or "")[:18],
        )))

    print("-" * (sum(widths) + 2 * (len(widths) - 1)))
    print(f"Total strategies        : {len(rows)}")
    print(f"Projected n>=30 by 5/31 : {n_track_30}")
    print(f"Projected n>=10 by 5/31 : {n_track_10}")
    print(f"THIN (some movement)    : {n_thin}")
    print(f"DEAD (rate=0, n<10)     : {n_dead}")
    print(f"\nWrote {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
