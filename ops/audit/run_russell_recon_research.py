"""Russell reconstitution research — does IWM show an abnormal return
pattern around the annual late-June rebalance Friday?

Cohort risk audit identified that we need diversifying strategies.
Russell recon is a calendar-based event (last Friday of June each year)
where Russell indexes rebalance their constituents. Index funds
front-run / track the rebalance, creating documented order-flow
imbalance around the event.

This is RESEARCH, not yet a strategy. Output is a JSON + markdown
report of average IWM returns in 5-day windows around each year's
recon Friday, vs a calendar-matched baseline. If a clear pattern
emerges, the strategy gets built in a follow-up batch.

Two hypotheses tested:
  H1 (front-run): IWM outperforms in the 5-10 days BEFORE recon Friday
                  (small-caps gain inflows as funds prepare to add them)
  H2 (reversal):  IWM underperforms / mean-reverts the 5 days AFTER

USAGE:
    python -m ops.audit.run_russell_recon_research
    python -m ops.audit.run_russell_recon_research --ticker IWM
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"


def _last_friday_of_june(year: int) -> date:
    for day in range(30, 0, -1):
        d = date(year, 6, day)
        if d.weekday() == 4:
            return d
    raise RuntimeError(f"unreachable for year {year}")


def _fetch_daily_closes(ticker: str, start_year: int, end_year: int) -> pd.DataFrame:
    import yfinance as yf
    start = f"{start_year}-01-01"
    end = f"{end_year}-12-31"
    df = yf.download(ticker, start=start, end=end, interval="1d",
                       progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Close"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df


def _bar_n_trading_days_from(df: pd.DataFrame, anchor: date, n_offset: int) -> Optional[int]:
    """Find the bar index that is n_offset trading days from the anchor
    date (negative = before, positive = after, 0 = on or first-on-or-after)."""
    pd_anchor = pd.Timestamp(anchor)
    # Find first bar at or after the anchor
    pos = df.index.searchsorted(pd_anchor)
    if pos >= len(df):
        return None
    target = pos + n_offset
    if target < 0 or target >= len(df):
        return None
    return target


def _window_return(df: pd.DataFrame, start_idx: int, end_idx: int) -> Optional[float]:
    """Compound return from close at start_idx to close at end_idx."""
    if start_idx < 0 or end_idx >= len(df) or start_idx >= end_idx:
        return None
    p0 = float(df["Close"].iloc[start_idx])
    p1 = float(df["Close"].iloc[end_idx])
    if p0 <= 0:
        return None
    return (p1 / p0) - 1.0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="IWM",
                          help="Russell 2000 proxy (default IWM)")
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2025)
    args = parser.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[1] fetching {args.ticker} daily {args.start_year}-{args.end_year}...")
    df = _fetch_daily_closes(args.ticker, args.start_year, args.end_year)
    print(f"    {len(df)} daily bars")

    # Build per-year event records
    print()
    print("[2] computing windowed returns around each annual recon Friday...")
    records = []
    for year in range(args.start_year, args.end_year + 1):
        friday = _last_friday_of_june(year)
        friday_idx = _bar_n_trading_days_from(df, friday, 0)
        if friday_idx is None:
            continue
        # If the bar index is BEFORE the actual Friday (e.g., June 26 Fri but bar is June 25),
        # roll forward to the next-available bar.
        if df.index[friday_idx] < pd.Timestamp(friday):
            friday_idx += 1
            if friday_idx >= len(df):
                continue
        record = {
            "year": year,
            "friday": friday.isoformat(),
            "friday_close": float(df["Close"].iloc[friday_idx]),
        }
        # Windows: (-10, -5), (-5, 0), (0, +5), (+5, +10)
        # Indices are inclusive open; closed convention: return from
        # close(start_idx) to close(end_idx).
        windows = {
            "pre10_to_pre5":   (-10, -5),
            "pre5_to_friday":  (-5,   0),
            "friday_to_post5": ( 0,  +5),
            "post5_to_post10":(+5, +10),
            "full_20day":     (-10,+10),
        }
        for name, (a, b) in windows.items():
            ai = _bar_n_trading_days_from(df, friday, a)
            bi = _bar_n_trading_days_from(df, friday, b)
            if ai is None or bi is None:
                record[name] = None
            else:
                # Roll to the actual Friday bar
                if df.index[bi] < pd.Timestamp(friday) and b == 0:
                    bi += 1
                ret = _window_return(df, ai, bi)
                record[name] = (round(ret * 100, 3)
                                  if ret is not None else None)
        records.append(record)

    df_summary = pd.DataFrame(records).set_index("year")
    print()
    print("Per-year returns around recon Friday (%):")
    print(df_summary[["pre10_to_pre5", "pre5_to_friday",
                        "friday_to_post5", "post5_to_post10", "full_20day"]].to_string())

    # Aggregate stats per window
    print()
    print("Aggregate (across years):")
    stats: dict = {}
    for col in ("pre10_to_pre5", "pre5_to_friday", "friday_to_post5",
                  "post5_to_post10", "full_20day"):
        vals = df_summary[col].dropna().values
        if len(vals) == 0:
            continue
        mean = float(np.mean(vals))
        median = float(np.median(vals))
        std = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
        t_stat = (mean / (std / np.sqrt(len(vals)))) if std > 0 else 0.0
        n_positive = int(np.sum(vals > 0))
        stats[col] = {
            "n": len(vals),
            "mean_pct": round(mean, 4),
            "median_pct": round(median, 4),
            "std_pct": round(std, 4),
            "t_stat": round(t_stat, 3),
            "p_value_two_sided": round(2.0 * (1.0 - _normal_cdf(abs(t_stat))), 5),
            "n_positive": n_positive,
            "win_rate": round(n_positive / len(vals), 3),
        }
        print(f"  {col:<20} n={len(vals):<3} mean={mean:+.3f}% median={median:+.3f}% "
              f"std={std:.3f}% t={t_stat:+.2f} wr={n_positive}/{len(vals)}")

    # Persist
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = OUT_DIR / f"russell_recon_research_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ticker": args.ticker,
        "year_range": [args.start_year, args.end_year],
        "windows": stats,
        "per_year": records,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str),
                          encoding="utf-8")

    md_path = OUT_DIR / "russell_recon_research.md"
    md_lines: list[str] = []
    md_lines.append("# Russell reconstitution research — IWM windowed returns")
    md_lines.append("")
    md_lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    md_lines.append(f"Ticker: {args.ticker}  Years: "
                       f"{args.start_year}-{args.end_year}")
    md_lines.append("")
    md_lines.append("## Aggregate window statistics")
    md_lines.append("")
    md_lines.append("| Window | n | mean | median | std | t-stat | p (two-sided) | WR |")
    md_lines.append("|---|---|---|---|---|---|---|---|")
    for col, s in stats.items():
        md_lines.append(
            f"| {col} | {s['n']} | {s['mean_pct']:+.3f}% | "
            f"{s['median_pct']:+.3f}% | {s['std_pct']:.3f}% | "
            f"{s['t_stat']:+.2f} | {s['p_value_two_sided']:.4f} | "
            f"{s['win_rate']*100:.0f}% |"
        )
    md_lines.append("")
    md_lines.append("## Interpretation")
    md_lines.append("")
    md_lines.append(
        "Each window is the compound return on IWM from close(day A) to "
        "close(day B), where A/B are offsets relative to the annual "
        "Russell reconstitution Friday (last Friday of June). "
        "Significance is tested with a one-sample t-statistic against "
        "zero mean. Bonferroni-adjust at 5 tests: significant at p < 0.01."
    )
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print()
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via error function (no scipy import)."""
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


if __name__ == "__main__":
    raise SystemExit(main())
