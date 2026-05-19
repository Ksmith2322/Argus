"""Emergency Close CLI — manually close specific or all positions via IBKR.

Connects directly to IB Gateway/TWS, queries open positions, and submits
market orders to close them. Independent of the runner process.

Usage:
    python -m argus_flow.ops.emergency_close                    # close ALL positions
    python -m argus_flow.ops.emergency_close --symbol EURUSD    # close specific symbol
    python -m argus_flow.ops.emergency_close --dry-run          # show what would close
    python -m argus_flow.ops.emergency_close --cancel-orders    # cancel all open orders first
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

try:
    from ib_insync import IB, MarketOrder
except ImportError:
    print("ERROR: ib-insync not installed. Run: pip install ib-insync")
    sys.exit(1)


DEFAULT_HOST = "127.0.0.1"
# 2026-05-18: default 7497 (paper); was 7496 live — split-brain risk
DEFAULT_PORT = 7497
# Use high client ID to avoid conflicting with runner (which uses 1 or 2)
EMERGENCY_CLIENT_ID = 999


def _connect(host: str, port: int) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=EMERGENCY_CLIENT_ID, timeout=15)
    return ib


def _get_positions(ib: IB) -> list[dict]:
    positions = []
    for p in ib.positions():
        qty = float(p.position)
        if qty == 0:
            continue
        contract = p.contract
        positions.append({
            "symbol": contract.symbol,
            "currency": getattr(contract, "currency", ""),
            "sec_type": contract.secType,
            "local_symbol": getattr(contract, "localSymbol", ""),
            "qty": qty,
            "direction": "LONG" if qty > 0 else "SHORT",
            "avg_cost": float(p.avgCost),
            "contract": contract,
        })
    return positions


def _close_position(ib: IB, pos: dict, dry_run: bool = False) -> bool:
    qty = abs(pos["qty"])
    side = "SELL" if pos["direction"] == "LONG" else "BUY"
    contract = pos["contract"]

    desc = f"{pos['symbol']}{('/' + pos['currency']) if pos['currency'] else ''}"
    print(f"  {'[DRY RUN] Would close' if dry_run else 'Closing'}: "
          f"{desc} {pos['direction']} qty={qty} via {side} MARKET")

    if dry_run:
        return True

    order = MarketOrder(side, qty)
    trade = ib.placeOrder(contract, order)

    # Wait for fill (up to 30 seconds)
    for _ in range(60):
        ib.sleep(0.5)
        if trade.isDone():
            status = trade.orderStatus.status
            fill_px = trade.orderStatus.avgFillPrice
            print(f"    {status} @ {fill_px}")
            return status == "Filled"

    print(f"    TIMEOUT — order may still be pending. Check TWS manually.")
    return False


def main():
    parser = argparse.ArgumentParser(description="Emergency position close via IBKR")
    parser.add_argument("--symbol", type=str, default=None,
                        help="Close only this symbol (e.g., EURUSD, EUR). Omit for ALL.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true",
                        help="Show positions without closing")
    parser.add_argument("--cancel-orders", action="store_true",
                        help="Cancel all open orders before closing positions")
    parser.add_argument("--yes", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    print("=" * 55)
    print(f"  EMERGENCY CLOSE — {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"  Target: {args.symbol or 'ALL POSITIONS'}")
    print(f"  Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 55)

    # Connect
    print(f"\n  Connecting to {args.host}:{args.port} (clientId={EMERGENCY_CLIENT_ID})...")
    try:
        ib = _connect(args.host, args.port)
        print(f"  Connected.")
    except Exception as e:
        print(f"  FAILED to connect: {e}")
        print(f"  Ensure TWS/Gateway is running and API is enabled on port {args.port}")
        sys.exit(1)

    try:
        # Cancel open orders if requested
        if args.cancel_orders:
            open_trades = ib.openTrades()
            if open_trades:
                print(f"\n  Canceling {len(open_trades)} open order(s)...")
                for trade in open_trades:
                    sym = getattr(trade.contract, "symbol", "?")
                    print(f"    Canceling {sym} order {trade.order.orderId}")
                    if not args.dry_run:
                        ib.cancelOrder(trade.order)
                        ib.sleep(0.3)
            else:
                print("\n  No open orders to cancel.")

        # Get positions
        positions = _get_positions(ib)
        if args.symbol:
            target = args.symbol.upper()
            positions = [p for p in positions if
                         p["symbol"].upper() == target or
                         p["local_symbol"].upper().replace(".", "") == target]

        if not positions:
            print(f"\n  No open positions found" +
                  (f" matching '{args.symbol}'" if args.symbol else "") + ".")
            return

        print(f"\n  Found {len(positions)} position(s):")
        for p in positions:
            desc = f"{p['symbol']}{('/' + p['currency']) if p['currency'] else ''}"
            print(f"    {desc}: {p['direction']} qty={abs(p['qty'])} avg_cost={p['avg_cost']:.5f}")

        # Confirm
        if not args.dry_run and not args.yes:
            response = input(f"\n  Close {'ALL' if not args.symbol else args.symbol} position(s)? [y/N]: ")
            if response.lower() != "y":
                print("  Aborted.")
                return

        # Close positions
        print()
        closed = 0
        for pos in positions:
            if _close_position(ib, pos, dry_run=args.dry_run):
                closed += 1

        print(f"\n  {'Would close' if args.dry_run else 'Closed'} {closed}/{len(positions)} position(s).")

    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    main()
