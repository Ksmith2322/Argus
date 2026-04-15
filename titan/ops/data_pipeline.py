#!/usr/bin/env python3
"""titan/ops/data_pipeline.py -- Multi-timeframe data pipeline for Titan.

Downloads and maintains 1H, 4H, and Daily OHLCV data for the Titan universe.
Sources: yfinance (free, covers stocks + ETFs + some futures proxies).

Usage:
    python -m titan.ops.data_pipeline                  # Download all
    python -m titan.ops.data_pipeline --symbols NVDA GDX  # Specific symbols
    python -m titan.ops.data_pipeline --timeframe daily    # Single timeframe
"""
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("ERROR: pip install yfinance")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "titan" / "data"

# ── Titan Universe ────────────────────────────────────────────────
# High-move instruments suitable for swing trading

UNIVERSE = {
    # Tier 1: Gold ecosystem (strong trends, your trendline cascade idea)
    "GLD": {"name": "Gold ETF", "sector": "gold", "tier": 1},
    "GDX": {"name": "Gold Miners", "sector": "gold", "tier": 1},
    "SLV": {"name": "Silver ETF", "sector": "silver", "tier": 1},

    # Tier 1: Oil / Energy
    "USO": {"name": "Oil ETF", "sector": "oil", "tier": 1},
    "XLE": {"name": "Energy Sector", "sector": "energy", "tier": 1},

    # Tier 1: Top breakout stocks (from our research)
    "NVDA": {"name": "NVIDIA", "sector": "semi", "tier": 1},
    "PLTR": {"name": "Palantir", "sector": "tech", "tier": 1},
    "SOFI": {"name": "SoFi", "sector": "fintech", "tier": 1},

    # Tier 2: Strong movers
    "TSLA": {"name": "Tesla", "sector": "ev", "tier": 2},
    "MRNA": {"name": "Moderna", "sector": "biotech", "tier": 2},
    "COIN": {"name": "Coinbase", "sector": "crypto", "tier": 2},
    "AMD": {"name": "AMD", "sector": "semi", "tier": 2},
    "MARA": {"name": "Marathon Digital", "sector": "crypto", "tier": 2},

    # Tier 2: Index proxies (for regime context)
    "SPY": {"name": "S&P 500", "sector": "index", "tier": 2},
    "QQQ": {"name": "NASDAQ 100", "sector": "index", "tier": 2},

    # Tier 2: High-frequency breakout vehicles (from breakout_profile research)
    "HOOD": {"name": "Robinhood", "sector": "fintech", "tier": 2},
    "UPST": {"name": "Upstart", "sector": "fintech", "tier": 2},
    "ARM": {"name": "ARM Holdings", "sector": "semi", "tier": 2},

    # Tier 2: Industrial / Defense (sector diversification, currently favored)
    "CAT": {"name": "Caterpillar", "sector": "industrial", "tier": 2},
    "RTX": {"name": "RTX Corp", "sector": "defense", "tier": 2},

    # Tier 3: Sector ETFs (rotation signals)
    "SMH": {"name": "Semiconductor ETF", "sector": "semi_etf", "tier": 3},
    "XBI": {"name": "Biotech ETF", "sector": "bio_etf", "tier": 3},
    "ARKK": {"name": "ARK Innovation", "sector": "growth_etf", "tier": 3},
}

# Timeframe configs for yfinance
TIMEFRAMES = {
    "1h": {"interval": "1h", "period": "730d", "max_days": 730},
    "4h": {"interval": "1h", "period": "730d", "max_days": 730, "resample": "4h"},
    "daily": {"interval": "1d", "period": "5y", "max_days": 1825},
}


def download_symbol(sym: str, timeframe: str = "daily") -> pd.DataFrame | None:
    """Download OHLCV data for a single symbol and timeframe."""
    tf_config = TIMEFRAMES.get(timeframe)
    if not tf_config:
        print(f"  Unknown timeframe: {timeframe}")
        return None

    try:
        df = yf.download(
            sym,
            period=tf_config["period"],
            interval=tf_config["interval"],
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return None

        # Flatten multi-level columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Resample if needed (e.g., 1h -> 4h)
        if "resample" in tf_config:
            df = df.resample(tf_config["resample"]).agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }).dropna()

        df.index.name = "date"
        return df

    except Exception as e:
        print(f"  {sym} {timeframe}: download failed: {e}")
        return None


def download_universe(symbols: list[str] | None = None,
                      timeframes: list[str] | None = None,
                      force: bool = False):
    """Download all timeframes for all symbols."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    syms = symbols or list(UNIVERSE.keys())
    tfs = timeframes or list(TIMEFRAMES.keys())

    total = len(syms) * len(tfs)
    done = 0
    skipped = 0
    failed = []

    print(f"Downloading {len(syms)} symbols x {len(tfs)} timeframes ({total} total)...")

    for sym in syms:
        for tf in tfs:
            out_path = DATA_DIR / f"{sym}_{tf}.csv"

            # Skip if fresh (< 12h old) unless forced
            if not force and out_path.exists():
                age_h = (time.time() - out_path.stat().st_mtime) / 3600
                if age_h < 12:
                    done += 1
                    skipped += 1
                    continue

            df = download_symbol(sym, tf)
            if df is not None and len(df) >= 30:
                df.to_csv(out_path)
                done += 1
            else:
                failed.append(f"{sym}_{tf}")

            if done % 10 == 0:
                print(f"  {done}/{total}...")
                time.sleep(0.3)

    print(f"  Done: {done} OK ({skipped} cached), {len(failed)} failed")
    if failed:
        print(f"  Failed: {', '.join(failed[:10])}")

    return done, failed


def get_data(sym: str, timeframe: str = "daily") -> pd.DataFrame | None:
    """Load cached data for a symbol. Downloads if missing."""
    path = DATA_DIR / f"{sym}_{timeframe}.csv"
    if not path.exists():
        df = download_symbol(sym, timeframe)
        if df is not None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
            return df
        return None

    try:
        return pd.read_csv(path, index_col=0, parse_dates=True)
    except Exception:
        return None


def universe_summary():
    """Print a summary of available data."""
    print(f"\n{'Symbol':8s} {'Name':20s} {'Tier':>4s} {'Daily':>8s} {'4H':>8s} {'1H':>8s}")
    print("-" * 65)

    for sym, info in sorted(UNIVERSE.items(), key=lambda x: x[1]["tier"]):
        counts = {}
        for tf in TIMEFRAMES:
            path = DATA_DIR / f"{sym}_{tf}.csv"
            if path.exists():
                try:
                    df = pd.read_csv(path, index_col=0)
                    counts[tf] = len(df)
                except Exception:
                    counts[tf] = 0
            else:
                counts[tf] = 0

        print(f"{sym:8s} {info['name']:20s} T{info['tier']}   "
              f"{counts.get('daily', 0):>6d}d  "
              f"{counts.get('4h', 0):>6d}b  "
              f"{counts.get('1h', 0):>6d}b")


def main():
    parser = argparse.ArgumentParser(description="Titan Data Pipeline")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to download")
    parser.add_argument("--timeframe", choices=["1h", "4h", "daily"], help="Single timeframe")
    parser.add_argument("--force", action="store_true", help="Force re-download even if cached")
    parser.add_argument("--summary", action="store_true", help="Show data summary only")
    args = parser.parse_args()

    if args.summary:
        universe_summary()
        return

    tfs = [args.timeframe] if args.timeframe else None
    download_universe(symbols=args.symbols, timeframes=tfs, force=args.force)
    universe_summary()


if __name__ == "__main__":
    main()
