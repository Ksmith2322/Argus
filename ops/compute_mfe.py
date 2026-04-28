"""Compute true MFE (Max Favorable Excursion) for closed trades using bar
lookback. Replaces the target_capture proxy with real exit-quality data.

For each closed trade in canonical_fills.jsonl (within window):
  1. Fetch intraday bars (yfinance) covering [entry_ts, exit_ts]
  2. MFE = max favorable price during hold (max High for long, min Low for short)
  3. MAE = max adverse price during hold (min Low for long, max High for short)
  4. capture_ratio = realized_R / mfe_R  (how much of the favorable move was kept)
     R = abs(planned_target_distance) for normalization
  5. Aggregate per strategy: mean capture, MFE/realized distribution, R-multiples

Caches bars by (symbol, day, interval) — multiple trades on same symbol/day
share the fetch.

Output: argus_flow/logs/mfe_capture.json
Used by: dashboard /api/mfe_capture endpoint

Usage:
    python -m ops.compute_mfe                # default 30d window
    python -m ops.compute_mfe --window 7     # last 7d only
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO = Path(__file__).resolve().parents[1]
OUT_PATH = REPO / "argus_flow" / "logs" / "mfe_capture.json"

# Per-strategy bar interval map. Conservative: use 5m where strategy uses anything
# ≤5m (catches the high), 15m for 15m strategies, 1h for 1h+.
_INTERVAL_MAP = {
    "forge_spy_mean_rev":      "5m",
    "forge_multi_orb":         "5m",
    "forge_vix_intraday":      "5m",
    "forge_aud_asian_breakout":"15m",
    "forge_jpy_pm_short":      "1h",
    "forge_nq_overnight":      "1h",
    "forge_nq_london_close":   "5m",
    "forge_gld_pm_long":       "1h",
    "forge_wick_gbpusd":       "5m",
    "argus_usdjpy":            "5m",
    "argus_gbpusd":            "5m",
    "argus_cadjpy":            "5m",
}

# yfinance ticker map — some of our internal symbols differ
_YF_TICKER = {
    "USDJPY": "USDJPY=X",
    "GBPUSD": "GBPUSD=X",
    "CADJPY": "CADJPY=X",
    "AUDUSD": "AUDUSD=X",
    "EURUSD": "EURUSD=X",
    "GBPJPY": "GBPJPY=X",
}

# Bar cache: key = (symbol, interval, period_days) -> DataFrame
_bar_cache: dict = {}


def _yf_ticker(symbol: str) -> str:
    return _YF_TICKER.get(symbol.upper(), symbol.upper())


def _fetch_bars(symbol: str, interval: str, days: int) -> pd.DataFrame:
    """Fetch bars with caching. period uses yfinance shorthand."""
    key = (symbol, interval, days)
    if key in _bar_cache:
        return _bar_cache[key]
    period = f"{days}d"
    try:
        df = yf.download(_yf_ticker(symbol), period=period, interval=interval,
                         progress=False, auto_adjust=False, threads=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.title)[["Open", "High", "Low", "Close"]].dropna()
        df.index = pd.to_datetime(df.index, utc=True)
        _bar_cache[key] = df
        return df
    except Exception as e:
        print(f"  yfinance fetch failed for {symbol} ({interval},{period}): {e}", file=sys.stderr)
        _bar_cache[key] = pd.DataFrame()
        return _bar_cache[key]


def _per_strategy_invalid_set() -> dict:
    """Reuses dashboard logic — invalid trades by entry_ts."""
    import csv
    invalid = {}
    for csv_path in (REPO / "forge" / "logs").glob("*/trades.csv"):
        strategy_dir = csv_path.parent.name
        label = f"forge_{strategy_dir}"
        try:
            with csv_path.open(encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    if str(r.get("experiment_valid", "true")).lower() == "false":
                        ets = r.get("ts") or r.get("entry_ts") or ""
                        invalid.setdefault(label, set()).add(ets)
        except Exception:
            continue
    return invalid


def compute_mfe_for_trade(symbol: str, entry_ts: datetime, exit_ts: datetime,
                          interval: str, fetch_days: int) -> dict | None:
    """Returns {mfe_high, mfe_low, n_bars} for the [entry_ts, exit_ts] window."""
    df = _fetch_bars(symbol, interval, fetch_days)
    if df.empty:
        return None
    # Slice to hold window — pad by 1 bar on each side
    mask = (df.index >= entry_ts) & (df.index <= exit_ts)
    win = df.loc[mask]
    if win.empty:
        # Try with widened window (sometimes entry_ts is just before first bar)
        widened_start = entry_ts - timedelta(minutes=10)
        widened_end = exit_ts + timedelta(minutes=10)
        win = df.loc[(df.index >= widened_start) & (df.index <= widened_end)]
    if win.empty:
        return None
    return {
        "mfe_high": float(win["High"].max()),
        "mfe_low":  float(win["Low"].min()),
        "n_bars":   len(win),
    }


def main(window_days: int = 30) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    fp = REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"
    if not fp.exists():
        print(f"FAIL: {fp} not found")
        OUT_PATH.write_text(json.dumps({"window_days": window_days, "strategies": [], "n_trades_processed": 0}, indent=2))
        return 1

    invalid_map = _per_strategy_invalid_set()

    # Determine fetch_days based on window + buffer (yfinance limits: 5m=60d, 1h=730d)
    fetch_days = min(window_days + 5, 60)

    by_strategy: dict[str, list] = {}
    n_total = 0
    n_processed = 0
    n_skipped_no_bars = 0
    n_skipped_invalid = 0

    with fp.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("side") != "EXIT" or r.get("source") == "backfill_from_trade_csv":
                continue
            try:
                exit_ts = datetime.fromisoformat(str(r.get("exit_ts","")).replace("Z","+00:00"))
                entry_ts = datetime.fromisoformat(str(r.get("entry_ts","")).replace("Z","+00:00"))
                if exit_ts.tzinfo is None: exit_ts = exit_ts.replace(tzinfo=timezone.utc)
                if entry_ts.tzinfo is None: entry_ts = entry_ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if exit_ts < cutoff:
                continue
            n_total += 1

            strat = r.get("strategy") or "unknown"
            if (r.get("entry_ts") or "") in invalid_map.get(strat, set()):
                n_skipped_invalid += 1
                continue

            symbol = r.get("symbol") or ""
            try:
                entry_px = float(r["entry_px"])
                exit_px  = float(r["exit_px"])
            except Exception:
                continue
            direction = (r.get("direction") or "long").lower()

            interval = _INTERVAL_MAP.get(strat, "5m")
            mfe = compute_mfe_for_trade(symbol, entry_ts, exit_ts, interval, fetch_days)
            if not mfe:
                n_skipped_no_bars += 1
                continue
            n_processed += 1

            # MFE in price terms
            if direction == "long":
                mfe_price = mfe["mfe_high"]
                mae_price = mfe["mfe_low"]
                mfe_distance = mfe_price - entry_px       # positive = favorable
                mae_distance = entry_px - mae_price       # positive = adverse
                realized_distance = exit_px - entry_px
            else:
                mfe_price = mfe["mfe_low"]
                mae_price = mfe["mfe_high"]
                mfe_distance = entry_px - mfe_price
                mae_distance = mae_price - entry_px
                realized_distance = entry_px - exit_px

            # Capture ratio: how much of the favorable move was kept
            # Avoid divide-by-zero with min positive denominator
            mfe_safe = max(abs(mfe_distance), 1e-9)
            capture_ratio = realized_distance / mfe_safe   # 1.0 = exited at peak; <1 = left money on table; <0 = stopped

            by_strategy.setdefault(strat, []).append({
                "symbol": symbol,
                "direction": direction,
                "entry_ts": entry_ts.isoformat(),
                "exit_ts": exit_ts.isoformat(),
                "mfe_distance": round(mfe_distance, 6),
                "mae_distance": round(mae_distance, 6),
                "realized_distance": round(realized_distance, 6),
                "capture_ratio": round(capture_ratio, 4),
                "mfe_to_mae_ratio": round(mfe_distance / max(mae_distance, 1e-9), 3),
                "n_bars": mfe["n_bars"],
            })

    # Aggregate per strategy
    rows = []
    for strat, trades in by_strategy.items():
        if not trades: continue
        n = len(trades)
        captures = [t["capture_ratio"] for t in trades]
        captures_sorted = sorted(captures)
        mean_cap = sum(captures) / n
        median_cap = captures_sorted[n // 2]
        # Distribution buckets
        excellent = sum(1 for c in captures if c >= 0.80)
        good      = sum(1 for c in captures if 0.50 <= c < 0.80)
        partial   = sum(1 for c in captures if 0 < c < 0.50)
        scratch   = sum(1 for c in captures if -0.10 <= c <= 0)
        adverse   = sum(1 for c in captures if c < -0.10)
        # Average MFE-to-MAE ratio (how often did the trade go favorable BEFORE going adverse)
        mfe_mae_avg = sum(t["mfe_to_mae_ratio"] for t in trades) / n
        rows.append({
            "strategy": strat,
            "n": n,
            "mean_capture": round(mean_cap, 3),
            "median_capture": round(median_cap, 3),
            "excellent_pct": round(excellent / n * 100, 1),
            "good_pct":      round(good / n * 100, 1),
            "partial_pct":   round(partial / n * 100, 1),
            "scratch_pct":   round(scratch / n * 100, 1),
            "adverse_pct":   round(adverse / n * 100, 1),
            "mean_mfe_to_mae": round(mfe_mae_avg, 3),
            # Per-trade detail for drill-down (capped at 100 trades to keep file size sane)
            "trades": trades[-100:],
        })
    rows.sort(key=lambda x: -x["mean_capture"])

    payload = {
        "computed_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "n_trades_total": n_total,
        "n_trades_processed": n_processed,
        "n_skipped_invalid": n_skipped_invalid,
        "n_skipped_no_bars": n_skipped_no_bars,
        "strategies": rows,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"OK: {n_processed}/{n_total} trades processed (invalid={n_skipped_invalid}, no_bars={n_skipped_no_bars}), {len(rows)} strategies, written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window", type=int, default=30, help="Trade window in days (default: 30)")
    args = parser.parse_args()
    sys.exit(main(window_days=args.window))
