"""Kraken REST API client for Argus Cascade.

Handles authentication, rate limiting, and error handling.
Public and private endpoint support.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from typing import Any

import requests
from dotenv import load_dotenv


class KrakenAPIError(Exception):
    """Raised when Kraken API returns errors."""


class KrakenClient:
    BASE_URL = "https://api.kraken.com"

    # Kraken pair name mapping
    PAIRS = {
        "BTCUSD": "XBTUSD",
        "ETHUSD": "ETHUSD",
        "XBTUSD": "XBTUSD",
    }

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        load_dotenv()
        self.api_key = api_key or os.getenv("KRAKEN_API_KEY", "")
        self.api_secret = api_secret or os.getenv("KRAKEN_API_SECRET", "")
        self._last_public_call = 0.0
        self._last_private_call = 0.0

    # ── auth ────────────────────────────────────────────────
    def _sign(self, uri_path: str, data: dict[str, Any]) -> str:
        post_data = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + post_data).encode()
        message = uri_path.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    # ── rate limiting ───────────────────────────────────────
    def _throttle_public(self) -> None:
        elapsed = time.monotonic() - self._last_public_call
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        self._last_public_call = time.monotonic()

    def _throttle_private(self) -> None:
        elapsed = time.monotonic() - self._last_private_call
        if elapsed < 2.0:
            time.sleep(2.0 - elapsed)
        self._last_private_call = time.monotonic()

    # ── request helpers ─────────────────────────────────────
    def _public(self, endpoint: str, params: dict | None = None) -> dict:
        self._throttle_public()
        url = f"{self.BASE_URL}/0/public/{endpoint}"
        r = requests.get(url, params=params or {}, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise KrakenAPIError(f"{endpoint}: {body['error']}")
        return body["result"]

    def _private(self, endpoint: str, data: dict | None = None) -> dict:
        self._throttle_private()
        uri_path = f"/0/private/{endpoint}"
        url = f"{self.BASE_URL}{uri_path}"
        data = data or {}
        data["nonce"] = str(int(time.time() * 1000))
        headers = {
            "API-Key": self.api_key,
            "API-Sign": self._sign(uri_path, data),
        }
        r = requests.post(url, headers=headers, data=data, timeout=15)
        r.raise_for_status()
        body = r.json()
        if body.get("error"):
            raise KrakenAPIError(f"{endpoint}: {body['error']}")
        return body["result"]

    # ── public endpoints ────────────────────────────────────
    def get_system_status(self) -> dict:
        return self._public("SystemStatus")

    def get_ticker(self, pair: str = "XBTUSD") -> dict:
        return self._public("Ticker", {"pair": pair})

    def get_trades(
        self,
        pair: str = "XBTUSD",
        since: str | None = None,
        count: int = 1000,
    ) -> tuple[list, str]:
        """Return (trades_list, last_cursor).

        Each trade: [price, volume, time, buy/sell, market/limit, misc, trade_id]
        """
        params: dict[str, Any] = {"pair": pair, "count": count}
        if since is not None:
            params["since"] = since
        result = self._public("Trades", params)
        # Result has pair key (e.g. XXBTZUSD) + 'last'
        last = result.pop("last", "")
        trades = list(result.values())[0] if result else []
        return trades, str(last)

    def get_ohlc(
        self,
        pair: str = "XBTUSD",
        interval: int = 1,
        since: int | None = None,
    ) -> list:
        params: dict[str, Any] = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        result = self._public("OHLC", params)
        result.pop("last", None)
        return list(result.values())[0] if result else []

    def get_asset_pairs(self, pair: str | None = None) -> dict:
        params = {"pair": pair} if pair else {}
        return self._public("AssetPairs", params)

    # ── private endpoints ───────────────────────────────────
    def get_balance(self) -> dict:
        return self._private("Balance")

    def get_open_orders(self) -> dict:
        return self._private("OpenOrders")

    def get_trade_history(self, start: int | None = None) -> dict:
        data: dict[str, Any] = {}
        if start is not None:
            data["start"] = start
        return self._private("TradesHistory", data)