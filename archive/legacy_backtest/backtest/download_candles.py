#!/usr/bin/env python3
"""
download_candles.py

Download OHLCV candles from Coinbase Exchange (public endpoint) and write a CSV
that your backtest/loader.py can read with BACKTEST_FORMAT="coinbase_exchange".

Endpoint:
  https://api.exchange.coinbase.com/products/{PRODUCT_ID}/candles?granularity=60&start=...&end=...

Notes:
- Coinbase Exchange candles endpoint returns rows like:
    [ time, low, high, open, close, volume ]
  and returns them in DESC time order.
- Max candles per request is limited (commonly 300), so this script paginates.

Env vars (optional):
  PRODUCT_ID     default: ETH-USD
  GRANULARITY    default: 60   (seconds)
  START_ISO      default: ""   (if blank, uses END_ISO - DAYS_BACK)
  END_ISO        default: ""   (if blank, uses now UTC)
  DAYS_BACK      default: 7
  OUT_CSV        default: <trade_bot>/data/eth_usd_1m.csv
  WITH_HEADER    default: 1 (1/0)

UPDATES INCLUDED:
- Robust pagination with NO gaps and NO duplicates (chunk overlap handling)
- Retries with exponential backoff on common rate-limit / transient errors
- Optional `MAX_CANDLES_PER_REQ` env override
- Writes deterministic ASC order with de-dupe by epoch
- Prints summary stats + candle interval sanity checks
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any

import requests

API_BASE = "https://api.exchange.coinbase.com"
DEFAULT_TIMEOUT_S = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> datetime:
    # Accept "2026-01-01T00:00:00Z" or "+00:00"
    s = (s or "").strip()
    if not s:
        raise ValueError("empty iso string")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_out_csv(product_id: str, granularity_s: int) -> Path:
    here = Path(__file__).resolve()
    trade_bot_dir = here.parents[1]  # .../trade_bot/backtest/download_candles.py -> trade_bot
    data_dir = trade_bot_dir / "data"
    tf = f"{granularity_s}s"
    name = f"{product_id.lower().replace('-', '_')}_{tf}.csv"
    if product_id.upper() == "ETH-USD" and granularity_s == 60:
        name = "eth_usd_1m.csv"
    return data_dir / name


def _user_agent() -> str:
    return "NovaTradingBot/1.0 (download_candles)"


def fetch_candles_chunk(
    session: requests.Session,
    *,
    product_id: str,
    granularity_s: int,
    start_dt: datetime,
    end_dt: datetime,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> List[List[float]]:
    """
    Returns raw candle rows: [time, low, high, open, close, volume] (DESC order from API).
    """
    url = f"{API_BASE}/products/{product_id}/candles"
    params = {
        "granularity": int(granularity_s),
        "start": _to_iso(start_dt),
        "end": _to_iso(end_dt),
    }
    r = session.get(url, params=params, timeout=int(timeout_s), headers={"User-Agent": _user_agent()})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        return []
    return data


def _retry_fetch(
    session: requests.Session,
    *,
    product_id: str,
    granularity_s: int,
    start_dt: datetime,
    end_dt: datetime,
    timeout_s: int,
    max_attempts: int = 5,
) -> List[List[float]]:
    """
    Retries on common transient failures (429/5xx, timeouts).
    """
    sleep_s = 0.5
    last_err: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_candles_chunk(
                session,
                product_id=product_id,
                granularity_s=granularity_s,
                start_dt=start_dt,
                end_dt=end_dt,
                timeout_s=timeout_s,
            )
        except requests.HTTPError as e:
            last_err = e
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (429, 500, 502, 503, 504):
                if attempt == max_attempts:
                    raise
                time.sleep(sleep_s)
                sleep_s = min(8.0, sleep_s * 2)
                continue
            raise
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            if attempt == max_attempts:
                raise
            time.sleep(sleep_s)
            sleep_s = min(8.0, sleep_s * 2)

    if last_err:
        raise last_err
    return []


def write_candles_csv(*, out_csv: Path, rows_asc: List[List[float]], with_header: bool) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if with_header:
            w.writerow(["time", "low", "high", "open", "close", "volume"])
        for r in rows_asc:
            if not r or len(r) < 6:
                continue
            w.writerow(r)


def _env_int(name: str, default: int) -> int:
    v = (os.environ.get(name, "") or "").strip()
    if not v:
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)


def _env_str(name: str, default: str) -> str:
    v = (os.environ.get(name, "") or "").strip()
    return v if v else default


def main() -> int:
    product_id = _env_str("PRODUCT_ID", "ETH-USD")
    granularity_s = _env_int("GRANULARITY", 60)

    end_iso = (os.environ.get("END_ISO", "") or "").strip()
    start_iso = (os.environ.get("START_ISO", "") or "").strip()
    days_back = _env_int("DAYS_BACK", 7)

    timeout_s = _env_int("HTTP_TIMEOUT", DEFAULT_TIMEOUT_S)
    max_candles = _env_int("MAX_CANDLES_PER_REQ", 300)  # Coinbase commonly caps at ~300

    if end_iso:
        end_dt = _parse_iso(end_iso)
    else:
        end_dt = _utc_now()

    if start_iso:
        start_dt = _parse_iso(start_iso)
    else:
        start_dt = end_dt - timedelta(days=days_back)

    if start_dt >= end_dt:
        raise SystemExit("START must be < END.")

    out_csv = (os.environ.get("OUT_CSV", "") or "").strip()
    out_path = Path(out_csv) if out_csv else _default_out_csv(product_id, granularity_s)

    with_header = ((os.environ.get("WITH_HEADER", "1") or "1").strip() != "0")

    # Request window sizing:
    # We request up to (max_candles * granularity) seconds of data per call.
    # IMPORTANT: we *do not* add +granularity to cur; instead we advance to the last candle time
    # we actually received to prevent gaps when Coinbase returns fewer than requested.
    chunk_seconds = max_candles * granularity_s
    chunk = timedelta(seconds=chunk_seconds)

    print(f"[DL] product={product_id} granularity={granularity_s}s max_candles={max_candles}")
    print(f"[DL] start={_to_iso(start_dt)}")
    print(f"[DL] end  ={_to_iso(end_dt)}")
    print(f"[DL] out  ={out_path}")
    print("")

    dedup: Dict[int, List[float]] = {}
    total_api_rows = 0

    cur = start_dt

    with requests.Session() as session:
        while cur < end_dt:
            nxt = min(cur + chunk, end_dt)

            rows = _retry_fetch(
                session,
                product_id=product_id,
                granularity_s=granularity_s,
                start_dt=cur,
                end_dt=nxt,
                timeout_s=timeout_s,
                max_attempts=5,
            )

            total_api_rows += len(rows)

            # API returns DESC; normalize to ASC by epoch
            rows_sorted = sorted(rows, key=lambda r: int(r[0])) if rows else []

            # Insert into dedup map by epoch
            last_epoch_in_chunk: Optional[int] = None
            for r in rows_sorted:
                if not r or len(r) < 6:
                    continue
                try:
                    t = int(r[0])
                except Exception:
                    continue
                dedup[t] = r
                last_epoch_in_chunk = t

            print(
                f"[DL] {cur.strftime('%Y-%m-%d %H:%M')} -> {nxt.strftime('%Y-%m-%d %H:%M')} "
                f"api_rows={len(rows)} dedup_total={len(dedup)}"
            )

            # Advance cursor:
            # - If we got candles, jump to (last_epoch + granularity) to avoid duplicates and prevent gaps.
            # - If we got none, move forward by the chunk (still safe).
            if last_epoch_in_chunk is not None:
                cur = datetime.fromtimestamp(last_epoch_in_chunk + granularity_s, tz=timezone.utc)
            else:
                cur = nxt

            # Gentle pacing (public endpoint)
            time.sleep(0.20)

    rows_asc = [dedup[t] for t in sorted(dedup.keys())]

    # Quick sanity check: detect large gaps
    gaps = 0
    if len(rows_asc) >= 2:
        prev = int(rows_asc[0][0])
        for r in rows_asc[1:]:
            t = int(r[0])
            if (t - prev) > (granularity_s * 2):
                gaps += 1
            prev = t

    write_candles_csv(out_csv=out_path, rows_asc=rows_asc, with_header=with_header)

    print("")
    if rows_asc:
        first_t = int(rows_asc[0][0])
        last_t = int(rows_asc[-1][0])
        span_s = last_t - first_t
        print(f"[OK] wrote {len(rows_asc)} candles -> {out_path}")
        print(f"[OK] first_epoch={first_t} last_epoch={last_t} span_days={span_s/86400:.2f}")
        print(f"[OK] api_rows_total={total_api_rows} deduped={len(rows_asc)} gaps_detected={gaps}")
    else:
        print(f"[OK] wrote 0 candles -> {out_path} (check dates/product/granularity)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
