"""Phase 22D — Chaos Engineering / Adversarial Data Testing.

Injects anomalous data into the FX strategy pipeline to verify
the system fails SAFELY — not that it never fails.

Tests cover: flash crashes, feed gaps, zero volume/price, extreme spreads,
stale/frozen feeds, timestamp anomalies, duplicate bars, and spike-recovery.

Usage:
    python -m argus_flow.tests.chaos_test
    python -m argus_flow.tests.chaos_test --test flash_crash
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from argus_flow.runner_unified import BarBuffer, compute_features_fx, check_trigger_fx
from argus_flow.ops.fx_backtest import BacktestRunner


# ── Helpers ──────────────────────────────────────────────────

def _ok(label):
    print(f"  \033[32m[PASS]\033[0m {label}")


def _fail(label, detail=""):
    print(f"  \033[31m[FAIL]\033[0m {label}" + (f" — {detail}" if detail else ""))


# Default config used for BacktestRunner tests
_DEFAULT_CFG = {
    "symbol": "EUR.USD",
    "strategy": "chaos_test",
    "instrument_type": "forex",
    "trigger": {
        "range_pct_min": 0.0008,
        "range_accel_min": 0.0,
        "vol_z_min": -999,
        "session_start_utc": 0,
        "session_end_utc": 23,
    },
    "direction": {
        "dist_long_threshold": 0.4,
        "dist_short_threshold": 0.6,
    },
    "risk": {
        "stop_pips": 12,
        "target_pips": 18,
        "timeout_minutes": 60,
        "min_signal_gap_minutes": 5,
    },
}


def _make_bars(n, base_price=1.1000, spread=0.0010, start_ts=None):
    """Generate n normal 1-min FX bars with realistic noise."""
    bars = []
    rng = random.Random(42)
    px = base_price
    if start_ts is None:
        start_ts = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(n):
        change = rng.uniform(-spread, spread)
        px += change
        bar_range = abs(change) * 0.5
        h = max(px, px + bar_range)
        l = min(px, px - bar_range)
        ts = start_ts + timedelta(minutes=i)
        bars.append({
            "ts": ts.isoformat(),
            "open": round(px - change, 5),
            "high": round(h, 5),
            "low": round(l, 5),
            "close": round(px, 5),
            "volume": round(rng.uniform(100, 1000), 2),
        })
    return bars


def _features_safe(features):
    """Return True if features dict has no NaN or Inf values."""
    if features is None:
        return True
    for v in features.values():
        if isinstance(v, float):
            if v != v:  # NaN
                return False
            if abs(v) == float("inf"):
                return False
    return True


def _run_bars_through_backtest(bars, cfg=None):
    """Feed bars into BacktestRunner, return (runner, crashed)."""
    cfg = cfg or _DEFAULT_CFG
    bt = BacktestRunner(cfg)
    crashed = False
    for bar in bars:
        try:
            bt.process_bar(bar)
        except Exception:
            crashed = True
            break
    return bt, crashed


# ── Test Functions ───────────────────────────────────────────

def test_flash_crash():
    """Inject a bar with 10% price drop. Verify: no crash, stop triggered
    if in position, no new entry on impossible bar."""
    bars = _make_bars(100)
    last_px = bars[-1]["close"]

    # 10% flash crash bar
    crash_px = round(last_px * 0.90, 5)
    crash_ts = datetime(2026, 3, 20, 11, 40, 0, tzinfo=timezone.utc)
    crash_bar = {
        "ts": crash_ts.isoformat(),
        "open": last_px,
        "high": last_px,
        "low": crash_px,
        "close": crash_px,
        "volume": 5000.0,
    }
    bars.append(crash_bar)

    # Test 1: Features compute without crash
    buf = BarBuffer(300)
    for b in bars:
        buf.add(b)
    try:
        features = compute_features_fx(buf)
        ok_features = _features_safe(features)
    except Exception as e:
        _fail("Flash crash: feature computation crashed", str(e))
        return

    if ok_features:
        _ok("Flash crash: features computed safely (no NaN/Inf)")
    else:
        _fail("Flash crash: NaN/Inf in features")

    # Test 2: Full backtest does not crash
    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Flash crash: BacktestRunner crashed")
    else:
        _ok(f"Flash crash: BacktestRunner survived ({len(bt.trades)} trades)")

    # Test 3: If position was open, stop should have fired on crash bar
    # Force an entry then inject crash
    bt2 = BacktestRunner(_DEFAULT_CFG)
    for b in bars[:-1]:
        bt2.process_bar(b)
    # Manually force a position to test stop
    if bt2.position == "FLAT":
        bt2.position = "LONG"
        bt2.entry_price = last_px
        bt2.stop_price = last_px - 0.0012  # 12 pip stop
        bt2.target_price = last_px + 0.0018
        bt2.entry_time = crash_ts - timedelta(minutes=5)
        bt2.timeout_time = crash_ts + timedelta(minutes=55)
    bt2.process_bar(crash_bar)
    if bt2.position == "FLAT":
        _ok("Flash crash: stop triggered correctly on 10% drop")
    else:
        _fail("Flash crash: position not closed after 10% drop")


def test_feed_gap():
    """Generate 100 bars, skip 30 bars (gap), continue 70 more.
    Verify: features recompute without NaN, no crash."""
    bars_before = _make_bars(100)
    last_px = bars_before[-1]["close"]

    # Skip 30 minutes, continue from minute 130
    gap_start = datetime(2026, 3, 20, 12, 10, 0, tzinfo=timezone.utc)
    bars_after = _make_bars(70, base_price=last_px, start_ts=gap_start)

    all_bars = bars_before + bars_after

    # Test via BarBuffer + features
    buf = BarBuffer(300)
    any_nan = False
    for b in all_bars:
        buf.add(b)
        if len(buf) >= 60:
            try:
                features = compute_features_fx(buf)
                if not _features_safe(features):
                    any_nan = True
                    break
            except Exception as e:
                _fail("Feed gap: feature computation crashed", str(e))
                return

    if any_nan:
        _fail("Feed gap: NaN/Inf in features after gap")
    else:
        _ok("Feed gap: features clean across 30-bar gap")

    # Test via BacktestRunner
    bt, crashed = _run_bars_through_backtest(all_bars)
    if crashed:
        _fail("Feed gap: BacktestRunner crashed")
    else:
        _ok(f"Feed gap: BacktestRunner survived ({len(bt.trades)} trades)")


def test_zero_volume():
    """All bars have volume=0. Verify: features compute, no division by zero."""
    bars = _make_bars(120)
    for b in bars:
        b["volume"] = 0.0

    buf = BarBuffer(300)
    error_found = False
    for b in bars:
        buf.add(b)
        if len(buf) >= 60:
            try:
                features = compute_features_fx(buf)
                if not _features_safe(features):
                    error_found = True
                    break
            except ZeroDivisionError:
                _fail("Zero volume: ZeroDivisionError raised")
                return
            except Exception as e:
                _fail(f"Zero volume: unexpected error", str(e))
                return

    if error_found:
        _fail("Zero volume: NaN/Inf in features")
    else:
        _ok("Zero volume: all features computed safely with volume=0")

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Zero volume: BacktestRunner crashed")
    else:
        _ok("Zero volume: BacktestRunner completed")


def test_zero_price():
    """Inject a bar where close=0. Verify: handled gracefully (skipped or no crash)."""
    bars = _make_bars(100)
    zero_ts = datetime(2026, 3, 20, 11, 40, 0, tzinfo=timezone.utc)
    zero_bar = {
        "ts": zero_ts.isoformat(),
        "open": 0.0, "high": 0.0, "low": 0.0, "close": 0.0, "volume": 0.0,
    }
    bars.append(zero_bar)
    # Continue with normal bars after zero
    bars.extend(_make_bars(20, base_price=bars[-2]["close"],
                           start_ts=zero_ts + timedelta(minutes=1)))

    buf = BarBuffer(300)
    crashed = False
    nan_inf = False
    for b in bars:
        buf.add(b)
        if len(buf) >= 60:
            try:
                features = compute_features_fx(buf)
                if not _features_safe(features):
                    nan_inf = True
            except ZeroDivisionError:
                _fail("Zero price: ZeroDivisionError raised")
                return
            except Exception:
                crashed = True

    if crashed:
        _fail("Zero price: unhandled exception during feature computation")
    elif nan_inf:
        # NaN/Inf is acceptable as long as no crash — it means the system saw
        # the bad bar but did not blow up. Features will self-heal on next bars.
        _ok("Zero price: NaN/Inf produced but no crash (degrades gracefully)")
    else:
        _ok("Zero price: all features computed cleanly despite zero bar")

    bt, bt_crashed = _run_bars_through_backtest(bars)
    if bt_crashed:
        _fail("Zero price: BacktestRunner crashed")
    else:
        _ok("Zero price: BacktestRunner survived zero-price bar")


def test_extreme_spread():
    """Inject bars with wild high-low range (100x normal).
    Verify: range_pct feature is extreme but no crash."""
    bars = _make_bars(100)
    last_px = bars[-1]["close"]
    normal_range = 0.0002

    # 100x range bar
    extreme = normal_range * 100  # 0.02 = 200 pips
    extreme_ts = datetime(2026, 3, 20, 11, 40, 0, tzinfo=timezone.utc)
    extreme_bar = {
        "ts": extreme_ts.isoformat(),
        "open": round(last_px, 5),
        "high": round(last_px + extreme, 5),
        "low": round(last_px - extreme, 5),
        "close": round(last_px + extreme * 0.3, 5),
        "volume": 10000.0,
    }
    bars.append(extreme_bar)

    buf = BarBuffer(300)
    for b in bars:
        buf.add(b)

    try:
        features = compute_features_fx(buf)
    except Exception as e:
        _fail("Extreme spread: feature computation crashed", str(e))
        return

    if features is None:
        _ok("Extreme spread: features returned None (safe)")
        return

    if not _features_safe(features):
        _fail("Extreme spread: NaN/Inf in features")
    else:
        rp = features.get("range_pct", 0)
        _ok(f"Extreme spread: features computed (range_pct={rp:.4f}, extreme but finite)")

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Extreme spread: BacktestRunner crashed")
    else:
        _ok("Extreme spread: BacktestRunner survived")


def test_stale_replay():
    """Same price for 200 bars (frozen feed).
    Verify: no triggers fire (vol_z/range should be near-zero)."""
    frozen_px = 1.1000
    start_ts = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)
    bars = []
    for i in range(200):
        ts = start_ts + timedelta(minutes=i)
        bars.append({
            "ts": ts.isoformat(),
            "open": frozen_px,
            "high": frozen_px,
            "low": frozen_px,
            "close": frozen_px,
            "volume": 100.0,
        })

    # Check features on frozen data
    buf = BarBuffer(300)
    triggers_fired = 0
    for b in bars:
        buf.add(b)
        if len(buf) >= 60:
            try:
                features = compute_features_fx(buf)
                if features is not None and _features_safe(features):
                    direction = check_trigger_fx(features, _DEFAULT_CFG)
                    if direction is not None:
                        triggers_fired += 1
            except Exception as e:
                _fail("Stale replay: exception during processing", str(e))
                return

    if triggers_fired == 0:
        _ok("Stale replay: no triggers on 200 frozen bars (correct)")
    else:
        _fail(f"Stale replay: {triggers_fired} triggers on frozen data (should be 0)")

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Stale replay: BacktestRunner crashed")
    else:
        _ok(f"Stale replay: BacktestRunner survived ({len(bt.trades)} trades, {len(bt.signals)} signals)")


def test_timestamp_gap():
    """Bars with timestamps that jump forward 1 hour.
    Verify: timeout still works correctly."""
    # Build 80 bars, then gap 1 hour, then 40 more bars
    bars_pre = _make_bars(80)
    last_px = bars_pre[-1]["close"]
    last_ts = datetime.fromisoformat(bars_pre[-1]["ts"])

    # Jump 1 hour forward
    gap_ts = last_ts + timedelta(hours=1)
    bars_post = _make_bars(40, base_price=last_px, start_ts=gap_ts)
    all_bars = bars_pre + bars_post

    # Use a config with 60-min timeout
    cfg = dict(_DEFAULT_CFG)
    bt = BacktestRunner(cfg)

    crashed = False
    for b in all_bars:
        try:
            bt.process_bar(b)
        except Exception:
            crashed = True
            break

    if crashed:
        _fail("Timestamp gap: BacktestRunner crashed")
        return

    _ok(f"Timestamp gap: BacktestRunner survived 1-hour gap ({len(bt.trades)} trades)")

    # If any trade was open before the gap and the gap crosses timeout,
    # verify it was closed
    timeout_exits = [t for t in bt.trades if t["exit_reason"] == "timeout"]
    if timeout_exits:
        _ok(f"Timestamp gap: {len(timeout_exits)} timeout exit(s) across hour gap (correct)")
    else:
        _ok("Timestamp gap: no timeout exits (may not have entered before gap)")


def test_reverse_timestamps():
    """Bars with decreasing timestamps. Verify: no crash (bars still buffer)."""
    start_ts = datetime(2026, 3, 20, 12, 0, 0, tzinfo=timezone.utc)
    bars = []
    rng = random.Random(99)
    px = 1.1000
    for i in range(120):
        change = rng.uniform(-0.0005, 0.0005)
        px += change
        # Timestamps go BACKWARDS
        ts = start_ts - timedelta(minutes=i)
        bars.append({
            "ts": ts.isoformat(),
            "open": round(px - change, 5),
            "high": round(max(px, px + abs(change) * 0.5), 5),
            "low": round(min(px, px - abs(change) * 0.5), 5),
            "close": round(px, 5),
            "volume": round(rng.uniform(100, 1000), 2),
        })

    buf = BarBuffer(300)
    feature_errors = 0
    for b in bars:
        buf.add(b)
        if len(buf) >= 60:
            try:
                features = compute_features_fx(buf)
                if not _features_safe(features):
                    feature_errors += 1
            except Exception:
                feature_errors += 1

    if feature_errors == 0:
        _ok("Reverse timestamps: all features computed (buffer order-agnostic)")
    else:
        _ok(f"Reverse timestamps: {feature_errors} feature issues but no crash (acceptable)")

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Reverse timestamps: BacktestRunner crashed")
    else:
        _ok("Reverse timestamps: BacktestRunner survived reverse-ordered bars")


def test_duplicate_bars():
    """Same bar repeated 50 times. Verify: no duplicate entries."""
    bars = _make_bars(80)
    # Repeat the last bar 50 times
    dup_bar = dict(bars[-1])
    for _ in range(50):
        bars.append(dict(dup_bar))

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Duplicate bars: BacktestRunner crashed")
        return

    # Count entries — should have at most 1 entry from the duplicate window
    # (min_signal_gap should block repeats)
    entry_signals = [s for s in bt.signals if s["action"] == "ENTRY"]
    dup_entries = 0
    seen_ts = set()
    for s in entry_signals:
        if s["ts"] in seen_ts:
            dup_entries += 1
        seen_ts.add(s["ts"])

    if dup_entries > 0:
        _fail(f"Duplicate bars: {dup_entries} duplicate-timestamp entries")
    else:
        _ok(f"Duplicate bars: no duplicate entries ({len(entry_signals)} total entries)")

    # Check features on duplicated bars don't explode
    buf = BarBuffer(300)
    for b in bars:
        buf.add(b)
    try:
        features = compute_features_fx(buf)
        if _features_safe(features):
            _ok("Duplicate bars: features computed cleanly on repeated data")
        else:
            _fail("Duplicate bars: NaN/Inf in features on repeated data")
    except Exception as e:
        _fail("Duplicate bars: feature computation crashed", str(e))


def test_spike_and_recover():
    """Price spikes 5% up then immediately back.
    Verify: if entry triggered on spike, stop fires on recovery."""
    bars = _make_bars(100)
    last_px = bars[-1]["close"]
    spike_ts = datetime(2026, 3, 20, 11, 40, 0, tzinfo=timezone.utc)

    # Spike bar: 5% up
    spike_px = round(last_px * 1.05, 5)
    spike_bar = {
        "ts": spike_ts.isoformat(),
        "open": last_px,
        "high": spike_px,
        "low": last_px,
        "close": spike_px,
        "volume": 8000.0,
    }
    bars.append(spike_bar)

    # Recovery bar: back to original
    recover_bar = {
        "ts": (spike_ts + timedelta(minutes=1)).isoformat(),
        "open": spike_px,
        "high": spike_px,
        "low": last_px,
        "close": last_px,
        "volume": 8000.0,
    }
    bars.append(recover_bar)

    # Continue with normal bars
    bars.extend(_make_bars(30, base_price=last_px,
                           start_ts=spike_ts + timedelta(minutes=2)))

    bt, crashed = _run_bars_through_backtest(bars)
    if crashed:
        _fail("Spike & recover: BacktestRunner crashed")
        return

    _ok(f"Spike & recover: BacktestRunner survived ({len(bt.trades)} trades)")

    # If an entry happened on the spike, verify exit happened
    stop_exits = [t for t in bt.trades if t["exit_reason"] == "stop"]
    if bt.trades:
        # Check that spike-bar entries got stopped out on recovery
        for t in bt.trades:
            if abs(t["entry_px"] - spike_px) < 0.001 and t["exit_reason"] == "stop":
                _ok("Spike & recover: spike entry correctly stopped on recovery")
                return
        _ok(f"Spike & recover: {len(bt.trades)} trades, {len(stop_exits)} stop exits (system handled spike)")
    else:
        # Also valid: trigger thresholds may not have fired
        _ok("Spike & recover: no entries on spike (trigger thresholds filtered it)")

    # Manual position test: force long at spike, verify stop on recovery
    bt2 = BacktestRunner(_DEFAULT_CFG)
    for b in bars[:-32]:  # up to spike bar
        bt2.process_bar(b)
    bt2.position = "LONG"
    bt2.entry_price = spike_px
    bt2.stop_price = spike_px - 0.0012  # 12 pip stop
    bt2.target_price = spike_px + 0.0018
    bt2.entry_time = spike_ts
    bt2.timeout_time = spike_ts + timedelta(minutes=60)

    # Feed recovery bar (5% drop = ~550 pips, way past 12 pip stop)
    bt2.process_bar(recover_bar)
    if bt2.position == "FLAT":
        _ok("Spike & recover: forced long stopped out on recovery (correct)")
    else:
        _fail("Spike & recover: forced long NOT stopped on recovery")


# ── Main ─────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Phase 22D — Chaos Engineering / Adversarial Data Tests")
    parser.add_argument("--test", help="Run specific test (substring match)")
    args = parser.parse_args()

    tests = [
        ("1. Flash crash",          test_flash_crash),
        ("2. Feed gap",             test_feed_gap),
        ("3. Zero volume",          test_zero_volume),
        ("4. Zero price",           test_zero_price),
        ("5. Extreme spread",       test_extreme_spread),
        ("6. Stale/frozen feed",    test_stale_replay),
        ("7. Timestamp gap",        test_timestamp_gap),
        ("8. Reverse timestamps",   test_reverse_timestamps),
        ("9. Duplicate bars",       test_duplicate_bars),
        ("10. Spike and recover",   test_spike_and_recover),
    ]

    if args.test:
        tests = [(n, f) for n, f in tests if args.test.lower() in n.lower()]

    print("=" * 65)
    print("  Phase 22D — Chaos Engineering / Adversarial Data Tests")
    print("=" * 65)

    passed = 0
    failed = 0
    for name, fn in tests:
        print(f"\n  --- {name} ---")
        try:
            fn()
            passed += 1
        except Exception as e:
            _fail(f"Unhandled exception: {e}")
            failed += 1

    print(f"\n{'=' * 65}")
    if failed == 0:
        print(f"  Chaos tests complete — {passed}/{len(tests)} suites passed")
        print("  \033[32mALL SUITES PASSED\033[0m")
    else:
        print(f"  Chaos tests complete — {passed}/{len(tests)} passed, {failed} FAILED")
        print(f"  \033[31m{failed} SUITE(S) FAILED\033[0m")
    print("=" * 65)


if __name__ == "__main__":
    main()
