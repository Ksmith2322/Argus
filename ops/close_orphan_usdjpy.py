"""One-shot script to close the USDJPY orphan position discovered 2026-04-26.

Argus reconciliation flagged: local=FLAT, broker=LONG 47260 @ 159.53. The position
exists at IBKR paper but argus's local state thinks it was closed (per 4/23 EXIT
records in canonical_fills.jsonl). Closes the orphan via market sell, restoring
the runner's ability to take new entries.

Run once:
    python -m ops.close_orphan_usdjpy

After successful close, restart argus runner_unified or trigger reconciliation
refresh to clear the entries_blocked=RECON_DRIFT state.
"""
from __future__ import annotations

import os
import sys
import time
from ib_insync import IB, Forex, MarketOrder

CLIENT_ID = 200  # unused by any active runner
HOST = "127.0.0.1"
PORT = int(os.getenv("IBKR_PORT", "7497"))
SYMBOL = "USDJPY"
EXPECTED_QTY = 47260.0


def main() -> int:
    ib = IB()
    print(f"Connecting to IBKR at {HOST}:{PORT} client_id={CLIENT_ID} ...")
    ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15)
    try:
        contract = Forex(SYMBOL)
        ib.qualifyContracts(contract)
        print(f"Qualified contract: {contract}")

        positions = [p for p in ib.positions() if p.contract.symbol == "USD" and p.contract.currency == "JPY"]
        if not positions:
            print(f"No {SYMBOL} position at broker. Nothing to close.")
            return 0

        for p in positions:
            print(f"Found position: qty={p.position} avg_cost={p.avgCost}")

        target_qty = sum(p.position for p in positions)
        if abs(target_qty) < 1:
            print("Position effectively flat. Nothing to close.")
            return 0

        side = "SELL" if target_qty > 0 else "BUY"
        size = abs(target_qty)
        print(f"Placing MARKET {side} {size} {SYMBOL} ...")
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

        if status not in ("Filled", "PreSubmitted", "Submitted"):
            print(f"WARNING: order not filled cleanly — status={status} whyHeld={trade.orderStatus.whyHeld}")

        ib.sleep(2)
        positions_after = [p for p in ib.positions() if p.contract.symbol == "USD" and p.contract.currency == "JPY"]
        residual = sum(p.position for p in positions_after) if positions_after else 0
        print(f"Residual {SYMBOL} position: {residual}")

        if abs(residual) < 1:
            print("SUCCESS: orphan position closed.")
            return 0
        else:
            print(f"PARTIAL: residual {residual} remains. Manual intervention needed.")
            return 2

    finally:
        ib.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    sys.exit(main())
