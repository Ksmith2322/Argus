"""Pre-launch smoke test — verify each runner can connect, subscribe, and write.

Usage:
    python -m argus_flow.ops.smoke_test
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from ib_insync import IB, Future, Forex
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()


def test_gateway():
    """Test IBKR gateway connectivity."""
    print("TEST 1: Gateway connectivity")
    try:
        ib = IB()
        ib.connect("127.0.0.1", int(os.getenv("IBKR_PORT", "4002")), clientId=80, timeout=5)
        accounts = ib.managedAccounts()
        print(f"  [OK] Connected. Account: {accounts}")

        for item in ib.accountSummary():
            if item.tag in ["TotalCashValue", "NetLiquidation"]:
                print(f"  [OK] {item.tag}: {item.value} {item.currency}")

        ib.disconnect()
        return True
    except Exception as e:
        print(f"  [FAIL] {e}")
        return False


def test_instrument(ib, name, contract, what_to_show="BID"):
    """Test one instrument: qualify, subscribe, get data."""
    print(f"\nTEST: {name}")

    # Qualify
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print(f"  [FAIL] Could not qualify contract")
        return False
    contract = qualified[0]
    print(f"  [OK] Qualified: conId={contract.conId}")

    # Historical data
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 D",
            barSizeSetting="1 min", whatToShow=what_to_show,
            useRTH=False, formatDate=1, timeout=15,
        )
        if bars:
            print(f"  [OK] Historical: {len(bars)} bars, last={bars[-1].close}")
        else:
            print(f"  [WARN] No historical bars (market may be closed)")
    except Exception as e:
        print(f"  [WARN] Historical data: {e}")

    # Live data
    try:
        ticker = ib.reqMktData(contract, snapshot=False)
        ib.sleep(2)
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else "closed"
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else "closed"
        print(f"  [OK] Live data: bid={bid} ask={ask}")
    except Exception as e:
        print(f"  [WARN] Live data: {e}")

    return True


def test_configs():
    """Validate all config files."""
    print("\nTEST: Config validation")
    config_dir = Path("argus_flow/configs")
    if not config_dir.exists():
        print(f"  [FAIL] Config directory not found")
        return False

    all_ok = True
    for cfg_file in sorted(config_dir.glob("*_paper_v1.json")):
        try:
            cfg = json.loads(cfg_file.read_text())
            required = ["strategy", "version", "symbol", "trigger", "risk", "replay_expectations"]
            missing = [k for k in required if k not in cfg]
            if missing:
                print(f"  [FAIL] {cfg_file.name}: missing {missing}")
                all_ok = False
            else:
                print(f"  [OK] {cfg_file.name}: {cfg['strategy']} {cfg['symbol']}")
        except Exception as e:
            print(f"  [FAIL] {cfg_file.name}: {e}")
            all_ok = False

    return all_ok


def test_log_dirs():
    """Verify log directories exist and are writable."""
    print("\nTEST: Log directories")
    dirs = ["argus_flow/logs/eurusd", "argus_flow/logs/mnq", "argus_flow/logs/gbpusd"]
    all_ok = True
    for d in dirs:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / ".write_test"
        try:
            test_file.write_text("test")
            test_file.unlink()
            print(f"  [OK] {d}")
        except Exception as e:
            print(f"  [FAIL] {d}: {e}")
            all_ok = False
    return all_ok


def test_restart_safety():
    """Verify state files can be written and read back."""
    print("\nTEST: State file read/write")
    dirs = ["argus_flow/logs/eurusd", "argus_flow/logs/mnq", "argus_flow/logs/gbpusd"]
    all_ok = True
    for d in dirs:
        state_file = Path(d) / "state.json"
        test_state = {
            "position": "FLAT",
            "entry_price": 0.0,
            "trade_count": 0,
            "pnl_pips": 0.0,
            "test": True,
        }
        try:
            state_file.write_text(json.dumps(test_state))
            loaded = json.loads(state_file.read_text())
            assert loaded["position"] == "FLAT"
            state_file.unlink()
            print(f"  [OK] {d}")
        except Exception as e:
            print(f"  [FAIL] {d}: {e}")
            all_ok = False
    return all_ok


def main():
    print("=" * 60)
    print("  IBKR Fleet Pre-Launch Smoke Test")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)

    results = {}

    # Gateway
    results["gateway"] = test_gateway()

    # Configs
    results["configs"] = test_configs()

    # Log dirs
    results["log_dirs"] = test_log_dirs()

    # State files
    results["restart"] = test_restart_safety()

    # Instruments (only if gateway connected)
    if results["gateway"]:
        ib = IB()
        ib.connect("127.0.0.1", int(os.getenv("IBKR_PORT", "4002")), clientId=81, timeout=5)

        results["eurusd"] = test_instrument(ib, "EUR/USD", Forex("EURUSD"), "BID")
        time.sleep(1)
        results["gbpusd"] = test_instrument(ib, "GBP/USD", Forex("GBPUSD"), "BID")
        time.sleep(1)
        results["mnq"] = test_instrument(
            ib, "MNQ", Future(symbol="MNQ", exchange="CME", lastTradeDateOrContractMonth="20260618"), "TRADES"
        )

        ib.disconnect()

    # Summary
    print(f"\n{'='*60}")
    print("  SMOKE TEST SUMMARY")
    print(f"{'='*60}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, ok in results.items():
        status = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
        print(f"  {name:>15s}: {status}")

    print(f"\n  Result: {passed}/{total} passed")
    if passed == total:
        print("  \033[32mALL CLEAR — ready for Sunday launch\033[0m")
    else:
        print("  \033[31mFIX FAILURES before launching\033[0m")

    print()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()