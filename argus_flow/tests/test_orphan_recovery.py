"""Orphan Order Recovery Integration Test.

Proves: when an order is submitted but ACK is lost (crash before confirmation),
the system detects the orphaned position on restart and handles it correctly.

Scenarios tested:
  A: Local=FLAT, Broker=LONG (orphan) → entries blocked, incident written
  B: Local=LONG, Broker=FLAT (phantom) → auto-correct to FLAT
  C: Local=FLAT, Broker=FLAT (clean) → no action
  D: Local=LONG, Broker=LONG (matched) → no action
  E: Untracked broker position (not in any runner) → detected as orphan

Usage:
    python -m argus_flow.tests.test_orphan_recovery
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
TEST_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_sandbox_tests"


def _new_test_dir(prefix: str) -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_TMP_ROOT / f"argus_{prefix}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ok(label):
    print(f"  \033[32m[PASS]\033[0m {label}")


def _fail(label, detail=""):
    print(f"  \033[31m[FAIL]\033[0m {label}" + (f" — {detail}" if detail else ""))
    raise AssertionError(f"{label}: {detail}" if detail else label)


# ── Mock classes ──────────────────────────────────────────────

class MockState:
    """Minimal State mock with save/load."""
    def __init__(self, state_file: Path, position: str = "FLAT"):
        self.file = state_file
        self.position = position
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time = None
        self.entry_time = None
        self.avg_entry_price = 0.0
        self.pyramid_adds = 0
        self.position_size = 0.0
        self.entry_risk_usd = 0.0

    def clear_trade_state(self):
        self.position = "FLAT"
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time = None
        self.entry_time = None
        self.avg_entry_price = 0.0
        self.pyramid_adds = 0
        self.position_size = 0.0
        self.entry_risk_usd = 0.0

    def save(self):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"position": self.position}))


class MockInstrument:
    """Minimal InstrumentRunner mock for reconciliation."""
    def __init__(self, symbol: str, position: str, log_dir: Path):
        self.symbol = symbol
        self.contract = SimpleNamespace(
            secType="CASH", symbol=symbol[:3].upper(),
            currency=symbol[3:].upper(), exchange="IDEALPRO",
            localSymbol=f"{symbol[:3].upper()}.{symbol[3:].upper()}"
        )
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state = MockState(log_dir / "state.json", position)
        # `cfg` is consumed by runner_unified.reconcile_instruments to
        # classify argus (FX) vs forge (equities/futures) ownership of
        # non-tracked broker positions. Mocked instruments are all FX.
        self.cfg = {"instrument_type": "forex"}
        self._reconciliation = None
        self._reconciliation_detail = ""
        self._broker_position = "FLAT"
        self._broker_qty = 0.0
        self._broker_avg_cost = 0.0
        self._log = SimpleNamespace(
            info=lambda msg: None,
            warning=lambda msg: None,
            error=lambda msg: None,
        )


class MockIB:
    """Mock IB connection that returns configured positions."""
    def __init__(self, positions_map: dict):
        self._positions = positions_map

    def positions(self):
        result = []
        for key, (qty, avg_cost) in self._positions.items():
            parts = key.split(".")
            contract = SimpleNamespace(
                secType="CASH",
                symbol=parts[0] if len(parts) >= 1 else key,
                currency=parts[1] if len(parts) >= 2 else "USD",
                exchange="IDEALPRO",
                localSymbol=key,
            )
            result.append(SimpleNamespace(contract=contract, position=qty, avgCost=avg_cost))
        return result

    def isConnected(self):
        return True


# ── Tests ─────────────────────────────────────────────────────

def test_scenario_a_orphan():
    """Local=FLAT, Broker=LONG → entries blocked, incident written."""
    print("\n  Scenario A: Orphan (local FLAT, broker LONG)")
    from argus_flow.runner_unified import reconcile_instruments, _determine_runtime_mode, ReconcileResult, RuntimeMode

    test_dir = _new_test_dir("orphan_a")
    inst = MockInstrument("EURUSD", "FLAT", test_dir / "eurusd")

    # Broker has a LONG position (order filled but bot crashed before seeing it)
    ib = MockIB({"EUR.USD": (20000, 1.085)})

    results = reconcile_instruments(ib, [inst])

    r = results.get("EURUSD", {})
    if r.get("result") == ReconcileResult.LOCAL_FLAT_BROKER_OPEN:
        _ok("Detected orphan: LOCAL_FLAT_BROKER_OPEN")
    else:
        _fail("Expected LOCAL_FLAT_BROKER_OPEN", f"got {r.get('result')}")

    # Verify runtime mode blocks entries
    mode = _determine_runtime_mode(results)
    if mode == RuntimeMode.RECOVERY_REQUIRED:
        _ok("Runtime mode: RECOVERY_REQUIRED (entries blocked)")
    else:
        _fail("Expected RECOVERY_REQUIRED", f"got {mode}")

    # Verify local state NOT auto-corrected (orphan requires manual review)
    if inst.state.position == "FLAT":
        _ok("Local state preserved as FLAT (requires manual review)")
    else:
        _fail("Local state should remain FLAT for manual review")


def test_scenario_b_phantom():
    """Local=LONG, Broker=FLAT → auto-correct to FLAT."""
    print("\n  Scenario B: Phantom (local LONG, broker FLAT)")
    from argus_flow.runner_unified import reconcile_instruments, ReconcileResult

    test_dir = _new_test_dir("orphan_b")
    inst = MockInstrument("EURUSD", "LONG", test_dir / "eurusd")
    inst.state.entry_price = 1.085
    inst.state.stop_price = 1.083
    inst.state.target_price = 1.090

    # Broker is flat (stop was hit, but bot crashed before seeing the fill)
    ib = MockIB({})

    results = reconcile_instruments(ib, [inst])

    r = results.get("EURUSD", {})
    if r.get("result") == ReconcileResult.LOCAL_OPEN_BROKER_FLAT:
        _ok("Detected phantom: LOCAL_OPEN_BROKER_FLAT")
    else:
        _fail("Expected LOCAL_OPEN_BROKER_FLAT", f"got {r.get('result')}")

    # Verify auto-correction
    if inst.state.position == "FLAT":
        _ok("Auto-corrected to FLAT (trust broker truth)")
    else:
        _fail("Should auto-correct to FLAT", f"got {inst.state.position}")

    if inst.state.entry_price == 0.0 and inst.state.stop_price == 0.0:
        _ok("Lifecycle state cleared (entry_price, stop_price = 0)")
    else:
        _fail("Lifecycle state should be cleared")


def test_scenario_c_clean_flat():
    """Local=FLAT, Broker=FLAT → clean, no action."""
    print("\n  Scenario C: Clean flat (both sides agree)")
    from argus_flow.runner_unified import reconcile_instruments, ReconcileResult

    test_dir = _new_test_dir("orphan_c")
    inst = MockInstrument("EURUSD", "FLAT", test_dir / "eurusd")
    ib = MockIB({})

    results = reconcile_instruments(ib, [inst])

    r = results.get("EURUSD", {})
    if r.get("result") == ReconcileResult.CLEAN_FLAT:
        _ok("CLEAN_FLAT — no action needed")
    else:
        _fail("Expected CLEAN_FLAT", f"got {r.get('result')}")


def test_scenario_d_matched():
    """Local=LONG, Broker=LONG → clean match, no action."""
    print("\n  Scenario D: Matched (both agree LONG)")
    from argus_flow.runner_unified import reconcile_instruments, ReconcileResult

    test_dir = _new_test_dir("orphan_d")
    inst = MockInstrument("EURUSD", "LONG", test_dir / "eurusd")
    inst.state.entry_price = 1.085
    ib = MockIB({"EUR.USD": (20000, 1.085)})

    results = reconcile_instruments(ib, [inst])

    r = results.get("EURUSD", {})
    if r.get("result") == ReconcileResult.CLEAN_OPEN_MATCHED:
        _ok("CLEAN_OPEN_MATCHED — position confirmed")
    else:
        _fail("Expected CLEAN_OPEN_MATCHED", f"got {r.get('result')}")


def test_scenario_e_untracked():
    """Broker has position in symbol not tracked by any runner → detected."""
    print("\n  Scenario E: Untracked broker position")
    from argus_flow.runner_unified import reconcile_instruments, ReconcileResult

    test_dir = _new_test_dir("orphan_e")
    inst = MockInstrument("EURUSD", "FLAT", test_dir / "eurusd")

    # Broker has EUR/USD (tracked) + GBP/USD (untracked)
    ib = MockIB({"GBP.USD": (20000, 1.265)})

    results = reconcile_instruments(ib, [inst])

    # Check for untracked position detection
    orphan_keys = [k for k in results if k.startswith("_orphan_")]
    if orphan_keys:
        _ok(f"Untracked broker position detected: {orphan_keys}")
    else:
        _fail("Should detect untracked GBP.USD position")

    orphan = results[orphan_keys[0]]
    if orphan["result"] == ReconcileResult.UNRESOLVED:
        _ok("Untracked position classified as UNRESOLVED")
    else:
        _fail("Expected UNRESOLVED", f"got {orphan['result']}")


def test_scenario_f_broker_unavailable():
    """Broker connection fails → BROKER_UNAVAILABLE, no auto-correction."""
    print("\n  Scenario F: Broker unavailable")
    from argus_flow.runner_unified import reconcile_instruments, ReconcileResult

    test_dir = _new_test_dir("orphan_f")
    inst = MockInstrument("EURUSD", "LONG", test_dir / "eurusd")
    inst.state.entry_price = 1.085

    # Broker throws on positions() — simulates network failure
    class BrokenIB:
        def positions(self):
            raise ConnectionError("Network timeout")
        def isConnected(self):
            return False

    results = reconcile_instruments(BrokenIB(), [inst])

    r = results.get("EURUSD", {})
    if r.get("result") == ReconcileResult.BROKER_UNAVAILABLE:
        _ok("BROKER_UNAVAILABLE — no auto-correction")
    else:
        _fail("Expected BROKER_UNAVAILABLE", f"got {r.get('result')}")

    # Position must NOT be auto-corrected when broker is unavailable
    if inst.state.position == "LONG":
        _ok("Position preserved (no blind correction without broker truth)")
    else:
        _fail("Position should be preserved when broker unavailable")


# ── Main ──────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Orphan Order Recovery — Integration Tests")
    print("=" * 60)

    tests = [
        test_scenario_a_orphan,
        test_scenario_b_phantom,
        test_scenario_c_clean_flat,
        test_scenario_d_matched,
        test_scenario_e_untracked,
        test_scenario_f_broker_unavailable,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"  \033[31m[FAIL]\033[0m {test.__name__} â€” {e}")
            failed += 1

    print(f"\n  {'=' * 40}")
    print(f"  Results: {passed} passed, {failed} failed")
    if failed:
        print(f"  \033[31mFAILED\033[0m")
        sys.exit(1)
    else:
        print(f"  \033[32mALL PASSED\033[0m")


if __name__ == "__main__":
    main()
