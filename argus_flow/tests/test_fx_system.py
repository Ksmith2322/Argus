"""FX System Core Component Tests — schemas, configs, BarBuffer, State, divergence guard.

Self-contained: no IBKR connection needed.

Usage:
    python -m argus_flow.tests.test_fx_system
    pytest argus_flow/tests/test_fx_system.py -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"

_pass_count = 0
_fail_count = 0


def _ok(label):
    global _pass_count
    _pass_count += 1
    print(f"  \033[32m[PASS]\033[0m {label}")


def _fail(label, detail=""):
    global _fail_count
    _fail_count += 1
    print(f"  \033[31m[FAIL]\033[0m {label}" + (f" — {detail}" if detail else ""))


# ── Helpers ───────────────────────────────────────────────────

def _load_configs() -> list[tuple[str, dict]]:
    """Load all config JSON files (excluding hashes.json)."""
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        if p.name == "hashes.json":
            continue
        configs.append((p.name, json.loads(p.read_text(encoding="utf-8"))))
    return configs


# ═════════════════════════════════════════════════════════════
# 1. Signal header dispatch
# ═════════════════════════════════════════════════════════════

def test_schema_signal_headers():
    from argus_flow.schemas import signal_header, SIGNAL_FIELDS_FX, SIGNAL_FIELDS_FUTURES

    fx = signal_header("forex")
    fut = signal_header("future")

    if fx == SIGNAL_FIELDS_FX:
        _ok("signal_header('forex') == SIGNAL_FIELDS_FX")
    else:
        _fail("signal_header('forex') mismatch", f"got {fx}")

    if fut == SIGNAL_FIELDS_FUTURES:
        _ok("signal_header('future') == SIGNAL_FIELDS_FUTURES")
    else:
        _fail("signal_header('future') mismatch", f"got {fut}")

    # Ensure returns are copies, not the original list
    fx.append("junk")
    if len(signal_header("forex")) == len(SIGNAL_FIELDS_FX):
        _ok("signal_header returns a copy (mutation-safe)")
    else:
        _fail("signal_header returns the original list (mutation risk)")


# ═════════════════════════════════════════════════════════════
# 2. Trade header dispatch
# ═════════════════════════════════════════════════════════════

def test_schema_trade_headers():
    from argus_flow.schemas import trade_header, TRADE_FIELDS_PIPS, TRADE_FIELDS_POINTS

    pips = trade_header(True)
    pts = trade_header(False)

    if pips == TRADE_FIELDS_PIPS:
        _ok("trade_header(True) == TRADE_FIELDS_PIPS")
    else:
        _fail("trade_header(True) mismatch", f"got {pips}")

    if pts == TRADE_FIELDS_POINTS:
        _ok("trade_header(False) == TRADE_FIELDS_POINTS")
    else:
        _fail("trade_header(False) mismatch", f"got {pts}")


# ═════════════════════════════════════════════════════════════
# 3. build_signal_row length and order
# ═════════════════════════════════════════════════════════════

def test_schema_build_signal_row():
    from argus_flow.schemas import build_signal_row, signal_header

    features = {
        "ts": "2026-03-24T12:00:00Z",
        "price": 1.0850,
        "range_pct": 0.001234,
        "vol_z": 1.5000,
        "range_accel": 0.0023,
        "dist_from_low": 0.3500,
        "hour": 14,
    }

    row_fx = build_signal_row(features, "long", "ENTRY", "forex", "abc123", "sess1")
    header_fx = signal_header("forex")

    if len(row_fx) == len(header_fx):
        _ok(f"FX signal row length matches header ({len(row_fx)} fields)")
    else:
        _fail(f"FX signal row length {len(row_fx)} != header {len(header_fx)}")

    # Verify key positions: first field is ts, last is session_id
    if row_fx[0] == features["ts"]:
        _ok("FX signal row[0] == ts")
    else:
        _fail("FX signal row[0] != ts", f"got {row_fx[0]}")

    if row_fx[-1] == "sess1":
        _ok("FX signal row[-1] == session_id")
    else:
        _fail("FX signal row[-1] != session_id", f"got {row_fx[-1]}")

    # Futures row has extra vol_burst_z field
    features["vol_burst_z"] = 2.1
    row_fut = build_signal_row(features, "short", "ENTRY", "future", "def456", "sess2")
    header_fut = signal_header("future")

    if len(row_fut) == len(header_fut):
        _ok(f"Futures signal row length matches header ({len(row_fut)} fields)")
    else:
        _fail(f"Futures signal row length {len(row_fut)} != header {len(header_fut)}")


# ═════════════════════════════════════════════════════════════
# 4. VALIDITY_FIELDS subset of both trade headers
# ═════════════════════════════════════════════════════════════

def test_schema_validity_fields_present():
    from argus_flow.schemas import VALIDITY_FIELDS, TRADE_FIELDS_PIPS, TRADE_FIELDS_POINTS

    vset = set(VALIDITY_FIELDS)

    if vset.issubset(set(TRADE_FIELDS_PIPS)):
        _ok("VALIDITY_FIELDS subset of TRADE_FIELDS_PIPS")
    else:
        missing = vset - set(TRADE_FIELDS_PIPS)
        _fail("VALIDITY_FIELDS not subset of TRADE_FIELDS_PIPS", f"missing: {missing}")

    if vset.issubset(set(TRADE_FIELDS_POINTS)):
        _ok("VALIDITY_FIELDS subset of TRADE_FIELDS_POINTS")
    else:
        missing = vset - set(TRADE_FIELDS_POINTS)
        _fail("VALIDITY_FIELDS not subset of TRADE_FIELDS_POINTS", f"missing: {missing}")


# ═════════════════════════════════════════════════════════════
# 5. Config required fields
# ═════════════════════════════════════════════════════════════

def test_config_required_fields():
    required = {"strategy", "version", "symbol", "instrument_type",
                "trigger", "risk", "direction", "replay_expectations"}
    configs = _load_configs()

    if not configs:
        _fail("No config files found in configs/")
        return

    all_ok = True
    for name, cfg in configs:
        missing = required - set(cfg.keys())
        if missing:
            _fail(f"{name}: missing required fields {missing}")
            all_ok = False

    if all_ok:
        _ok(f"All {len(configs)} configs have required fields")


# ═════════════════════════════════════════════════════════════
# 6. Config hash integrity
# ═════════════════════════════════════════════════════════════

def test_config_hash_integrity():
    hashes_path = CONFIGS_DIR / "hashes.json"
    if not hashes_path.exists():
        _fail("hashes.json not found")
        return

    expected = json.loads(hashes_path.read_text(encoding="utf-8"))
    configs = _load_configs()

    mismatches = []
    missing_entries = []
    for name, _ in configs:
        cfg_path = CONFIGS_DIR / name
        # Must match config_check.py / runner_unified.py: read_text().encode()
        raw = cfg_path.read_text(encoding="utf-8").encode()
        computed = hashlib.sha256(raw).hexdigest()[:16]

        if name not in expected:
            missing_entries.append(name)
            continue
        if computed != expected[name]:
            mismatches.append((name, expected[name], computed))

    if missing_entries:
        _fail(f"Configs missing from hashes.json: {missing_entries}")
    elif mismatches:
        for name, exp, got in mismatches:
            _fail(f"{name}: hash mismatch (expected {exp}, got {got})")
    else:
        _ok(f"All {len(configs)} config hashes match hashes.json")


# ═════════════════════════════════════════════════════════════
# 7. Unique client IDs
# ═════════════════════════════════════════════════════════════

def test_config_unique_client_ids():
    configs = _load_configs()
    seen: dict[int, str] = {}
    dupes = []

    for name, cfg in configs:
        cid = cfg.get("ibkr_client_id")
        if cid is None:
            continue
        if cid in seen:
            dupes.append((cid, seen[cid], name))
        else:
            seen[cid] = name

    if dupes:
        for cid, first, second in dupes:
            _fail(f"Duplicate ibkr_client_id={cid}: {first} and {second}")
    else:
        _ok(f"All {len(seen)} ibkr_client_ids are unique")


# ═════════════════════════════════════════════════════════════
# 8. Config replay_expectations
# ═════════════════════════════════════════════════════════════

def test_config_replay_expectations():
    configs = _load_configs()
    required_keys = {"signals_per_day", "win_rate"}
    all_ok = True

    for name, cfg in configs:
        re = cfg.get("replay_expectations")
        if re is None:
            _fail(f"{name}: missing replay_expectations")
            all_ok = False
            continue
        missing = required_keys - set(re.keys())
        if missing:
            _fail(f"{name}: replay_expectations missing {missing}")
            all_ok = False

    if all_ok:
        _ok(f"All {len(configs)} configs have replay_expectations with signals_per_day and win_rate")


# ═════════════════════════════════════════════════════════════
# 9. Cohort validity flags
# ═════════════════════════════════════════════════════════════

def test_cohort_validity_flags():
    from argus_flow.schemas import VALIDITY_FIELDS

    expected = [
        "experiment_valid", "invalid_reason",
        "config_hash", "session_id", "runtime_epoch", "git_sha",
    ]

    if VALIDITY_FIELDS == expected:
        _ok(f"VALIDITY_FIELDS matches expected ({len(expected)} fields)")
    else:
        _fail("VALIDITY_FIELDS mismatch", f"got {VALIDITY_FIELDS}, expected {expected}")


# ═════════════════════════════════════════════════════════════
# 10. BarBuffer basic operations
# ═════════════════════════════════════════════════════════════

def test_bar_buffer_basic():
    from argus_flow.runner_unified import BarBuffer

    buf = BarBuffer(300)

    for i in range(100):
        buf.add({
            "ts": f"2026-03-24T10:{i % 60:02d}:00",
            "open": 1.0850 + i * 0.0001,
            "high": 1.0855 + i * 0.0001,
            "low": 1.0845 + i * 0.0001,
            "close": 1.0852 + i * 0.0001,
            "volume": 100 + i,
        })

    if len(buf) == 100:
        _ok(f"BarBuffer length after 100 adds: {len(buf)}")
    else:
        _fail(f"BarBuffer length expected 100, got {len(buf)}")

    df = buf.to_df()
    expected_cols = {"ts", "open", "high", "low", "close", "volume"}
    if expected_cols.issubset(set(df.columns)):
        _ok("BarBuffer.to_df() has expected OHLCV columns")
    else:
        _fail("BarBuffer.to_df() missing columns", f"got {list(df.columns)}")

    if len(df) == 100:
        _ok("BarBuffer.to_df() returns DataFrame with correct row count")
    else:
        _fail(f"BarBuffer.to_df() row count expected 100, got {len(df)}")


# ═════════════════════════════════════════════════════════════
# 11. BarBuffer overflow trim
# ═════════════════════════════════════════════════════════════

def test_bar_buffer_overflow():
    from argus_flow.runner_unified import BarBuffer

    buf = BarBuffer(300)

    for i in range(400):
        buf.add({
            "ts": f"2026-03-24T{(i // 60) % 24:02d}:{i % 60:02d}:00",
            "open": 1.0850,
            "high": 1.0855,
            "low": 1.0845,
            "close": 1.0852,
            "volume": i,
        })

    if len(buf) == 300:
        _ok("BarBuffer overflow: length capped at 300")
    else:
        _fail(f"BarBuffer overflow: expected 300, got {len(buf)}")

    # Verify oldest bars were trimmed (first bar should be i=100)
    df = buf.to_df()
    if df.iloc[0]["volume"] == 100:
        _ok("BarBuffer overflow: oldest bars trimmed correctly")
    else:
        _fail(f"BarBuffer overflow: first bar volume={df.iloc[0]['volume']}, expected 100")


# ═════════════════════════════════════════════════════════════
# 12. State save/load roundtrip
# ═════════════════════════════════════════════════════════════

def test_state_roundtrip():
    from argus_flow.runner_unified import State

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "test_state.json"

        s = State(state_file)
        s.position = "LONG"
        s.entry_price = 1.08550
        s.stop_price = 1.08350
        s.target_price = 1.08950
        s.trade_count = 7
        s.pnl_pips = 42.5
        s.pnl_points = 0.0
        s.save()

        s2 = State(state_file)
        s2.load("TEST")

        checks = [
            ("position", s2.position, "LONG"),
            ("entry_price", s2.entry_price, 1.08550),
            ("stop_price", s2.stop_price, 1.08350),
            ("target_price", s2.target_price, 1.08950),
            ("trade_count", s2.trade_count, 7),
            ("pnl_pips", s2.pnl_pips, 42.5),
        ]

        all_ok = True
        for field, got, expected in checks:
            if got != expected:
                _fail(f"State roundtrip: {field}={got}, expected {expected}")
                all_ok = False

        if all_ok:
            _ok("State roundtrip: all fields match after save/load")


# ═════════════════════════════════════════════════════════════
# 13. State missing file defaults to FLAT
# ═════════════════════════════════════════════════════════════

def test_state_missing_file():
    from argus_flow.runner_unified import State

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / "nonexistent_state.json"

        s = State(state_file)
        s.load("TEST")

        if s.position == "FLAT":
            _ok("State missing file: defaults to FLAT")
        else:
            _fail(f"State missing file: position={s.position}, expected FLAT")

        if s.trade_count == 0:
            _ok("State missing file: trade_count=0")
        else:
            _fail(f"State missing file: trade_count={s.trade_count}, expected 0")


# ═════════════════════════════════════════════════════════════
# 14. Divergence guard RUNNERS match Class A pairs
# ═════════════════════════════════════════════════════════════

def test_divergence_guard_runners_match_cohort():
    guard_path = Path(__file__).resolve().parents[1] / "ops" / "divergence_guard.py"
    if not guard_path.exists():
        _fail("divergence_guard.py not found")
        return

    code = guard_path.read_text(encoding="utf-8")

    # Extract symbols from RUNNERS list
    expected_symbols = {"EURUSD", "GBPUSD", "EURJPY"}
    found_symbols = set()

    # Parse RUNNERS block: look for "symbol": "XXX" entries
    in_runners = False
    for line in code.split("\n"):
        stripped = line.strip()
        if "RUNNERS" in stripped and "=" in stripped and "[" in stripped:
            in_runners = True
            continue
        if in_runners and "]" in stripped:
            break
        if in_runners and '"symbol"' in stripped:
            # Extract value: "symbol": "EURUSD"
            parts = stripped.split('"symbol"')
            if len(parts) > 1:
                rest = parts[1]
                # Find the quoted value after the colon
                start = rest.find('"')
                if start >= 0:
                    end = rest.find('"', start + 1)
                    if end > start:
                        found_symbols.add(rest[start + 1:end])

    if found_symbols == expected_symbols:
        _ok(f"divergence_guard RUNNERS matches Class A pairs: {sorted(found_symbols)}")
    elif not found_symbols:
        _fail("Could not parse RUNNERS symbols from divergence_guard.py")
    else:
        _fail(f"RUNNERS mismatch: found {sorted(found_symbols)}, expected {sorted(expected_symbols)}")


# ═════════════════════════════════════════════════════════════
# Main — test runner
# ═════════════════════════════════════════════════════════════

def main():
    global _pass_count, _fail_count
    _pass_count = 0
    _fail_count = 0

    print("=" * 60)
    print("  FX System Core Component Tests")
    print("=" * 60)

    tests = [
        ("1.  Signal header dispatch", test_schema_signal_headers),
        ("2.  Trade header dispatch", test_schema_trade_headers),
        ("3.  build_signal_row length/order", test_schema_build_signal_row),
        ("4.  VALIDITY_FIELDS in trade headers", test_schema_validity_fields_present),
        ("5.  Config required fields", test_config_required_fields),
        ("6.  Config hash integrity", test_config_hash_integrity),
        ("7.  Unique ibkr_client_ids", test_config_unique_client_ids),
        ("8.  replay_expectations present", test_config_replay_expectations),
        ("9.  Cohort validity flags", test_cohort_validity_flags),
        ("10. BarBuffer basic", test_bar_buffer_basic),
        ("11. BarBuffer overflow", test_bar_buffer_overflow),
        ("12. State save/load roundtrip", test_state_roundtrip),
        ("13. State missing file -> FLAT", test_state_missing_file),
        ("14. Divergence guard RUNNERS", test_divergence_guard_runners_match_cohort),
    ]

    for name, fn in tests:
        print(f"\n  --- {name} ---")
        try:
            fn()
        except Exception as e:
            _fail(f"Unhandled: {e}")

    print(f"\n{'=' * 60}")
    total = _pass_count + _fail_count
    if _fail_count == 0:
        print(f"  \033[32mAll {_pass_count} checks passed.\033[0m")
    else:
        print(f"  {_pass_count}/{total} passed, \033[31m{_fail_count} FAILED\033[0m")
    print("=" * 60)

    return _fail_count == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
