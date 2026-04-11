"""FX System Core Component Tests — schemas, configs, BarBuffer, State, divergence guard.

Self-contained: no IBKR connection needed.

Usage:
    python -m argus_flow.tests.test_fx_system
    pytest argus_flow/tests/test_fx_system.py -v
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "configs"
TEST_TMP_ROOT = Path(__file__).resolve().parents[2] / ".tmp_fx_tests"

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
    """Load strategy config JSON files (excluding hashes.json and non-runner metadata)."""
    configs = []
    for p in sorted(CONFIGS_DIR.glob("*.json")):
        if p.name == "hashes.json":
            continue
        payload = json.loads(p.read_text(encoding="utf-8"))
        if "instrument_type" not in payload:
            continue
        configs.append((p.name, payload))
    return configs


def _new_test_dir(prefix: str) -> Path:
    TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TEST_TMP_ROOT / f"{prefix}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


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

    required = {"pnl_usd", "position_size", "risk_usd", "sizing_policy"}
    if required.issubset(set(pips)) and required.issubset(set(pts)):
        _ok("trade headers include sizing/dollar PnL fields")
    else:
        _fail("trade headers missing sizing/dollar fields", f"missing from pips={required - set(pips)} pts={required - set(pts)}")


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

    # Verify key positions by header name, not hard-coded offsets
    if row_fx[0] == features["ts"]:
        _ok("FX signal row[0] == ts")
    else:
        _fail("FX signal row[0] != ts", f"got {row_fx[0]}")

    session_idx = header_fx.index("session_id")
    if row_fx[session_idx] == "sess1":
        _ok("FX signal row[session_id] == session_id")
    else:
        _fail("FX signal row[session_id] != session_id", f"got {row_fx[session_idx]}")

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


def test_sizing_helpers():
    from argus_flow.sizing import fx_units_for_risk, futures_contracts_for_risk

    fx_units = fx_units_for_risk(
        equity_usd=10000,
        risk_pct=0.02,
        stop_pips=35,
        symbol="EURUSD",
    )
    if fx_units == 57000:
        _ok("fx_units_for_risk matches EURUSD cohort baseline")
    else:
        _fail("fx_units_for_risk baseline mismatch", f"got {fx_units}")

    fut_contracts = futures_contracts_for_risk(
        equity_usd=10000,
        risk_pct=0.02,
        entry_price=20000,
        stop_bps=30,
        multiplier=2,
    )
    if fut_contracts == 1:
        _ok("futures_contracts_for_risk sizes MNQ-like contract conservatively")
    else:
        _fail("futures_contracts_for_risk mismatch", f"got {fut_contracts}")


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


def test_fleet_registry_stage_defaults():
    from argus_flow.ops.fleet_registry import (
        STAGE_PAPER,
        STAGE_QUARANTINE,
        default_log_dir,
        infer_stage,
        normalize_stage,
        resolve_risk_policy,
        stage_account,
        stage_execution_mode,
    )

    gbp_path = CONFIGS_DIR / "gbpusd_range_paper_v1.json"
    cad_path = CONFIGS_DIR / "cadjpy_mtf_paper_v1.json"

    gbp_cfg = json.loads(gbp_path.read_text(encoding="utf-8"))
    cad_cfg = json.loads(cad_path.read_text(encoding="utf-8"))

    if infer_stage(gbp_cfg, gbp_path) == "paper":
        _ok("fleet_registry infers GBPUSD legacy paper stage")
    else:
        _fail("fleet_registry paper-stage inference failed for GBPUSD")

    if infer_stage(cad_cfg, cad_path) == "watcher":
        _ok("fleet_registry infers CADJPY managed watcher stage")
    else:
        _fail("fleet_registry watcher-stage inference failed for CADJPY")

    if default_log_dir("EURUSD", "real").endswith("live_eurusd"):
        _ok("fleet_registry uses stage-aware live log dir")
    else:
        _fail("fleet_registry live log dir mismatch", default_log_dir("EURUSD", "real"))

    risk = resolve_risk_policy(gbp_cfg, gbp_path, stage="paper")
    if abs(float(risk.get("active_risk_pct", 0.0)) - 0.005) < 1e-9:
        _ok("fleet_registry default active risk policy is 0.5%")
    else:
        _fail("fleet_registry risk policy mismatch", f"got {risk.get('active_risk_pct')}")

    if abs(float(risk.get("earned_cap_pct", 0.0)) - 0.03) < 1e-9:
        _ok("fleet_registry earned risk cap is 3.0%")
    else:
        _fail("fleet_registry earned cap mismatch", f"got {risk.get('earned_cap_pct')}")

    watcher_risk = resolve_risk_policy(cad_cfg, cad_path, stage="watcher")
    if abs(float(watcher_risk.get("active_risk_pct", -1.0)) - 0.0) < 1e-9:
        _ok("fleet_registry watcher stage is observe-only (0 active risk)")
    else:
        _fail("fleet_registry watcher active risk mismatch", f"got {watcher_risk.get('active_risk_pct')}")

    if normalize_stage("qa") == STAGE_PAPER and normalize_stage("quarantined") == STAGE_QUARANTINE:
        _ok("fleet_registry normalizes QA/prod stage aliases")
    else:
        _fail("fleet_registry stage alias normalization mismatch")

    if stage_account("watcher") == "observer" and stage_execution_mode("watcher") == "observe":
        _ok("fleet_registry exposes observe-only watcher execution metadata")
    else:
        _fail("fleet_registry watcher execution metadata mismatch")


def test_generate_live_config_promoted_runner_parse():
    from argus_flow.ops.generate_live_config import _get_promoted_pairs

    report = {
        "runners": [
            {"symbol": "GBPUSD", "verdict": "PROMOTE"},
            {"symbol": "EURUSD", "verdict": "NOT_READY"},
        ]
    }
    paths = _get_promoted_pairs(report)
    names = sorted(path.name for path in paths)
    if "gbpusd_range_paper_v1.json" in names and "eurusd_t4_paper_v1.json" not in names:
        _ok("generate_live_config reads promoted pairs from gate report runners[]")
    else:
        _fail("generate_live_config promoted pair parsing mismatch", str(names))


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


def test_runner_client_id_resolution():
    from argus_flow.runner_unified import resolve_client_id

    gbpusd_cfg = CONFIGS_DIR / "gbpusd_range_paper_v1.json"
    primary_group = [
        str(CONFIGS_DIR / "cadjpy_mtf_paper_v1.json"),
        str(CONFIGS_DIR / "audjpy_mtf_paper_v1.json"),
        str(CONFIGS_DIR / "usdjpy_mtf_paper_v1.json"),
    ]
    secondary_group = [
        str(CONFIGS_DIR / "gbpusd_range_paper_v1.json"),
        str(CONFIGS_DIR / "cadjpy_mtf_paper_v1.json"),
    ]
    expected_single_id = int(json.loads(gbpusd_cfg.read_text(encoding="utf-8")).get("ibkr_client_id", 0))

    single_id, single_source = resolve_client_id([str(gbpusd_cfg)], default_client_id=1)
    if single_id == expected_single_id and single_source.startswith("config:"):
        _ok("runner_unified uses config ibkr_client_id for single-config launches")
    else:
        _fail("single-config client ID resolution mismatch", f"got id={single_id} source={single_source}")

    primary_id, primary_source = resolve_client_id(primary_group, default_client_id=1)
    reversed_id, reversed_source = resolve_client_id(list(reversed(primary_group)), default_client_id=1)
    secondary_id, secondary_source = resolve_client_id(secondary_group, default_client_id=1)
    if (
        primary_source == "auto-group"
        and reversed_source == "auto-group"
        and secondary_source == "auto-group"
        and primary_id == reversed_id
        and primary_id != secondary_id
    ):
        _ok("runner_unified derives stable distinct client IDs for grouped launches")
    else:
        _fail(
            "grouped client ID resolution mismatch",
            (
                f"primary=({primary_id},{primary_source}) "
                f"reversed=({reversed_id},{reversed_source}) "
                f"secondary=({secondary_id},{secondary_source})"
            ),
        )


def test_trade_history_hydration_from_journal():
    from argus_flow.runner_unified import InstrumentRunner, State

    tmpdir = _new_test_dir("journal_hydrate")
    try:
        log_dir = tmpdir / "gbpusd"
        log_dir.mkdir(parents=True, exist_ok=True)

        state = State(log_dir / "state.json")
        state.position = "FLAT"
        state.trade_count = 0
        state.pnl_pips = 0.0
        state.pnl_usd = 0.0
        state.save()

        trade_log = log_dir / "trades.csv"
        trade_log.write_text(
            "\n".join(
                [
                    "ts,direction,entry_px,exit_px,pnl_pips,exit_reason,duration_min,trade_num,pnl_usd",
                    "2026-03-31T10:00:00Z,long,1.20000,1.20050,5.00,timeout,90.0,1,2.75",
                    "2026-03-31T11:00:00Z,short,1.20100,1.20000,10.00,target,20.0,2,5.50",
                ]
            ),
            encoding="utf-8",
        )

        runner = InstrumentRunner.__new__(InstrumentRunner)
        runner.trade_log = trade_log
        runner.uses_pips = True
        runner.state = state
        runner._hydrate_trade_history_from_journal()

        if runner.state.trade_count == 2 and abs(runner.state.pnl_pips - 15.0) < 1e-9 and abs(runner.state.pnl_usd - 8.25) < 1e-9:
            _ok("runner_unified hydrates trade count and realized PnL from journal on restart")
        else:
            _fail(
                "journal hydration mismatch",
                f"trade_count={runner.state.trade_count} pnl_pips={runner.state.pnl_pips} pnl_usd={runner.state.pnl_usd}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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

    tmpdir = _new_test_dir("state_roundtrip")
    try:
        state_file = tmpdir / "test_state.json"

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
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# 13. State missing file defaults to FLAT
# ═════════════════════════════════════════════════════════════

def test_state_missing_file():
    from argus_flow.runner_unified import State

    tmpdir = _new_test_dir("state_missing")
    try:
        state_file = tmpdir / "nonexistent_state.json"

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
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
# 14. Divergence guard RUNNERS match Class A pairs
# ═════════════════════════════════════════════════════════════

def test_broker_truth_helpers():
    from argus_flow.ops.broker_truth import atomic_write_json, is_fresh, read_json, runner_broker_state_path

    tmpdir = _new_test_dir("broker_truth")
    try:
        log_dir = tmpdir / "eurusd"
        path = runner_broker_state_path(log_dir)
        payload = {"broker": {"position": "LONG", "qty": 1000}}
        atomic_write_json(path, payload)

        loaded = read_json(path)
        if loaded == payload:
            _ok("broker_truth atomic_write_json/read_json roundtrip")
        else:
            _fail("broker_truth roundtrip mismatch", f"got {loaded}")

        if is_fresh(path, 60):
            _ok("broker_truth freshness check passes for fresh file")
        else:
            _fail("broker_truth freshness check failed for fresh file")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_evidence_registry_broker_truth():
    from argus_flow.ops.broker_truth import atomic_write_json
    from argus_flow.ops.evidence_registry import _build_broker_truth

    tmpdir = _new_test_dir("evidence_broker")
    try:
        payload = {
            "broker_connected": True,
            "account": {"net_liquidation_usd": 12500.0, "buying_power_usd": 48000.0},
            "broker": {"position": "LONG", "qty": 57000, "avg_cost": 1.085, "open_orders": [{}, {}]},
            "reconciliation": {"result": "CLEAN_OPEN_MATCHED", "detail": "Both agree"},
        }
        atomic_write_json(tmpdir / "broker_state.json", payload)
        section = _build_broker_truth(tmpdir)

        checks = [
            ("available", section.get("available"), True),
            ("broker_connected", section.get("broker_connected"), True),
            ("reconciliation_result", section.get("reconciliation_result"), "CLEAN_OPEN_MATCHED"),
            ("broker_position", section.get("broker_position"), "LONG"),
            ("open_orders", section.get("open_orders"), 2),
        ]
        all_ok = True
        for field, got, expected in checks:
            if got != expected:
                _fail(f"evidence_registry broker truth: {field}={got}, expected {expected}")
                all_ok = False
        if all_ok:
            _ok("evidence_registry broker truth section loads canonical broker_state.json")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_walkforward_summary_logic():
    from argus_flow.ops.walkforward_validation import build_fold_ranges, summarize_folds

    ranges = build_fold_ranges(total_bars=1500, folds=4, warmup_bars=300)
    if len(ranges) == 4 and ranges[0][0] == 300 and ranges[-1][1] == 1500:
        _ok("walkforward fold ranges partition data after warmup")
    else:
        _fail("walkforward fold range generation mismatch", f"got {ranges}")

    pass_summary = summarize_folds(
        [
            {"trades": 5, "expectancy": 1.2, "max_drawdown": 2.0, "profit_factor": 1.4, "total_pnl": 6.0},
            {"trades": 4, "expectancy": 0.8, "max_drawdown": 1.0, "profit_factor": 1.2, "total_pnl": 3.2},
            {"trades": 6, "expectancy": -0.2, "max_drawdown": 3.0, "profit_factor": 0.9, "total_pnl": -1.2},
            {"trades": 5, "expectancy": 0.4, "max_drawdown": 1.5, "profit_factor": 1.1, "total_pnl": 2.0},
        ],
        min_trades_per_fold=3,
        baseline_expectancy=0.5,
    )
    if pass_summary["status"] == "PASS" and pass_summary["positive_folds"] == 3:
        _ok("walkforward summary marks majority-positive folds as PASS")
    else:
        _fail("walkforward PASS classification mismatch", f"got {pass_summary}")

    fail_summary = summarize_folds(
        [
            {"trades": 5, "expectancy": -0.8, "max_drawdown": 2.0, "profit_factor": 0.8, "total_pnl": -4.0},
            {"trades": 5, "expectancy": -0.4, "max_drawdown": 1.5, "profit_factor": 0.7, "total_pnl": -2.0},
        ],
        min_trades_per_fold=3,
    )
    if fail_summary["status"] == "FAIL" and fail_summary["positive_folds"] == 0:
        _ok("walkforward summary marks all-negative folds as FAIL")
    else:
        _fail("walkforward FAIL classification mismatch", f"got {fail_summary}")


def test_walkforward_missing_data_report():
    import argus_flow.ops.walkforward_validation as walkforward_validation

    tmpdir = _new_test_dir("walkforward_missing")
    old_logs_dir = walkforward_validation.LOGS_DIR
    old_data_dir = walkforward_validation.DATA_DIR
    try:
        config_path = tmpdir / "m2k_range_paper_v1.json"
        config_path.write_text(
            json.dumps(
                {
                    "symbol": "M2K",
                    "strategy": "range_accel",
                    "instrument_type": "future",
                    "replay_expectations": {},
                }
            ),
            encoding="utf-8",
        )

        walkforward_validation.LOGS_DIR = tmpdir / "logs"
        walkforward_validation.DATA_DIR = tmpdir / "data"
        walkforward_validation.DATA_DIR.mkdir(parents=True, exist_ok=True)

        report, out_path = walkforward_validation.run_one(
            config_path=config_path,
            data_path=None,
            folds=4,
            warmup_bars=10,
            min_trades_per_fold=1,
            enable_pyramid=False,
        )

        if report["status"] == "MISSING_DATA" and out_path.exists():
            _ok("walkforward_validation writes explicit MISSING_DATA reports")
        else:
            _fail("walkforward missing-data report mismatch", f"report={report} out={out_path}")
    finally:
        walkforward_validation.LOGS_DIR = old_logs_dir
        walkforward_validation.DATA_DIR = old_data_dir
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_promotion_gate_v2_walkforward_checks():
    from argus_flow.ops.promotion_gate_v2 import check_live_drawdown_vs_walkforward, check_walk_forward_positive

    tmpdir = _new_test_dir("promotion_gate")
    try:
        missing = check_walk_forward_positive(tmpdir)
        if missing.classification == "unevidenced":
            _ok("promotion_gate_v2 treats missing walkforward report as unevidenced")
        else:
            _fail("promotion_gate_v2 missing walkforward classification mismatch", f"got {missing}")

        report = {
            "status": "PASS",
            "summary": {
                "status": "PASS",
                "rationale": "3/4 scored folds positive",
                "folds_scored": 4,
                "folds_total": 4,
                "positive_ratio": 0.75,
                "mean_expectancy": 0.8,
                "max_fold_drawdown": 10.0,
            },
        }
        (tmpdir / "walkforward_report.json").write_text(json.dumps(report), encoding="utf-8")

        passed = check_walk_forward_positive(tmpdir)
        if passed.passed and passed.classification == "hard":
            _ok("promotion_gate_v2 accepts PASS walkforward reports as hard-ready")
        else:
            _fail("promotion_gate_v2 PASS walkforward mismatch", f"got {passed}")

        valid_trades = [{"pnl_pips": "1.0"} for _ in range(30)] + [{"pnl_pips": "-0.4"} for _ in range(30)]
        dd_result = check_live_drawdown_vs_walkforward(valid_trades, tmpdir)
        if dd_result.passed:
            _ok("promotion_gate_v2 compares live drawdown against walkforward budget")
        else:
            _fail("promotion_gate_v2 live drawdown comparison failed unexpectedly", f"got {dd_result}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_shared_risk_progression_reaches_earned_cap():
    from argus_flow.ops.demotion_check import recommend_risk_pct

    risk_pct = recommend_risk_pct(
        n_trades=300,
        pf=1.40,
        max_dd=15.0,
        model_dd=10.0,
        is_quarantined=False,
    )
    if abs(risk_pct - 0.03) < 1e-9:
        _ok("shared risk progression reaches the 3.0% earned cap")
    else:
        _fail("shared risk progression earned-cap mismatch", f"got {risk_pct}")


def test_generate_live_config_gate_report_reader():
    from argus_flow.ops.generate_live_config import _check_promotion_gate

    passing_report = {"runners": [{"symbol": "EURUSD", "verdict": "PROMOTE"}]}
    if _check_promotion_gate("EURUSD", passing_report):
        _ok("generate_live_config reads PROMOTE verdicts from runner-based gate report")
    else:
        _fail("generate_live_config failed to read PROMOTE verdict")

    blocked_report = {"runners": [{"symbol": "EURUSD", "verdict": "BLOCKED"}]}
    if not _check_promotion_gate("EURUSD", blocked_report):
        _ok("generate_live_config blocks non-PROMOTE verdicts from runner-based gate report")
    else:
        _fail("generate_live_config allowed blocked verdict unexpectedly")


def test_managed_governance_runner_selection():
    from argus_flow.ops.artifact_divergence import governed_runners as artifact_runners
    from argus_flow.ops.divergence_guard import governed_runners as divergence_runners
    from argus_flow.ops.fleet_registry import STAGE_PAPER, STAGE_QUARANTINE, STAGE_REAL, STAGE_WATCHER, discover_managed_runners
    from argus_flow.ops.kill_discipline import governed_runners as kill_runners

    managed = discover_managed_runners()
    expected_promotion_symbols = sorted(
        runner["symbol"]
        for runner in managed
        if runner["current_stage"] in (STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE)
    )
    expected_divergence_symbols = sorted(
        runner["symbol"]
        for runner in managed
        if runner["current_stage"] in (STAGE_WATCHER, STAGE_PAPER, STAGE_REAL, STAGE_QUARANTINE)
    )
    expected_artifact_symbols = sorted(
        runner["symbol"]
        for runner in managed
        if runner.get("launch_enabled", True)
    )

    div_symbols = sorted(runner["symbol"] for runner in divergence_runners())
    kill_symbols = sorted(runner["symbol"] for runner in kill_runners())
    artifact_symbols = sorted(runner["symbol"] for runner in artifact_runners())

    if div_symbols == expected_divergence_symbols:
        _ok("divergence_guard follows managed watcher/QA/prod runners")
    else:
        _fail("divergence_guard managed-runner mismatch", f"got {div_symbols} expected {expected_divergence_symbols}")

    if kill_symbols == expected_promotion_symbols:
        _ok("kill_discipline follows managed paper/real runners")
    else:
        _fail("kill_discipline managed-runner mismatch", f"got {kill_symbols} expected {expected_promotion_symbols}")

    if artifact_symbols == expected_artifact_symbols:
        _ok("artifact_divergence follows all managed runners")
    else:
        _fail("artifact_divergence managed-runner mismatch", f"got {artifact_symbols} expected {expected_artifact_symbols}")


def test_alert_reason_fallback_from_flags():
    from argus_flow.ops.alert_escalation_v2 import _report_reason

    divergence_item = {
        "flags": ["SIGNAL_FREQ_KILL: 0.0/day vs replay 8.0/day (ratio 0.00)"],
        "metrics": {"days_observed": 1.02, "signals_per_day": 0.0, "replay_signals_per_day": 8.0},
    }
    divergence_reason = _report_reason(divergence_item, "KILL")
    if "SIGNAL_FREQ_KILL" in divergence_reason and "signals/day=0.0 vs replay=8.0" in divergence_reason:
        _ok("alert escalation derives divergence reasons from flags + metrics")
    else:
        _fail("alert escalation divergence reason fallback mismatch", divergence_reason)

    kill_item = {
        "flags": ["KILL_DRAWDOWN: 42.0 pips > 3.0x model (10.0)"],
        "metrics": {"valid_trades": 14},
    }
    kill_reason = _report_reason(kill_item, "KILL")
    if "KILL_DRAWDOWN" in kill_reason and "trades=14" in kill_reason:
        _ok("alert escalation derives kill-discipline reasons from flags + metrics")
    else:
        _fail("alert escalation kill-discipline reason fallback mismatch", kill_reason)


def test_managed_watchdog_task_target():
    register_tasks = Path(__file__).resolve().parents[2] / "ops" / "register_tasks.ps1"
    watchdog_managed = Path(__file__).resolve().parents[2] / "ops" / "watchdog_managed.ps1"

    if not watchdog_managed.exists():
        _fail("watchdog_managed.ps1 not found")
        return

    code = register_tasks.read_text(encoding="utf-8", errors="replace")
    if "watchdog_managed.ps1" in code:
        _ok("register_tasks points ArgusWatchdog at watchdog_managed.ps1")
    else:
        _fail("register_tasks still targets legacy watchdog")


def test_onboard_pair_percentage_normalization():
    from argus_flow.ops.onboard_pair import _evaluate_results

    passed, kills, _ = _evaluate_results({"win_rate": 10.0, "profit_factor": 1.2})
    if not passed and any("WR" in kill for kill in kills):
        _ok("onboard_pair normalizes numeric percentage-style win rates")
    else:
        _fail("onboard_pair win-rate normalization failed", f"passed={passed} kills={kills}")


def test_weekly_pair_onboarding_candidate_plan():
    from argus_flow.ops.weekly_pair_onboarding import build_candidate_plan

    candidates, skipped = build_candidate_plan(
        ["NZDUSD", "EURUSD", "USDCAD", "NZDUSD", "bad", ""],
        {"EURUSD", "AUDUSD"},
    )
    if candidates == ["NZDUSD", "USDCAD"] and skipped == ["EURUSD"]:
        _ok("weekly_pair_onboarding filters invalid/duplicate/existing candidates")
    else:
        _fail("weekly_pair_onboarding candidate plan mismatch", f"candidates={candidates} skipped={skipped}")


def test_weekly_pair_onboarding_config_render():
    from argus_flow.ops.weekly_pair_onboarding import build_final_candidate_config

    template_path = CONFIGS_DIR / "cadjpy_mtf_paper_v1.json"
    template_cfg = json.loads(template_path.read_text(encoding="utf-8"))
    config_name, cfg = build_final_candidate_config(
        template_cfg=template_cfg,
        template_path=template_path,
        symbol="USDCHF",
        stage="watcher",
        results={
            "win_rate": 54.2,
            "expectancy": 1.11,
            "total_entries": 42,
            "stop_rate": 20.0,
            "target_rate": 10.0,
            "timeout_rate": 70.0,
        },
    )

    deployment = cfg.get("deployment", {}) if isinstance(cfg.get("deployment", {}), dict) else {}
    replay = cfg.get("replay_expectations", {}) if isinstance(cfg.get("replay_expectations", {}), dict) else {}
    if (
        config_name == "usdchf_mtf_paper_v1.json"
        and cfg.get("symbol") == "USDCHF"
        and cfg.get("stage") == "watcher"
        and deployment.get("managed") is True
        and deployment.get("stage") == "watcher"
        and abs(float(replay.get("win_rate", 0.0)) - 0.542) < 1e-9
    ):
        _ok("weekly_pair_onboarding renders managed watcher configs from template + results")
    else:
        _fail(
            "weekly_pair_onboarding config render mismatch",
            f"name={config_name} deployment={deployment} replay={replay}",
        )


def test_weekly_pair_onboarding_top_five_selection():
    from argus_flow.ops.weekly_pair_onboarding import select_top_candidates

    candidates = []
    for idx, score in enumerate([1.0, 7.0, 3.5, 9.0, 4.0, 8.0, 2.0], start=1):
        candidates.append(
            {
                "symbol": f"AA{idx:02d}BB",
                "status": "PASS",
                "score": score,
                "walkforward_summary": {"mean_expectancy": score / 10.0},
                "backtest": {"expectancy": score / 20.0},
            }
        )
    candidates.append({"symbol": "FAILME", "status": "FAIL", "score": 100.0})

    winners = select_top_candidates(candidates, 5)
    winner_symbols = [item["symbol"] for item in winners]
    if winner_symbols == ["AA04BB", "AA06BB", "AA02BB", "AA05BB", "AA03BB"]:
        _ok("weekly_pair_onboarding keeps only the top five passing candidates")
    else:
        _fail("weekly_pair_onboarding top-five selection mismatch", f"got {winner_symbols}")


def test_register_tasks_has_weekly_pair_onboarding():
    register_tasks = Path(__file__).resolve().parents[2] / "ops" / "register_tasks.ps1"
    code = register_tasks.read_text(encoding="utf-8", errors="replace")
    if "ArgusWeeklyPairOnboarding" in code and "run_weekly_pair_onboarding.ps1" in code:
        _ok("register_tasks includes the Friday pair-onboarding job")
    else:
        _fail("register_tasks missing weekly pair onboarding task")


def test_artifact_divergence_uses_futures_points_field():
    from argus_flow.ops.artifact_divergence import check_runner

    tmpdir = _new_test_dir("artifact_div")
    try:
        log_dir = tmpdir / "mgc"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "state.json").write_text(
            json.dumps(
                {
                    "position": "FLAT",
                    "trade_count": 11,
                    "pnl_pips": 0.0,
                    "pnl_points": -11.5,
                }
            ),
            encoding="utf-8",
        )
        (log_dir / "trades.csv").write_text(
            "\n".join(
                [
                    "ts,direction,entry_px,exit_px,pnl_pts,pnl_usd,exit_reason,duration_min,trade_num,experiment_valid,invalid_reason,config_hash,session_id,runtime_epoch,git_sha",
                    "2026-03-30T11:54:54.998177+00:00,short,4571.50,4590.80,-11.50,-115.00,stop,17.9,11,true,,hash,session,123,sha",
                ]
            ),
            encoding="utf-8",
        )
        (log_dir / "signals.csv").write_text("ts,action\n2026-03-31T00:00:00+00:00,HOLD\n", encoding="utf-8")
        (log_dir / "heartbeat.json").write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
        (log_dir / "evidence_registry.json").write_text(json.dumps({"cohort": {"valid_trade_count": 1, "total_trade_count": 1}}), encoding="utf-8")

        cfg_path = tmpdir / "mgc_range_paper_v1.json"
        cfg_path.write_text("{}", encoding="utf-8")
        config_hash_dir = tmpdir / "argus_flow" / "configs"
        config_hash_dir.mkdir(parents=True, exist_ok=True)
        config_hash_file = config_hash_dir / "hashes.json"
        config_hash_file.write_text(
            json.dumps(
                {
                    cfg_path.name: hashlib.sha256(cfg_path.read_text(encoding="utf-8").encode()).hexdigest()[:16]
                }
            ),
            encoding="utf-8",
        )

        import argus_flow.ops.artifact_divergence as artifact_divergence

        old_repo = artifact_divergence.REPO
        artifact_divergence.REPO = tmpdir
        try:
            result = check_runner(
                {
                    "name": "MGC",
                    "symbol": "MGC",
                    "log_dir": "mgc",
                    "config": "mgc_range_paper_v1.json",
                    "pip_tolerance": 0.1,
                    "instrument_type": "future",
                }
            )
        finally:
            artifact_divergence.REPO = old_repo

        pnl_checks = [c for c in result["checks"] if c["name"] == "pnl_match"]
        if pnl_checks and pnl_checks[0]["passed"]:
            _ok("artifact_divergence uses pnl_points for futures state checks")
        else:
            _fail("artifact_divergence futures pnl-field selection mismatch", str(pnl_checks))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_discord_alerts_follow_managed_runners():
    from argus_flow.ops.discord_alerts import notification_runners
    from argus_flow.ops.fleet_registry import discover_managed_runners

    expected = sorted(
        runner["symbol"]
        for runner in discover_managed_runners()
        if runner.get("launch_enabled", True)
    )
    actual = sorted(runner["symbol"] for runner in notification_runners())
    if actual == expected:
        _ok("discord_alerts follows managed runner registry")
    else:
        _fail("discord_alerts managed-runner mismatch", f"got {actual} expected {expected}")


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
        ("5.  sizing helpers", test_sizing_helpers),
        ("6.  Config required fields", test_config_required_fields),
        ("7.  Config hash integrity", test_config_hash_integrity),
        ("8.  Unique ibkr_client_ids", test_config_unique_client_ids),
        ("9.  runner client-id resolution", test_runner_client_id_resolution),
        ("10. journal trade-history hydration", test_trade_history_hydration_from_journal),
        ("11. replay_expectations present", test_config_replay_expectations),
        ("12. Cohort validity flags", test_cohort_validity_flags),
        ("13. BarBuffer basic", test_bar_buffer_basic),
        ("14. BarBuffer overflow", test_bar_buffer_overflow),
        ("15. State save/load roundtrip", test_state_roundtrip),
        ("16. State missing file -> FLAT", test_state_missing_file),
        ("17. Broker truth helpers", test_broker_truth_helpers),
        ("18. Evidence registry broker truth", test_evidence_registry_broker_truth),
        ("19. Walkforward summary logic", test_walkforward_summary_logic),
        ("20. Walkforward missing-data reporting", test_walkforward_missing_data_report),
        ("21. Promotion gate v2 walkforward checks", test_promotion_gate_v2_walkforward_checks),
        ("22. shared risk progression cap", test_shared_risk_progression_reaches_earned_cap),
        ("23. generate_live_config gate report reader", test_generate_live_config_gate_report_reader),
        ("24. managed governance runner selection", test_managed_governance_runner_selection),
        ("25. alert reason fallback", test_alert_reason_fallback_from_flags),
        ("26. managed watchdog task target", test_managed_watchdog_task_target),
        ("27. onboard_pair percentage normalization", test_onboard_pair_percentage_normalization),
        ("28. weekly onboarding candidate plan", test_weekly_pair_onboarding_candidate_plan),
        ("29. weekly onboarding config render", test_weekly_pair_onboarding_config_render),
        ("30. weekly onboarding top five", test_weekly_pair_onboarding_top_five_selection),
        ("31. register_tasks weekly onboarding task", test_register_tasks_has_weekly_pair_onboarding),
        ("32. artifact divergence futures pnl field", test_artifact_divergence_uses_futures_points_field),
        ("33. discord alerts managed runner selection", test_discord_alerts_follow_managed_runners),
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
