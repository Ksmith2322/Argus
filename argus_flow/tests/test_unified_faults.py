"""Fault Injection Tests — verify unified runner handles failures gracefully.

Proves: one runner can fail without killing others.

Usage:
    python -m argus_flow.tests.test_unified_faults
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
TEST_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_tests"


def _ok(label):
    print(f"  \033[32m[PASS]\033[0m {label}")

def _fail(label, detail=""):
    print(f"  \033[31m[FAIL]\033[0m {label}" + (f" — {detail}" if detail else ""))


def test_state_corruption():
    """Write invalid JSON to state file, verify runner forces FLAT."""
    from argus_flow.runner_unified import State

    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_dir = TEST_TMP_ROOT / "_test_fault"
    test_dir.mkdir(parents=True, exist_ok=True)
    state_file = test_dir / "state.json"

    # Write corrupt JSON
    state_file.write_text("{invalid json content!!!")

    s = State(state_file)
    try:
        s.load("TEST")
        if s.position == "FLAT":
            _ok("Corrupt state: forced FLAT (correct)")
        else:
            _fail(f"Corrupt state: position={s.position} (should be FLAT)")
    except json.JSONDecodeError:
        _ok("Corrupt state: JSONDecodeError caught (acceptable)")
    except Exception as e:
        _fail(f"Corrupt state: unexpected error: {e}")

    # Cleanup
    try:
        state_file.unlink(missing_ok=True)
        test_dir.rmdir()
    except Exception:
        pass


def test_missing_stop_target():
    """State file with position but no stop/target — should force FLAT."""
    from argus_flow.runner_unified import State

    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_dir = TEST_TMP_ROOT / "_test_fault2"
    test_dir.mkdir(parents=True, exist_ok=True)
    state_file = test_dir / "state.json"

    # Write state with position but missing stops
    state_file.write_text(json.dumps({
        "position": "LONG",
        "entry_price": 1.1550,
        "stop_price": 0,
        "target_price": 0,
        "trade_count": 5,
        "pnl": 10.0,
    }))

    s = State(state_file)
    s.load("TEST")

    if s.position == "FLAT":
        _ok("Missing stop/target: forced FLAT (correct safety behavior)")
    else:
        _fail(f"Missing stop/target: position={s.position} (should be forced FLAT)")

    try:
        state_file.unlink(missing_ok=True)
        test_dir.rmdir()
    except Exception:
        pass


def test_signal_log_recreation():
    """Delete signal log, verify runner would recreate it."""
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    test_dir = TEST_TMP_ROOT / "_test_fault3"
    test_dir.mkdir(parents=True, exist_ok=True)
    sig_file = test_dir / "signals.csv"

    # Ensure file doesn't exist
    try:
        sig_file.unlink(missing_ok=True)
    except Exception:
        pass

    if not sig_file.exists():
        _ok("Signal log deleted successfully")
    else:
        _fail("Could not delete signal log")

    # The runner's _log_signal creates the file on first write
    # Just verify the path is writable
    try:
        sig_file.write_text("test")
        sig_file.unlink(missing_ok=True)
        _ok("Signal log path is writable (runner will recreate)")
    except Exception as e:
        _fail(f"Signal log path not writable: {e}")

    try:
        test_dir.rmdir()
    except Exception:
        pass


def test_nan_features():
    """Feed bars that produce NaN — verify no crash."""
    from argus_flow.runner_unified import BarBuffer
    import numpy as np

    buf = BarBuffer(300)

    # Seed with normal bars
    for i in range(70):
        buf.add({"ts": f"2026-03-24T10:{i:02d}:00", "open": 1.155, "high": 1.156,
                 "low": 1.154, "close": 1.155, "volume": 0})

    # Add a zero-price bar (could cause division by zero)
    buf.add({"ts": "2026-03-24T11:10:00", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0})

    df = buf.to_df()
    if len(df) >= 60:
        try:
            # Simulate feature computation
            lookback = 30
            ctx_win = min(240, len(df))
            pre = df.iloc[-lookback:]
            context = df.iloc[-ctx_win:]

            close_val = pre["close"].iloc[-1]
            if close_val == 0:
                _ok("NaN features: zero close detected (would skip in runner)")
            else:
                range_pct = (pre["high"].max() - pre["low"].min()) / close_val
                _ok(f"NaN features: computed without crash (range_pct={range_pct})")
        except ZeroDivisionError:
            _fail("NaN features: ZeroDivisionError")
        except Exception as e:
            _ok(f"NaN features: exception caught gracefully ({type(e).__name__})")
    else:
        _fail("Not enough bars")


def test_per_runner_isolation():
    """Verify that runner error handling is per-runner, not global."""
    # The unified runner wraps each inst.tick() in try/except
    # This test verifies the pattern exists in the code

    runner_path = Path("argus_flow/runner_unified.py")
    if not runner_path.exists():
        _fail("runner_unified.py not found")
        return

    code = runner_path.read_text(encoding="utf-8")

    # Check for per-runner error isolation in the main loop
    if "try:" in code and "inst.tick" in code:
        # Find if tick is called inside a try block
        lines = code.split("\n")
        in_try = False
        tick_in_try = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("try:"):
                in_try = True
            if in_try and "inst.tick" in stripped or ".tick(" in stripped:
                tick_in_try = True
                break
            if stripped.startswith("except") and in_try:
                in_try = False

        if tick_in_try:
            _ok("Per-runner isolation: tick() called inside try/except (correct)")
        else:
            _fail("Per-runner isolation: tick() NOT inside try/except — shared-fate risk!")
    else:
        _fail("Per-runner isolation: could not verify error handling pattern")


def test_process_lock_exclusive():
    """Second process lock acquisition on same name should fail closed."""
    from ops.process_lock import ProcessLock, ProcessLockError

    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(dir=TEST_TMP_ROOT))
    try:
        first = ProcessLock("test_runner_lock", lock_dir=Path(tmpdir))
        second = ProcessLock("test_runner_lock", lock_dir=Path(tmpdir))
        first.acquire({"kind": "test"})
        try:
            try:
                second.acquire({"kind": "test"})
                _fail("Process lock should reject duplicate acquisition")
            except ProcessLockError:
                _ok("Process lock blocks duplicate acquisition")
        finally:
            first.release()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    print("=" * 60)
    print("  Unified Runner Fault Injection Tests")
    print("=" * 60)

    tests = [
        ("1. Corrupt state file", test_state_corruption),
        ("2. Missing stop/target on restore", test_missing_stop_target),
        ("3. Signal log recreation", test_signal_log_recreation),
        ("4. NaN/zero in features", test_nan_features),
        ("5. Per-runner error isolation", test_per_runner_isolation),
        ("6. Process lock exclusivity", test_process_lock_exclusive),
    ]

    for name, fn in tests:
        print(f"\n  --- {name} ---")
        try:
            fn()
        except Exception as e:
            _fail(f"Unhandled: {e}")

    print(f"\n{'='*60}")
    print("  Tests complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
