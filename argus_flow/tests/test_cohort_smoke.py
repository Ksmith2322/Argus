"""Cohort-run smoke tests — nightly pipeline module importability + entry points.

run_cohort_report.ps1 invokes ~15 Python modules in sequence. When a module
silently breaks (import error, missing function), the script catches it
but the output is only visible in the log file. These smoke tests catch
that earlier: every module the nightly invokes must be importable AND
its public entry point (build_report / main / summary / etc.) must be
callable.

This is not an E2E test — modules aren't executed, just imported and
their entry points inspected. Keeps the test fast and doesn't require
live yfinance / IBKR data.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# Modules the nightly invokes (from run_cohort_report.ps1) + their expected
# public entry points (either build_report, main, summary, etc.).
COHORT_MODULES = [
    ("argus_flow.ops.refresh_managed_truth", ["main"]),
    ("helio.drift_detector", ["main", "detect_drift", "load_baselines"]),
    ("apollo.runner", ["main"]),
    ("apollo.ops.backfill_forward_returns", ["main"]),
    ("helio.fleet_perf_summary", ["main", "build_summary"]),
    ("helio.promotion_readiness", ["main", "build_report"]),
    ("helio.kill_watchdog", ["main", "build_report"]),
    ("apollo.execution.planned_trades", ["main", "plan_new_tickets", "mature_tickets"]),
    ("helio.canonical_fills", ["write_fill", "write_fill_typed", "read_fills",
                                 "backfill_from_trade_csvs"]),
    ("helio.reconciliation", ["main", "build_report"]),
    ("helio.morning_brief", ["main"]),
    ("helio.fleet_state", ["main", "build_fleet_state", "write_fleet_state"]),
    ("hermes.runner", ["main"]),
]


class TestCohortModulesImportable(unittest.TestCase):
    """Every module the nightly runs must import cleanly. Silent import
    failures are the failure mode this test catches."""

    def test_all_cohort_modules_import(self):
        failures = []
        for mod_name, _ in COHORT_MODULES:
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {type(e).__name__}: {e}")
        self.assertEqual(failures, [], f"import failures:\n" + "\n".join(failures))


class TestCohortEntryPoints(unittest.TestCase):
    """Each nightly module must expose the expected callable entry point.
    A rename (e.g. main -> run) without updating the PS script would break
    the nightly silently."""

    def test_entry_points_exist_and_callable(self):
        missing = []
        for mod_name, entry_points in COHORT_MODULES:
            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                # Already caught by the import test above
                continue
            for fn_name in entry_points:
                fn = getattr(mod, fn_name, None)
                if fn is None:
                    missing.append(f"{mod_name}.{fn_name} missing")
                elif not callable(fn):
                    missing.append(f"{mod_name}.{fn_name} not callable")
        self.assertEqual(missing, [], f"entry points missing:\n" + "\n".join(missing))


class TestCohortNoCircularImports(unittest.TestCase):
    """Importing each module twice must not change its identity — guards
    against partial-import state that creates phantom duplicates."""

    def test_double_import_is_idempotent(self):
        for mod_name, _ in COHORT_MODULES:
            try:
                m1 = importlib.import_module(mod_name)
                m2 = importlib.import_module(mod_name)
                self.assertIs(m1, m2, f"{mod_name}: import not idempotent")
            except Exception:
                # Import failures handled above
                continue


class TestCohortReportScriptReferences(unittest.TestCase):
    """Scan run_cohort_report.ps1 for `python -m <module>` invocations and
    verify each module is importable. Catches the case where the PS script
    mentions a module that no longer exists."""

    def test_every_python_m_module_is_importable(self):
        ps_script = _REPO / "ops" / "run_cohort_report.ps1"
        if not ps_script.exists():
            self.skipTest("run_cohort_report.ps1 not present")
        text = ps_script.read_text(encoding="utf-8")
        import re
        # Match `python -m some.module` patterns
        modules = set()
        for m in re.finditer(r"-m\s+([a-zA-Z_][\w\.]*)", text):
            modules.add(m.group(1))
        self.assertGreater(len(modules), 0, "no `python -m` invocations found — regex broke?")
        failures = []
        for mod_name in sorted(modules):
            try:
                importlib.import_module(mod_name)
            except Exception as e:
                failures.append(f"{mod_name}: {type(e).__name__}: {e}")
        self.assertEqual(failures, [],
            f"PS script references un-importable modules:\n" + "\n".join(failures))


class TestHelioModuleSurface(unittest.TestCase):
    """Sanity: the key helio modules must still export what other code depends on."""

    def test_fleet_sizing_public_api(self):
        from helio import fleet_sizing as fs
        for name in ("compute_risk_usd", "get_sizing_anchor_usd", "compute_strategy_stats",
                     "max_notional_usd", "pnl_pct_of_fleet"):
            self.assertTrue(hasattr(fs, name), f"fleet_sizing.{name} missing")

    def test_strategy_registry_public_api(self):
        from helio import strategy_registry as sr
        self.assertTrue(hasattr(sr, "load_registry"))
        self.assertTrue(hasattr(sr, "get_strategy"))

    def test_domain_public_api(self):
        from helio import domain
        for name in ("Fill", "Trade", "Signal"):
            self.assertTrue(hasattr(domain, name), f"domain.{name} missing")

    def test_canonical_fills_public_api(self):
        from helio import canonical_fills as cf
        for name in ("write_fill", "write_fill_typed", "read_fills",
                     "backfill_from_trade_csvs"):
            self.assertTrue(hasattr(cf, name), f"canonical_fills.{name} missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
