"""apollo/ops/backfill_forward_returns.py

Compute T+1 / T+3 / T+5 forward returns for Apollo scan signals so edge claims
are falsifiable. Apollo's live runner only writes scan scores — it does not
track what happened next. Without that, Apollo is structurally unfalsifiable:
there is no way to answer "do score >= 75 alerts actually outperform?"

This script backfills that missing data by pulling historical prices from
yfinance for every (symbol, scan_date) pair that has already matured
(scan_date + max_horizon_days < today).

Output: apollo/logs/forward_returns.jsonl (one JSON object per line,
append-only, idempotent via processed-key cache).

Run manually or wire into run_cohort_report.ps1:
    python -m apollo.ops.backfill_forward_returns
"""
from __future__ import annotations

import glob
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

try:
    import yfinance as yf
except ImportError:
    print("ERROR: pip install yfinance", file=sys.stderr)
    sys.exit(1)

LOGS_DIR = REPO / "apollo" / "logs"
OUT_PATH = LOGS_DIR / "forward_returns.jsonl"
PROCESSED_CACHE = LOGS_DIR / "forward_returns_processed.json"
# Horizons are measured in TRADING days (indexed off the actual close series
# returned by yfinance), not calendar days — so T+1 always means "next market
# close," independent of weekends/holidays. See _forward_close().
HORIZONS_TRADING_DAYS = (1, 3, 5)
SCORE_FLOOR = 75  # match post-dedup Apollo threshold


def _load_processed() -> set[str]:
    if not PROCESSED_CACHE.exists():
        return set()
    try:
        return set(json.loads(PROCESSED_CACHE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def _save_processed(keys: set[str]) -> None:
    PROCESSED_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_CACHE.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def _scan_date_from_filename(fname: str) -> date | None:
    stem = Path(fname).stem
    if not stem.startswith("scan_"):
        return None
    try:
        return datetime.strptime(stem[len("scan_"):], "%Y%m%d").date()
    except ValueError:
        return None


def _fetch_closes(symbol: str, start: date, end: date) -> pd.Series | None:
    try:
        df = yf.download(symbol, start=start.isoformat(), end=end.isoformat(),
                         progress=False, auto_adjust=True, threads=False)
    except Exception as e:
        print(f"  [{symbol}] fetch error: {e}", file=sys.stderr)
        return None
    if df is None or df.empty:
        return None
    col = df["Close"]
    if isinstance(col, pd.DataFrame):
        col = col.iloc[:, 0]
    return col


def _anchor_pos(closes: pd.Series, scan_date: date) -> int | None:
    """Return the first index position whose date >= scan_date (the anchor bar)."""
    for i, ts in enumerate(closes.index):
        d = ts.date() if hasattr(ts, "date") else ts
        if d >= scan_date:
            return i
    return None


def _forward_close(closes: pd.Series, anchor_pos: int, trading_days: int) -> tuple[date, float] | None:
    """Return the close `trading_days` market closes after the anchor."""
    target = anchor_pos + trading_days
    if target >= len(closes):
        return None
    ts = closes.index[target]
    d = ts.date() if hasattr(ts, "date") else ts
    return d, float(closes.iloc[target])


def process_scan_file(path: Path, processed: set[str]) -> list[dict]:
    scan_date = _scan_date_from_filename(path.name)
    if scan_date is None:
        return []

    today = datetime.now(timezone.utc).date()
    max_horizon = max(HORIZONS_TRADING_DAYS)
    # Skip only scans too fresh for the SHORTEST horizon to be available.
    # Longer horizons fall out naturally in _forward_close() — they produce
    # partial records rather than being dropped up-front.
    if scan_date + timedelta(days=3) > today:
        return []

    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"  {path.name}: unreadable ({e})", file=sys.stderr)
        return []
    if not isinstance(rows, list):
        return []

    eligible = [r for r in rows if isinstance(r, dict) and (r.get("score") or 0) >= SCORE_FLOOR and r.get("symbol")]

    out: list[dict] = []
    window_end = scan_date + timedelta(days=max_horizon + 10)

    for r in eligible:
        symbol = str(r["symbol"]).upper()
        key = f"{symbol}|{scan_date.isoformat()}"
        if key in processed:
            continue

        closes = _fetch_closes(symbol, scan_date, window_end)
        if closes is None or closes.empty:
            continue

        ap = _anchor_pos(closes, scan_date)
        if ap is None:
            continue
        anchor_ts = closes.index[ap]
        anchor_date = anchor_ts.date() if hasattr(anchor_ts, "date") else anchor_ts
        anchor_px = float(closes.iloc[ap])

        record = {
            "symbol": symbol,
            "scan_date": scan_date.isoformat(),
            "anchor_date": anchor_date.isoformat(),
            "anchor_close": anchor_px,
            "score": r.get("score"),
            "direction": r.get("direction"),
            "days_until_earnings": r.get("days_until"),
            "earnings_date": r.get("earnings_date"),
            "post_er_play": bool(r.get("post_er_play")),
            "horizon_unit": "trading_days",
            "forward_returns_pct": {},
        }

        for horizon in HORIZONS_TRADING_DAYS:
            fc = _forward_close(closes, ap, horizon)
            if fc is None:
                continue
            t_date, t_px = fc
            ret_pct = (t_px - anchor_px) / anchor_px * 100.0
            if r.get("direction") == "short":
                ret_pct = -ret_pct
            record["forward_returns_pct"][f"T+{horizon}"] = {
                "date": t_date.isoformat(),
                "close": t_px,
                "return_pct": round(ret_pct, 3),
            }

        if record["forward_returns_pct"]:
            out.append(record)
            processed.add(key)

    return out


def main() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    processed = _load_processed()
    scan_files = sorted(glob.glob(str(LOGS_DIR / "scan_*.json")))
    if not scan_files:
        print("No scan_*.json files found.")
        return 0

    total_written = 0
    with open(OUT_PATH, "a", encoding="utf-8") as f:
        for sf in scan_files:
            records = process_scan_file(Path(sf), processed)
            for rec in records:
                f.write(json.dumps(rec) + "\n")
                total_written += 1
            if records:
                print(f"  {Path(sf).name}: +{len(records)} records")

    _save_processed(processed)
    print(f"Done. Wrote {total_written} new records to {OUT_PATH.name}. "
          f"Processed-cache: {len(processed)} keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
