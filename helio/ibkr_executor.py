"""helio/ibkr_executor.py -- Shared IBKR execution helper for Greek family runners.

Provides a clean async interface for Titan, Ares, Hermes, Apollo to place orders.
Uses ib_insync for the heavy lifting.

Key design choices:
  - Connection pooling: each runner gets its own connection (clientId)
  - Stocks use SMART routing
  - Bracket orders (entry + stop + target) when possible
  - Paper mode by default (TWS port 7497 with DUP472829 paper account)
  - All orders logged to logs/<system>/orders.csv

Usage:
    from helio.ibkr_executor import IBKRExecutor

    exec = IBKRExecutor(client_id=60, system="titan")
    exec.connect()
    order_id = exec.submit_bracket(
        symbol="NVDA", direction="long", quantity=10,
        entry_price=180.50, stop_price=170.00, target_price=200.00,
    )
    # ... later
    exec.close_position("NVDA")
    exec.disconnect()
"""
from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from ib_insync import IB, Stock, Order, MarketOrder, LimitOrder, StopOrder, BracketOrder
except ImportError:
    IB = None

REPO = Path(__file__).resolve().parents[1]
_log = logging.getLogger("helio.executor")

# Client ID allocation per system
CLIENT_IDS = {
    "titan": 60,
    "titan_breakout": 61,
    "ares": 70,
    "hermes": 80,
    "apollo": 90,
}


class IBKRExecutor:
    """Synchronous wrapper around ib_insync for Greek family runners."""

    def __init__(
        self,
        client_id: int,
        system: str,
        host: str = None,
        port: int = None,
        paper: bool = True,
        model_equity_usd: float = 10000,
    ):
        if IB is None:
            raise ImportError("ib_insync not installed. Run: pip install ib_insync")

        self.client_id = client_id
        self.system = system
        self.host = host or os.getenv("IBKR_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("IBKR_PORT", "7497"))
        self.paper = paper
        self.model_equity_usd = model_equity_usd
        self._ib = IB()
        self._log = logging.getLogger(f"helio.exec.{system}")

        self.orders_log = REPO / system / "logs" / "orders.csv"
        self.orders_log.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> bool:
        try:
            self._ib.connect(self.host, self.port, clientId=self.client_id, timeout=15)
            self._log.info(f"Connected to IBKR {self.host}:{self.port} clientId={self.client_id}")
            return True
        except Exception as e:
            self._log.error(f"IBKR connection failed: {e}")
            return False

    def disconnect(self):
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
        except Exception:
            pass

    def is_connected(self) -> bool:
        try:
            return self._ib.isConnected()
        except Exception:
            return False

    def get_current_price(self, symbol: str) -> float | None:
        """Get current mid price for a stock."""
        try:
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract, "", False, False)
            self._ib.sleep(2)  # let data populate
            mid = ticker.marketPrice()
            self._ib.cancelMktData(contract)
            return float(mid) if mid and mid > 0 else None
        except Exception as e:
            self._log.warning(f"{symbol} price fetch failed: {e}")
            return None

    def submit_bracket(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        stop_price: float,
        target_price: float,
        order_type: str = "MKT",
    ) -> str | None:
        """Submit a bracket order: entry + stop loss + take profit.

        Returns the parent order ID on success, None on failure.

        Pre-trade guards (mirrors helio/ibkr_execution.py:submit_bracket):
          1. Fleet kill-switch (HALT.flag) — refuses entries when fleet is halted
          2. Cluster exposure cap — refuses entries that would breach
        OCO bracket structure is already correct (uses ib_insync.bracketOrder).
        """
        # Guard 1: fleet halt
        try:
            from helio.ibkr_execution import is_fleet_halted
            halted, halt_reason = is_fleet_halted()
            if halted:
                self._log.warning(f"FLEET_HALTED: refusing {direction} {quantity} {symbol}. Reason: {halt_reason}")
                return None
        except Exception as exc:
            # Fail closed: if the halt check itself raises, we cannot tell
            # whether a fleet-wide halt is in effect. Refuse rather than
            # potentially trade through a HALT.flag. Codex audit 2026-05-18 X5.
            self._log.error(f"fleet halt check failed, REFUSING entry: {exc}")
            return None

        # Guard 2: cluster exposure cap
        try:
            from helio.cluster_exposure import would_breach_cluster_cap
            est_notional = float(quantity) * float(entry_price)
            if est_notional > 0:
                breach = would_breach_cluster_cap(symbol, direction.lower(), est_notional)
                if breach:
                    self._log.warning(
                        f"CLUSTER_CAP_BREACH: {breach} would exceed cap on "
                        f"{direction} {quantity} {symbol} (~${est_notional:,.0f}). Refusing entry."
                    )
                    return None
        except Exception as exc:
            # Fail closed: cluster-cap exception means we cannot evaluate
            # cluster exposure. Refuse rather than risk a cluster blowup.
            # Codex audit 2026-05-18 X5 — guards must fail closed.
            self._log.error(f"cluster cap check failed, REFUSING entry: {exc}")
            return None

        # Guard 3: real-money boundary. No-op for paper connections; fail-closed
        # for real-account connections unless this system is explicitly
        # allowlisted and within the real-money order cap.
        try:
            from helio.real_money import (
                AccountBoundaryViolationError,
                enforce_real_money_boundary,
                real_money_order_tag,
            )
            est_notional = float(quantity) * float(entry_price)
            enforce_real_money_boundary(
                self._ib,
                strategy_label=self.system,
                notional_usd=est_notional,
            )
            real_order_ref = real_money_order_tag(self.system)
        except AccountBoundaryViolationError as exc:
            self._log.error(
                f"REAL_MONEY_BOUNDARY: refusing {direction} {quantity} {symbol} "
                f"system={self.system} — {exc}"
            )
            return None
        except Exception as exc:
            # 2026-05-20 BUGFIX: fail CLOSED on any unexpected error. The
            # previous "allowing paper path" behaviour silently let orders
            # through if enforce_real_money_boundary itself raised (ImportError
            # after refactor, allowlist file unreadable, AccountValidationError
            # typo). If port is ever switched to 7496 with this code path
            # active, that would submit live-account orders unchecked. Codex
            # X5 doctrine applied — guards must fail closed.
            self._log.error(
                f"REAL_MONEY_BOUNDARY: REFUSING {direction} {quantity} {symbol} — "
                f"unexpected error in boundary check: {type(exc).__name__}: {exc}"
            )
            return None

        try:
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)

            action = "BUY" if direction.lower() == "long" else "SELL"
            close_action = "SELL" if action == "BUY" else "BUY"

            # Build bracket
            bracket = self._ib.bracketOrder(
                action=action,
                quantity=quantity,
                limitPrice=entry_price,
                takeProfitPrice=target_price,
                stopLossPrice=stop_price,
            )

            # Set parent order to MKT if requested
            if order_type == "MKT":
                bracket.parent.orderType = "MKT"
                bracket.parent.lmtPrice = 0

            if real_order_ref:
                for o in bracket:
                    o.orderRef = real_order_ref

            # Submit all 3 orders
            for o in bracket:
                self._ib.placeOrder(contract, o)

            self._ib.sleep(1)  # let orders settle

            parent_id = str(bracket.parent.orderId)
            self._log_order(
                action="ENTRY", symbol=symbol, direction=direction,
                quantity=quantity, price=entry_price, stop=stop_price,
                target=target_price, order_id=parent_id,
            )

            self._log.info(
                f"BRACKET: {symbol} {direction.upper()} {quantity} @ ${entry_price:.2f} "
                f"stop=${stop_price:.2f} target=${target_price:.2f} parent={parent_id}"
            )
            return parent_id

        except Exception as e:
            self._log.error(f"Bracket submit failed for {symbol}: {e}")
            return None

    def submit_market(
        self,
        symbol: str,
        direction: str,
        quantity: int,
    ) -> str | None:
        """Submit a simple market order (no stop/target). Used by Ares."""
        try:
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)

            action = "BUY" if direction.lower() == "long" else "SELL"
            order = MarketOrder(action, quantity)

            try:
                from helio.real_money import (
                    AccountBoundaryViolationError,
                    enforce_real_money_boundary,
                    real_money_order_tag,
                )
                price = self.get_current_price(symbol) or 0.0
                enforce_real_money_boundary(
                    self._ib,
                    strategy_label=self.system,
                    notional_usd=(float(quantity) * float(price)) if price else None,
                )
                tag = real_money_order_tag(self.system)
                if tag:
                    order.orderRef = tag
            except AccountBoundaryViolationError as exc:
                self._log.error(
                    f"REAL_MONEY_BOUNDARY: refusing market {direction} {quantity} "
                    f"{symbol} system={self.system} — {exc}"
                )
                return None
            except Exception as exc:
                # 2026-05-20 BUGFIX: fail CLOSED — see submit_bracket above
                self._log.error(
                    f"REAL_MONEY_BOUNDARY: REFUSING market {direction} {quantity} "
                    f"{symbol} — unexpected error: {type(exc).__name__}: {exc}"
                )
                return None

            trade = self._ib.placeOrder(contract, order)
            self._ib.sleep(1)

            order_id = str(trade.order.orderId)
            self._log_order(
                action="MARKET", symbol=symbol, direction=direction,
                quantity=quantity, price=0, stop=0, target=0, order_id=order_id,
            )
            self._log.info(f"MARKET: {symbol} {direction.upper()} {quantity} order={order_id}")
            return order_id
        except Exception as e:
            self._log.error(f"Market order failed for {symbol}: {e}")
            return None

    def close_position(self, symbol: str) -> bool:
        """Close all open positions for a symbol via market order."""
        try:
            positions = self._ib.positions()
            for pos in positions:
                if pos.contract.symbol == symbol and pos.position != 0:
                    contract = pos.contract
                    qty = abs(pos.position)
                    direction = "sell" if pos.position > 0 else "buy"
                    order = MarketOrder("SELL" if pos.position > 0 else "BUY", qty)
                    self._ib.placeOrder(contract, order)
                    self._log_order(
                        action="CLOSE", symbol=symbol, direction=direction,
                        quantity=qty, price=0, stop=0, target=0, order_id="",
                    )
                    self._log.info(f"CLOSE: {symbol} {direction.upper()} {qty}")
                    return True
            self._log.warning(f"CLOSE: no position found for {symbol}")
            return False
        except Exception as e:
            self._log.error(f"Close failed for {symbol}: {e}")
            return False

    def get_positions(self) -> list[dict]:
        """Get current open positions for this client."""
        try:
            positions = self._ib.positions()
            return [
                {
                    "symbol": p.contract.symbol,
                    "quantity": int(p.position),
                    "avg_cost": float(p.avgCost),
                    "side": "long" if p.position > 0 else "short",
                }
                for p in positions if p.position != 0
            ]
        except Exception as e:
            self._log.error(f"get_positions failed: {e}")
            return []

    def get_account_value(self) -> float:
        """Get account net liquidation value."""
        try:
            account = self._ib.accountValues()
            for v in account:
                if v.tag == "NetLiquidation" and v.currency == "USD":
                    return float(v.value)
        except Exception:
            pass
        return self.model_equity_usd

    def _log_order(self, action: str, symbol: str, direction: str, quantity: int,
                   price: float, stop: float, target: float, order_id: str):
        """Append order to system's orders.csv."""
        write_header = not self.orders_log.exists()
        with open(self.orders_log, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["ts", "action", "symbol", "direction", "quantity",
                            "entry_price", "stop_price", "target_price", "order_id"])
            w.writerow([
                datetime.now(timezone.utc).isoformat(),
                action, symbol, direction, quantity,
                round(price, 2), round(stop, 2), round(target, 2), order_id,
            ])
