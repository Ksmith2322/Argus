"""One-shot to close the GLD orphan from gld_pm_long Saturday spam.

Background: gld_pm_long fired 10 entries during the 4/25-4/26 weekend (no weekday
guard at the time). IBKR queued each as Inactive. When markets opened Monday
2026-04-27 13:30 UTC, 2 of the 10 activated, leaving the broker with LONG 54
shares @ avg $431.62. Argus is now in RECOVERY_REQUIRED until this is resolved.

This script:
  1. Cancels any remaining open GLD orders (queued spam from 4/25-4/26)
  2. Closes the 54-share GLD position at market
  3. Reports residual

After running, manually edit forge/logs/gld_pm_long/state.json to set
open_trade=null so the runner stops thinking it owns a paper position.
"""
from __future__ import annotations

import os
import sys
import time
from ib_insync import IB, Stock, MarketOrder

CLIENT_ID = 201  # one-shot, no collision
HOST = "127.0.0.1"
PORT = int(os.getenv("IBKR_PORT", "7497"))
SYMBOL = "GLD"


def main() -> int:
    ib = IB()
    print(f"Connecting to IBKR at {HOST}:{PORT} client_id={CLIENT_ID} ...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        contract = Stock(SYMBOL, "SMART", "USD")
        ib.qualifyContracts(contract)
        print(f"Qualified: {contract}")

        # Step 1: cancel any open GLD orders (the queued spam orders)
        ib.reqAllOpenOrders()
        ib.sleep(1.5)
        all_open = ib.openTrades()
        gld_open = [t for t in all_open if t.contract.symbol == "GLD"]
        print(f"\nFound {len(gld_open)} open GLD orders:")
        for t in gld_open:
            print(f"  orderId={t.order.orderId} action={t.order.action} qty={t.order.totalQuantity} status={t.orderStatus.status}")
            ib.cancelOrder(t.order)
        if gld_open:
            ib.sleep(2)
            print("All GLD open orders canceled.")
        else:
            print("(none)")

        # Step 2: check current position
        positions = [p for p in ib.positions() if p.contract.symbol == "GLD" and p.contract.secType == "STK"]
        if not positions:
            print("\nNo GLD position at broker. Nothing to close.")
            return 0
        for p in positions:
            print(f"\nGLD position: qty={p.position} avg_cost={p.avgCost}")
        target_qty = sum(p.position for p in positions)
        if abs(target_qty) < 1:
            print("Position effectively flat.")
            return 0

        # Step 3: close at market
        side = "SELL" if target_qty > 0 else "BUY"
        size = abs(target_qty)
        print(f"\nPlacing MARKET {side} {size} {SYMBOL} ...")
        order = MarketOrder(side, size)
        trade = ib.placeOrder(contract, order)

        deadline = time.time() + 30
        while time.time() < deadline:
            ib.sleep(0.5)
            if trade.isDone():
                break

        status = trade.orderStatus.status
        filled = trade.orderStatus.filled
        avg_fill = trade.orderStatus.avgFillPrice
        print(f"Order status: {status} filled={filled} avg_fill={avg_fill}")

        ib.sleep(2)
        positions_after = [p for p in ib.positions() if p.contract.symbol == "GLD" and p.contract.secType == "STK"]
        residual = sum(p.position for p in positions_after) if positions_after else 0
        print(f"Residual GLD: {residual}")

        if abs(residual) < 1:
            print("SUCCESS: orphan closed.")
            return 0
        print(f"PARTIAL: residual {residual} — manual intervention needed.")
        return 2

    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    sys.exit(main())
