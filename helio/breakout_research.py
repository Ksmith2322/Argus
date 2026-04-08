#!/usr/bin/env python3
"""helio/breakout_research.py -- Reverse-engineer stock breakout patterns.

Phase 1: Download historical data for a universe of stocks known for big moves.
Phase 2: Detect all breakout events (>5% moves in 1-5 days).
Phase 3: Analyze what happened BEFORE each breakout (the setup fingerprint).
Phase 4: Score which stocks break out most reliably and what the pre-signals are.

Usage:
    python -m helio.breakout_research --scan          # Download + detect breakouts
    python -m helio.breakout_research --analyze        # Reverse-engineer patterns
    python -m helio.breakout_research --scan --analyze  # Both
    python -m helio.breakout_research --top 20          # Show top 20 breakout stocks
"""
import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = REPO / "helio" / "data" / "breakout"
RESULTS_DIR = REPO / "helio" / "data" / "breakout_results"

# ── Stock Universe ────────────────────────────────────────────────
# Stocks known for volatility, breakouts, momentum plays
# Categorized by type for analysis

UNIVERSE = {
    # Growth / Tech (high beta, frequent breakouts)
    "SOFI": "fintech", "PLTR": "tech", "HOOD": "fintech", "MARA": "crypto_proxy",
    "RIOT": "crypto_proxy", "COIN": "crypto_proxy", "SQ": "fintech", "AFRM": "fintech",
    "UPST": "fintech", "NU": "fintech",
    # AI / Semiconductor
    "NVDA": "semi", "AMD": "semi", "SMCI": "semi", "ARM": "semi", "MRVL": "semi",
    "TSM": "semi", "MU": "semi", "AVGO": "semi",
    # EV / Energy
    "TSLA": "ev", "RIVN": "ev", "LCID": "ev", "NIO": "ev", "PLUG": "energy",
    "FSLR": "energy", "ENPH": "energy",
    # Biotech (big gap potential)
    "MRNA": "biotech", "BNTX": "biotech", "CRSP": "biotech", "EDIT": "biotech",
    "NTLA": "biotech",
    # Retail / Consumer
    "GME": "meme", "AMC": "meme", "BBBY": "meme", "SNAP": "social",
    "PINS": "social", "RBLX": "gaming",
    # China / EM
    "BABA": "china", "PDD": "china", "JD": "china", "NIO": "china", "BIDU": "china",
    # Large cap movers
    "META": "mega_tech", "GOOGL": "mega_tech", "AMZN": "mega_tech", "AAPL": "mega_tech",
    "MSFT": "mega_tech", "NFLX": "mega_tech",
    # ETFs for sector breakouts
    "XBI": "etf_bio", "ARKK": "etf_growth", "KWEB": "etf_china", "SMH": "etf_semi",
    "XLE": "etf_energy", "GDX": "etf_gold",
}


# ── Phase 1: Download Historical Data ────────────────────────────

def download_universe(symbols: list[str] | None = None, period: str = "2y"):
    """Download daily OHLCV data for the stock universe."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tickers = symbols or list(UNIVERSE.keys())

    print(f"Downloading {len(tickers)} stocks ({period} history)...")
    success = 0
    failed = []

    for i, sym in enumerate(tickers):
        out_path = DATA_DIR / f"{sym}_daily.csv"

        # Skip if fresh (< 24h old)
        if out_path.exists():
            age_h = (time.time() - out_path.stat().st_mtime) / 3600
            if age_h < 24:
                success += 1
                continue

        try:
            df = yf.download(sym, period=period, interval="1d", progress=False, auto_adjust=True)
            if df.empty or len(df) < 60:
                failed.append(sym)
                continue

            # Flatten multi-level columns if needed
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            df.index.name = "date"
            df.to_csv(out_path)
            success += 1

            if (i + 1) % 10 == 0:
                print(f"  {i + 1}/{len(tickers)} downloaded...")
                time.sleep(0.5)  # rate limit

        except Exception as e:
            failed.append(sym)
            continue

    print(f"  Done: {success} OK, {len(failed)} failed")
    if failed:
        print(f"  Failed: {', '.join(failed)}")
    return success, failed


# ── Phase 2: Detect Breakout Events ──────────────────────────────

def detect_breakouts(min_move_pct: float = 5.0, lookback_days: int = 5):
    """Scan all downloaded stocks for breakout events.

    A breakout = price moves >= min_move_pct within lookback_days.
    Returns list of breakout events with pre-breakout context.
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_breakouts = []

    csv_files = sorted(DATA_DIR.glob("*_daily.csv"))
    print(f"\nScanning {len(csv_files)} stocks for breakouts (>={min_move_pct}% in {lookback_days}d)...")

    for csv_path in csv_files:
        sym = csv_path.stem.replace("_daily", "")
        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        except Exception:
            continue

        if len(df) < 60 or "Close" not in df.columns:
            continue

        close = df["Close"].values
        high = df["High"].values
        low = df["Low"].values
        volume = df["Volume"].values if "Volume" in df.columns else np.zeros(len(df))
        dates = df.index

        # Compute rolling features
        vol_20 = pd.Series(volume).rolling(20).mean().values
        atr_14 = _compute_atr(high, low, close, 14)
        bb_width = _compute_bb_width(close, 20)
        rsi_14 = _compute_rsi(close, 14)
        ema_20 = pd.Series(close).ewm(span=20).mean().values
        ema_50 = pd.Series(close).ewm(span=50).mean().values
        ema_200 = pd.Series(close).ewm(span=200).mean().values

        # Scan for breakout events
        for i in range(lookback_days, len(close) - 1):
            # Check forward move from this bar
            future_end = min(i + lookback_days, len(close) - 1)
            max_future_high = np.max(high[i:future_end + 1])
            min_future_low = np.min(low[i:future_end + 1])

            # Upside breakout
            up_move = (max_future_high - close[i]) / close[i] * 100
            # Downside breakout
            down_move = (close[i] - min_future_low) / close[i] * 100

            if up_move >= min_move_pct:
                breakout = _build_breakout_record(
                    sym, "UP", dates[i], close, high, low, volume,
                    vol_20, atr_14, bb_width, rsi_14,
                    ema_20, ema_50, ema_200, i, up_move
                )
                all_breakouts.append(breakout)

            elif down_move >= min_move_pct:
                breakout = _build_breakout_record(
                    sym, "DOWN", dates[i], close, high, low, volume,
                    vol_20, atr_14, bb_width, rsi_14,
                    ema_20, ema_50, ema_200, i, -down_move
                )
                all_breakouts.append(breakout)

    # Save all breakouts
    if all_breakouts:
        out_path = RESULTS_DIR / "all_breakouts.csv"
        df_out = pd.DataFrame(all_breakouts)
        df_out.to_csv(out_path, index=False)
        print(f"  Found {len(all_breakouts)} breakout events across {len(csv_files)} stocks")
        print(f"  Saved: {out_path}")
    else:
        print("  No breakouts found.")

    return all_breakouts


def _build_breakout_record(sym, direction, date, close, high, low, volume,
                            vol_20, atr_14, bb_width, rsi_14,
                            ema_20, ema_50, ema_200, idx, move_pct):
    """Build a feature-rich breakout record for pattern analysis."""
    # Pre-breakout features (what happened BEFORE the move)
    lookback = 20  # 20 bars before

    # Volume pattern before breakout
    pre_vol = volume[max(0, idx - lookback):idx]
    vol_avg_pre = np.mean(pre_vol) if len(pre_vol) > 0 else 0
    vol_trend = (np.mean(pre_vol[-5:]) / np.mean(pre_vol[:5]) - 1) if len(pre_vol) >= 10 and np.mean(pre_vol[:5]) > 0 else 0

    # Price compression before breakout
    pre_close = close[max(0, idx - lookback):idx]
    price_range_20d = (np.max(pre_close) - np.min(pre_close)) / np.mean(pre_close) * 100 if len(pre_close) > 0 and np.mean(pre_close) > 0 else 0

    # Consecutive narrow range days
    narrow_days = 0
    if idx >= 5:
        daily_ranges = [(high[j] - low[j]) / close[j] * 100 for j in range(max(0, idx - 10), idx)]
        avg_range = np.mean(daily_ranges) if daily_ranges else 1
        for j in range(idx - 1, max(0, idx - 10), -1):
            if (high[j] - low[j]) / close[j] * 100 < avg_range * 0.7:
                narrow_days += 1
            else:
                break

    return {
        "symbol": sym,
        "sector": UNIVERSE.get(sym, "unknown"),
        "date": str(date.date()) if hasattr(date, 'date') else str(date)[:10],
        "direction": direction,
        "move_pct": round(move_pct, 2),
        "price": round(close[idx], 2),
        # Pre-breakout indicators
        "bb_width": round(bb_width[idx], 4) if idx < len(bb_width) and not np.isnan(bb_width[idx]) else 0,
        "bb_width_pctile": round(_percentile_rank(bb_width[:idx], bb_width[idx]), 2) if idx > 20 else 0,
        "rsi_14": round(rsi_14[idx], 1) if idx < len(rsi_14) and not np.isnan(rsi_14[idx]) else 50,
        "atr_14_pct": round(atr_14[idx] / close[idx] * 100, 3) if idx < len(atr_14) and close[idx] > 0 and not np.isnan(atr_14[idx]) else 0,
        "vol_ratio": round(volume[idx] / vol_20[idx], 2) if idx < len(vol_20) and vol_20[idx] > 0 and not np.isnan(vol_20[idx]) else 1,
        "vol_trend_5d": round(vol_trend, 3),
        "price_range_20d": round(price_range_20d, 2),
        "narrow_range_days": narrow_days,
        # EMA structure
        "above_ema20": 1 if idx < len(ema_20) and close[idx] > ema_20[idx] else 0,
        "above_ema50": 1 if idx < len(ema_50) and close[idx] > ema_50[idx] else 0,
        "above_ema200": 1 if idx < len(ema_200) and close[idx] > ema_200[idx] else 0,
        "ema20_above_ema50": 1 if idx < min(len(ema_20), len(ema_50)) and ema_20[idx] > ema_50[idx] else 0,
        # Price distance from key levels
        "dist_from_ema20_pct": round((close[idx] - ema_20[idx]) / ema_20[idx] * 100, 2) if idx < len(ema_20) and ema_20[idx] > 0 else 0,
        "dist_from_ema50_pct": round((close[idx] - ema_50[idx]) / ema_50[idx] * 100, 2) if idx < len(ema_50) and ema_50[idx] > 0 else 0,
        # Recent momentum
        "return_5d": round((close[idx] / close[max(0, idx - 5)] - 1) * 100, 2) if idx >= 5 and close[max(0, idx - 5)] > 0 else 0,
        "return_20d": round((close[idx] / close[max(0, idx - 20)] - 1) * 100, 2) if idx >= 20 and close[max(0, idx - 20)] > 0 else 0,
    }


# ── Phase 3: Reverse-Engineer Patterns ────────────────────────────

def analyze_breakout_patterns():
    """Analyze pre-breakout features to find the setup fingerprint."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    bo_path = RESULTS_DIR / "all_breakouts.csv"
    if not bo_path.exists():
        print("No breakout data. Run --scan first.")
        return

    df = pd.read_csv(bo_path)
    print(f"\nAnalyzing {len(df)} breakout events...")

    # Split up vs down
    up = df[df["direction"] == "UP"]
    down = df[df["direction"] == "DOWN"]
    print(f"  UP breakouts: {len(up)} | DOWN breakouts: {len(down)}")

    # ── Stock Rankings ────────────────────────────────────────
    print(f"\n{'='*60}")
    print("TOP BREAKOUT STOCKS (by frequency)")
    print(f"{'='*60}")

    stock_stats = []
    for sym in df["symbol"].unique():
        sym_df = df[df["symbol"] == sym]
        sym_up = sym_df[sym_df["direction"] == "UP"]
        sym_down = sym_df[sym_df["direction"] == "DOWN"]
        avg_move = sym_df["move_pct"].abs().mean()

        # Get data span for frequency calculation
        csv_path = DATA_DIR / f"{sym}_daily.csv"
        try:
            hist = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            trading_days = len(hist)
        except Exception:
            trading_days = 504  # ~2 years

        freq_per_month = len(sym_df) / (trading_days / 21)

        stock_stats.append({
            "symbol": sym,
            "sector": UNIVERSE.get(sym, "?"),
            "total_breakouts": len(sym_df),
            "up": len(sym_up),
            "down": len(sym_down),
            "avg_move_pct": round(avg_move, 1),
            "max_move_pct": round(sym_df["move_pct"].abs().max(), 1),
            "freq_per_month": round(freq_per_month, 1),
            "up_ratio": round(len(sym_up) / len(sym_df), 2) if len(sym_df) > 0 else 0,
            # Avg pre-breakout features
            "avg_bb_width": round(sym_df["bb_width"].mean(), 4),
            "avg_bb_pctile": round(sym_df["bb_width_pctile"].mean(), 1),
            "avg_rsi": round(sym_df["rsi_14"].mean(), 1),
            "avg_vol_ratio": round(sym_df["vol_ratio"].mean(), 2),
            "avg_narrow_days": round(sym_df["narrow_range_days"].mean(), 1),
        })

    stats_df = pd.DataFrame(stock_stats).sort_values("freq_per_month", ascending=False)

    for _, row in stats_df.head(25).iterrows():
        bias = "UP-biased" if row["up_ratio"] > 0.6 else ("DOWN-biased" if row["up_ratio"] < 0.4 else "balanced")
        print(f"  {row['symbol']:6s} [{row['sector']:12s}] {row['freq_per_month']:.1f}/mo | "
              f"{row['total_breakouts']} total ({row['up']}U/{row['down']}D) | "
              f"avg {row['avg_move_pct']:.1f}% max {row['max_move_pct']:.1f}% | {bias}")

    # ── Pre-Breakout Fingerprint ──────────────────────────────
    print(f"\n{'='*60}")
    print("PRE-BREAKOUT FINGERPRINT (UP breakouts)")
    print(f"{'='*60}")

    features = ["bb_width_pctile", "rsi_14", "vol_ratio", "vol_trend_5d",
                "price_range_20d", "narrow_range_days", "above_ema20",
                "above_ema50", "above_ema200", "ema20_above_ema50",
                "dist_from_ema20_pct", "return_5d", "return_20d"]

    for f in features:
        if f in up.columns:
            vals = up[f].dropna()
            if len(vals) > 0:
                print(f"  {f:25s} mean={vals.mean():.3f}  median={vals.median():.3f}  "
                      f"std={vals.std():.3f}  [p25={vals.quantile(0.25):.3f}, p75={vals.quantile(0.75):.3f}]")

    # ── Ideal Setup Profile ───────────────────────────────────
    print(f"\n{'='*60}")
    print("IDEAL BREAKOUT SETUP (strongest predictors)")
    print(f"{'='*60}")

    # Compare breakout days vs random days for top stocks
    top_syms = stats_df.head(10)["symbol"].tolist()
    breakout_features = []
    normal_features = []

    for sym in top_syms:
        csv_path = DATA_DIR / f"{sym}_daily.csv"
        try:
            hist = pd.read_csv(csv_path, index_col=0, parse_dates=True)
        except Exception:
            continue

        sym_breakouts = set(df[df["symbol"] == sym]["date"].tolist())
        close = hist["Close"].values
        bb_w = _compute_bb_width(close, 20)
        rsi = _compute_rsi(close, 14)
        vol = hist["Volume"].values if "Volume" in hist.columns else np.zeros(len(hist))
        vol_20 = pd.Series(vol).rolling(20).mean().values

        for i in range(30, len(hist)):
            date_str = str(hist.index[i].date()) if hasattr(hist.index[i], 'date') else str(hist.index[i])[:10]
            feat = {
                "bb_width": bb_w[i] if not np.isnan(bb_w[i]) else 0,
                "rsi": rsi[i] if not np.isnan(rsi[i]) else 50,
                "vol_ratio": vol[i] / vol_20[i] if vol_20[i] > 0 and not np.isnan(vol_20[i]) else 1,
            }
            if date_str in sym_breakouts:
                breakout_features.append(feat)
            else:
                normal_features.append(feat)

    if breakout_features and normal_features:
        bo_df = pd.DataFrame(breakout_features)
        norm_df = pd.DataFrame(normal_features)
        print(f"  Breakout days: {len(bo_df)} | Normal days: {len(norm_df)}")
        for col in bo_df.columns:
            bo_mean = bo_df[col].mean()
            norm_mean = norm_df[col].mean()
            diff = ((bo_mean / norm_mean) - 1) * 100 if norm_mean != 0 else 0
            marker = " ***" if abs(diff) > 20 else ""
            print(f"  {col:15s}  breakout={bo_mean:.3f}  normal={norm_mean:.3f}  diff={diff:+.1f}%{marker}")

    # Save full results
    stats_path = RESULTS_DIR / "stock_rankings.csv"
    stats_df.to_csv(stats_path, index=False)
    print(f"\n  Rankings saved: {stats_path}")

    # Save the ideal setup profile
    profile = {
        "up_breakout_profile": {
            f: {"mean": round(float(up[f].mean()), 4), "median": round(float(up[f].median()), 4),
                "p25": round(float(up[f].quantile(0.25)), 4), "p75": round(float(up[f].quantile(0.75)), 4)}
            for f in features if f in up.columns and len(up[f].dropna()) > 0
        },
        "top_stocks": stats_df.head(15).to_dict(orient="records"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_breakouts": len(df),
        "up_breakouts": len(up),
        "down_breakouts": len(down),
    }
    profile_path = RESULTS_DIR / "breakout_profile.json"
    profile_path.write_text(json.dumps(profile, indent=2, default=str))
    print(f"  Profile saved: {profile_path}")

    return stats_df, profile


# ── Technical Indicator Helpers ───────────────────────────────────

def _compute_atr(high, low, close, period=14):
    tr = np.maximum(high[1:] - low[1:],
                     np.maximum(abs(high[1:] - close[:-1]), abs(low[1:] - close[:-1])))
    tr = np.concatenate([[tr[0]], tr])
    atr = pd.Series(tr).rolling(period).mean().values
    return atr


def _compute_bb_width(close, period=20):
    s = pd.Series(close)
    sma = s.rolling(period).mean()
    std = s.rolling(period).std()
    width = (2 * std) / sma  # normalized BB width
    return width.values


def _compute_rsi(close, period=14):
    delta = np.diff(close, prepend=close[0])
    gains = np.where(delta > 0, delta, 0)
    losses = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gains).rolling(period).mean().values
    avg_loss = pd.Series(losses).rolling(period).mean().values
    rs = np.divide(avg_gain, avg_loss, out=np.ones_like(avg_gain) * 50, where=avg_loss > 0)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _percentile_rank(arr, value):
    """What percentile is value in the array? 0=lowest, 100=highest."""
    valid = arr[~np.isnan(arr)]
    if len(valid) == 0:
        return 50
    return np.searchsorted(np.sort(valid), value) / len(valid) * 100


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stock Breakout Research")
    parser.add_argument("--scan", action="store_true", help="Download data + detect breakouts")
    parser.add_argument("--analyze", action="store_true", help="Reverse-engineer patterns")
    parser.add_argument("--top", type=int, default=15, help="Show top N stocks")
    parser.add_argument("--min-move", type=float, default=5.0, help="Minimum breakout move %% (default: 5)")
    parser.add_argument("--period", default="2y", help="History period (default: 2y)")
    args = parser.parse_args()

    if not args.scan and not args.analyze:
        args.scan = True
        args.analyze = True

    if args.scan:
        print(f"{'='*60}")
        print("PHASE 1: Download Universe")
        print(f"{'='*60}")
        download_universe(period=args.period)

        print(f"\n{'='*60}")
        print("PHASE 2: Detect Breakouts")
        print(f"{'='*60}")
        detect_breakouts(min_move_pct=args.min_move)

    if args.analyze:
        print(f"\n{'='*60}")
        print("PHASE 3: Reverse-Engineer Patterns")
        print(f"{'='*60}")
        analyze_breakout_patterns()


if __name__ == "__main__":
    main()
