"""oracle/ops/polymarket_client.py -- Polymarket API client.

Wraps Gamma API and Data API for market scanning and analytics.
No authentication required for read-only operations.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.request import Request, urlopen
from urllib.parse import urlencode

_log = logging.getLogger("oracle.api")

GAMMA_BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

REQUEST_TIMEOUT = 15


def _get(url: str, params: dict | None = None) -> dict | list | None:
    """HTTP GET with error handling."""
    if params:
        url = f"{url}?{urlencode(params)}"
    try:
        req = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Oracle/1.0",
        })
        resp = urlopen(req, timeout=REQUEST_TIMEOUT)
        return json.loads(resp.read())
    except Exception as e:
        _log.warning(f"API error: {url} -> {e}")
        return None


# ── Gamma API (Market Discovery) ─────────────────────────────

def get_markets(
    active: bool = True,
    limit: int = 100,
    order: str = "volume24hr",
    ascending: bool = False,
    tag: str | None = None,
) -> list[dict]:
    """Get markets sorted by volume, liquidity, or recency."""
    params = {
        "active": str(active).lower(),
        "limit": limit,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    if tag:
        params["tag"] = tag
    return _get(f"{GAMMA_BASE}/markets", params) or []


def get_events(
    active: bool = True,
    limit: int = 50,
    order: str = "volume",
    ascending: bool = False,
) -> list[dict]:
    """Get events (groups of related markets)."""
    params = {
        "active": str(active).lower(),
        "limit": limit,
        "order": order,
        "ascending": str(ascending).lower(),
    }
    return _get(f"{GAMMA_BASE}/events", params) or []


def search_markets(query: str, limit: int = 20) -> list[dict]:
    """Search markets by keyword."""
    return _get(f"{GAMMA_BASE}/markets", {"query": query, "limit": limit}) or []


def get_market_by_id(market_id: str) -> dict | None:
    """Get single market by ID."""
    return _get(f"{GAMMA_BASE}/markets/{market_id}")


def get_market_by_slug(slug: str) -> list[dict]:
    """Get market by slug."""
    return _get(f"{GAMMA_BASE}/markets", {"slug": slug}) or []


def get_tags() -> list[dict]:
    """Get available market tags/categories."""
    return _get(f"{GAMMA_BASE}/tags") or []


# ── CLOB API (Price Data — no auth for reads) ────────────────

def get_midpoint(token_id: str) -> float | None:
    """Get current midpoint price for a token."""
    data = _get(f"{CLOB_BASE}/midpoint", {"token_id": token_id})
    if data and "mid" in data:
        return float(data["mid"])
    return None


def get_price_history(token_id: str, interval: str = "1d", fidelity: int = 60) -> list[dict]:
    """Get price history for a token.

    interval: 1d, 1w, 1m, 3m, all
    fidelity: resolution in minutes
    """
    data = _get(f"{CLOB_BASE}/prices-history", {
        "market": token_id,
        "interval": interval,
        "fidelity": fidelity,
    })
    return data.get("history", []) if isinstance(data, dict) else []


def get_orderbook(token_id: str) -> dict | None:
    """Get current orderbook for a token."""
    return _get(f"{CLOB_BASE}/book", {"token_id": token_id})


def get_spread(token_id: str) -> dict | None:
    """Get bid-ask spread."""
    return _get(f"{CLOB_BASE}/spread", {"token_id": token_id})


# ── Derived Analytics ─────────────────────────────────────────

def parse_market(m: dict) -> dict:
    """Parse raw market data into clean analytics format."""
    try:
        prices = json.loads(m.get("outcomePrices", "[]"))
        outcomes = json.loads(m.get("outcomes", "[]"))
    except (json.JSONDecodeError, TypeError):
        prices = []
        outcomes = []

    yes_price = float(prices[0]) if len(prices) > 0 else 0
    no_price = float(prices[1]) if len(prices) > 1 else 0

    vol_24h = m.get("volume24hr", 0) or 0
    vol_1w = m.get("volume1wk", 0) or 0
    vol_total = m.get("volumeNum", 0) or m.get("volume", 0) or 0
    liquidity = m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0

    # Time to resolution
    end_date = m.get("endDate", "")
    days_to_resolution = None
    if end_date:
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            days_to_resolution = (end_dt - datetime.now(timezone.utc)).days
        except Exception:
            pass

    return {
        "id": m.get("id", ""),
        "question": m.get("question", ""),
        "slug": m.get("slug", ""),
        "yes_price": round(yes_price, 4),
        "no_price": round(no_price, 4),
        "implied_prob": round(yes_price * 100, 1),
        "volume_24h": round(vol_24h, 2),
        "volume_1w": round(vol_1w, 2),
        "volume_total": round(vol_total, 2),
        "liquidity": round(float(liquidity), 2),
        "end_date": end_date[:10] if end_date else "",
        "days_to_resolution": days_to_resolution,
        "active": m.get("active", False),
        "closed": m.get("closed", False),
        "outcomes": outcomes,
        "clob_token_ids": json.loads(m.get("clobTokenIds", "[]")) if m.get("clobTokenIds") else [],
        "event_slug": m.get("events", [{}])[0].get("slug", "") if m.get("events") else "",
    }
