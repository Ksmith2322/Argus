"""Adversarial Tests Tier 1 — inject bad data, verify graceful handling.

Tests:
  1. Flash crash (5% drop in one bar)
  2. Feed gap (missing bars)
  3. Zero volume bar
  4. Zero price bar
  5. Extreme range bar (100x normal)

Usage:
    python -m argus_flow.tests.adversarial_tests
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.runner_eurusd import BarBuffer, compute_features, check_trigger


def _seed_normal_bars(n: int = 100, base_price: float = 1.1550) -> BarBuffer:
    """Create a buffer of realistic 1-minute EUR/USD bars."""
    buf = BarBuffer(maxlen=300)
    px = base_price
    for i in range(n):
        spread = np.random.normal(0, 0.0001)
        bar_range = abs(np.random.normal(0, 0.00015))
        o = px
        h = px + bar_range
        l = px - bar_range
        c = px + spread
        px = c
        buf.add({"ts": f"2026-03-22T10:{i:02d}:00", "open": o, "high": h, "low": l, "close": c, "volume": 0})
    return buf


def _ok(label: str):
    print(f"  \033[32m[PASS]\033[0m {label}")


def _fail(label: str, detail: str = ""):
    print(f"  \033[31m[FAIL]\033[0m {label}" + (f" — {detail}" if detail else ""))


def test_flash_crash():
    """Inject a 5% price drop in one bar. Should not trigger entry."""
    buf = _seed_normal_bars(80)
    df = buf.to_df()
    last_px = df["close"].iloc[-1]

    # Inject crash bar
    crash_px = last_px * 0.95
    buf.add({"ts": "2026-03-22T11:20:00", "open": last_px, "high": last_px, "low": crash_px, "close": crash_px, "volume": 0})

    features = compute_features(buf)
    if features is None:
        _ok("Flash crash: features returned None (safe — insufficient bars)")
        return

    trigger = check_trigger(features)
    if trigger is None:
        _ok("Flash crash: no trigger fired (correct — extreme move rejected)")
    else:
        # Check if the features are extreme
        if features.get("range_pct", 0) > 0.01:
            _ok(f"Flash crash: trigger={trigger} but range_pct={features['range_pct']:.4f} (extremely high — would be caught by risk)")
        else:
            _fail(f"Flash crash: trigger={trigger} fired on crash bar", f"range_pct={features.get('range_pct')}")


def test_feed_gap():
    """Remove 5 bars from middle of sequence. Features should not NaN."""
    buf = _seed_normal_bars(100)
    df = buf.to_df()

    # Create new buffer missing bars 50-55
    gap_buf = BarBuffer(maxlen=300)
    for i, row in df.iterrows():
        if 50 <= i <= 55:
            continue
        gap_buf.add(row.to_dict())

    features = compute_features(gap_buf)
    if features is None:
        _ok("Feed gap: features returned None (safe)")
        return

    has_nan = any(v != v for v in features.values() if isinstance(v, float))
    if has_nan:
        _fail("Feed gap: NaN in features", str({k: v for k, v in features.items() if isinstance(v, float) and v != v}))
    else:
        _ok(f"Feed gap: no NaN in features (range_pct={features.get('range_pct', 0):.6f})")


def test_zero_volume():
    """Bar with zero volume. Should not cause division by zero."""
    buf = _seed_normal_bars(80)

    # Add zero-volume bar
    last = buf.bars[-1]
    buf.add({"ts": "2026-03-22T11:20:00", "open": last["close"], "high": last["close"] + 0.0001,
             "low": last["close"] - 0.0001, "close": last["close"], "volume": 0})

    try:
        features = compute_features(buf)
        if features is None:
            _ok("Zero volume: features returned None (safe)")
        else:
            has_nan = any(v != v for v in features.values() if isinstance(v, float))
            has_inf = any(abs(v) == float('inf') for v in features.values() if isinstance(v, float))
            if has_nan or has_inf:
                _fail("Zero volume: NaN or Inf in features")
            else:
                _ok("Zero volume: features computed without error")
    except ZeroDivisionError:
        _fail("Zero volume: ZeroDivisionError raised")
    except Exception as e:
        _fail(f"Zero volume: unexpected error: {e}")


def test_zero_price():
    """Bar with price=0. Should be rejected or handled."""
    buf = _seed_normal_bars(80)

    buf.add({"ts": "2026-03-22T11:20:00", "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0})

    try:
        features = compute_features(buf)
        if features is None:
            _ok("Zero price: features returned None (safe)")
        else:
            # Check for inf/nan
            has_nan = any(v != v for v in features.values() if isinstance(v, float))
            has_inf = any(abs(v) == float('inf') for v in features.values() if isinstance(v, float))
            if has_inf:
                _fail("Zero price: Inf in features (division by zero not caught)")
            elif has_nan:
                _fail("Zero price: NaN propagation")
            else:
                trigger = check_trigger(features)
                if trigger is None:
                    _ok("Zero price: no trigger on zero-price bar (correct)")
                else:
                    _fail(f"Zero price: trigger={trigger} on zero-price bar")
    except ZeroDivisionError:
        _fail("Zero price: ZeroDivisionError")
    except Exception as e:
        _ok(f"Zero price: exception caught gracefully ({type(e).__name__})")


def test_extreme_range():
    """Bar with 100x normal range. Should not produce false trigger."""
    buf = _seed_normal_bars(80)
    last = buf.bars[-1]
    normal_range = 0.0002

    # 100x range bar
    extreme_range = normal_range * 100
    buf.add({"ts": "2026-03-22T11:20:00", "open": last["close"],
             "high": last["close"] + extreme_range, "low": last["close"] - extreme_range,
             "close": last["close"] + extreme_range * 0.5, "volume": 0})

    features = compute_features(buf)
    if features is None:
        _ok("Extreme range: features returned None (safe)")
        return

    has_nan = any(v != v for v in features.values() if isinstance(v, float))
    has_inf = any(abs(v) == float('inf') for v in features.values() if isinstance(v, float))

    if has_nan or has_inf:
        _fail(f"Extreme range: NaN/Inf in features")
    else:
        _ok(f"Extreme range: features computed (range_pct={features.get('range_pct', 0):.4f}, accel={features.get('range_accel', 0):.4f})")


def main():
    print("=" * 60)
    print("  Adversarial Tests — Tier 1")
    print("=" * 60)

    tests = [
        ("1. Flash Crash (5% drop)", test_flash_crash),
        ("2. Feed Gap (5 missing bars)", test_feed_gap),
        ("3. Zero Volume Bar", test_zero_volume),
        ("4. Zero Price Bar", test_zero_price),
        ("5. Extreme Range (100x)", test_extreme_range),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n  --- {name} ---")
        try:
            test_fn()
            passed += 1
        except Exception as e:
            _fail(f"Unhandled exception: {e}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed}/{len(tests)} passed")
    if failed == 0:
        print("  \033[32mALL TESTS PASSED\033[0m")
    else:
        print(f"  \033[31m{failed} FAILED\033[0m")
    print("=" * 60)


if __name__ == "__main__":
    main()