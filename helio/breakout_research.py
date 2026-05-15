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


# Sub-$10 universe — Phase 1 research focus (2026-05-14).
# Curated list of names that frequently trade under $10 with meaningful
# volume. Some of these may currently trade above $10 — that's fine; the
# scanner filters by price-at-breakout, so we only learn from setups that
# fired while the stock was actually under our threshold. Tickers that have
# delisted will yfinance-fail at download and skip gracefully.
SUB10_UNIVERSE = {
    # Biotech / pharma (high gap potential — FDA, trial data)
    "SAVA": "biotech", "OCGN": "biotech", "ATER": "biotech", "BNGO": "biotech",
    "ATOS": "biotech", "VSTM": "biotech", "SNGX": "biotech", "CETX": "biotech",
    "PRTC": "biotech", "ANNX": "biotech", "ABEO": "biotech", "EYEN": "biotech",
    "ONCT": "biotech", "IMRN": "biotech", "IBRX": "biotech", "OBSV": "biotech",
    "MNMD": "biotech", "ETON": "biotech", "GRTS": "biotech", "JAGX": "biotech",
    "INVO": "biotech", "KZIA": "biotech", "RGC": "biotech",
    # Mining / resources / uranium (commodity-driven breakouts)
    "DNN": "mining_uranium", "UUUU": "mining_uranium", "NXE": "mining_uranium",
    "USAS": "mining", "AG": "mining_silver", "EXK": "mining_silver",
    "GORO": "mining_gold", "NAK": "mining", "BORR": "energy_drilling",
    # Cannabis / CPG (sector-rotation driven)
    "SNDL": "cannabis", "TLRY": "cannabis", "ACB": "cannabis", "AGFY": "cannabis",
    "CRON": "cannabis", "VFF": "cannabis", "OGI": "cannabis",
    # EV / clean energy (low-priced) — momentum often catalyst-driven
    "FCEL": "energy_fuel_cell", "BLNK": "ev_charging", "MULN": "ev",
    "GOEV": "ev", "RIDE": "ev", "FFIE": "ev", "ASTS": "satellite",
    "PLUG": "energy_fuel_cell",
    # China small caps (sometimes sub-$10)
    "VIPS": "china_ecomm", "HUYA": "china_streaming", "DOYU": "china_streaming",
    "GOTU": "china_edtech", "IQ": "china_streaming", "BILI": "china_media",
    # Crypto proxies (when below $10)
    "CAN": "crypto_proxy", "BTBT": "crypto_proxy", "BTCS": "crypto_proxy",
    "MIGI": "crypto_proxy", "HIVE": "crypto_proxy", "BITF": "crypto_proxy",
    # Meme / momentum (low-priced runners)
    "PROG": "meme", "BBIG": "meme", "MMAT": "meme", "GNUS": "meme",
    "FAMI": "meme", "NEGG": "meme", "MTVR": "meme", "GREE": "meme",
    "ANY": "meme", "IDEX": "meme", "TRKA": "meme", "GFAI": "meme",
    # SPACs / recent IPOs (low float, big moves)
    "AHRN": "spac", "CCIV": "spac", "SPCE": "space",
    # Telecom / industrials (low-priced large caps)
    "NOK": "telecom", "BB": "tech_legacy",
    # Healthcare / diagnostics
    "TKAT": "med_diag", "OPGN": "med_diag",
    # Cyclicals / value names that occasionally sub-$10
    "F": "auto", "T": "telecom_mega",
    # ETFs that can break out as group proxies
    "SOXS": "etf_inverse_semi", "TZA": "etf_inverse_smallcap",
}


# Available universe selectors for the --universe CLI flag.
UNIVERSES = {
    "mainline": UNIVERSE,
    "sub10": SUB10_UNIVERSE,
    "all": {**UNIVERSE, **SUB10_UNIVERSE},
}


# ── Phase 1: Download Historical Data ────────────────────────────

def download_universe_batch(symbols: list[str], period: str = "2y",
                              batch_size: int = 100, skip_fresh_hours: float = 24.0) -> tuple[int, list[str]]:
    """Batch-download many tickers via yfinance (50-100 per call). Much faster
    than one-at-a-time for big universes. Skips files updated within
    skip_fresh_hours. Returns (n_success, failed_tickers)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    pending = []
    n_skipped = 0
    for sym in symbols:
        out_path = DATA_DIR / f"{sym}_daily.csv"
        if out_path.exists():
            age_h = (time.time() - out_path.stat().st_mtime) / 3600
            if age_h < skip_fresh_hours:
                n_skipped += 1
                continue
        pending.append(sym)
    print(f"  {n_skipped} fresh files skipped, {len(pending)} to download")
    if not pending:
        return n_skipped, []

    n_success = n_skipped
    failed: list[str] = []
    n_batches = (len(pending) + batch_size - 1) // batch_size
    for bi in range(n_batches):
        batch = pending[bi * batch_size : (bi + 1) * batch_size]
        try:
            df = yf.download(batch, period=period, interval="1d", progress=False,
                             auto_adjust=True, threads=True, group_by="ticker")
        except Exception as e:
            print(f"  batch {bi+1}/{n_batches}: download error: {e}")
            failed.extend(batch)
            continue
        for sym in batch:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if sym not in df.columns.get_level_values(0):
                        failed.append(sym)
                        continue
                    sub = df[sym].copy()
                else:
                    sub = df.copy()
                if sub.empty or "Close" not in sub.columns:
                    failed.append(sym)
                    continue
                sub = sub.dropna(subset=["Close"])
                if len(sub) < 60:
                    failed.append(sym)
                    continue
                out_path = DATA_DIR / f"{sym}_daily.csv"
                sub.to_csv(out_path)
                n_success += 1
            except Exception:
                failed.append(sym)
        if (bi + 1) % 5 == 0 or bi == n_batches - 1:
            print(f"  batch {bi+1}/{n_batches} done — {n_success} OK, {len(failed)} failed")
        time.sleep(0.5)
    return n_success, failed


def download_universe(symbols: list[str] | None = None, period: str = "2y",
                       universe_name: str = "mainline"):
    """Download daily OHLCV data for the stock universe.

    universe_name selects which curated dict to download: "mainline" (legacy
    growth/tech), "sub10" (sub-$10 actively-traded names), or "all" (union).
    Explicit `symbols` overrides the universe selection."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if symbols is None:
        u_dict = UNIVERSES.get(universe_name, UNIVERSE)
        tickers = list(u_dict.keys())
    else:
        tickers = symbols

    # Use the batch path when we have many tickers — much faster
    if len(tickers) > 50:
        print(f"Batch-downloading {len(tickers)} stocks ({period} history)...")
        n_ok, failed = download_universe_batch(tickers, period=period)
        print(f"  Done: {n_ok} OK, {len(failed)} failed")
        if failed:
            print(f"  Failed (first 20): {', '.join(failed[:20])}")
        return

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

def detect_breakouts(min_move_pct: float = 5.0, lookback_days: int = 5,
                      max_price_at_breakout: float | None = None,
                      output_suffix: str = ""):
    """Scan all downloaded stocks for breakout events.

    A breakout = price moves >= min_move_pct within lookback_days.
    Returns list of breakout events with pre-breakout context.

    max_price_at_breakout: if set, only record breakouts where the price
    AT the breakout bar was <= this value. Used for the sub-$10 research
    cohort — we only want to learn from setups that fired in the price
    range we'll actually trade.

    output_suffix: appended to the output CSV filename (e.g. "_sub10").
    """
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_breakouts = []

    csv_files = sorted(DATA_DIR.glob("*_daily.csv"))
    price_filter_desc = f", price@bar<=${max_price_at_breakout:.2f}" if max_price_at_breakout else ""
    print(f"\nScanning {len(csv_files)} stocks for breakouts (>={min_move_pct}% in {lookback_days}d{price_filter_desc})...")

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

            # Price filter: only record breakouts that fired in our target price range
            if max_price_at_breakout is not None and close[i] > max_price_at_breakout:
                continue

            # Data-quality floor: yfinance back-adjusts prices through reverse
            # splits, so a stock that did 1-for-50 RS will show pre-split bars
            # at near-zero prices, producing fake 10,000%+ moves. Skip bars
            # where the adjusted close is implausibly low for a tradable stock
            # AND clip absurd move % (real penny moves rarely exceed 200% in
            # a week — anything bigger is almost always a split artifact).
            if close[i] < 0.10:
                continue
            if up_move > 200 or down_move > 95:
                continue

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
        out_path = RESULTS_DIR / f"all_breakouts{output_suffix}.csv"
        df_out = pd.DataFrame(all_breakouts)
        # Sort by move size (descending absolute %) so biggest movers float to top
        df_out["abs_move"] = df_out["move_pct"].abs()
        df_out = df_out.sort_values("abs_move", ascending=False).drop(columns=["abs_move"])
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

    # Look up sector in the union universe so both mainline + sub10 get tagged
    _SECTOR_LOOKUP = {**UNIVERSE, **SUB10_UNIVERSE}
    return {
        "symbol": sym,
        "sector": _SECTOR_LOOKUP.get(sym, "unknown"),
        "date": str(date.date()) if hasattr(date, 'date') else str(date)[:10],
        "direction": direction,
        "move_pct": round(move_pct, 2),
        "price": round(close[idx], 2),
        # Hand-label after review: catalyst type (earnings | FDA | M&A |
        # short_squeeze | sector_rotation | technical | social | unknown).
        # Phase 3 (reverse-engineer the edge) keys off this column.
        "catalyst_label": "",
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

def analyze_breakout_patterns(output_suffix: str = ""):
    """Analyze pre-breakout features to find the setup fingerprint.

    output_suffix matches the suffix used by detect_breakouts (e.g. "_sub10")
    so analyze operates on the cohort the user actually wants."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    bo_path = RESULTS_DIR / f"all_breakouts{output_suffix}.csv"
    if not bo_path.exists():
        print(f"No breakout data at {bo_path}. Run --scan first.")
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
            "sector": {**UNIVERSE, **SUB10_UNIVERSE}.get(sym, "?"),
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
    stats_path = RESULTS_DIR / f"stock_rankings{output_suffix}.csv"
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
    profile_path = RESULTS_DIR / f"breakout_profile{output_suffix}.json"
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
    parser.add_argument("--universe", choices=list(UNIVERSES.keys()), default="mainline",
                        help="Universe to scan: mainline (legacy growth/tech), sub10 (sub-$10 cohort), or all")
    parser.add_argument("--universe-file", default=None,
                        help="Path to a text file with one ticker per line. Overrides --universe. "
                             "Use this for the Nasdaq Trader-derived expanded universe.")
    parser.add_argument("--suffix", default=None,
                        help="Output filename suffix (default: auto from --universe). "
                             "Use a custom suffix for --universe-file runs (e.g. '_sub10_exp').")
    parser.add_argument("--max-price", type=float, default=None,
                        help="Only record breakouts where price-at-breakout-bar <= this value. "
                             "Auto-set to 10.0 when --universe=sub10 or --universe-file is used.")
    args = parser.parse_args()

    if not args.scan and not args.analyze:
        args.scan = True
        args.analyze = True

    # Determine ticker list and suffix
    if args.universe_file:
        from pathlib import Path as _P
        u_path = _P(args.universe_file)
        if not u_path.exists():
            print(f"ERROR: --universe-file not found: {u_path}")
            return
        ticker_list = [t.strip().upper() for t in u_path.read_text().splitlines() if t.strip()]
        universe_label = u_path.stem
        suffix = args.suffix or f"_{universe_label}"
        print(f"Loaded {len(ticker_list)} tickers from {u_path}")
    else:
        ticker_list = None
        universe_label = args.universe
        suffix = args.suffix or (f"_{args.universe}" if args.universe != "mainline" else "")

    # Auto-apply $10 cap when universe is sub10-themed
    max_price = args.max_price
    if max_price is None and (args.universe == "sub10" or args.universe_file):
        max_price = 10.0

    if args.scan:
        print(f"{'='*60}")
        print(f"PHASE 1: Download Universe ({universe_label})")
        print(f"{'='*60}")
        if ticker_list is not None:
            download_universe(symbols=ticker_list, period=args.period)
        else:
            download_universe(period=args.period, universe_name=args.universe)

        print(f"\n{'='*60}")
        print("PHASE 2: Detect Breakouts")
        print(f"{'='*60}")
        detect_breakouts(
            min_move_pct=args.min_move,
            max_price_at_breakout=max_price,
            output_suffix=suffix,
        )

    if args.analyze:
        print(f"\n{'='*60}")
        print("PHASE 3: Reverse-Engineer Patterns")
        print(f"{'='*60}")
        analyze_breakout_patterns(output_suffix=suffix)


if __name__ == "__main__":
    main()
