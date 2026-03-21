"""Kraken WebSocket trade feed capture.

Subscribes to the trade channel and appends normalized trades to CSV.
For Phase A live data capture.

Usage:
    python -m argus_flow.capture.kraken_ws_feed --pair XBT/USD --output argus_flow/data/kraken_btcusd_live.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import websocket
except ImportError:
    websocket = None  # type: ignore[assignment]


CANONICAL_HEADER = [
    "ts", "price", "qty", "aggressor", "trade_id", "order_type",
]

WS_URL = "wss://ws.kraken.com/v2"


def _on_message(ws, message, writer, file_handle, stats):
    """Handle incoming WebSocket message."""
    data = json.loads(message)

    # Handle subscription confirmations / heartbeats
    if data.get("channel") == "heartbeat":
        return
    if data.get("method") in ("subscribe", "pong"):
        return

    if data.get("channel") != "trade":
        return

    trades = data.get("data", [])
    for t in trades:
        row = {
            "ts": t.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "price": t["price"],
            "qty": t["qty"],
            "aggressor": "buy" if t.get("side") == "buy" else "sell",
            "trade_id": t.get("trade_id", ""),
            "order_type": t.get("ord_type", ""),
        }
        writer.writerow(row)
        stats["count"] += 1

        if stats["count"] % 100 == 0:
            file_handle.flush()
            print(f"  {stats['count']} trades captured | last: {row['ts']}")


def _on_open(ws, pair):
    """Subscribe to trade channel on connect."""
    sub_msg = {
        "method": "subscribe",
        "params": {
            "channel": "trade",
            "symbol": [pair],
        },
    }
    ws.send(json.dumps(sub_msg))
    print(f"Subscribed to {pair} trades")


def _on_error(ws, error):
    print(f"WebSocket error: {error}")


def _on_close(ws, close_status_code, close_msg):
    print(f"WebSocket closed: {close_status_code} {close_msg}")


def capture(pair: str = "XBT/USD", output: str | None = None) -> None:
    if websocket is None:
        print("ERROR: websocket-client not installed. Run: pip install websocket-client")
        sys.exit(1)

    if output is None:
        pair_clean = pair.lower().replace("/", "").replace("xbt", "btc")
        output_path = Path("argus_flow/data") / f"kraken_{pair_clean}_live.csv"
    else:
        output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Append mode if file exists, write header if new
    file_exists = output_path.exists() and output_path.stat().st_size > 0
    file_handle = open(output_path, "a", newline="")
    writer = csv.DictWriter(file_handle, fieldnames=CANONICAL_HEADER)
    if not file_exists:
        writer.writeheader()

    stats = {"count": 0}

    print(f"Starting WebSocket capture for {pair} → {output_path}")
    print("Press Ctrl+C to stop\n")

    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=lambda ws: _on_open(ws, pair),
                on_message=lambda ws, msg: _on_message(ws, msg, writer, file_handle, stats),
                on_error=_on_error,
                on_close=_on_close,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
        except KeyboardInterrupt:
            print(f"\nStopped. {stats['count']} trades captured to {output_path}")
            break
        except Exception as e:
            print(f"Connection lost: {e}. Reconnecting in 5s...")
            time.sleep(5)
        finally:
            file_handle.flush()

    file_handle.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kraken WebSocket trade capture")
    parser.add_argument("--pair", default="XBT/USD", help="Kraken WS pair (default: XBT/USD)")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()
    capture(pair=args.pair, output=args.output)


if __name__ == "__main__":
    main()