"""Tests for helio.morning_brief._build_checklist — the monitor-mode
summary.

The checklist is the operator's Mon-Fri glance view. Each of 7 items is
a yes/no question whose OK bit must be correctly computed from the brief
sections. A bug here means the operator misses a real issue or panics
over a non-issue.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from helio.morning_brief import _build_checklist  # noqa: E402


def _happy_brief() -> dict:
    """A brief where every checklist item is OK."""
    return {
        "sections": {
            "fleet_health": {
                "non_ok": [],
                "risk_disagreement": None,
                "control_files": {},
                "total_systems": 8,
            },
            "kill_watchdog": {"kill_candidates": [], "drift_warnings": []},
            "drift": {"severe": [], "warning": []},
            "promotion": {
                "promotion_eligible": [], "review_gate_passed": [],
                "kill_candidates": [],
            },
            "apollo": {
                "total_forward_records": 50, "new_forward_records": 3,
                "new_matured": 2, "total_matured": 20,
                "new_counterfactual_pnl_usd": 30.0,
            },
            "broker": {"latest_equity_usd": 10000, "samples_collected": 100},
            "new_trades": {"count": 0, "total_pnl_usd": 0, "by_strategy": {}},
        },
    }


class TestChecklistOverall(unittest.TestCase):
    def test_happy_path_is_ok(self):
        result = _build_checklist(_happy_brief())
        self.assertEqual(result["overall"], "OK")
        # Every individual item must be OK
        for item in result["items"]:
            self.assertTrue(item["ok"],
                f"happy brief has a non-OK item: {item['label']}")

    def test_any_failing_item_flips_overall_to_attention(self):
        brief = _happy_brief()
        brief["sections"]["kill_watchdog"]["kill_candidates"] = ["forge_broken"]
        result = _build_checklist(brief)
        self.assertEqual(result["overall"], "ATTENTION")


class TestChecklistItemCount(unittest.TestCase):
    def test_always_returns_seven_items(self):
        """The checklist has 7 fixed questions. Adding/removing one is a
        behavior change that breaks the operator's scanning habit."""
        result = _build_checklist(_happy_brief())
        self.assertEqual(len(result["items"]), 7)

    def test_every_item_has_label_and_ok_and_detail(self):
        result = _build_checklist(_happy_brief())
        for item in result["items"]:
            self.assertIn("label", item)
            self.assertIn("ok", item)
            self.assertIn("detail", item)


class TestSystemsHealthy(unittest.TestCase):
    def test_non_ok_systems_flag_item(self):
        brief = _happy_brief()
        brief["sections"]["fleet_health"]["non_ok"] = [
            ("forge_gld_pm_long", "STALE")]
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "All systems" in i["label"])
        self.assertFalse(item["ok"])
        self.assertIn("forge_gld_pm_long", item["detail"])


class TestRiskDisagreement(unittest.TestCase):
    def test_risk_drift_flags_item(self):
        brief = _happy_brief()
        brief["sections"]["fleet_health"]["risk_disagreement"] = "broker 1.2% vs paper 0.8%"
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "risk drift" in i["label"].lower())
        self.assertFalse(item["ok"])


class TestPauseEntries(unittest.TestCase):
    def test_pause_entries_flag_item(self):
        brief = _happy_brief()
        brief["sections"]["fleet_health"]["control_files"] = {
            "PAUSE_ENTRIES": {"present": True, "age_s": 300},
        }
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "pause" in i["label"].lower())
        self.assertFalse(item["ok"])
        self.assertIn("300s", item["detail"])


class TestKillCandidates(unittest.TestCase):
    def test_kill_candidate_flags_item(self):
        brief = _happy_brief()
        brief["sections"]["kill_watchdog"]["kill_candidates"] = ["forge_broken"]
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "kill candidates" in i["label"].lower())
        self.assertFalse(item["ok"])
        self.assertIn("forge_broken", item["detail"])


class TestSevereDrift(unittest.TestCase):
    def test_severe_drift_flags_item(self):
        brief = _happy_brief()
        brief["sections"]["drift"]["severe"] = ["argus_usdjpy"]
        result = _build_checklist(brief)
        item = next(i for i in result["items"]
                    if "signal-frequency" in i["label"].lower())
        self.assertFalse(item["ok"])

    def test_warning_drift_does_not_flag_item(self):
        """Only SEVERE drift flips the checklist. Warning-level is a note,
        not an action item."""
        brief = _happy_brief()
        brief["sections"]["drift"]["warning"] = ["argus_usdjpy"]
        result = _build_checklist(brief)
        item = next(i for i in result["items"]
                    if "signal-frequency" in i["label"].lower())
        self.assertTrue(item["ok"])


class TestApolloPipeline(unittest.TestCase):
    def test_zero_forward_records_flags_item(self):
        brief = _happy_brief()
        brief["sections"]["apollo"]["total_forward_records"] = 0
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "apollo" in i["label"].lower())
        self.assertFalse(item["ok"])
        self.assertIn("backfill_forward_returns", item["detail"])


class TestBrokerEquity(unittest.TestCase):
    def test_missing_equity_flags_item(self):
        brief = _happy_brief()
        brief["sections"]["broker"]["latest_equity_usd"] = None
        result = _build_checklist(brief)
        item = next(i for i in result["items"] if "broker equity" in i["label"].lower())
        self.assertFalse(item["ok"])
        self.assertIn("fleet_monitor", item["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
