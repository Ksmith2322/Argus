"""Schema smoke tests for report JSON artifacts.

Many modules (dashboard, morning_brief, fleet_state) consume JSON reports
produced by other modules. When a producer silently changes the shape, the
consumers break silently too — and we only notice when the dashboard goes
blank.

These tests assert the minimum shape each report must have, so if a
producer drops a required key, CI catches it. Each test uses the LIVE
report file if present (smoke test against real data) and skips when the
file hasn't been generated yet.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

LOGS = _REPO / "argus_flow" / "logs"


def _load_if_present(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


class TestFleetStatusSchema(unittest.TestCase):
    PATH = LOGS / "fleet_status.json"

    def test_top_level_keys(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("ts", "systems"):
            self.assertIn(key, data, f"fleet_status missing required '{key}'")

    def test_systems_is_dict_with_status_per_entry(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        systems = data.get("systems", {})
        self.assertIsInstance(systems, dict)
        for label, block in systems.items():
            self.assertIn("status", block, f"system {label} missing 'status'")


class TestKillWatchdogSchema(unittest.TestCase):
    PATH = LOGS / "kill_watchdog_report.json"

    def test_top_level_keys(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("generated_at", "anchor_at_time_usd", "strategies", "summary"):
            self.assertIn(key, data)

    def test_summary_has_kill_candidates_and_drift_warnings(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        summary = data.get("summary", {})
        self.assertIn("kill_candidates", summary)
        self.assertIn("drift_warnings", summary)
        self.assertIn("total_evaluated", summary)

    def test_each_strategy_has_status_and_stats(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for s in data.get("strategies", []):
            self.assertIn("strategy", s)
            self.assertIn("status", s)
            self.assertIn("stats", s)
            self.assertIn("triggered_rules", s)
            # Status is one of the known values — catches typo drift
            self.assertIn(s["status"], {"OK", "KILL_CANDIDATE", "DRIFT_WARNING"})


class TestReconciliationSchema(unittest.TestCase):
    PATH = LOGS / "reconciliation_report.json"

    def test_top_level_keys(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("generated_at", "tolerance_usd", "strategies", "summary"):
            self.assertIn(key, data)

    def test_summary_has_all_reconciled_flag(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        self.assertIn("all_reconciled", data.get("summary", {}))

    def test_each_strategy_has_csv_and_canonical_counts(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for s in data.get("strategies", []):
            for k in ("strategy", "status", "csv_row_count", "canonical_row_count",
                      "pnl_drift_usd", "drift_flags"):
                self.assertIn(k, s, f"reconciliation strategy missing '{k}'")
            self.assertIn(s["status"], {"OK", "DRIFT"})


class TestFleetStateSchema(unittest.TestCase):
    PATH = LOGS / "fleet_state.json"

    def test_top_level_keys(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("generated_at", "source_of_truth_status", "strategies", "fleet"):
            self.assertIn(key, data)

    def test_source_of_truth_marker_is_read_model(self):
        """Guard: no one should silently flip fleet_state to authoritative."""
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        self.assertEqual(data["source_of_truth_status"], "READ_MODEL_ONLY")

    def test_each_strategy_block_has_substructure(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for label, block in data.get("strategies", {}).items():
            for sub in ("performance", "gates", "watchdog", "reconciliation",
                        "is_healthy"):
                self.assertIn(sub, block, f"{label} block missing '{sub}'")
            self.assertIsInstance(block["is_healthy"], bool)


class TestFleetPerfSummarySchema(unittest.TestCase):
    PATH = LOGS / "fleet_perf_summary.json"

    def test_top_level_keys(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("generated_at", "anchor_capital_usd", "live_window", "all_time"):
            self.assertIn(key, data)

    def test_live_and_all_time_share_shape(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for view_name in ("live_window", "all_time"):
            view = data[view_name]
            self.assertIn("strategies", view)
            self.assertIn("fleet_total", view)
            self.assertIn("pnl_usd", view["fleet_total"])


class TestPromotionReadinessSchema(unittest.TestCase):
    """Promotion readiness is computed on demand in fleet_state; also sometimes
    written standalone. Tolerate either path."""
    PATH = LOGS / "promotion_readiness.json"

    def test_if_present_has_required_shape(self):
        data = _load_if_present(self.PATH)
        if data is None:
            self.skipTest(f"{self.PATH.name} not present")
        for key in ("generated_at", "review_gate", "canonical_gate", "strategies"):
            self.assertIn(key, data)
        for s in data.get("strategies", []):
            for k in ("strategy", "next_action", "review_gate_passed",
                      "canonical_gate_passed"):
                self.assertIn(k, s, f"promotion_readiness strategy missing '{k}'")


class TestCanonicalFillsShape(unittest.TestCase):
    """Every row in canonical_fills.jsonl must be valid JSON with a strategy
    label. Malformed lines would silently poison downstream reconciliation."""
    PATH = LOGS / "canonical_fills.jsonl"

    def test_every_row_parses_and_has_strategy(self):
        if not self.PATH.exists():
            self.skipTest(f"{self.PATH.name} not present")
        bad_lines = []
        missing_strategy = 0
        for i, line in enumerate(self.PATH.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                bad_lines.append(i)
                continue
            if not r.get("strategy"):
                missing_strategy += 1
        self.assertEqual(bad_lines, [], f"malformed JSONL lines: {bad_lines}")
        self.assertEqual(missing_strategy, 0,
            f"{missing_strategy} rows have empty/missing 'strategy'")


class TestMorningBriefText(unittest.TestCase):
    PATH = LOGS / "morning_brief.txt"

    def test_if_present_is_non_empty_text(self):
        if not self.PATH.exists():
            self.skipTest(f"{self.PATH.name} not present")
        text = self.PATH.read_text(encoding="utf-8")
        self.assertGreater(len(text), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
