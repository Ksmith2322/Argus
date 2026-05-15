#!/usr/bin/env python3
"""helio/pead_research.py — Post-Earnings Announcement Drift on small-caps.

Different signal than breakout_research:
  - Breakout signal: stock moved ≥10% in 5 days (price-anchored).
  - PEAD signal: stock reported earnings (event-anchored). Surprise direction
    classifies positive vs negative.

Methodology (academic PEAD literature, Bernard-Thomas 1989+):
  - For each earnings event, classify by EPS surprise % vs analyst estimate.
  - Compute forward returns at 1d, 3d, 5d, 30d, 60d after announcement.
  - Test: positive surprises drift positive, negative surprises drift negative.
  - Documented IR ~0.6-0.8 on $500M-$2B small-caps. Effect strongest in
    weeks 2-10 post-announcement.

Reuses scaffolding:
  - Universe via helio.universe_builder (set --min-price $5, --max-price $50)
  - Daily OHLCV via helio.breakout_research.download_universe
  - Forward returns engine adapted from helio.breakout_forward_returns
  - Edge audit / friction model via helio.edge_audit (transparently reused)

Usage:
    python -m helio.pead_research --fetch-earnings --universe-file ...
    python -m helio.pead_research --compute-returns --universe-file ...
    python -m helio.pead_research --audit --suffix _smallcap
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PEAD_DIR = REPO / "helio" / "data" / "pead"
EARNINGS_CACHE = PEAD_DIR / "earnings"
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"
DAILY_DATA_DIR = REPO / "helio" / "data" / "breakout"

# Surprise classification thresholds (% vs consensus)
POSITIVE_SURPRISE_PCT = 5.0
NEGATIVE_SURPRISE_PCT = -5.0


@dataclass
class EarningsEvent:
    ticker: str
    date: str          # YYYY-MM-DD
    eps_estimate: float | None
    eps_actual: float | None
    surprise_pct: float | None
    direction: str     # POSITIVE / NEGATIVE / NEUTRAL / UNKNOWN


def _classify_surprise(pct: float | None) -> str:
    if pct is None:
        return "UNKNOWN"
    if pct >= POSITIVE_SURPRISE_PCT:
        return "POSITIVE"
    if pct <= NEGATIVE_SURPRISE_PCT:
        return "NEGATIVE"
    return "NEUTRAL"


def fetch_earnings_for_ticker(ticker: str) -> list[EarningsEvent]:
    """Pull earnings history via yfinance. Returns list of EarningsEvent."""
    import yfinance as yf
    try:
        t = yf.Ticker(ticker)
        df = t.earnings_dates
    except Exception:
        return []
    if df is None or df.empty:
        return []

    # yfinance column names can vary across versions
    surprise_col = None
    actual_col = None
    estimate_col = None
    for c in df.columns:
        lc = c.lower()
        if "surprise" in lc and "%" in lc:
            surprise_col = c
        elif "reported" in lc and "eps" in lc:
            actual_col = c
        elif "estimate" in lc and "eps" in lc:
            estimate_col = c
        elif "eps estimate" in lc:
            estimate_col = c

    events: list[EarningsEvent] = []
    for ts, row in df.iterrows():
        try:
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
        except Exception:
            continue
        est = row.get(estimate_col) if estimate_col else None
        act = row.get(actual_col) if actual_col else None
        surprise = row.get(surprise_col) if surprise_col else None
        # Compute surprise if missing
        if (surprise is None or _is_nan(surprise)) and est is not None and act is not None:
            try:
                if est != 0 and not _is_nan(est) and not _is_nan(act):
                    surprise = float((act - est) / abs(est) * 100)
                else:
                    surprise = None
            except Exception:
                surprise = None
        events.append(EarningsEvent(
            ticker=ticker.upper(),
            date=date_str,
            eps_estimate=_to_float(est),
            eps_actual=_to_float(act),
            surprise_pct=_to_float(surprise),
            direction=_classify_surprise(_to_float(surprise)),
        ))
    return events


def _is_nan(x) -> bool:
    try:
        import math
        return isinstance(x, float) and math.isnan(x)
    except Exception:
        return False


def _to_float(x) -> float | None:
    if x is None or _is_nan(x):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def bulk_fetch_earnings(tickers: list[str], delay_s: float = 0.4) -> dict[str, list[EarningsEvent]]:
    """Fetch earnings history for many tickers (throttled). Caches per-ticker."""
    EARNINGS_CACHE.mkdir(parents=True, exist_ok=True)
    results: dict[str, list[EarningsEvent]] = {}
    print(f"Fetching earnings for {len(tickers)} tickers (throttled to {1/delay_s:.1f}/s)...")
    n_ok = 0
    n_empty = 0
    for i, sym in enumerate(tickers, 1):
        cache_path = EARNINGS_CACHE / f"{sym}_earnings.json"
        # Reuse cache if < 7 days old
        if cache_path.exists():
            age_days = (time.time() - cache_path.stat().st_mtime) / 86400
            if age_days < 7:
                try:
                    raw = json.loads(cache_path.read_text())
                    results[sym] = [EarningsEvent(**r) for r in raw]
                    n_ok += 1
                    continue
                except Exception:
                    pass
        time.sleep(delay_s)
        try:
            events = fetch_earnings_for_ticker(sym)
        except Exception:
            events = []
        if events:
            n_ok += 1
            cache_path.write_text(json.dumps([e.__dict__ for e in events], indent=2))
        else:
            n_empty += 1
        results[sym] = events
        if i % 50 == 0:
            print(f"  {i}/{len(tickers)} done — {n_ok} OK, {n_empty} empty")
    print(f"  Complete: {n_ok} OK, {n_empty} empty")
    return results


def build_pead_events_csv(universe_tickers: list[str],
                           output_csv: Path,
                           since_date: str | None = None,
                           include_neutral: bool = False) -> Path:
    """Load cached earnings and emit a flat CSV of (ticker, date, surprise%, direction)."""
    PEAD_DIR.mkdir(parents=True, exist_ok=True)
    if since_date is None:
        # 2 years back to match our 2y breakout data window
        from datetime import timedelta
        since_date = (datetime.now(timezone.utc) - timedelta(days=730)).strftime("%Y-%m-%d")
    rows = []
    n_pos = n_neg = n_neutral = n_unknown = 0
    for sym in universe_tickers:
        cache_path = EARNINGS_CACHE / f"{sym}_earnings.json"
        if not cache_path.exists():
            continue
        try:
            raw = json.loads(cache_path.read_text())
        except Exception:
            continue
        for r in raw:
            if r.get("date", "") < since_date:
                continue
            direction = r.get("direction", "UNKNOWN")
            if not include_neutral and direction == "NEUTRAL":
                continue
            if direction == "POSITIVE":
                n_pos += 1
            elif direction == "NEGATIVE":
                n_neg += 1
            elif direction == "NEUTRAL":
                n_neutral += 1
            else:
                n_unknown += 1
            rows.append(r)
    print(f"Built {len(rows)} PEAD events: {n_pos} positive, {n_neg} negative, "
          f"{n_neutral} neutral, {n_unknown} unknown")
    if not rows:
        return output_csv
    fieldnames = list(rows[0].keys())
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Saved: {output_csv}")
    return output_csv


def compute_pead_forward_returns(events_csv: Path, output_csv: Path,
                                   windows=(1, 3, 5, 10, 30, 60)) -> Path:
    """For each PEAD event, compute forward returns vs SPY benchmark.

    Entry: open of next trading day after announcement (T+1).
    Exit: close at T+k for each k in windows.
    """
    import pandas as pd
    from helio.breakout_forward_returns import _next_day_open_return, _load_daily

    spy_df = _load_daily("SPY")
    if spy_df is None:
        raise RuntimeError("SPY data missing. Run helio.breakout_research with SPY first.")

    df = pd.read_csv(events_csv)
    df["date"] = pd.to_datetime(df["date"])
    print(f"Computing forward returns for {len(df)} PEAD events...")

    # Cache per-ticker daily data
    cache: dict[str, pd.DataFrame | None] = {}
    for sym in df["ticker"].unique():
        cache[sym] = _load_daily(sym)
    n_loaded = sum(1 for v in cache.values() if v is not None)
    print(f"  {n_loaded}/{len(cache)} tickers have daily data")

    results = []
    skipped = 0
    for _, row in df.iterrows():
        sym = row["ticker"]
        ticker_df = cache.get(sym)
        if ticker_df is None:
            skipped += 1
            continue
        # PEAD entry is T+1 open, NOT T+post_window+1 (unlike breakout — there's
        # no detection window for an earnings event, the date is the date).
        row_out = {
            "ticker": sym,
            "date": row["date"].strftime("%Y-%m-%d"),
            "direction": row["direction"],
            "surprise_pct": row.get("surprise_pct"),
        }
        any_ok = False
        for k in windows:
            stock_ret = _next_day_open_return(ticker_df, row["date"], k, post_window_offset=0)
            spy_ret = _next_day_open_return(spy_df, row["date"], k, post_window_offset=0)
            row_out[f"ret_{k}d"] = stock_ret if stock_ret is not None else float("nan")
            row_out[f"spy_ret_{k}d"] = spy_ret if spy_ret is not None else float("nan")
            if stock_ret is not None and spy_ret is not None:
                row_out[f"excess_{k}d"] = stock_ret - spy_ret
                any_ok = True
            else:
                row_out[f"excess_{k}d"] = float("nan")
        if any_ok:
            results.append(row_out)
        else:
            skipped += 1

    out_df = pd.DataFrame(results)
    out_df.to_csv(output_csv, index=False)
    print(f"  Saved {len(out_df)} forward-return rows ({skipped} skipped)")
    print(f"  Output: {output_csv}")
    return output_csv


def analyze_pead_returns(returns_csv: Path) -> dict:
    """Summary stats by surprise direction."""
    import pandas as pd
    import numpy as np
    from helio.breakout_forward_returns import _t_statistic, _information_ratio, _bootstrap_ci

    df = pd.read_csv(returns_csv)
    print(f"\nAnalyzing {len(df)} PEAD events...")
    summary: dict = {"n_events": len(df), "by_direction": {}}

    windows = [c.replace("excess_", "") for c in df.columns if c.startswith("excess_")]
    print(f"\n{'='*75}")
    print(f"PEAD FORWARD RETURNS SUMMARY")
    print(f"{'='*75}")
    print(f"  {'Direction':9s}  {'k':>4s}  {'n':>6s}  {'mean%':>7s}  {'t':>6s}  {'p':>7s}  {'IR':>6s}  verdict")

    for direction in ("POSITIVE", "NEGATIVE"):
        sub = df[df["direction"] == direction]
        summary["by_direction"][direction] = {}
        for w in windows:
            col = f"excess_{w}"
            vals = sub[col].dropna().to_numpy()
            n = len(vals)
            if n == 0:
                continue
            mean = float(np.mean(vals))
            t, p = _t_statistic(vals)
            ir = _information_ratio(vals)
            summary["by_direction"][direction][w] = {
                "n": n,
                "mean_pct": round(mean * 100, 3),
                "t_stat": round(t, 3),
                "p_value": round(p, 5),
                "ir": round(ir, 3),
            }
            verdict = "FOLLOW" if ir >= 0.5 else ("FADE" if ir <= -0.5 else "NOISE")
            marker = " <<<" if abs(ir) >= 0.5 else ""
            print(f"  {direction:9s}  {w:>4s}  {n:>6d}  {mean*100:>+6.2f}%  {t:>+6.2f}  "
                  f"{p:>7.4f}  {ir:>+6.2f}  {verdict}{marker}")

    out_json = returns_csv.parent / f"{returns_csv.stem}_summary.json"
    out_json.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n  Saved: {out_json}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="PEAD Research")
    parser.add_argument("--universe-file", required=False,
                        help="Path to a text file with one ticker per line.")
    parser.add_argument("--fetch-earnings", action="store_true",
                        help="Fetch earnings history for the universe (throttled, cached).")
    parser.add_argument("--build-events", action="store_true",
                        help="Build flat CSV of PEAD events from cached earnings.")
    parser.add_argument("--compute-returns", action="store_true",
                        help="Compute forward returns on events CSV.")
    parser.add_argument("--analyze", action="store_true",
                        help="Summary stats by surprise direction.")
    parser.add_argument("--suffix", default="_smallcap",
                        help="Output filename suffix (default: _smallcap)")
    args = parser.parse_args()

    if not any([args.fetch_earnings, args.build_events, args.compute_returns, args.analyze]):
        # Default: do them all in sequence
        args.fetch_earnings = True
        args.build_events = True
        args.compute_returns = True
        args.analyze = True

    if args.fetch_earnings or args.build_events:
        if not args.universe_file:
            print("ERROR: --universe-file required for --fetch-earnings / --build-events")
            return
        tickers = [t.strip().upper() for t in Path(args.universe_file).read_text().splitlines() if t.strip()]
        print(f"Loaded {len(tickers)} tickers from {args.universe_file}")
    else:
        tickers = None

    events_csv = RESULTS_DIR / f"pead_events{args.suffix}.csv"
    returns_csv = RESULTS_DIR / f"pead_returns{args.suffix}.csv"

    if args.fetch_earnings:
        bulk_fetch_earnings(tickers)

    if args.build_events:
        build_pead_events_csv(tickers, events_csv)

    if args.compute_returns:
        if not events_csv.exists():
            print(f"ERROR: {events_csv} not found. Run --build-events first.")
            return
        compute_pead_forward_returns(events_csv, returns_csv)

    if args.analyze:
        if not returns_csv.exists():
            print(f"ERROR: {returns_csv} not found. Run --compute-returns first.")
            return
        analyze_pead_returns(returns_csv)


if __name__ == "__main__":
    main()
