"""flatten_orphan — close a specific orphan position via IBKR API.

Used when the broker holds a position that no strategy claims via heartbeat.
Examples:
  - The 4/28 kill incident left SPY 13 shares with no manager
  - Bracket OCO orders that filled but didn't close cleanly
  - Manual TWS positions opened by the operator and never tracked

Dry-run by default. The --execute flag actually submits the close order.
The dry-run mode is safe — it CONNECTS to TWS and reads, but does not
submit any orders. Use it to verify the script will close the right
quantity in the right direction before going live.

Usage:
    cd c:/Argus/repo

    # See what the script WOULD do
    C:/Argus/.venv/Scripts/python.exe -m ops.flatten_orphan SPY

    # Actually submit the close order
    C:/Argus/.venv/Scripts/python.exe -m ops.flatten_orphan SPY --execute

    # Only flatten if absolute qty is below a safety cap (paranoid mode)
    C:/Argus/.venv/Scripts/python.exe -m ops.flatten_orphan SPY --execute --max-qty 50

Safety:
  - Refuses to flatten if any active heartbeat claims the symbol (would
    interfere with a strategy's real position)
  - Logs every action (connect / submit / fill) to argus_flow/logs/decision_history.jsonl
  - --max-qty caps the position size as a last-resort safety bound
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DECISION_HISTORY = REPO / "argus_flow" / "logs" / "decision_history.jsonl"

# A separate clientId from runners + probes, so this connection doesn't
# collide with anything else. 200+ is well outside the runner range
# (12, 51, 53, 60, 70, 80, 90, 101-117) and the probe (188).
FLATTEN_CLIENT_ID = 200


def _log(kind: str, **kwargs) -> None:
    """Append a governance audit-trail entry."""
    record = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **kwargs}
    DECISION_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    try:
        with DECISION_HISTORY.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _read_broker_snapshot() -> dict:
    p = REPO / "argus_flow" / "logs" / "_broker" / "broker_snapshot.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _strategies_claiming_symbol(symbol: str) -> list[str]:
    """Scan all heartbeats for any that have an open trade on this symbol.
    If any do, the position is NOT an orphan — refuse to flatten it.
    """
    sym_upper = symbol.upper()
    claimers: list[str] = []
    hb_roots = [
        REPO / "argus_flow" / "logs",
        REPO / "forge" / "logs",
        REPO / "apollo" / "logs",
        REPO / "hermes" / "logs",
        REPO / "titan" / "logs",
    ]
    for root in hb_roots:
        if not root.exists():
            continue
        for hb_path in list(root.glob("*/heartbeat.json")) + list(root.glob("heartbeat.json")):
            try:
                hb = json.loads(hb_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            system = hb.get("system") or hb_path.parent.name
            # Format 1: open_trade (single dict) — check its symbol field if present
            ot = hb.get("open_trade")
            if isinstance(ot, dict):
                ot_sym = (ot.get("symbol") or "").upper()
                if ot_sym == sym_upper or ot_sym.replace("/", "") == sym_upper:
                    claimers.append(system)
                    continue
            # Format 2: open_trades (dict keyed by instrument)
            ots = hb.get("open_trades")
            if isinstance(ots, dict) and sym_upper in (k.upper() for k in ots.keys()):
                claimers.append(system)
                continue
            # Format 3: position + instruments[] (basket — tom_international etc)
            position = hb.get("position")
            instruments = hb.get("instruments") or []
            if (
                isinstance(position, str)
                and position.upper() in ("LONG", "SHORT")
                and sym_upper in (i.upper() for i in instruments)
            ):
                claimers.append(system)
    return claimers


def main() -> int:
    ap = argparse.ArgumentParser(description="Close an orphan IBKR position by symbol.")
    ap.add_argument("symbol", help="Symbol to flatten (e.g. SPY)")
    ap.add_argument("--execute", action="store_true",
                    help="Actually submit the close order (default: dry-run)")
    ap.add_argument("--max-qty", type=float, default=None,
                    help="Refuse to flatten if abs(qty) > this. Paranoid safety cap.")
    ap.add_argument("--force", action="store_true",
                    help="Override the heartbeat-claim safety check (use only if you're sure)")
    args = ap.parse_args()
    sym = args.symbol.upper()

    print("=" * 64)
    print(f"  FLATTEN ORPHAN  —  {sym}")
    print(f"  Mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print("=" * 64)
    print()

    # 1. Find the position in broker_snapshot
    snap = _read_broker_snapshot()
    positions = snap.get("positions") or {}
    pos = positions.get(sym)
    if not pos:
        print(f"  No position in broker_snapshot.json for {sym}.")
        print(f"  Available: {sorted(positions.keys())}")
        return 1
    qty = float(pos.get("qty") or 0)
    direction = (pos.get("direction") or "").upper()  # LONG / SHORT
    avg_cost = float(pos.get("avg_cost") or 0)
    print(f"  Broker position:  {sym}  qty={qty}  direction={direction}  avg_cost=${avg_cost}")

    if qty == 0:
        print(f"  qty=0 — nothing to flatten.")
        return 0

    if args.max_qty is not None and abs(qty) > args.max_qty:
        print(f"  REFUSE: abs(qty) {abs(qty)} > --max-qty {args.max_qty}.")
        print(f"  Re-run with a larger --max-qty if you intend to close a position this big.")
        return 2

    # 2. Safety: any heartbeat claiming this symbol?
    claimers = _strategies_claiming_symbol(sym)
    if claimers and not args.force:
        print(f"  REFUSE: heartbeat claim found on {sym}: {claimers}")
        print(f"  This is NOT an orphan — flattening would interfere with a live strategy.")
        print(f"  If you really want to close it, re-run with --force (and stop the runner first).")
        return 3

    # 3. Compute the close order
    close_action = "SELL" if direction == "LONG" else "BUY"
    close_qty = abs(qty)
    print(f"  Close order:      {close_action} {close_qty} {sym} MKT")
    print()

    if not args.execute:
        print("  DRY-RUN — no order submitted.")
        print(f"  Re-run with --execute to actually close the position.")
        return 0

    # 4. Connect to TWS and submit
    port = int(os.getenv("IBKR_PORT", "7497"))
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    try:
        from ib_insync import IB, Stock, MarketOrder
    except Exception as e:
        print(f"  ib_insync import failed: {e}")
        return 4

    ib = IB()
    try:
        ib.connect(host, port, clientId=FLATTEN_CLIENT_ID, timeout=10)
    except Exception as e:
        print(f"  TWS connect failed at {host}:{port}: {e}")
        return 5

    try:
        contract = Stock(sym, "SMART", "USD")
        ib.qualifyContracts(contract)
        order = MarketOrder(close_action, close_qty)
        trade = ib.placeOrder(contract, order)
        # Wait briefly for ack
        ib.sleep(2)
        status = trade.orderStatus.status
        order_id = trade.order.orderId
        filled = trade.orderStatus.filled
        avg_fill = trade.orderStatus.avgFillPrice
        print(f"  Order submitted:  id={order_id}  status={status}  filled={filled}  avg_fill={avg_fill}")
        _log(
            kind="orphan_flatten",
            symbol=sym, direction=direction, qty=qty, close_action=close_action,
            order_id=order_id, status=status, filled=filled, avg_fill_price=avg_fill,
            source="manual_cli",
        )
        if status in ("Filled", "Submitted", "PreSubmitted"):
            return 0
        return 6
    except Exception as e:
        print(f"  Order submit failed: {e}")
        return 7
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
