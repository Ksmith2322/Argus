#!/usr/bin/env python3
"""
ops/test_phase16_ops.py  --  Phase 16 Acceptance Test Harness

Validates all Phase 16 unattended operations modules end-to-end:
  1. Runtime mode transitions, persistence, escalation-only, action gating
  2. Health checks with auto-escalation to safe modes
  3. Auto-throttle on consecutive losses and slippage anomalies
  4. Artifact integrity detection (truncation, missing files, hash verification)
  5. Run manifest creation, config hash, config drift detection
  6. Invariant engine (position/fill match, duplicate IDs, linkage, timestamps)
  7. Alert throttle deduplication
  8. Backup & restore drill

All tests use temporary directories — no side effects on real artifacts.

Usage:
    C:\\Argus\\.venv\\Scripts\\python.exe ops\\test_phase16_ops.py
"""

import json
import os
import shutil
import sys
import time
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Tuple

# Ensure repo root on sys.path
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import runtime_mode
from ops.health import (
    check_feed_staleness,
    check_fill_latency,
    check_consecutive_losses,
    check_slippage_anomaly,
    check_invariant,
    run_health_checks,
    HealthResult,
    HealthReport,
)
from ops.invariants import (
    check_position_matches_fills,
    check_no_duplicate_fill_ids,
    check_order_fill_linkage,
    check_no_duplicate_lifecycle_closure,
    check_timestamp_ordering,
    check_cash_exposure_limits,
    run_invariant_checks,
)
from ops.auto_throttle import (
    compute_multiplier,
    record_trade_result,
    apply_throttle_to_qty,
    load_throttle_state,
    save_throttle_state,
)
from ops.run_manifest import (
    config_hash,
    code_hash,
    create_manifest,
    save_manifest,
    load_manifest,
    finalize_manifest,
    check_config_drift,
)
from ops.artifact_integrity import (
    file_hash,
    file_row_count,
    compute_artifact_hashes,
    save_integrity_manifest,
    load_integrity_manifest,
    verify_artifact_integrity,
    check_csv_not_truncated,
    check_fills_csv_integrity,
)
from ops.alerting import AlertThrottle
from ops.backup_restore import run_backup, run_restore_drill, run_full_cycle, cleanup_old_backups


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_PASS = 0
_FAIL = 0
_TESTS: List[str] = []
_TMP_ROOT = os.path.join(_REPO_ROOT, ".tmp_phase16")


def _test(name: str, condition: bool, detail: str = "") -> bool:
    global _PASS, _FAIL
    status = "PASS" if condition else "FAIL"
    _TESTS.append(f"  [{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if condition:
        _PASS += 1
    else:
        _FAIL += 1
        print(f"  [FAIL] {name} -- {detail}")
    return condition


def _make_tmpdir() -> str:
    os.makedirs(_TMP_ROOT, exist_ok=True)
    d = os.path.join(_TMP_ROOT, f"argus_p16_test_{uuid.uuid4().hex[:8]}")
    os.makedirs(d, exist_ok=True)
    return d


def _cleanup(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _safe_remove(path: str) -> None:
    for _ in range(3):
        try:
            os.remove(path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.remove(path)


# ---------------------------------------------------------------------------
# 1. Runtime Mode Tests
# ---------------------------------------------------------------------------

def test_runtime_mode() -> None:
    print("\n=== 1. Runtime Mode ===")
    tmp = _make_tmpdir()
    mode_file = os.path.join(tmp, "runtime_mode.json")

    try:
        # 1a: Default mode is FULL when no file exists
        mode, meta = runtime_mode.load_mode(mode_file)
        _test("default_mode_is_FULL", mode == "FULL")

        # 1b: Save and reload mode
        runtime_mode.save_mode("NO_NEW_ENTRY", reason="test", triggered_by="harness", mode_file=mode_file)
        mode, meta = runtime_mode.load_mode(mode_file)
        _test("save_and_reload_mode", mode == "NO_NEW_ENTRY")
        _test("mode_metadata_reason", meta.get("reason") == "test")

        # 1c: Escalation only (more restrictive)
        changed, current = runtime_mode.escalate(
            "OBSERVATION_ONLY", reason="escalate_test", triggered_by="harness", mode_file=mode_file
        )
        _test("escalation_to_more_restrictive", changed and current == "OBSERVATION_ONLY")

        # 1d: Escalation blocked (less restrictive)
        changed, current = runtime_mode.escalate(
            "REDUCE_ONLY", reason="should_block", triggered_by="harness", mode_file=mode_file
        )
        _test("escalation_blocked_less_restrictive", not changed and current == "OBSERVATION_ONLY")

        # 1e: Reset to FULL (explicit operator reset)
        runtime_mode.reset_to_full(reason="operator_reset", triggered_by="harness", mode_file=mode_file)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("reset_to_full", mode == "FULL")

        # 1f: Transition with audit trail
        changed, prev, new = runtime_mode.transition(
            "REDUCE_ONLY", reason="audit_test", triggered_by="harness", mode_file=mode_file
        )
        _test("transition_records_prev", prev == "FULL" and new == "REDUCE_ONLY" and changed)

        # 1g: Check history file exists
        history_path = mode_file.replace(".json", "_history.jsonl")
        _test("history_file_created", os.path.exists(history_path))

        # 1h: History contains transition record
        with open(history_path, "r") as f:
            records = [json.loads(line) for line in f if line.strip()]
        _test("history_has_records", len(records) >= 1)
        _test("history_record_correct",
              records[-1].get("prev_mode") == "FULL" and records[-1].get("new_mode") == "REDUCE_ONLY")

        # 1i: Action gating
        allowed, reason = runtime_mode.gate_action("BUY", "FULL")
        _test("gate_buy_allowed_in_FULL", allowed)

        allowed, reason = runtime_mode.gate_action("BUY", "NO_NEW_ENTRY")
        _test("gate_buy_blocked_in_NO_NEW_ENTRY", not allowed and "entry_blocked" in reason)

        allowed, reason = runtime_mode.gate_action("SELL", "REDUCE_ONLY")
        _test("gate_sell_allowed_in_REDUCE_ONLY", allowed)

        allowed, reason = runtime_mode.gate_action("SELL", "OBSERVATION_ONLY")
        _test("gate_sell_blocked_in_OBSERVATION_ONLY", not allowed)

        allowed, reason = runtime_mode.gate_action("HOLD", "RECONCILIATION_ONLY")
        _test("gate_hold_always_allowed", allowed)

        # 1j: Permission queries
        _test("can_entry_FULL", runtime_mode.can_entry("FULL"))
        _test("cannot_entry_REDUCE_ONLY", not runtime_mode.can_entry("REDUCE_ONLY"))
        _test("can_exit_NO_NEW_ENTRY", runtime_mode.can_exit("NO_NEW_ENTRY"))
        _test("cannot_exit_OBSERVATION_ONLY", not runtime_mode.can_exit("OBSERVATION_ONLY"))
        _test("can_observe_OBSERVATION_ONLY", runtime_mode.can_observe("OBSERVATION_ONLY"))
        _test("cannot_observe_RECONCILIATION_ONLY", not runtime_mode.can_observe("RECONCILIATION_ONLY"))

        # 1k: Crash-safe persistence (simulate crash by writing then reading)
        runtime_mode.save_mode("RECONCILIATION_ONLY", reason="crash_test", mode_file=mode_file)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("crash_safe_persistence", mode == "RECONCILIATION_ONLY")

        # 1l: Invalid mode string in file defaults to FULL
        with open(mode_file, "w") as f:
            json.dump({"mode": "INVALID_MODE"}, f)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("invalid_mode_defaults_to_FULL", mode == "FULL")

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 2. Health Check Tests
# ---------------------------------------------------------------------------

def test_health_checks() -> None:
    print("\n=== 2. Health Checks ===")
    tmp = _make_tmpdir()
    mode_file = os.path.join(tmp, "runtime_mode.json")

    try:
        # 2a: Feed staleness — fresh tick
        r = check_feed_staleness(time.time() - 10, threshold_s=120)
        _test("feed_fresh_ok", r.ok)

        # 2b: Feed staleness — stale tick
        r = check_feed_staleness(time.time() - 200, threshold_s=120)
        _test("feed_stale_fail", not r.ok)

        # 2c: Fill latency — within threshold
        now = time.time()
        r = check_fill_latency(now - 5, now - 2, threshold_s=30)
        _test("fill_latency_ok", r.ok)

        # 2d: Fill latency — exceeds threshold
        r = check_fill_latency(now - 60, now - 5, threshold_s=30)
        _test("fill_latency_fail", not r.ok)

        # 2e: Fill latency — no pending order
        r = check_fill_latency(0, 0, threshold_s=30)
        _test("fill_latency_no_pending_ok", r.ok)

        # 2f: Consecutive losses — under threshold
        r = check_consecutive_losses(3, threshold=5)
        _test("consecutive_losses_under_ok", r.ok)

        # 2g: Consecutive losses — at threshold
        r = check_consecutive_losses(5, threshold=5)
        _test("consecutive_losses_at_threshold_fail", not r.ok)

        # 2h: Slippage — normal
        r = check_slippage_anomaly(20.0, p99_threshold_bps=50.0)
        _test("slippage_normal_ok", r.ok)

        # 2i: Slippage — anomalous
        r = check_slippage_anomaly(75.0, p99_threshold_bps=50.0)
        _test("slippage_anomaly_fail", not r.ok)

        # 2j: Invariant check — pass
        r = check_invariant(True, "all_matched")
        _test("invariant_pass", r.ok)

        # 2k: Invariant check — fail
        r = check_invariant(False, "mismatch")
        _test("invariant_fail", not r.ok)

        # 2l: Aggregate health — all OK, no escalation
        runtime_mode.save_mode("FULL", mode_file=mode_file)
        report = run_health_checks(
            last_tick_epoch=time.time() - 5,
            feed_stale_threshold_s=120,
            consecutive_losses=0,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("aggregate_all_ok", report.all_ok)
        _test("aggregate_no_escalation", not report.escalated)

        # 2m: Aggregate health — feed stale triggers OBSERVATION_ONLY
        runtime_mode.save_mode("FULL", mode_file=mode_file)
        report = run_health_checks(
            last_tick_epoch=time.time() - 300,
            feed_stale_threshold_s=120,
            consecutive_losses=0,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("stale_feed_escalates", report.escalated)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("stale_feed_mode_is_OBSERVATION_ONLY", mode == "OBSERVATION_ONLY")

        # 2n: Aggregate health — consecutive losses triggers REDUCE_ONLY
        runtime_mode.save_mode("FULL", mode_file=mode_file)
        report = run_health_checks(
            last_tick_epoch=time.time() - 5,
            feed_stale_threshold_s=120,
            consecutive_losses=6,
            consecutive_loss_threshold=5,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("consecutive_losses_escalates", report.escalated)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("losses_mode_is_REDUCE_ONLY", mode == "REDUCE_ONLY")

        # 2o: Aggregate health — slippage anomaly triggers NO_NEW_ENTRY
        runtime_mode.save_mode("FULL", mode_file=mode_file)
        report = run_health_checks(
            last_tick_epoch=time.time() - 5,
            feed_stale_threshold_s=120,
            last_slippage_bps=80.0,
            slippage_p99_bps=50.0,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("slippage_escalates", report.escalated)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("slippage_mode_is_NO_NEW_ENTRY", mode == "NO_NEW_ENTRY")

        # 2p: Multiple failures picks most restrictive
        runtime_mode.save_mode("FULL", mode_file=mode_file)
        report = run_health_checks(
            last_tick_epoch=time.time() - 300,  # stale -> OBSERVATION_ONLY
            feed_stale_threshold_s=120,
            consecutive_losses=6,               # losses -> REDUCE_ONLY
            consecutive_loss_threshold=5,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("multiple_failures_most_restrictive", report.escalated)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("multiple_failures_mode_OBSERVATION_ONLY", mode == "OBSERVATION_ONLY",
              f"expected OBSERVATION_ONLY, got {mode}")

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 3. Auto-Throttle Tests
# ---------------------------------------------------------------------------

def test_auto_throttle() -> None:
    print("\n=== 3. Auto-Throttle ===")
    tmp = _make_tmpdir()
    state_file = os.path.join(tmp, "throttle_state.json")

    try:
        cfg = {
            "THROTTLE_ENABLED": True,
            "THROTTLE_LOSS_THRESHOLD": 3,
            "THROTTLE_LOSS_STEP": 0.25,
            "THROTTLE_MIN_MULTIPLIER": 0.25,
            "THROTTLE_SLIPPAGE_P99_BPS": 50.0,
            "THROTTLE_SLIPPAGE_MULTIPLIER": 0.5,
            "THROTTLE_RECOVERY_WINS": 2,
        }

        # 3a: No throttle with 0 losses
        mult, reason = compute_multiplier(consecutive_losses=0, cfg=cfg)
        _test("no_throttle_zero_losses", mult == 1.0 and reason == "")

        # 3b: No throttle below threshold
        mult, reason = compute_multiplier(consecutive_losses=2, cfg=cfg)
        _test("no_throttle_below_threshold", mult == 1.0)

        # 3c: Throttle at threshold
        mult, reason = compute_multiplier(consecutive_losses=3, cfg=cfg)
        _test("throttle_at_threshold", mult < 1.0, f"mult={mult}")

        # 3d: Throttle increases with more losses
        mult_3, _ = compute_multiplier(consecutive_losses=3, cfg=cfg)
        mult_5, _ = compute_multiplier(consecutive_losses=5, cfg=cfg)
        _test("throttle_increases_with_losses", mult_5 < mult_3,
              f"mult_3={mult_3} mult_5={mult_5}")

        # 3e: Throttle never goes below minimum
        mult, _ = compute_multiplier(consecutive_losses=100, cfg=cfg)
        _test("throttle_floor_at_minimum", mult >= 0.25, f"mult={mult}")

        # 3f: Slippage anomaly throttle
        mult, reason = compute_multiplier(last_slippage_bps=80.0, cfg=cfg)
        _test("slippage_anomaly_throttle", mult == 0.5, f"mult={mult}")

        # 3g: Record trade results — losses accumulate
        record_trade_result(is_win=False, slippage_bps=10, cfg=cfg, state_file=state_file)
        record_trade_result(is_win=False, slippage_bps=10, cfg=cfg, state_file=state_file)
        record_trade_result(is_win=False, slippage_bps=10, cfg=cfg, state_file=state_file)
        state = load_throttle_state(state_file)
        _test("losses_accumulate", state["consecutive_losses"] == 3)

        # 3h: Win resets loss counter
        record_trade_result(is_win=True, slippage_bps=5, cfg=cfg, state_file=state_file)
        state = load_throttle_state(state_file)
        _test("win_resets_losses", state["consecutive_losses"] == 0)

        # 3i: Apply throttle to quantity
        # Reset state — simulate 4 consecutive losses
        for _ in range(4):
            record_trade_result(is_win=False, slippage_bps=10, cfg=cfg, state_file=state_file)
        adj_qty, mult, reason = apply_throttle_to_qty(
            Decimal("1.0"), cfg=cfg, state_file=state_file
        )
        _test("throttle_reduces_qty", adj_qty < Decimal("1.0"),
              f"adj_qty={adj_qty} mult={mult}")

        # 3j: State persists across load
        state = load_throttle_state(state_file)
        _test("state_persists", state["consecutive_losses"] == 4)

        # 3k: Disabled throttle returns 1.0
        disabled_cfg = dict(cfg)
        disabled_cfg["THROTTLE_ENABLED"] = False
        mult, reason = compute_multiplier(consecutive_losses=10, cfg=disabled_cfg)
        _test("disabled_throttle_returns_1", mult == 1.0)

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 4. Artifact Integrity Tests
# ---------------------------------------------------------------------------

def test_artifact_integrity() -> None:
    print("\n=== 4. Artifact Integrity ===")
    tmp = _make_tmpdir()

    try:
        # 4a: Create test artifacts
        fills_path = os.path.join(tmp, "fills.csv")
        with open(fills_path, "w", newline="") as f:
            f.write("fill_id,order_id,side,qty,price,ts\n")
            f.write("f001,o001,BUY,0.1,2000.0,1234567890\n")
            f.write("f002,o002,SELL,0.1,2050.0,1234567900\n")

        orders_path = os.path.join(tmp, "orders.csv")
        with open(orders_path, "w", newline="") as f:
            f.write("order_id,side,qty,price,ts\n")
            f.write("o001,BUY,0.1,2000.0,1234567889\n")

        # 4b: File hash is deterministic
        h1 = file_hash(fills_path)
        h2 = file_hash(fills_path)
        _test("file_hash_deterministic", h1 == h2 and h1 is not None)

        # 4c: Row count is correct
        rows = file_row_count(fills_path)
        _test("row_count_correct", rows == 2, f"rows={rows}")

        # 4d: Save integrity manifest
        hashes = compute_artifact_hashes(tmp)
        manifest_path = save_integrity_manifest(tmp, hashes)
        _test("integrity_manifest_saved", os.path.exists(manifest_path))

        # 4e: Load manifest matches saved
        loaded = load_integrity_manifest(tmp)
        _test("integrity_manifest_loadable", loaded is not None)
        _test("manifest_has_artifacts", "fills.csv" in loaded.get("artifacts", {}))

        # 4f: Verify integrity — no changes = PASS
        ok, issues = verify_artifact_integrity(tmp)
        _test("integrity_no_changes_pass", ok and len(issues) == 0)

        # 4g: Verify integrity — truncation detected
        # Shrink fills.csv (simulate truncation)
        with open(fills_path, "w", newline="") as f:
            f.write("fill_id,order_id,side,qty,price,ts\n")
            # Only 1 row now instead of 2
            f.write("f001,o001,BUY,0.1,2000.0,1234567890\n")
        ok, issues = verify_artifact_integrity(tmp)
        _test("truncation_detected", not ok, f"issues={issues}")
        _test("truncation_issue_detail",
              any("truncated" in i.get("issue", "") or "rows_decreased" in i.get("issue", "") for i in issues),
              f"issues={issues}")

        # 4h: Verify integrity — missing file detected
        loaded["artifacts"]["ghost.csv"] = {
            "hash": "ghost",
            "size": 10,
            "rows": 1,
            "ts": time.time(),
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(loaded, f, indent=2)
        ok, issues = verify_artifact_integrity(tmp)
        has_missing = any(i.get("issue") == "missing" for i in issues)
        _test("missing_file_detected", has_missing, f"issues={issues}")

        # 4i: CSV not truncated — proper file
        proper_csv = os.path.join(tmp, "proper.csv")
        with open(proper_csv, "w") as f:
            f.write("a,b\n1,2\n")
        ok, detail = check_csv_not_truncated(proper_csv)
        _test("proper_csv_not_truncated", ok)

        # 4j: CSV truncated — no trailing newline
        bad_csv = os.path.join(tmp, "bad.csv")
        with open(bad_csv, "wb") as f:
            f.write(b"a,b\n1,2")  # no trailing newline
        ok, detail = check_csv_not_truncated(bad_csv)
        _test("truncated_csv_detected", not ok, f"detail={detail}")

        # 4k: Fills CSV integrity — duplicate fill_id
        dup_fills = os.path.join(tmp, "fills.csv")
        with open(dup_fills, "w", newline="") as f:
            f.write("fill_id,order_id,side,qty,price,ts\n")
            f.write("f001,o001,BUY,0.1,2000.0,1234567890\n")
            f.write("f001,o002,SELL,0.1,2050.0,1234567900\n")  # duplicate fill_id
        ok, detail = check_fills_csv_integrity(tmp)
        _test("duplicate_fill_id_detected", not ok and "duplicate" in detail,
              f"detail={detail}")

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 5. Run Manifest Tests
# ---------------------------------------------------------------------------

def test_run_manifest() -> None:
    print("\n=== 5. Run Manifest ===")
    tmp = _make_tmpdir()

    try:
        test_cfg = {
            "SYMBOL": "ETH-USD",
            "EXECUTION_MODE": "ADAPTER",
            "RISK_PER_TRADE": 0.02,
        }

        # 5a: Config hash is deterministic
        h1 = config_hash(test_cfg)
        h2 = config_hash(test_cfg)
        _test("config_hash_deterministic", h1 == h2 and len(h1) == 64)

        # 5b: Config hash changes with different config
        diff_cfg = dict(test_cfg)
        diff_cfg["RISK_PER_TRADE"] = 0.05
        h3 = config_hash(diff_cfg)
        _test("config_hash_changes", h3 != h1)

        # 5c: Code hash is deterministic
        ch1 = code_hash(_REPO_ROOT)
        ch2 = code_hash(_REPO_ROOT)
        _test("code_hash_deterministic", ch1 == ch2 and len(ch1) == 64)

        # 5d: Create manifest
        manifest = create_manifest(
            run_id="test_run_001",
            cfg=test_cfg,
            repo_root=_REPO_ROOT,
            runtime_mode_str="FULL",
        )
        _test("manifest_has_run_id", manifest["run_id"] == "test_run_001")
        _test("manifest_has_config_hash", len(manifest["config_hash"]) == 64)
        _test("manifest_has_code_hash", len(manifest["code_hash"]) == 64)
        _test("manifest_status_running", manifest["status"] == "RUNNING")

        # 5e: Save and load manifest
        path = save_manifest(manifest, tmp)
        _test("manifest_saved", os.path.exists(path))
        loaded = load_manifest(tmp, "test_run_001")
        _test("manifest_loaded", loaded is not None)
        _test("manifest_roundtrip", loaded["run_id"] == "test_run_001")

        # 5f: Finalize manifest
        finalized = finalize_manifest(
            manifest,
            artifacts=["fills.csv", "orders.csv"],
            status="COMPLETE",
        )
        _test("manifest_finalized", finalized["status"] == "COMPLETE")
        _test("manifest_has_artifacts", len(finalized["artifacts"]) == 2)
        _test("manifest_has_end_ts", finalized["end_ts"] is not None)

        # 5g: Config drift detection — no drift
        _test("no_config_drift", not check_config_drift(manifest, test_cfg))

        # 5h: Config drift detection — drift detected
        drifted_cfg = dict(test_cfg)
        drifted_cfg["RISK_PER_TRADE"] = 0.10
        _test("config_drift_detected", check_config_drift(manifest, drifted_cfg))

        # 5i: Load nonexistent manifest returns None
        _test("missing_manifest_returns_none", load_manifest(tmp, "nonexistent") is None)

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 6. Invariant Engine Tests
# ---------------------------------------------------------------------------

def test_invariants() -> None:
    print("\n=== 6. Invariant Engine ===")

    # 6a: Position matches fills — exact match
    r = check_position_matches_fills(Decimal("0.5"), Decimal("0.5"))
    _test("position_exact_match", r.ok)

    # 6b: Position matches fills — within tolerance
    r = check_position_matches_fills(Decimal("0.50005"), Decimal("0.5"), tolerance=Decimal("0.001"))
    _test("position_within_tolerance", r.ok)

    # 6c: Position mismatch
    r = check_position_matches_fills(Decimal("0.5"), Decimal("0.3"))
    _test("position_mismatch_detected", not r.ok and r.critical)

    # 6d: No duplicate fill IDs — clean
    r = check_no_duplicate_fill_ids(["f001", "f002", "f003"])
    _test("no_duplicate_fill_ids_clean", r.ok)

    # 6e: Duplicate fill IDs detected
    r = check_no_duplicate_fill_ids(["f001", "f002", "f001", "f003"])
    _test("duplicate_fill_id_detected", not r.ok and r.critical)

    # 6f: Order/fill linkage — all linked
    r = check_order_fill_linkage({"o001", "o002"}, {"o001", "o002", "o003"})
    _test("all_fills_linked", r.ok)

    # 6g: Order/fill linkage — orphaned fill
    r = check_order_fill_linkage({"o001", "o999"}, {"o001", "o002"})
    _test("orphaned_fill_detected", not r.ok)

    # 6h: Timestamp ordering — valid
    r = check_timestamp_ordering([100.0, 200.0, 300.0, 400.0])
    _test("timestamps_valid", r.ok)

    # 6i: Timestamp ordering — violation
    r = check_timestamp_ordering([100.0, 300.0, 200.0, 400.0])
    _test("timestamp_violation_detected", not r.ok)

    # 6j: Cash/exposure limits — within bounds
    r = check_cash_exposure_limits(
        cash=Decimal("400"), exposure=Decimal("100"),
        max_exposure_pct=Decimal("0.50"), starting_cash=Decimal("500"),
    )
    _test("exposure_within_limits", r.ok)

    # 6k: Cash/exposure limits — exceeded
    r = check_cash_exposure_limits(
        cash=Decimal("100"), exposure=Decimal("300"),
        max_exposure_pct=Decimal("0.50"), starting_cash=Decimal("500"),
    )
    _test("exposure_limit_exceeded", not r.ok)

    # 6l: Aggregate invariant check — all pass
    report = run_invariant_checks(
        adapter_position_qty=Decimal("0.5"),
        fills_net_qty=Decimal("0.5"),
        fill_ids=["f001", "f002"],
        order_ids_with_fills={"o001"},
        order_ids_submitted={"o001", "o002"},
        event_timestamps=[100.0, 200.0, 300.0],
        cash=Decimal("400"),
        exposure=Decimal("100"),
    )
    _test("aggregate_invariants_all_pass", report.all_ok)

    # 6m: Aggregate invariant check — critical failure
    report = run_invariant_checks(
        adapter_position_qty=Decimal("0.5"),
        fills_net_qty=Decimal("0.0"),  # mismatch
        fill_ids=["f001", "f001"],     # duplicate
    )
    _test("aggregate_has_critical_failure", report.has_critical_failure)
    _test("aggregate_critical_count",
          len(report.critical_failures) >= 2,
          f"critical_failures={len(report.critical_failures)}")


# ---------------------------------------------------------------------------
# 7. Alert Throttle Tests
# ---------------------------------------------------------------------------

def test_alert_throttle() -> None:
    print("\n=== 7. Alert Throttle ===")

    # 7a: First alert should send
    throttle = AlertThrottle(cooldown_s=1.0)
    _test("first_alert_sends", throttle.should_send("test_key"))

    # 7b: Immediate duplicate should be suppressed
    _test("duplicate_suppressed", not throttle.should_send("test_key"))

    # 7c: Different key should send
    _test("different_key_sends", throttle.should_send("other_key"))

    # 7d: After cooldown, same key should send again
    time.sleep(1.1)
    _test("after_cooldown_sends", throttle.should_send("test_key"))

    # 7e: Mark sent manually
    throttle.mark_sent("manual_key")
    _test("manually_marked_suppressed", not throttle.should_send("manual_key"))


# ---------------------------------------------------------------------------
# 8. Backup & Restore Tests
# ---------------------------------------------------------------------------

def test_backup_restore() -> None:
    print("\n=== 8. Backup & Restore ===")
    tmp = _make_tmpdir()
    log_dir = os.path.join(tmp, "logs")
    state_dir = os.path.join(tmp, "state")
    backup_root = os.path.join(tmp, "backups")

    os.makedirs(log_dir, exist_ok=True)
    os.makedirs(state_dir, exist_ok=True)

    try:
        # Create test artifacts
        with open(os.path.join(log_dir, "fills.csv"), "w") as f:
            f.write("fill_id,side,qty\nf001,BUY,0.1\n")
        with open(os.path.join(log_dir, "orders.csv"), "w") as f:
            f.write("order_id,side\no001,BUY\n")
        with open(os.path.join(log_dir, "summary.json"), "w") as f:
            json.dump({"trades": 5}, f)
        with open(os.path.join(state_dir, "runtime_state.json"), "w") as f:
            json.dump({"position": "FLAT"}, f)

        # 8a: Backup creates backup dir
        ok, backup_dir, manifest = run_backup(
            log_dir=log_dir, state_dir=state_dir, backup_root=backup_root
        )
        _test("backup_ok", ok)
        _test("backup_dir_created", os.path.isdir(backup_dir))
        _test("backup_files_copied", manifest.get("files_copied", 0) >= 3,
              f"copied={manifest.get('files_copied', 0)}")

        # 8b: Backup manifest exists
        backup_manifest = os.path.join(backup_dir, "backup_manifest.json")
        _test("backup_manifest_exists", os.path.exists(backup_manifest))

        # 8c: Restore drill passes
        drill_ok, issues = run_restore_drill(backup_dir)
        _test("restore_drill_pass", drill_ok, f"issues={issues}")

        # 8d: Full cycle (backup + restore)
        cycle_ok, report = run_full_cycle(
            log_dir=log_dir, state_dir=state_dir, backup_root=backup_root
        )
        _test("full_cycle_pass", cycle_ok)
        _test("full_cycle_backup_ok", report.get("backup_ok", False))
        _test("full_cycle_restore_ok", report.get("restore_ok", False))

        # 8e: Cleanup keeps recent backups
        # Create a few more backups so we have multiple (need 1s apart for distinct dir names)
        for _ in range(3):
            time.sleep(1.1)  # ensure different second-level timestamps for dir names
            run_backup(log_dir=log_dir, state_dir=state_dir, backup_root=backup_root)
        removed = cleanup_old_backups(backup_root=backup_root, keep=2)
        _test("cleanup_removes_old", removed >= 2, f"removed={removed}")

        # Count remaining
        remaining = len([d for d in os.listdir(backup_root) if d.startswith("backup_")])
        _test("cleanup_keeps_recent", remaining == 2, f"remaining={remaining}")

        # 8f: Restore drill detects corruption
        # Tamper with a backed-up file
        backup_dirs = sorted(
            [d for d in os.listdir(backup_root) if d.startswith("backup_")],
            reverse=True,
        )
        if backup_dirs:
            latest_backup = os.path.join(backup_root, backup_dirs[0])
            # Find a CSV file to tamper with
            logs_subdir = os.path.join(latest_backup, "logs")
            if os.path.isdir(logs_subdir):
                for fname in os.listdir(logs_subdir):
                    if fname.endswith(".csv"):
                        tampered = os.path.join(logs_subdir, fname)
                        with open(tampered, "a") as f:
                            f.write("TAMPERED\n")
                        break
            drill_ok, issues = run_restore_drill(latest_backup)
            _test("corruption_detected_in_restore", not drill_ok,
                  f"issues={issues}")

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# 9. Integration: Health → Mode → Gating chain
# ---------------------------------------------------------------------------

def test_integration_chain() -> None:
    print("\n=== 9. Integration: Health -> Mode -> Gating Chain ===")
    tmp = _make_tmpdir()
    mode_file = os.path.join(tmp, "runtime_mode.json")

    try:
        # Start in FULL mode
        runtime_mode.save_mode("FULL", mode_file=mode_file)

        # 9a: Health check triggers mode escalation
        report = run_health_checks(
            last_tick_epoch=time.time() - 5,
            consecutive_losses=6,
            consecutive_loss_threshold=5,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        _test("chain_health_escalates", report.escalated)

        # 9b: Mode is now REDUCE_ONLY
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("chain_mode_reduce_only", mode == "REDUCE_ONLY")

        # 9c: BUY is blocked
        allowed, reason = runtime_mode.gate_action("BUY", mode)
        _test("chain_buy_blocked", not allowed)

        # 9d: SELL is still allowed
        allowed, reason = runtime_mode.gate_action("SELL", mode)
        _test("chain_sell_allowed", allowed)

        # 9e: Further health failure escalates further
        report = run_health_checks(
            last_tick_epoch=time.time() - 300,  # stale feed -> OBSERVATION_ONLY
            feed_stale_threshold_s=120,
            consecutive_losses=6,
            consecutive_loss_threshold=5,
            positions_match_fills=True,
            mode_file=mode_file,
        )
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("chain_escalated_to_observation", mode == "OBSERVATION_ONLY")

        # 9f: Now even SELL is blocked
        allowed, reason = runtime_mode.gate_action("SELL", mode)
        _test("chain_sell_blocked_in_observation", not allowed)

        # 9g: Only operator reset can restore to FULL
        runtime_mode.reset_to_full(reason="operator_reset", mode_file=mode_file)
        mode, _ = runtime_mode.load_mode(mode_file)
        _test("chain_operator_reset_to_full", mode == "FULL")

        # 9h: After reset, BUY is allowed again
        allowed, reason = runtime_mode.gate_action("BUY", mode)
        _test("chain_buy_allowed_after_reset", allowed)

    finally:
        _cleanup(tmp)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("Phase 16 Acceptance Test Harness")
    print("=" * 70)

    test_runtime_mode()
    test_health_checks()
    test_auto_throttle()
    test_artifact_integrity()
    test_run_manifest()
    test_invariants()
    test_alert_throttle()
    test_backup_restore()
    test_integration_chain()

    print("\n" + "=" * 70)
    print(f"RESULTS: {_PASS} passed, {_FAIL} failed, {_PASS + _FAIL} total")
    print("=" * 70)

    if _FAIL > 0:
        print("\nFailed tests:")
        for t in _TESTS:
            if "[FAIL]" in t:
                print(t)

    print(f"\nPhase 16 Acceptance: {'PASS' if _FAIL == 0 else 'FAIL'}")
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
