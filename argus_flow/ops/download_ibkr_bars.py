"""IBKR Historical Data Downloader — pull bars to CSV for backtesting.

Usage:
    python -m argus_flow.ops.download_ibkr_bars --symbol USDJPY --days 30
    python -m argus_flow.ops.download_ibkr_bars --symbol AUDUSD --days 90
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "argus_flow" / "data"

IBKR_HOST = "127.0.0.1"
# 2026-05-18: default 7497 (paper); was 7496 live — split-brain risk
IBKR_PORT = int(os.getenv("IBKR_PORT", "7497"))
DEFAULT_CLIENT_ID_BASE = 9000


def _load_config(config_path: str | None) -> dict:
    if not config_path:
        return {}
    path = Path(config_path)
    if not path.is_absolute():
        path = (REPO / path).resolve()
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_label(symbol: str, instrument_type: str, label: str | None = None) -> str:
    if label:
        return label
    return symbol.upper() if instrument_type == "future" else symbol.lower()


def _build_contract(symbol: str, instrument_type: str, exchange: str = "", expiry: str = ""):
    from ib_insync import Forex, Future

    if instrument_type == "future":
        if not exchange or not expiry:
            raise ValueError("Futures downloads require both exchange and expiry")
        return Future(symbol=symbol.upper(), exchange=exchange.upper(), lastTradeDateOrContractMonth=expiry)
    return Forex(symbol.upper())


def _default_what_to_show(instrument_type: str) -> str:
    return "TRADES" if instrument_type == "future" else "MIDPOINT"


def _duration_chunk_days(instrument_type: str, bar_size: str) -> int:
    if instrument_type == "future" and bar_size.strip().lower() == "1 min":
        return 7
    return 1


def _deterministic_client_id(symbol: str, instrument_type: str) -> int:
    total = 0
    seed = f"{instrument_type}:{symbol.upper()}"
    for idx, ch in enumerate(seed, start=1):
        total += idx * ord(ch)
    return DEFAULT_CLIENT_ID_BASE + (total % 800)


def download_bars(
    symbol: str,
    days: int,
    bar_size: str = "1 min",
    *,
    instrument_type: str = "forex",
    exchange: str = "",
    expiry: str = "",
    what_to_show: str | None = None,
    client_id: int | None = None,
) -> list[dict]:
    from ib_insync import IB

    instrument_type = str(instrument_type or "forex").strip().lower()
    if instrument_type not in {"forex", "future"}:
        raise ValueError(f"Unsupported instrument_type: {instrument_type}")

    ib = IB()
    resolved_client_id = int(client_id) if client_id is not None else _deterministic_client_id(symbol, instrument_type)
    ib.connect(IBKR_HOST, IBKR_PORT, clientId=resolved_client_id, timeout=15)

    contract = _build_contract(symbol=symbol, instrument_type=instrument_type, exchange=exchange, expiry=expiry)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        ib.disconnect()
        raise RuntimeError(f"Could not qualify {instrument_type} contract for {symbol}")
    contract = qualified[0]

    all_bars: list[dict] = []
    chunk_days = max(_duration_chunk_days(instrument_type, bar_size), 1)
    chunks = max(math.ceil(max(int(days), 1) / chunk_days), 1)
    end_dt = datetime.now(timezone.utc)
    request_what_to_show = what_to_show or _default_what_to_show(instrument_type)

    try:
        for chunk_idx in range(chunks):
            dt = end_dt - timedelta(days=chunk_idx * chunk_days)
            dt_str = dt.strftime("%Y%m%d %H:%M:%S") + " UTC"
            duration_str = f"{chunk_days} D"

            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=dt_str,
                    durationStr=duration_str,
                    barSizeSetting=bar_size,
                    whatToShow=request_what_to_show,
                    useRTH=False,
                    formatDate=2,
                )
                for bar in bars:
                    all_bars.append(
                        {
                            "ts": str(bar.date),
                            "open": bar.open,
                            "high": bar.high,
                            "low": bar.low,
                            "close": bar.close,
                            "volume": bar.volume,
                        }
                    )
                print(f"  Chunk {chunk_idx + 1}/{chunks}: {len(bars)} bars")
            except Exception as exc:
                print(f"  Chunk {chunk_idx + 1}/{chunks}: ERROR {exc}")

            time.sleep(1)
    finally:
        ib.disconnect()

    all_bars.sort(key=lambda row: row["ts"])
    seen: set[str] = set()
    unique: list[dict] = []
    for row in all_bars:
        ts = row["ts"]
        if ts in seen:
            continue
        seen.add(ts)
        unique.append(row)
    return unique


def _write_bars(out_path: Path, bars: list[dict]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "open", "high", "low", "close", "volume"])
        writer.writeheader()
        writer.writerows(bars)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download IBKR historical bars")
    parser.add_argument("--config", help="Managed config path to infer symbol/instrument/exchange/expiry")
    parser.add_argument("--symbol", help="Instrument symbol or FX pair (e.g., USDJPY, M2K)")
    parser.add_argument("--instrument-type", default="", help="Instrument type (forex or future)")
    parser.add_argument("--exchange", default="", help="Exchange for futures (e.g., CME, COMEX, NYMEX)")
    parser.add_argument("--expiry", default="", help="Expiry/contract month for futures")
    parser.add_argument("--label", default="", help="Optional output label used in ibkr_<label>_1m.csv")
    parser.add_argument("--days", type=int, default=30, help="Days of history (default: 30)")
    parser.add_argument("--bar-size", default="1 min", help="Bar size (default: '1 min')")
    parser.add_argument("--what-to-show", default="", help="Override IBKR whatToShow field")
    parser.add_argument("--client-id", type=int, default=0, help="Optional IBKR client ID override")
    args = parser.parse_args()

    config_payload = _load_config(args.config)
    symbol = str(args.symbol or config_payload.get("symbol", "")).strip().upper()
    instrument_type = str(args.instrument_type or config_payload.get("instrument_type", "forex")).strip().lower()
    exchange = str(args.exchange or config_payload.get("exchange", "")).strip()
    expiry = str(args.expiry or config_payload.get("expiry", "")).strip()
    label = _normalized_label(symbol=symbol, instrument_type=instrument_type, label=str(args.label or "").strip() or None)
    what_to_show = str(args.what_to_show or "").strip() or None

    if not symbol:
        raise SystemExit("--symbol or --config is required")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(
        f"Downloading {symbol} {args.days}d {args.bar_size} bars from IBKR "
        f"(type={instrument_type}, label={label})..."
    )
    bars = download_bars(
        symbol,
        args.days,
        args.bar_size,
        instrument_type=instrument_type,
        exchange=exchange,
        expiry=expiry,
        what_to_show=what_to_show,
        client_id=(args.client_id or None),
    )

    if not bars:
        print("No bars downloaded.")
        return

    out_path = DATA_DIR / f"ibkr_{label}_1m.csv"
    _write_bars(out_path, bars)
    print(f"Saved {len(bars)} bars to {out_path}")


if __name__ == "__main__":
    main()
