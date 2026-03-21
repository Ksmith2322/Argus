"""Download historical trades from Kraken REST API.

Paginates using the `since` cursor. Maps to canonical schema:
  ts, price, qty, aggressor, trade_id, order_type

Usage:
    python -m argus_flow.capture.ingest_kraken_trades --pair XBTUSD --days 14
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.adapters.kraken_client import KrakenClient, KrakenAPIError


CANONICAL_HEADER = [
    "ts", "price", "qty", "aggressor", "trade_id", "order_type",
]

CHECKPOINT_SUFFIX = ".checkpoint.json"


def _map_trade(raw: list) -> dict:
    """Map Kraken trade array to canonical dict.

    Kraken schema: [price, volume, time, buy/sell, market/limit, misc, trade_id]
    """
    ts_unix = float(raw[2])
    return {
        "ts": datetime.fromtimestamp(ts_unix, tz=timezone.utc).isoformat(),
        "price": raw[0],
        "qty": raw[1],
        "aggressor": "buy" if raw[3] == "b" else "sell",
        "trade_id": raw[6] if len(raw) > 6 else "",
        "order_type": "market" if raw[4] == "m" else "limit",
    }


def _load_checkpoint(path: Path) -> str | None:
    cp_path = path.with_suffix(path.suffix + CHECKPOINT_SUFFIX)
    if cp_path.exists():
        data = json.loads(cp_path.read_text())
        return data.get("last_cursor")
    return None


def _save_checkpoint(path: Path, cursor: str, count: int) -> None:
    cp_path = path.with_suffix(path.suffix + CHECKPOINT_SUFFIX)
    cp_path.write_text(json.dumps({
        "last_cursor": cursor,
        "total_rows": count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }))


def ingest(
    pair: str = "XBTUSD",
    days: int = 14,
    output: str | None = None,
    resume: bool = True,
) -> Path:
    client = KrakenClient()

    # Default output path
    pair_clean = pair.lower().replace("xbt", "btc")
    if output is None:
        output_path = Path("argus_flow/data") / f"kraken_{pair_clean}_trades.csv"
    else:
        output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Determine start cursor
    start_ts = datetime.now(timezone.utc) - timedelta(days=days)
    start_ns = str(int(start_ts.timestamp() * 1_000_000_000))

    cursor = None
    total_rows = 0
    file_mode = "w"

    if resume:
        saved_cursor = _load_checkpoint(output_path)
        if saved_cursor and output_path.exists():
            cursor = saved_cursor
            # Count existing rows
            with open(output_path, "r") as f:
                total_rows = sum(1 for _ in f) - 1  # minus header
            file_mode = "a"
            print(f"Resuming from checkpoint: cursor={cursor}, {total_rows} rows exist")

    if cursor is None:
        cursor = start_ns

    cutoff_ts = time.time()
    batch_count = 0
    stall_count = 0

    with open(output_path, file_mode, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_HEADER)
        if file_mode == "w":
            writer.writeheader()

        while True:
            try:
                trades, next_cursor = client.get_trades(pair=pair, since=cursor)
            except KrakenAPIError as e:
                print(f"API error: {e} — retrying in 5s")
                time.sleep(5)
                continue
            except Exception as e:
                print(f"Request error: {e} — retrying in 10s")
                time.sleep(10)
                continue

            if not trades:
                print("No more trades returned.")
                break

            # Check if we've caught up to present
            last_trade_ts = float(trades[-1][2])
            if last_trade_ts >= cutoff_ts:
                # Write only trades up to cutoff
                for raw in trades:
                    if float(raw[2]) >= cutoff_ts:
                        break
                    writer.writerow(_map_trade(raw))
                    total_rows += 1
                print(f"Caught up to present. Total rows: {total_rows}")
                break

            # Write batch
            for raw in trades:
                writer.writerow(_map_trade(raw))
                total_rows += 1

            # Detect stall (same cursor returned)
            if next_cursor == cursor:
                stall_count += 1
                if stall_count > 3:
                    print(f"Cursor stalled after {total_rows} rows. Done.")
                    break
            else:
                stall_count = 0

            cursor = next_cursor
            batch_count += 1

            # Progress
            if batch_count % 10 == 0:
                trade_dt = datetime.fromtimestamp(last_trade_ts, tz=timezone.utc)
                print(
                    f"  batch {batch_count}: {total_rows:,} rows | "
                    f"last trade: {trade_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC"
                )

            # Checkpoint every 50 batches
            if batch_count % 50 == 0:
                f.flush()
                _save_checkpoint(output_path, cursor, total_rows)

    # Final checkpoint
    _save_checkpoint(output_path, cursor, total_rows)
    print(f"\nDone. {total_rows:,} trades saved to {output_path}")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Kraken historical trades")
    parser.add_argument("--pair", default="XBTUSD", help="Kraken pair (default: XBTUSD)")
    parser.add_argument("--days", type=int, default=14, help="Days of history (default: 14)")
    parser.add_argument("--output", default=None, help="Output CSV path")
    parser.add_argument("--no-resume", action="store_true", help="Start fresh, ignore checkpoint")
    args = parser.parse_args()

    ingest(
        pair=args.pair,
        days=args.days,
        output=args.output,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()