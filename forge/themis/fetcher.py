"""Themis — QuiverQuant congressional trading fetcher."""

import json
import os
import time
from datetime import datetime, timezone

import requests

from forge.themis.db import (
    DB_PATH,
    get_connection,
    init_db,
    insert_trade,
    parse_amount_range,
)

QUIVER_URL = "https://api.quiverquant.com/beta/live/congresstrading"


def _get_api_key() -> str | None:
    return os.environ.get("QUIVER_API_KEY")


def _normalize_trade(raw: dict) -> dict:
    """Map QuiverQuant fields to our schema."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # QuiverQuant field names (capitalized)
    rep = raw.get("Representative") or raw.get("representative", "")
    ticker = raw.get("Ticker") or raw.get("ticker", "")
    tx_type = raw.get("Transaction") or raw.get("transaction_type", "")
    tx_date = raw.get("TransactionDate") or raw.get("transaction_date", "")
    disc_date = raw.get("ReportDate") or raw.get("DisclosureDate") or raw.get("disclosure_date", "")
    amount = raw.get("Range") or raw.get("Amount") or raw.get("amount_range", "")
    chamber = raw.get("House") or raw.get("chamber", "")
    party = raw.get("Party") or raw.get("party", "")
    desc = raw.get("Description") or raw.get("description", "")
    bioguide = raw.get("BioGuideID") or raw.get("BioguideID") or raw.get("bioguide_id", "")
    excess = raw.get("ExcessReturn") if raw.get("ExcessReturn") not in (None, "", "None") else None
    price_chg = raw.get("PriceChange") if raw.get("PriceChange") not in (None, "", "None") else None
    spy_chg = raw.get("SPYChange") or raw.get("SpyChange")
    if spy_chg in (None, "", "None"):
        spy_chg = None

    # Normalize chamber
    if chamber and chamber.lower() in ("senate", "s"):
        chamber = "Senate"
    elif chamber:
        chamber = "Representatives"

    low, high = parse_amount_range(amount)

    return {
        "representative": rep.strip() if rep else "",
        "bioguide_id": bioguide,
        "ticker": ticker.strip().upper() if ticker else "",
        "transaction_type": tx_type.strip() if tx_type else "",
        "transaction_date": tx_date,
        "disclosure_date": disc_date or tx_date,
        "amount_range": amount,
        "amount_low": low,
        "amount_high": high,
        "chamber": chamber,
        "party": party,
        "description": desc,
        "excess_return": float(excess) if excess is not None else None,
        "price_change": float(price_chg) if price_chg is not None else None,
        "spy_change": float(spy_chg) if spy_chg is not None else None,
        "fetched_at": now,
    }


def fetch_congress_trades() -> list[dict]:
    """Fetch from QuiverQuant API, return normalized trade dicts."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "Themis/1.0 (congressional trading tracker)",
    }
    # Add auth header only if API key is set (free tier works without it)
    api_key = _get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    for attempt in range(2):
        resp = requests.get(QUIVER_URL, headers=headers, timeout=30)
        if resp.status_code == 429:
            print("[themis] Rate limited (429) — waiting 60s...")
            time.sleep(60)
            continue
        if resp.status_code == 401:
            # 2026-04-25: QuiverQuant's public endpoint started 401-ing without a key.
            # Don't crash the loop — return empty list and let the runner sleep till
            # next cycle. Set QUIVER_API_KEY env var to restore live data. Themis is
            # informational-only so missing data is non-fatal.
            print("[themis] 401 Unauthorized — set QUIVER_API_KEY env var to enable")
            return []
        resp.raise_for_status()
        data = resp.json()
        break
    else:
        print("[themis] Failed after retry")
        return []

    trades = []
    for raw in data:
        try:
            t = _normalize_trade(raw)
            if t["ticker"] and t["representative"] and t["transaction_date"]:
                trades.append(t)
        except Exception as e:
            print(f"[themis] Skipping bad record: {e}")
    return trades


def fetch_and_store(conn=None) -> dict:
    """Fetch trades from API and insert into DB. Return counts."""
    close_conn = False
    if conn is None:
        init_db()
        conn = get_connection()
        close_conn = True

    trades = fetch_congress_trades()
    new = 0
    dupes = 0
    for t in trades:
        result = insert_trade(conn, t)
        if result is not None:
            new += 1
        else:
            dupes += 1

    if close_conn:
        conn.close()

    return {"fetched": len(trades), "new": new, "duplicates": dupes}


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        print("[themis] Test mode — checking API connectivity")
        trades = fetch_congress_trades()
        print(f"[themis] Fetched {len(trades)} trades")
        if trades:
            print(f"[themis] Sample: {json.dumps(trades[0], indent=2)}")
    else:
        init_db()
        result = fetch_and_store()
        print(f"[themis] Fetch result: {result}")
