"""Refresh IBKR Historical Data — pull latest bars for all instruments.

Usage:
    python -m argus_flow.ops.refresh_ibkr_data
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

try:
    from ib_insync import IB, Future, Forex
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "argus_flow" / "data"


def pull_and_merge(ib: IB, contract, label: str, what_to_show: str = "BID", max_chunks: int = 6):
    """Pull 7-day chunks and merge with existing data."""
    out_path = DATA_DIR / f"ibkr_{label}_1m.csv"

    existing = pd.DataFrame()
    if out_path.exists():
        existing = pd.read_csv(out_path)
        existing["ts"] = pd.to_datetime(existing["ts"], utc=True)
        print(f"  Existing: {len(existing):,} bars")

    all_bars = []
    end_dt = ""

    for i in range(max_chunks):
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end_dt, durationStr="7 D",
                barSizeSetting="1 min", whatToShow=what_to_show,
                useRTH=False, formatDate=1, timeout=30,
            )
            if not bars:
                print(f"    Chunk {i+1}: no data returned, stopping")
                break
            print(f"    Chunk {i+1}: {len(bars)} bars ({bars[0].date} to {bars[-1].date})")
            all_bars = list(bars) + all_bars
            end_dt = bars[0].date.strftime("%Y%m%d %H:%M:%S")
            time.sleep(2)
        except Exception as e:
            print(f"    Chunk {i+1}: {e}, stopping")
            break

    if not all_bars:
        print(f"  No new data pulled for {label}")
        return

    new_df = pd.DataFrame([{
        "ts": str(b.date), "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "volume": b.volume,
    } for b in all_bars])
    new_df["ts"] = pd.to_datetime(new_df["ts"], utc=True)

    # Merge and deduplicate
    if not existing.empty:
        combined = pd.concat([existing, new_df]).drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    else:
        combined = new_df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)

    combined.to_csv(out_path, index=False)
    n_days = (combined["ts"].max() - combined["ts"].min()).total_seconds() / 86400
    print(f"  Saved: {len(combined):,} bars ({n_days:.1f} days) → {out_path.name}")


def main():
    print("=" * 60)
    print(f"  IBKR Data Refresh — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print("=" * 60)

    port = int(os.getenv("IBKR_PORT", "4002"))
    ib = IB()
    try:
        ib.connect("127.0.0.1", port, clientId=86, timeout=10)
    except Exception as e:
        print(f"Connection failed: {e}")
        sys.exit(1)

    print(f"  Connected: {ib.managedAccounts()}")

    # EUR/USD
    print(f"\n  --- EUR/USD ---")
    eurusd = Forex("EURUSD")
    ib.qualifyContracts(eurusd)
    pull_and_merge(ib, eurusd, "eurusd", "BID")

    # GBP/USD
    print(f"\n  --- GBP/USD ---")
    gbpusd = Forex("GBPUSD")
    ib.qualifyContracts(gbpusd)
    pull_and_merge(ib, gbpusd, "gbpusd", "BID")

    # MNQ (delayed data)
    print(f"\n  --- MNQ ---")
    ib.reqMarketDataType(3)  # delayed
    mnq = Future(symbol="MNQ", exchange="CME", lastTradeDateOrContractMonth="20260618")
    qualified = ib.qualifyContracts(mnq)
    if qualified:
        pull_and_merge(ib, qualified[0], "MNQ_nasdaq_micro", "TRADES")
    else:
        print("  Could not qualify MNQ contract")

    ib.disconnect()
    print(f"\n  Done.")


if __name__ == "__main__":
    main()