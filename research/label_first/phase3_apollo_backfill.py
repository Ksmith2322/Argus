"""Phase 3B: Apollo retroactive outcome backfill.

Apollo (earnings/catalyst scanner) has emitted 356 signals across 8 daily
scans but never recorded post-signal price outcomes. We can't validate
whether high-score signals outperform mid-score without forward returns.

This module:
1. Loads all `apollo/logs/scan_*.json` files
2. For each (symbol, scan_date), fetches T+1, T+3, T+5 close prices via yfinance
3. Computes forward returns
4. Buckets by score tier; reports per-tier WR/PF/expectancy
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

REPO = Path(__file__).resolve().parents[2]
APOLLO_LOGS = REPO / "apollo" / "logs"
REPORT_DIR = Path(__file__).resolve().parent / "reports"


def load_signals() -> pd.DataFrame:
    rows = []
    for f in sorted(APOLLO_LOGS.glob("scan_*.json")):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        # scan filename: scan_20260416.json — date is filename
        date_str = f.stem.replace("scan_", "")
        try:
            scan_date = pd.to_datetime(date_str, format="%Y%m%d", utc=True)
        except Exception:
            continue
        for s in data:
            rows.append({
                "scan_date": scan_date,
                "symbol": s.get("symbol"),
                "score": s.get("score", 0),
                "direction": s.get("direction", "long"),
                "days_until_earnings": s.get("days_until", 0),
                "earnings_date": s.get("earnings_date", ""),
                "post_er_play": s.get("post_er_play", False),
            })
    return pd.DataFrame(rows)


def fetch_price_series(symbol: str, start: pd.Timestamp, days: int = 10) -> pd.DataFrame | None:
    """Fetch daily bars from start through start+days+5 (buffer for weekends)."""
    try:
        end = start + pd.Timedelta(days=days + 7)
        df = yf.download(symbol, start=start.date(), end=end.date(),
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.title)[["Open", "High", "Low", "Close"]]
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    except Exception:
        return None


def compute_forward_returns(signals: pd.DataFrame, fwd_days: tuple[int, ...] = (1, 3, 5)) -> pd.DataFrame:
    """For each (symbol, scan_date) pair, fetch price series and compute fwd returns."""
    # Group by symbol to minimize yfinance calls
    out_rows = []
    grouped = signals.groupby("symbol")
    n = len(grouped)
    for k, (symbol, sub) in enumerate(grouped):
        if not symbol or pd.isna(symbol):
            continue
        # Fetch one big window covering all signal dates for this symbol
        start = sub["scan_date"].min()
        end = sub["scan_date"].max() + pd.Timedelta(days=max(fwd_days) + 7)
        try:
            df = yf.download(symbol, start=start.date(), end=end.date(),
                             progress=False, auto_adjust=False)
        except Exception as e:
            print(f"  yfinance failed {symbol}: {e}")
            continue
        if df is None or df.empty:
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.title)[["Open", "High", "Low", "Close"]]
        df.index = pd.to_datetime(df.index, utc=True).normalize()  # date-only

        for _, sig in sub.iterrows():
            sd = sig["scan_date"].normalize()
            # Find the entry row: first trading day on or after scan_date
            entry_rows = df[df.index >= sd]
            if entry_rows.empty:
                continue
            entry_close = float(entry_rows.iloc[0]["Close"])
            entry_idx = entry_rows.index[0]
            forward = df[df.index > entry_idx]
            row = {"symbol": symbol, "scan_date": sd, "score": sig["score"],
                   "direction": sig["direction"], "entry_close": entry_close}
            for d in fwd_days:
                if d - 1 < len(forward):
                    fwd_close = float(forward.iloc[d - 1]["Close"])
                    ret = (fwd_close - entry_close) / entry_close
                    if sig["direction"] == "short":
                        ret = -ret
                    row[f"ret_{d}d"] = ret
                else:
                    row[f"ret_{d}d"] = np.nan
            out_rows.append(row)
        if (k + 1) % 20 == 0:
            print(f"  Processed {k+1}/{n} symbols...")
    return pd.DataFrame(out_rows)


def analyze_by_score_tier(returns: pd.DataFrame, fwd_days: tuple[int, ...] = (1, 3, 5)) -> pd.DataFrame:
    """Bucket by score tier; report per-tier mean return, hit rate, PF."""
    def tier(s):
        if s >= 90: return "sniper(90+)"
        if s >= 75: return "high(75-89)"
        if s >= 60: return "mid(60-74)"
        if s >= 45: return "low(45-59)"
        return "noise(<45)"
    returns["tier"] = returns["score"].apply(tier)

    rows = []
    for d in fwd_days:
        col = f"ret_{d}d"
        for t in ["sniper(90+)", "high(75-89)", "mid(60-74)", "low(45-59)", "noise(<45)"]:
            sub = returns[returns.tier == t][col].dropna()
            if len(sub) < 5:
                continue
            wr = (sub > 0).mean()
            mean_ret = sub.mean()
            wins = sub[sub > 0].sum()
            losses = abs(sub[sub <= 0].sum()) or 1e-9
            pf = wins / losses
            rows.append({
                "fwd_days": d, "tier": t, "n": len(sub),
                "win_rate": round(wr, 3),
                "mean_return": round(mean_ret, 4),
                "pf": round(pf, 2),
            })
    return pd.DataFrame(rows)


def main():
    print("=== Phase 3B: Apollo retroactive outcome backfill ===")
    signals = load_signals()
    print(f"Loaded {len(signals)} signals from {signals['scan_date'].nunique()} scans")
    print(f"  Date range: {signals['scan_date'].min()} -> {signals['scan_date'].max()}")
    print(f"  Unique symbols: {signals['symbol'].nunique()}")
    print()
    print("Score distribution:")
    print(signals["score"].describe().to_string())

    print("\nFetching forward returns for all (symbol, scan_date) pairs...")
    returns = compute_forward_returns(signals)
    returns.to_csv(REPORT_DIR / "phase3_apollo_returns.csv", index=False)
    print(f"\nSaved {len(returns)} signal+return rows to phase3_apollo_returns.csv")

    print("\nAnalysis by score tier:")
    analysis = analyze_by_score_tier(returns)
    analysis.to_csv(REPORT_DIR / "phase3_apollo_tier_analysis.csv", index=False)
    print(analysis.to_string(index=False))

    print("\nKey question: do higher-tier signals outperform lower-tier?")
    for d in (1, 3, 5):
        sub = analysis[analysis.fwd_days == d].sort_values("tier", key=lambda x: x.map({
            "sniper(90+)": 0, "high(75-89)": 1, "mid(60-74)": 2, "low(45-59)": 3, "noise(<45)": 4
        }))
        if len(sub) < 2:
            continue
        sniper_pf = sub[sub.tier == "sniper(90+)"]["pf"].iloc[0] if (sub.tier == "sniper(90+)").any() else None
        mid_pf = sub[sub.tier == "mid(60-74)"]["pf"].iloc[0] if (sub.tier == "mid(60-74)").any() else None
        if sniper_pf and mid_pf:
            print(f"  T+{d}: sniper PF {sniper_pf:.2f} vs mid PF {mid_pf:.2f} — {'SCORE PREDICTS' if sniper_pf > mid_pf * 1.1 else 'NO clear lift'}")


if __name__ == "__main__":
    main()
