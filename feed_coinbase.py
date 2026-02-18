# feed_coinbase.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests

# line above: import requests
from .utils import utc_ts, now_unix


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class PriceTick:
    ts: str
    px: Decimal
    epoch: int

    # Phase 5B liquidity inputs (preferred in live mode)
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None

    # Best-effort 1m volume (from latest Exchange candle)
    vol_1m: Optional[Decimal] = None

    # Optional future field (engine may ignore unless wired)
    atr_norm: Optional[Decimal] = None


# =============================================================================
# HTTP session
# =============================================================================

def make_http() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "NovaTradingBot/1.0"})
    return s


# =============================================================================
# Internal helpers
# =============================================================================

def _to_dec(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        d = Decimal(str(x).strip())
        return d if d > 0 else None
    except Exception:
        return None


def _safe_mid_px(px: Optional[Decimal], bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
    if bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / Decimal("2")
    return px


def _resolve_granularity(cfg: Dict[str, Any], override_seconds: Optional[int] = None) -> int:
    """
    Supports either:
      - override_seconds
      - cfg["CANDLE_SECONDS"]
      - cfg["granularity"] (legacy)
    """
    if override_seconds is not None:
        return int(override_seconds)
    if "CANDLE_SECONDS" in cfg:
        return int(cfg["CANDLE_SECONDS"])
    if "granularity" in cfg:
        return int(cfg["granularity"])
    return 60


def _resolve_ticker_url(cfg: Dict[str, Any]) -> str:
    """
    Ticker URL template priority:
      1) cfg["COINBASE_EXCHANGE_TICKER_URL"]  (recommended)
      2) cfg["COINBASE_TICKER_URL"]          (legacy)
    Must contain "{product_id}" for format(), OR be a fully-qualified URL.
    """
    url = cfg.get("COINBASE_EXCHANGE_TICKER_URL") or cfg.get("COINBASE_TICKER_URL")
    if not url:
        raise KeyError("Missing COINBASE_EXCHANGE_TICKER_URL (or legacy COINBASE_TICKER_URL) in cfg")
    return str(url).strip()


def _resolve_candles_url(cfg: Dict[str, Any]) -> str:
    """
    Candles URL template (Exchange) must contain "{product_id}".
    """
    url = cfg.get("COINBASE_EXCHANGE_CANDLES_URL") or cfg.get("COINBASE_CANDLES_URL")
    if not url:
        raise KeyError("Missing COINBASE_EXCHANGE_CANDLES_URL (or legacy COINBASE_CANDLES_URL) in cfg")
    return str(url).strip()


# =============================================================================
# Spot price (sync)
# =============================================================================

def fetch_coinbase_spot_sync(http: requests.Session, spot_url: str, timeout: float) -> Decimal:
    r = http.get(str(spot_url), timeout=float(timeout))
    r.raise_for_status()
    j = r.json()
    return Decimal(str(j["data"]["amount"]))


# =============================================================================
# Ticker (bid/ask) for liquidity (sync)
# =============================================================================

def fetch_coinbase_ticker_sync(
    http: requests.Session,
    ticker_url_tpl: str,
    product_id: str,
    timeout: float,
) -> Dict[str, Optional[Decimal]]:
    """
    Coinbase Exchange Products ticker endpoint typically returns:
      {
        "trade_id": ...,
        "price": "2339.12",
        "bid": "2339.11",
        "ask": "2339.13",
        ...
      }
    Returns: {"px": Decimal|None, "bid": Decimal|None, "ask": Decimal|None}
    """
    url = str(ticker_url_tpl).strip()
    if "{product_id}" in url:
        url = url.format(product_id=product_id)

    r = http.get(url, timeout=float(timeout))
    r.raise_for_status()

    j = r.json()
    if not isinstance(j, dict):
        j = {}

    px = _to_dec(j.get("price"))
    bid = _to_dec(j.get("bid"))
    ask = _to_dec(j.get("ask"))

    return {"px": px, "bid": bid, "ask": ask}


# =============================================================================
# Candles (sync) — for indicators + volume
# =============================================================================

def fetch_coinbase_candles_sync(
    http: requests.Session,
    exchange_candles_url_tpl: str,
    product_id: str,
    granularity: int,
    timeout: float,
    limit: int = 300,
) -> List[Dict[str, Decimal]]:
    """
    Coinbase Exchange candles endpoint:
      [ time, low, high, open, close, volume ]

    Returns list of dicts oldest->newest:
      { "ts", "low", "high", "open", "close", "volume" }
    """
    url = str(exchange_candles_url_tpl).strip()
    if "{product_id}" in url:
        url = url.format(product_id=product_id)

    params = {"granularity": int(granularity)}
    r = http.get(url, params=params, timeout=float(timeout))
    r.raise_for_status()

    data = r.json()
    if not isinstance(data, list) or not data:
        return []

    data = data[: int(limit)]
    data.reverse()  # oldest first

    out: List[Dict[str, Decimal]] = []
    for row in data:
        # row is [time, low, high, open, close, volume]
        try:
            ts_i = int(row[0])
            out.append(
                {
                    "ts": Decimal(str(ts_i)),
                    "low": Decimal(str(row[1])),
                    "high": Decimal(str(row[2])),
                    "open": Decimal(str(row[3])),
                    "close": Decimal(str(row[4])),
                    "volume": Decimal(str(row[5])),
                }
            )
        except Exception:
            continue

    return out


# =============================================================================
# Back-compat: spot price (async) — NOW includes bid/ask/vol_1m best-effort
# =============================================================================

async def fetch_spot_price(http: requests.Session, cfg: Dict[str, Any]) -> PriceTick:
    """
    Back-compat entrypoint used by older code paths.
    Returns a Phase-5B-ready tick (best effort).
    """
    return await fetch_tick(
        http,
        cfg,
        include_candle_volume=True,
        candle_seconds_for_volume=int(cfg.get("CANDLE_SECONDS", 60)),
    )


# =============================================================================
# Live tick (async) — includes bid/ask + best-effort 1m volume
# =============================================================================

async def fetch_tick(
    http: requests.Session,
    cfg: Dict[str, Any],
    *,
    include_candle_volume: bool = True,
    candle_seconds_for_volume: int = 60,
) -> PriceTick:
    """
    Phase 5B-ready live tick:
      - bid/ask from Exchange ticker
      - px = mid(bid,ask) if available else ticker.price else spot
      - vol_1m = latest candle volume (best-effort) if include_candle_volume=True

    Config used (expected):
      - PRODUCT_ID
      - HTTP_TIMEOUT
      - COINBASE_EXCHANGE_TICKER_URL (template with {product_id}) or full url
      - COINBASE_EXCHANGE_CANDLES_URL (template with {product_id}) if include_candle_volume
      - COINBASE_SPOT_URL (fallback if ticker fails)
    """
    timeout = float(cfg.get("HTTP_TIMEOUT", 15))
    product_id = str(cfg.get("PRODUCT_ID", "")).strip() or "ETH-USD"

    px: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None

    # 1) Preferred: Exchange ticker (bid/ask)
    try:
        ticker_url_tpl = _resolve_ticker_url(cfg)
        t = await asyncio.to_thread(fetch_coinbase_ticker_sync, http, ticker_url_tpl, product_id, timeout)
        px = t.get("px")
        bid = t.get("bid")
        ask = t.get("ask")
    except Exception:
        # 2) Fallback: spot price
        try:
            px = await asyncio.to_thread(fetch_coinbase_spot_sync, http, cfg["COINBASE_SPOT_URL"], timeout)
        except Exception:
            px = Decimal("0")

    px_final = _safe_mid_px(px, bid, ask)
    if px_final is None:
        px_final = Decimal("0")

    # 3) Best-effort: latest candle volume for requested TF (typically 60s)
    vol_1m: Optional[Decimal] = None
    if include_candle_volume:
        try:
            candles_url_tpl = _resolve_candles_url(cfg)
            gran = _resolve_granularity(cfg, override_seconds=int(candle_seconds_for_volume))
            candles = await asyncio.to_thread(
                fetch_coinbase_candles_sync,
                http,
                candles_url_tpl,
                product_id,
                gran,
                timeout,
                5,  # only need newest
            )
            if candles:
                v = candles[-1].get("volume", Decimal("0"))
                vol_1m = v if v > 0 else None
        except Exception:
            vol_1m = None

    return PriceTick(
        ts=utc_ts(),
        px=Decimal(str(px_final)),
        epoch=now_unix(),
        bid=bid,
        ask=ask,
        vol_1m=vol_1m,
        atr_norm=None,
    )


# =============================================================================
# Indicator preload
# =============================================================================

async def preload_indicator_history(
    indicator_engine,
    http: requests.Session,
    cfg: Dict[str, Any],
    needed_candles: int,
    *,
    candle_seconds: Optional[int] = None,
) -> int:
    """
    Preloads close history into indicator_engine.

    indicator_engine must expose:
      - push_close(price: Decimal)

    Notes:
    - This seeds only close history (indicators).
    - If you want to seed liquidity baselines too, do it in the caller by
      iterating the same candle list and feeding volume into liquidity engine.
    """
    # --------- LINE ABOVE: async def preload_indicator_history(...)

    granularity = _resolve_granularity(cfg, override_seconds=candle_seconds)
    candles_url_tpl = _resolve_candles_url(cfg)

    candles = await asyncio.to_thread(
        fetch_coinbase_candles_sync,
        http,
        candles_url_tpl,
        str(cfg.get("PRODUCT_ID", "ETH-USD")),
        granularity,
        float(cfg.get("HTTP_TIMEOUT", 15)),
        max(int(needed_candles), 300),
    )

    if not candles:
        return 0

    seeded = 0
    for c in candles[-int(needed_candles) :]:
        try:
            indicator_engine.push_close(Decimal(str(c["close"])))
            seeded += 1
        except Exception:
            continue

    return seeded
