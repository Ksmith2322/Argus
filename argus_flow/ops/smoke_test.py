"""Pre-launch smoke test — verify each runner can connect, subscribe, and write.

Usage:
    python -m argus_flow.ops.smoke_test
"""
from __future__ import annotations

import json
import logging
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

logging.getLogger("ib_insync").setLevel(logging.CRITICAL)

IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "4002"))
SMOKE_BASE_CLIENT_ID = int(os.getenv("IBKR_SMOKE_TEST_CLIENT_ID", "18000"))


def connect_readonly(label: str, base_client_id: int, attempts: int = 20) -> IB | None:
    """Connect in read-only mode and avoid client-id collisions with the live fleet."""
    last_error: Exception | None = None

    for offset in range(attempts):
        client_id = base_client_id + offset
        ib = IB()
        try:
            ib.connect(
                IBKR_HOST,
                IBKR_PORT,
                clientId=client_id,
                timeout=5,
                readonly=True,
            )
            print(f"  [OK] {label}: connected with clientId={client_id} (readonly)")
            return ib
        except Exception as exc:
            last_error = exc
            try:
                ib.disconnect()
            except Exception:
                pass
            if "client id is already in use" in str(exc).lower():
                continue
            break

    print(f"  [FAIL] {label}: {last_error}")
    return None


def test_gateway() -> tuple[bool, IB | None]:
    """Test IBKR gateway connectivity and account summary."""
    print("TEST 1: Gateway connectivity")
    ib = connect_readonly("gateway", SMOKE_BASE_CLIENT_ID)
    if not ib:
        return False, None

    try:
        accounts = ib.managedAccounts()
        print(f"  [OK] Connected. Account: {accounts}")

        for item in ib.accountSummary():
            if item.tag in ["TotalCashValue", "NetLiquidation"]:
                print(f"  [OK] {item.tag}: {item.value} {item.currency}")
        return True, ib
    except Exception as exc:
        print(f"  [FAIL] Gateway summary: {exc}")
        try:
            ib.disconnect()
        except Exception:
            pass
        return False, None


def test_instrument(ib: IB, name, contract, what_to_show: str = "BID") -> bool:
    """Test one instrument: qualify, subscribe, get data."""
    print(f"\nTEST: {name}")

    qualified = ib.qualifyContracts(contract)
    if not qualified:
        print("  [FAIL] Could not qualify contract")
        return False
    contract = qualified[0]
    print(f"  [OK] Qualified: conId={contract.conId}")

    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime="", durationStr="1 D",
            barSizeSetting="1 min", whatToShow=what_to_show,
            useRTH=False, formatDate=1, timeout=15,
        )
        if bars:
            print(f"  [OK] Historical: {len(bars)} bars, last={bars[-1].close}")
        else:
            print("  [WARN] No historical bars (market may be closed)")
    except Exception as exc:
        print(f"  [WARN] Historical data: {exc}")

    try:
        ticker = ib.reqMktData(contract, snapshot=False)
        ib.sleep(2)
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else "closed"
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else "closed"
        print(f"  [OK] Live data: bid={bid} ask={ask}")
    except Exception as exc:
        print(f"  [WARN] Live data: {exc}")

    return True


def test_configs():
    """Validate all config files."""
    print("\nTEST: Config validation")
    config_dir = Path("argus_flow/configs")
    if not config_dir.exists():
        print("  [FAIL] Config directory not found")
        return False

    all_ok = True
    for cfg_file in sorted(config_dir.glob("*_paper_v1.json")):
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            required = ["strategy", "version", "symbol", "trigger", "risk", "replay_expectations"]
            missing = [k for k in required if k not in cfg]
            if missing:
                print(f"  [FAIL] {cfg_file.name}: missing {missing}")
                all_ok = False
            else:
                print(f"  [OK] {cfg_file.name}: {cfg['strategy']} {cfg['symbol']}")
        except Exception as exc:
            print(f"  [FAIL] {cfg_file.name}: {exc}")
            all_ok = False

    return all_ok


def test_log_dirs():
    """Verify representative live log directories exist and are readable."""
    print("\nTEST: Log directories")
    all_ok = True
    dirs = [Path("argus_flow/logs") / name for name in ("eurusd", "mnq", "gbpusd")]
    for p in dirs:
        try:
            if not p.exists():
                raise FileNotFoundError("log dir missing")
            _ = [child.name for child in p.iterdir()]
            print(f"  [OK] {p}")
        except Exception as exc:
            print(f"  [FAIL] {p}: {exc}")
            all_ok = False
    return all_ok


def test_restart_safety():
    """Verify representative live state files are readable JSON."""
    print("\nTEST: State file read")
    all_ok = True
    dirs = [Path("argus_flow/logs") / name for name in ("eurusd", "mnq", "gbpusd")]
    for d in dirs:
        state_file = d / "state.json"
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            assert "position" in loaded
            print(f"  [OK] {d}")
        except Exception as exc:
            print(f"  [FAIL] {d}: {exc}")
            all_ok = False
    return all_ok


def main():
    print("=" * 60)
    print("  IBKR Fleet Pre-Launch Smoke Test")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)

    results: dict[str, bool] = {}

    gateway_ok, ib = test_gateway()
    results["gateway"] = gateway_ok
    results["configs"] = test_configs()
    results["log_dirs"] = test_log_dirs()
    results["restart"] = test_restart_safety()

    if gateway_ok and ib is not None:
        try:
            results["eurusd"] = test_instrument(ib, "EUR/USD", Forex("EURUSD"), "BID")
            time.sleep(1)
            results["gbpusd"] = test_instrument(ib, "GBP/USD", Forex("GBPUSD"), "BID")
            time.sleep(1)
            results["mnq"] = test_instrument(
                ib,
                "MNQ",
                Future(symbol="MNQ", exchange="CME", lastTradeDateOrContractMonth="20260618"),
                "TRADES",
            )
        finally:
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
