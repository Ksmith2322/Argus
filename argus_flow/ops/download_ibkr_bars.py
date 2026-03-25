"""IBKR Historical Data Downloader — pull bars to CSV for backtesting.

Usage:
    python -m argus_flow.ops.download_ibkr_bars --symbol USDJPY --days 30
    python -m argus_flow.ops.download_ibkr_bars --symbol AUDUSD --days 90
"""
import argparse, csv, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "argus_flow" / "data"

IBKR_HOST = "127.0.0.1"
IBKR_PORT = 7496
CLIENT_ID = 90  # dedicated for data downloads, won't conflict with runners


def download_bars(symbol: str, days: int, bar_size: str = "1 min"):
    from ib_insync import IB, Forex

    ib = IB()
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=CLIENT_ID, timeout=15)

    contract = Forex(symbol)
    ib.qualifyContracts(contract)

    all_bars = []
    # IBKR limits: 1 day of 1-min bars per request
    # Download in 1-day chunks going backwards
    end_dt = datetime.now(timezone.utc)
    for day_offset in range(days):
        dt = end_dt - timedelta(days=day_offset)
        dt_str = dt.strftime("%Y%m%d %H:%M:%S") + " UTC"

        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=dt_str,
                durationStr="1 D",
                barSizeSetting=bar_size,
                whatToShow="MIDPOINT",
                useRTH=False,
                formatDate=2,
            )
            for b in bars:
                all_bars.append({
                    "ts": str(b.date),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                })
            print(f"  Day {day_offset+1}/{days}: {len(bars)} bars")
        except Exception as e:
            print(f"  Day {day_offset+1}/{days}: ERROR {e}")

        time.sleep(1)  # respect rate limits

    ib.disconnect()

    # Sort by timestamp and deduplicate
    all_bars.sort(key=lambda b: b["ts"])
    seen = set()
    unique = []
    for b in all_bars:
        if b["ts"] not in seen:
            seen.add(b["ts"])
            unique.append(b)

    return unique


def main():
    parser = argparse.ArgumentParser(description="Download IBKR historical bars")
    parser.add_argument("--symbol", required=True, help="FX pair (e.g., USDJPY, AUDUSD)")
    parser.add_argument("--days", type=int, default=30, help="Days of history (default: 30)")
    parser.add_argument("--bar-size", default="1 min", help="Bar size (default: '1 min')")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Downloading {args.symbol} {args.days}d {args.bar_size} bars from IBKR...")
    bars = download_bars(args.symbol, args.days, args.bar_size)

    if not bars:
        print("No bars downloaded.")
        return

    out_path = DATA_DIR / f"{args.symbol.lower()}_1m.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(bars)

    print(f"Saved {len(bars)} bars to {out_path}")


if __name__ == "__main__":
    main()
