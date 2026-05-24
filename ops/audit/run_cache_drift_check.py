"""Live-vs-cache drift check for xs_momentum's broad-8 universe.

When yfinance succeeds, compare the latest live close against the
cached close. Alert if they diverge beyond a small threshold —
indicates either:
  - The cache is stale (refresh needed)
  - Or yfinance is serving bad data and we shouldn't overwrite

Read-only; does NOT mutate the cache. The operator decides whether
to refresh based on the report.

USAGE:
    python -m ops.audit.run_cache_drift_check                # default tickers
    python -m ops.audit.run_cache_drift_check --threshold 0.02
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[2]
DEFAULT_UNIVERSE = ("SPY", "QQQ", "IWM", "DIA", "EFA", "EEM", "GLD", "TLT")
DEFAULT_THRESHOLD = 0.01   # 1% close-price divergence triggers alert


def _cache_close(ticker: str) -> tuple[float | None, str | None]:
    """Read the most recent close from the CSV cache. Returns (close, date)
    or (None, None) if cache missing."""
    cache_path = (REPO / "helio" / "data_yfinance"
                    / f"{ticker.upper().replace('=', '_').replace('^', '_')}_daily.csv")
    if not cache_path.exists():
        return (None, None)
    try:
        lines = cache_path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return (None, None)
        last = lines[-1].split(",")
        if len(last) < 5:
            return (None, None)
        date_str = last[0]
        close = float(last[4])
        return (close, date_str)
    except Exception:
        return (None, None)


def _live_close(ticker: str) -> tuple[float | None, str | None]:
    """Fetch latest close from yfinance. Returns (close, date) or
    (None, None) on failure."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="5d", interval="1d",
                           auto_adjust=False, progress=False)
        if df is None or df.empty:
            return (None, None)
        if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        close = float(df["Close"].iloc[-1])
        date_str = str(df.index[-1].date())
        return (close, date_str)
    except Exception:
        return (None, None)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="+", default=list(DEFAULT_UNIVERSE),
                          help="Tickers to check (default: broad-8 universe)")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                          help="Drift threshold as fraction (default 0.01 = 1%)")
    args = parser.parse_args(argv)

    print(f"[1] checking {len(args.tickers)} tickers for cache↔live drift "
          f"(threshold {args.threshold*100:.1f}%)...")
    print()
    rows = []
    n_diverge = 0
    n_cache_only = 0
    n_live_only = 0
    n_both_missing = 0
    n_match = 0
    for ticker in args.tickers:
        cache_close, cache_date = _cache_close(ticker)
        live_close, live_date = _live_close(ticker)
        status = "?"
        drift_pct = None
        if cache_close is None and live_close is None:
            status = "BOTH_MISSING"
            n_both_missing += 1
        elif cache_close is None:
            status = "CACHE_MISSING"
            n_live_only += 1
        elif live_close is None:
            status = "LIVE_FAILED"
            n_cache_only += 1
        else:
            drift = (live_close - cache_close) / cache_close
            drift_pct = drift * 100
            if abs(drift) > args.threshold:
                status = "DIVERGES"
                n_diverge += 1
            else:
                status = "OK"
                n_match += 1
        rows.append({
            "ticker": ticker, "status": status,
            "cache_close": cache_close, "cache_date": cache_date,
            "live_close": live_close, "live_date": live_date,
            "drift_pct": round(drift_pct, 4) if drift_pct is not None else None,
        })
        print(f"  {ticker:<6} {status:<14} "
              f"cache=${cache_close:.2f if cache_close else 0} "
              f"({cache_date or '-'})  "
              f"live=${live_close:.2f if live_close else 0} "
              f"({live_date or '-'})  "
              f"drift={'(n/a)' if drift_pct is None else f'{drift_pct:+.3f}%'}")

    print()
    print(f"Summary: {n_match} OK, {n_diverge} DIVERGES, "
          f"{n_cache_only} live-failed, {n_live_only} cache-missing, "
          f"{n_both_missing} both-missing")

    out_dir = REPO / "ops" / "reports" / "system_audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cache_drift_check.json"
    out_path.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold": args.threshold,
        "summary": {
            "ok": n_match, "diverges": n_diverge,
            "live_failed": n_cache_only, "cache_missing": n_live_only,
            "both_missing": n_both_missing,
        },
        "rows": rows,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nJSON: {out_path}")

    # Exit code: 1 if any divergence/failure to surface in cron
    if n_diverge or n_cache_only or n_both_missing:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
