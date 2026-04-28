"""FLATTEN_EOD executor — closes all broker positions at market.

Triggered by:
  - FLATTEN_EOD.flag (touched manually OR automatically by daily_loss_circuit_breaker)
  - Manual invocation: python -m ops.flatten_eod_executor

Bypasses runner-level state. Goes directly to broker, lists positions, submits
market-close orders for each. For FX, uses outsideRth=True LIMIT (since FX
has 24/5 hours but IBKR sometimes routes oddly outside US RTH).

After flattening, this script does NOT clear FLATTEN_EOD.flag — that requires
manual review per real-money policy. Subsequent runs will re-attempt close on
any position that reappeared (e.g., from a runner that doesn't yet honor HALT).

Exit codes:
  0 — flatten run completed (positions either closed or none open to start)
  1 — TWS unreachable
  2 — flatten attempted but some positions remain
  3 — unexpected error
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FLATTEN_PATH = REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"

PROBE_CLIENT_ID = 186
WAIT_FILL_S = 8


def main(force: bool = False) -> int:
    if not force and not FLATTEN_PATH.exists():
        print("FLATTEN_EOD.flag not present - skipping (use --force to override)")
        return 0

    try:
        from ib_insync import IB, MarketOrder, LimitOrder, Stock, Forex, Future
    except Exception as e:
        print(f"FAIL: ib_insync import: {e}")
        return 3

    port = int(os.getenv("IBKR_PORT", "7497"))
    ib = IB()
    try:
        ib.connect("127.0.0.1", port, clientId=PROBE_CLIENT_ID, timeout=10)
    except Exception as e:
        print(f"FAIL: cannot connect to TWS: {e}")
        return 1

    try:
        positions = ib.positions()
        print(f"Open positions at start: {len(positions)}")
        if not positions:
            print("Already flat — nothing to do.")
            return 0

        for p in positions:
            qty = float(p.position)
            if qty == 0:
                continue
            sec_type = p.contract.secType
            symbol = p.contract.symbol
            print(f"  Closing {symbol} ({sec_type}) qty={qty}...")
            close_action = "SELL" if qty > 0 else "BUY"
            size = abs(int(qty)) if sec_type != "CASH" else abs(qty)

            # Re-qualify the contract since the one from positions() may need it
            try:
                ib.qualifyContracts(p.contract)
            except Exception:
                pass

            # FX: use wide LMT with outsideRth so it fills in extended hours
            # STK/ETF/FUT: use MARKET (RTH only — should be RTH if FLATTEN was triggered intraday)
            if sec_type == "CASH":
                # Estimate price from avgCost (close enough for wide LMT)
                ref = float(p.avgCost) if p.avgCost else 1.0
                # Wide LMT: pay up to 5% adverse. Broker fills at NBBO not the limit.
                lmt = round(ref * (1.05 if close_action == "BUY" else 0.95), 5)
                order = LimitOrder(close_action, size, lmt)
                order.outsideRth = True
                order.tif = "DAY"
            else:
                order = MarketOrder(close_action, size)

            trade = ib.placeOrder(p.contract, order)
            for _ in range(WAIT_FILL_S * 2):
                ib.sleep(0.5)
                if trade.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    break
            print(f"    status: {trade.orderStatus.status}", end="")
            if trade.fills:
                f = trade.fills[-1]
                print(f"  fill: {f.execution.shares} @ {f.execution.price}")
            else:
                print()

        # Re-poll positions to verify
        ib.sleep(2)
        remaining = [p for p in ib.positions() if abs(float(p.position)) > 0]
        print(f"\nAfter flatten: {len(remaining)} positions remain")
        for r in remaining:
            print(f"  STILL OPEN: {r.contract.symbol} qty={r.position}")
        return 0 if not remaining else 2
    except Exception as e:
        print(f"FAIL: unexpected: {e}")
        return 3
    finally:
        try: ib.disconnect()
        except Exception: pass


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    sys.exit(main(force=force_flag))
