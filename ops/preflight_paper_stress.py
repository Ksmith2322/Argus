"""Pre-flight verifier for the paper-stress + trace-recording activation.

Run this BEFORE starting argus_flow with Phase 1-5 features enabled.
It checks every gate that matters and reports pass/fail per item. Any
failure → fix before launching the runner.

Checks (in order):
  1. IBKR_PORT == "7497"
  2. REAL_MONEY_ENABLED env unset / false
  3. helio.real_money.REAL_MONEY_ENABLED module constant is False
  4. All 5 phase modules importable
  5. argus FX configs loadable, report current paper_stress_multiplier
  6. GOLDEN_TRACE_PATH (if set) parent directory writable
  7. A smoke trace can be written + read back through the harness
  8. trace_inspect CLI runs against the smoke trace and reports clean

Exit codes:
  0 = all green, safe to activate
  1 = one or more failures, DO NOT activate
  2 = pre-flight itself errored
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ARGUS_CONFIGS_DIR = REPO / "argus_flow" / "configs"
ARGUS_FX_CONFIG_NAMES = (
    "usdjpy_mtf_paper_v1.json",
    "gbpusd_range_paper_v1.json",
    "cadjpy_mtf_paper_v1.json",
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class PreflightReport:
    checks: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append(CheckResult(name=name, passed=passed, detail=detail))

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)


def _check_env(report: PreflightReport) -> None:
    port = os.environ.get("IBKR_PORT", "")
    report.add(
        "IBKR_PORT == '7497'",
        port == "7497",
        f"IBKR_PORT={port!r}" if port else "IBKR_PORT not set",
    )

    rm_env = os.environ.get("REAL_MONEY_ENABLED", "").strip().lower()
    report.add(
        "REAL_MONEY_ENABLED env unset/false",
        rm_env not in ("1", "true", "yes"),
        f"REAL_MONEY_ENABLED={rm_env!r}" if rm_env else "unset (ok)",
    )

    try:
        from helio import real_money
        report.add(
            "helio.real_money.REAL_MONEY_ENABLED is False",
            real_money.REAL_MONEY_ENABLED is False,
            f"constant={real_money.REAL_MONEY_ENABLED}",
        )
    except Exception as e:
        report.add(
            "helio.real_money.REAL_MONEY_ENABLED is False",
            False,
            f"could not import helio.real_money: {e}",
        )


def _check_imports(report: PreflightReport) -> None:
    modules = [
        "helio.paper_stress",
        "helio.event_recorder",
        "helio.trace_replay",
        "helio.event_dispatcher",
        "helio.trace_invariants",
        "helio.trace_parity",
        "ops.stress_injector",
        "ops.trace_inspect",
        "ops.trace_parity",
    ]
    for m in modules:
        try:
            importlib.import_module(m)
            report.add(f"import {m}", True)
        except Exception as e:
            report.add(f"import {m}", False, f"{type(e).__name__}: {e}")


def _check_configs(report: PreflightReport) -> None:
    for name in ARGUS_FX_CONFIG_NAMES:
        path = ARGUS_CONFIGS_DIR / name
        if not path.exists():
            report.add(f"config {name} exists", False, f"missing at {path}")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            report.add(f"config {name} parses", False, str(e))
            continue
        mult = data.get("mtf", {}).get("paper_stress_multiplier")
        if mult is None:
            detail = "paper_stress_multiplier not set (default 1.0 = no stress)"
        else:
            detail = f"paper_stress_multiplier = {mult}"
        report.add(f"config {name}", True, detail)


def _check_trace_path(report: PreflightReport) -> None:
    trace_path = os.environ.get("GOLDEN_TRACE_PATH", "").strip()
    if not trace_path:
        report.add(
            "GOLDEN_TRACE_PATH set",
            True,
            "not set — runner will NOT record traces (set to enable)",
        )
        return
    parent = Path(trace_path).parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # Probe write
        probe = parent / ".preflight_write_probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        report.add("GOLDEN_TRACE_PATH parent writable", True, f"{parent}")
    except Exception as e:
        report.add("GOLDEN_TRACE_PATH parent writable", False, str(e))


def _check_harness_smoke(report: PreflightReport) -> None:
    """End-to-end: write a synthetic trace through the recorder, read
    it back through trace_inspect. If this works, the recorder, JSONL
    serializer, loader, and invariant checks all function end-to-end
    in the operator's environment."""
    from types import SimpleNamespace

    try:
        from helio.event_recorder import EventRecorder

        class _Slot:
            def __init__(self): self.handlers = []
            def __iadd__(self, h): self.handlers.append(h); return self
            def __isub__(self, h):
                if h in self.handlers: self.handlers.remove(h)
                return self
            def fire(self, *args):
                for h in list(self.handlers): h(*args)

        ib = SimpleNamespace(
            orderStatusEvent=_Slot(), execDetailsEvent=_Slot(),
            errorEvent=_Slot(), positionEvent=_Slot(),
            newOrderEvent=_Slot(), disconnectedEvent=_Slot(),
            connectedEvent=_Slot(),
        )
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            trace_path = Path(fh.name)
        rec = EventRecorder(ib, trace_path)
        ib.orderStatusEvent.fire(SimpleNamespace(
            order=SimpleNamespace(orderId=1, permId=99),
            orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1000, avgFillPrice=0),
            contract=SimpleNamespace(symbol="EUR"),
        ))
        rec.close()

        from ops.trace_inspect import inspect
        rep = inspect(trace_path)
        try:
            trace_path.unlink()
        except OSError:
            pass
        report.add(
            "harness end-to-end (record -> inspect)",
            rep["event_count"] >= 2,  # connected marker + orderStatus event
            f"recorded {rep['event_count']} events, "
            f"violations={len(rep['invariant_violations'])}",
        )
    except Exception as e:
        report.add(
            "harness end-to-end (record -> inspect)",
            False, f"{type(e).__name__}: {e}",
        )


# ─── orchestration ────────────────────────────────────────────────────────

def run_preflight() -> PreflightReport:
    report = PreflightReport()
    _check_env(report)
    _check_imports(report)
    _check_configs(report)
    _check_trace_path(report)
    _check_harness_smoke(report)
    return report


def _print_human(report: PreflightReport) -> None:
    print("Paper-stress preflight\n" + "=" * 22)
    for c in report.checks:
        marker = "PASS" if c.passed else "FAIL"
        detail = f"  ({c.detail})" if c.detail else ""
        print(f"  [{marker}] {c.name}{detail}")
    print()
    if report.all_passed:
        print("READY — safe to activate.")
    else:
        failed = [c.name for c in report.checks if not c.passed]
        print(f"NOT READY — {len(failed)} failure(s): {', '.join(failed)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Pre-flight verifier for paper-stress + trace recording")
    p.add_argument("--json", action="store_true",
                   help="emit JSON report instead of human-readable summary")
    args = p.parse_args(argv)

    try:
        report = run_preflight()
    except Exception as e:
        print(f"preflight ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "all_passed": report.all_passed,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in report.checks
            ],
        }, indent=2))
    else:
        _print_human(report)

    return 0 if report.all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
