"""feed_ws.py — Real-time Coinbase Exchange WebSocket L2 order book feed.

Maintains a live order book snapshot and exposes order-book imbalance:

    imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)

Range: [-1.0, +1.0]
  +1.0 = all bid pressure (bullish microstructure)
  -1.0 = all ask pressure (bearish microstructure)
   0.0 = balanced book

Usage in runner_live.py:
    feed = CoinbaseWsFeed("ETH-USD")
    asyncio.create_task(feed.run())        # background — auto-reconnects
    ...
    imbalance = feed.get_imbalance(depth=10)   # None until first snapshot
    bid = feed.get_best_bid()
    ask = feed.get_best_ask()

Coinbase Exchange WebSocket spec:
  wss://ws-feed.exchange.coinbase.com
  channel: level2
  snapshot msg: {"type":"snapshot","bids":[["2330.12","0.5"],...],"asks":[...]}
  update msg:   {"type":"l2update","changes":[["buy","2330.12","0.0"],...]}
  size "0" = remove that price level from the book
"""
from __future__ import annotations

import asyncio
import json
import time
import threading
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

import websockets

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
_RECONNECT_DELAY_S = 5.0
_WS_PING_INTERVAL = 20
_WS_PING_TIMEOUT = 20
_WS_CLOSE_TIMEOUT = 10
_WS_MAX_MSG_SIZE = 10 * 1024 * 1024  # 10 MB — initial snapshots can be large


# ---------------------------------------------------------------------------
# Internal: thread-safe order book
# ---------------------------------------------------------------------------

class _OrderBook:
    """Thread-safe in-memory L2 order book (bids + asks).

    All mutations come from the async WS coroutine; all reads can come from
    any thread (the engine runs in the same event loop but read-side
    locking is cheap and correct).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bids: Dict[Decimal, Decimal] = {}  # price -> size
        self._asks: Dict[Decimal, Decimal] = {}  # price -> size
        self._ready = False

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def apply_snapshot(
        self,
        bids: List[List[str]],
        asks: List[List[str]],
    ) -> None:
        new_bids: Dict[Decimal, Decimal] = {}
        new_asks: Dict[Decimal, Decimal] = {}
        for entry in bids:
            try:
                sz = Decimal(entry[1])
                if sz > 0:
                    new_bids[Decimal(entry[0])] = sz
            except (InvalidOperation, ValueError, IndexError):
                pass
        for entry in asks:
            try:
                sz = Decimal(entry[1])
                if sz > 0:
                    new_asks[Decimal(entry[0])] = sz
            except (InvalidOperation, ValueError, IndexError):
                pass
        with self._lock:
            self._bids = new_bids
            self._asks = new_asks
            self._ready = True

    def apply_updates(self, changes: List[List[str]]) -> None:
        with self._lock:
            for item in changes:
                if len(item) < 3:
                    continue
                side, price_str, size_str = item[0], item[1], item[2]
                try:
                    px = Decimal(price_str)
                    sz = Decimal(size_str)
                except (InvalidOperation, ValueError):
                    continue
                book = self._bids if side == "buy" else self._asks
                if sz <= 0:
                    book.pop(px, None)
                else:
                    book[px] = sz

    # ------------------------------------------------------------------
    # Reads (thread-safe)
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        return self._ready

    def get_imbalance(self, depth: int = 10) -> Optional[float]:
        """(bid_vol - ask_vol) / total_vol over top `depth` price levels."""
        with self._lock:
            if not self._ready:
                return None
            top_bids = sorted(self._bids.items(), reverse=True)[:depth]
            top_asks = sorted(self._asks.items())[:depth]
            bid_vol = sum(sz for _, sz in top_bids)
            ask_vol = sum(sz for _, sz in top_asks)
            total = bid_vol + ask_vol
            if total <= 0:
                return None
            return float((bid_vol - ask_vol) / total)

    def get_best_bid(self) -> Optional[Decimal]:
        with self._lock:
            return max(self._bids.keys()) if self._bids else None

    def get_best_ask(self) -> Optional[Decimal]:
        with self._lock:
            return min(self._asks.keys()) if self._asks else None

    def get_spread_bps(self) -> Optional[float]:
        """Spread in basis points from the live book (best ask - best bid) / mid."""
        with self._lock:
            if not self._bids or not self._asks:
                return None
            best_bid = max(self._bids.keys())
            best_ask = min(self._asks.keys())
            if best_bid <= 0 or best_ask <= best_bid:
                return None
            mid = (best_bid + best_ask) / 2
            if mid <= 0:
                return None
            return float((best_ask - best_bid) / mid * 10000)

    def get_book_depth(self) -> int:
        """Total number of price levels (bids + asks) currently in the book."""
        with self._lock:
            return len(self._bids) + len(self._asks)


# ---------------------------------------------------------------------------
# Public: WS feed manager
# ---------------------------------------------------------------------------

class CoinbaseWsFeed:
    """Real-time Coinbase Exchange WebSocket Level-2 feed for a single product.

    Designed to run as a background asyncio task:
        feed = CoinbaseWsFeed("ETH-USD")
        asyncio.create_task(feed.run())

    All public getter methods are thread-safe and return None until the first
    L2 snapshot arrives (usually within 1–2 seconds of connecting).

    Graceful degradation: if the feed is down or not yet ready, every getter
    returns None — callers must treat None as "data unavailable" and skip the
    signal rather than error.
    """

    def __init__(self, product_id: str = "ETH-USD") -> None:
        self.product_id = product_id
        self._book = _OrderBook()
        self._connected = False
        # Sub-minute candle aggregation: stores (epoch, price, volume) tuples
        self._candle_lock = threading.Lock()
        self._candle_ticks: List[tuple] = []  # (epoch_s, Decimal price, Decimal volume)
        self._last_candle: Optional[dict] = None
        self._max_tick_history = 600  # keep ~10 min of ticks

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Background coroutine — connect, maintain book, auto-reconnect forever."""
        while True:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                self._connected = False
                return
            except Exception:
                self._connected = False
            # Brief pause before reconnecting
            await asyncio.sleep(_RECONNECT_DELAY_S)

    async def _connect_once(self) -> None:
        sub_msg = json.dumps({
            "type": "subscribe",
            "product_ids": [self.product_id],
            "channels": ["level2", "ticker"],
        })
        async with websockets.connect(
            COINBASE_WS_URL,
            ping_interval=_WS_PING_INTERVAL,
            ping_timeout=_WS_PING_TIMEOUT,
            close_timeout=_WS_CLOSE_TIMEOUT,
            max_size=_WS_MAX_MSG_SIZE,
        ) as ws:
            await ws.send(sub_msg)
            self._connected = True

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue

                mtype = msg.get("type", "")
                if mtype == "snapshot":
                    self._book.apply_snapshot(
                        msg.get("bids", []),
                        msg.get("asks", []),
                    )
                elif mtype == "l2update":
                    self._book.apply_updates(msg.get("changes", []))
                elif mtype == "ticker":
                    # Aggregate trade ticks for sub-minute candles
                    try:
                        px_str = msg.get("price", "")
                        vol_str = msg.get("last_size", "0")
                        if px_str:
                            px = Decimal(px_str)
                            vol = Decimal(vol_str) if vol_str else Decimal("0")
                            epoch = int(time.time())
                            with self._candle_lock:
                                self._candle_ticks.append((epoch, px, vol))
                                # Trim old ticks beyond max history
                                if len(self._candle_ticks) > self._max_tick_history:
                                    self._candle_ticks = self._candle_ticks[-self._max_tick_history:]
                    except (InvalidOperation, ValueError):
                        pass
                # ignore: subscriptions, heartbeat, error, etc.

        self._connected = False

    # ------------------------------------------------------------------
    # Public getters
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """True after the first L2 snapshot has been received."""
        return self._book.is_ready()

    def is_connected(self) -> bool:
        """True while the WebSocket connection is active."""
        return self._connected

    def get_imbalance(self, depth: int = 10) -> Optional[float]:
        """Order book imbalance over top `depth` price levels.

        Returns (bid_volume - ask_volume) / total_volume.
        Range: [-1.0, +1.0]. Positive = bid-heavy (bullish pressure).
        Returns None if book not yet initialised.
        """
        return self._book.get_imbalance(depth=depth)

    def get_best_bid(self) -> Optional[Decimal]:
        """Best bid price from the live book. None if not ready."""
        return self._book.get_best_bid()

    def get_best_ask(self) -> Optional[Decimal]:
        """Best ask price from the live book. None if not ready."""
        return self._book.get_best_ask()

    def get_spread_bps(self) -> Optional[float]:
        """Spread in basis points from the live order book. None if not ready."""
        return self._book.get_spread_bps()

    def get_book_depth(self) -> int:
        """Total number of price levels in the book (for diagnostics)."""
        return self._book.get_book_depth()

    # ------------------------------------------------------------------
    # Sub-minute candle aggregation
    # ------------------------------------------------------------------

    def get_candle(self, seconds: int = 15) -> Optional[dict]:
        """Get the current aggregated candle for the given interval.

        Returns dict with keys: open, high, low, close, volume, ts, n_ticks.
        Returns None if no ticks received yet.
        """
        with self._candle_lock:
            if not self._candle_ticks:
                return None
            now = int(time.time())
            bucket_start = now - (now % seconds)
            # Filter ticks in current bucket
            bucket_ticks = [
                t for t in self._candle_ticks
                if t[0] >= bucket_start
            ]
            if not bucket_ticks:
                # Return last completed candle if available
                return self._last_candle

            prices = [t[1] for t in bucket_ticks]
            volumes = [t[2] for t in bucket_ticks]
            candle = {
                "open": prices[0],
                "high": max(prices),
                "low": min(prices),
                "close": prices[-1],
                "volume": sum(volumes),
                "ts": bucket_start,
                "n_ticks": len(bucket_ticks),
            }
            return candle

    def get_last_price(self) -> Optional[Decimal]:
        """Latest trade price from the ticker channel."""
        with self._candle_lock:
            if self._candle_ticks:
                return self._candle_ticks[-1][1]
        return None

    def status(self) -> str:
        """Human-readable status string for logging/diagnostics."""
        if not self._connected:
            return "DISCONNECTED"
        if not self.is_ready():
            return "CONNECTED_NO_SNAPSHOT"
        imb = self.get_imbalance()
        depth = self.get_book_depth()
        imb_str = f"{imb:+.3f}" if imb is not None else "N/A"
        ticks = len(self._candle_ticks) if hasattr(self, '_candle_ticks') else 0
        return f"READY depth={depth} imbalance={imb_str} ticks={ticks}"