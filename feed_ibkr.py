"""feed_ibkr.py — IBKR Client Portal Web API price feed for forex.

Forex equivalent of feed_coinbase.py. Provides:
  - IBKRClient: session manager (tickle, auth check, conid lookup)
  - fetch_ibkr_tick_sync(): live bid/ask/last for one instrument
  - fetch_ibkr_candles_sync(): OHLCV history for indicators
  - fetch_tick(): async wrapper returning PriceTick (matches feed_coinbase interface)
  - preload_indicator_history(): seeds indicator engine with historical closes

IBKR Client Portal Gateway must be running locally (https://localhost:5000).
Download: https://www.interactivebrokers.com/en/trading/ib-api.php

Setup:
  1. Download and run IB Gateway (or Client Portal Gateway)
  2. Authenticate once via browser at https://localhost:5000
  3. Set IBKR_GATEWAY_URL, IBKR_ACCOUNT_ID in .env
  4. Gateway keeps session alive via /tickle (Argus calls this via heartbeat)
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
import urllib3

# Suppress InsecureRequestWarning for self-signed IBKR gateway cert
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Reuse the PriceTick dataclass from feed_coinbase so the rest of Argus
# doesn't need to know which feed is active.
from feed_coinbase import PriceTick
from utils import utc_ts, now_unix

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bar / period helpers
# ---------------------------------------------------------------------------

_GRANULARITY_TO_BAR: Dict[int, str] = {
    60: "1min",
    300: "5mins",
    3600: "1hour",
    86400: "1day",
}


def _seconds_to_bar(granularity_seconds: int) -> str:
    """Map granularity seconds to IBKR bar string."""
    bar = _GRANULARITY_TO_BAR.get(int(granularity_seconds))
    if bar:
        return bar
    # Fallback: use the next larger known bucket
    if granularity_seconds < 300:
        return "1min"
    if granularity_seconds < 3600:
        return "5mins"
    if granularity_seconds < 86400:
        return "1hour"
    return "1day"


def _total_seconds_to_period(total_seconds: int) -> str:
    """Map total duration in seconds to IBKR period string."""
    if total_seconds <= 86400:
        return "1d"
    if total_seconds <= 7 * 86400:
        return "1w"
    return "1m"


def _to_dec(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    try:
        d = Decimal(str(x).strip())
        return d if d > 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# IBKRClient
# ---------------------------------------------------------------------------


class IBKRClient:
    """Session manager for the IBKR Client Portal Web API.

    The gateway runs locally at https://localhost:5000 with a self-signed cert.
    Auth is session-based — user authenticates once via browser, then /tickle
    keeps the session alive.
    """

    def __init__(
        self,
        gateway_url: str = "https://localhost:5000",
        account_id: str = "",
        verify_ssl: bool = False,
    ) -> None:
        self._base = gateway_url.rstrip("/")
        self._account_id = account_id
        self._verify = verify_ssl
        self._session = requests.Session()
        self._session.verify = self._verify
        self._session.headers.update({"Content-Type": "application/json"})
        # conid cache: symbol (e.g. "EURUSD") -> conid int
        self._conid_cache: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Internal request helper
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict] = None, timeout: float = 10.0) -> Any:
        url = f"{self._base}{path}"
        r = self._session.get(url, params=params, timeout=timeout, verify=self._verify)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: Optional[Dict] = None, timeout: float = 10.0) -> Any:
        url = f"{self._base}{path}"
        r = self._session.post(url, json=body or {}, timeout=timeout, verify=self._verify)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str, timeout: float = 10.0) -> Any:
        url = f"{self._base}{path}"
        r = self._session.delete(url, timeout=timeout, verify=self._verify)
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def tickle(self) -> bool:
        """POST /tickle — keep session alive. Returns True if session is live."""
        try:
            data = self._post("/v1/api/tickle")
            # Response includes {"iserver": {"authStatus": {"authenticated": true, ...}}}
            iserver = data.get("iserver", {})
            auth = iserver.get("authStatus", {})
            return bool(auth.get("authenticated", False))
        except Exception as exc:
            logger.warning("[IBKR] tickle failed: %s", exc)
            return False

    def is_authenticated(self) -> bool:
        """GET /iserver/auth/status — returns True if session is authenticated."""
        try:
            data = self._get("/v1/api/iserver/auth/status")
            return bool(data.get("authenticated", False))
        except Exception as exc:
            logger.warning("[IBKR] auth/status failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Conid resolution
    # ------------------------------------------------------------------

    def get_conid(self, symbol: str) -> Optional[int]:
        """Resolve a forex symbol like 'EURUSD' to an IBKR conid.

        Results are cached in-process to avoid redundant API calls.
        For 'EURUSD' we search for 'EUR' with secType=CASH and look for
        the USD pair in the results.
        """
        sym = symbol.upper().strip()
        if sym in self._conid_cache:
            return self._conid_cache[sym]

        # Derive base currency: strip trailing "USD" (or other 3-letter quote)
        base = sym[:3] if len(sym) >= 6 else sym

        try:
            results = self._get(
                "/v1/api/iserver/secdef/search",
                params={"symbol": base, "secType": "CASH"},
                timeout=10.0,
            )
            if not isinstance(results, list):
                logger.warning("[IBKR] secdef/search returned non-list for %s: %r", sym, results)
                return None

            for item in results:
                # Each item may have "sections" with currency pairs
                sections = item.get("sections", [])
                for sec in sections:
                    if str(sec.get("secType", "")).upper() != "CASH":
                        continue
                    conid = sec.get("conid") or item.get("conid")
                    if conid is None:
                        continue
                    # Check if the description or currency matches
                    description = str(item.get("description", "")).upper()
                    currency = str(sec.get("currency", "")).upper()
                    # Try to match the full symbol "EURUSD" or pair like "EUR/USD"
                    sym_slash = f"{base}/{sym[3:]}" if len(sym) >= 6 else sym
                    if sym in description or sym_slash in description or currency == sym[3:]:
                        conid_int = int(conid)
                        self._conid_cache[sym] = conid_int
                        logger.debug("[IBKR] Resolved %s -> conid %d", sym, conid_int)
                        return conid_int

            logger.warning("[IBKR] Could not resolve conid for %s from secdef/search", sym)
            return None

        except Exception as exc:
            logger.warning("[IBKR] get_conid(%s) failed: %s", sym, exc)
            return None

    def cache_conid(self, symbol: str, conid: int) -> None:
        """Manually seed the conid cache (e.g. from config)."""
        self._conid_cache[symbol.upper().strip()] = int(conid)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_quote(self, conid: int, timeout: float = 8.0) -> Dict[str, Any]:
        """Fetch live bid/ask/last from IBKR snapshot endpoint.

        The snapshot endpoint requires TWO calls: the first call subscribes
        (may return empty/stale data), and the second call (after ~100ms)
        returns live data. We follow this protocol.

        Returns: {"bid": Decimal, "ask": Decimal, "px": Decimal}
        """
        fields = "31,84,86"  # 31=last, 84=bid, 86=ask
        params = {"conids": str(conid), "fields": fields}

        def _call() -> Dict:
            try:
                return self._get("/v1/api/iserver/marketdata/snapshot", params=params, timeout=timeout)
            except Exception as exc:
                logger.warning("[IBKR] snapshot call failed: %s", exc)
                return {}

        # First call to subscribe
        _call()
        # Wait for live data
        time.sleep(0.15)
        # Second call to retrieve
        raw = _call()

        # Response is a list of dicts when data is available
        if isinstance(raw, list) and raw:
            item = raw[0]
        elif isinstance(raw, dict):
            item = raw
        else:
            item = {}

        bid = _to_dec(item.get("84") or item.get("bid"))
        ask = _to_dec(item.get("86") or item.get("ask"))
        last = _to_dec(item.get("31") or item.get("last_price") or item.get("last"))

        # Best mid price
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / Decimal("2")
        else:
            mid = last or Decimal("0")

        return {"bid": bid, "ask": ask, "px": mid}

    def get_history(
        self,
        conid: int,
        granularity_seconds: int,
        limit: int = 300,
        timeout: float = 15.0,
    ) -> List[Dict[str, Any]]:
        """Fetch OHLCV history bars from IBKR.

        Args:
            conid: IBKR contract ID
            granularity_seconds: bar size in seconds (60, 300, 3600, etc.)
            limit: number of bars requested
            timeout: request timeout in seconds

        Returns:
            List of dicts oldest-first: {"ts", "open", "high", "low", "close", "volume"}
            ts is epoch seconds (int).
        """
        bar = _seconds_to_bar(granularity_seconds)
        total_seconds = limit * granularity_seconds
        period = _total_seconds_to_period(total_seconds)

        try:
            data = self._get(
                "/v1/api/hmds/history",
                params={
                    "conid": str(conid),
                    "period": period,
                    "bar": bar,
                    "outsideRth": "1",
                },
                timeout=timeout,
            )
        except Exception as exc:
            logger.warning("[IBKR] get_history(conid=%d) failed: %s", conid, exc)
            return []

        # IBKR returns {"data": [...], "points": N, ...}
        bars_raw = []
        if isinstance(data, dict):
            bars_raw = data.get("data", [])
        elif isinstance(data, list):
            bars_raw = data

        if not bars_raw:
            return []

        out: List[Dict[str, Any]] = []
        for bar_item in bars_raw:
            try:
                # "t" is epoch milliseconds
                ts_ms = int(bar_item.get("t", 0))
                ts_s = ts_ms // 1000 if ts_ms > 1_000_000_000_000 else ts_ms
                out.append(
                    {
                        "ts": ts_s,
                        "open": Decimal(str(bar_item.get("o", "0"))),
                        "high": Decimal(str(bar_item.get("h", "0"))),
                        "low": Decimal(str(bar_item.get("l", "0"))),
                        "close": Decimal(str(bar_item.get("c", "0"))),
                        "volume": Decimal(str(bar_item.get("v", "0"))),
                    }
                )
            except Exception:
                continue

        # Ensure oldest-first order
        out.sort(key=lambda x: x["ts"])

        # Trim to requested limit
        if len(out) > limit:
            out = out[-limit:]

        return out

    @property
    def account_id(self) -> str:
        return self._account_id


# ---------------------------------------------------------------------------
# Standalone sync helpers
# ---------------------------------------------------------------------------


def fetch_ibkr_tick_sync(
    client: IBKRClient,
    product_id: str,
    timeout: float = 8.0,
) -> Dict[str, Any]:
    """Resolve conid for product_id and fetch live quote.

    Returns {"bid": Decimal|None, "ask": Decimal|None, "px": Decimal}.
    On any error returns {"bid": None, "ask": None, "px": Decimal("0")}.
    """
    conid = client.get_conid(product_id)
    if conid is None:
        logger.warning("[IBKR] fetch_ibkr_tick_sync: cannot resolve conid for %s", product_id)
        return {"bid": None, "ask": None, "px": Decimal("0")}

    try:
        return client.get_quote(conid, timeout=timeout)
    except Exception as exc:
        logger.warning("[IBKR] fetch_ibkr_tick_sync(%s) failed: %s", product_id, exc)
        return {"bid": None, "ask": None, "px": Decimal("0")}


def fetch_ibkr_candles_sync(
    client: IBKRClient,
    product_id: str,
    granularity_seconds: int,
    timeout: float = 15.0,
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """Resolve conid for product_id and fetch OHLCV history.

    Returns list of bar dicts oldest-first (same schema as feed_coinbase candles).
    Returns [] on any error.
    """
    conid = client.get_conid(product_id)
    if conid is None:
        logger.warning("[IBKR] fetch_ibkr_candles_sync: cannot resolve conid for %s", product_id)
        return []

    try:
        return client.get_history(
            conid=conid,
            granularity_seconds=granularity_seconds,
            limit=limit,
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning("[IBKR] fetch_ibkr_candles_sync(%s) failed: %s", product_id, exc)
        return []


# ---------------------------------------------------------------------------
# Async wrappers — matches feed_coinbase interface
# ---------------------------------------------------------------------------


async def fetch_tick(
    client: IBKRClient,
    cfg: Dict[str, Any],
) -> PriceTick:
    """Async tick fetcher returning PriceTick — mirrors feed_coinbase.fetch_tick.

    Note: IBKR Client Portal does not expose order book imbalance data,
    so ob_imbalance is always None regardless of USE_OB_IMBALANCE setting.

    On any failure returns a zero-price tick (never raises).
    """
    timeout = float(cfg.get("HTTP_TIMEOUT", 15))
    product_id = str(cfg.get("PRODUCT_ID", "EURUSD")).strip()

    try:
        quote = await asyncio.to_thread(fetch_ibkr_tick_sync, client, product_id, timeout)
    except Exception as exc:
        logger.warning("[IBKR] fetch_tick failed: %s", exc)
        return PriceTick(
            ts=utc_ts(),
            px=Decimal("0"),
            epoch=now_unix(),
            bid=None,
            ask=None,
            vol_1m=None,
            atr_norm=None,
            ob_imbalance=None,
        )

    bid: Optional[Decimal] = quote.get("bid")
    ask: Optional[Decimal] = quote.get("ask")
    px: Decimal = quote.get("px") or Decimal("0")

    # Best-effort: latest candle volume
    vol_1m: Optional[Decimal] = None
    if cfg.get("CANDLE_SECONDS"):
        try:
            gran = int(cfg["CANDLE_SECONDS"])
            candles = await asyncio.to_thread(
                fetch_ibkr_candles_sync,
                client,
                product_id,
                gran,
                timeout,
                5,  # only need the most recent few
            )
            if candles:
                v = candles[-1].get("volume", Decimal("0"))
                vol_1m = v if v and v > 0 else None
        except Exception:
            vol_1m = None

    return PriceTick(
        ts=utc_ts(),
        px=px,
        epoch=now_unix(),
        bid=bid,
        ask=ask,
        vol_1m=vol_1m,
        atr_norm=None,
        ob_imbalance=None,  # IBKR CP API does not expose OB imbalance
    )


async def preload_indicator_history(
    indicator_engine: Any,
    client: IBKRClient,
    cfg: Dict[str, Any],
    needed_candles: int,
    *,
    candle_seconds: Optional[int] = None,
) -> int:
    """Preload close history into indicator_engine — mirrors feed_coinbase version.

    indicator_engine must expose push_close(price: Decimal).
    Returns the number of candles seeded.
    """
    granularity = candle_seconds or int(cfg.get("CANDLE_SECONDS", 60))
    product_id = str(cfg.get("PRODUCT_ID", "EURUSD")).strip()
    timeout = float(cfg.get("HTTP_TIMEOUT", 15))
    limit = max(int(needed_candles), 300)

    try:
        candles = await asyncio.to_thread(
            fetch_ibkr_candles_sync,
            client,
            product_id,
            granularity,
            timeout,
            limit,
        )
    except Exception as exc:
        logger.warning("[IBKR] preload_indicator_history failed: %s", exc)
        return 0

    if not candles:
        return 0

    seeded = 0
    for c in candles[-int(needed_candles):]:
        try:
            indicator_engine.push_close(Decimal(str(c["close"])))
            seeded += 1
        except Exception:
            continue

    return seeded