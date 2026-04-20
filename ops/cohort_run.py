"""Python cohort-run orchestrator — a parallel path to run_cohort_report.ps1.

Two independent ways to run the nightly cohort: the PowerShell script
(still the primary, used by Task Scheduler) and this Python script. If
the PS pipeline fails for any reason (Windows permissions, env var quirk,
etc.), running `python -m ops.cohort_run` exercises the same sequence
and logs per-step pass/fail to argus_flow/logs/cohort_run.log.

This is NOT a replacement for the PS script — it's a backup. Steps are
kept in lockstep; a regex-scan test in test_cohort_smoke verifies that
every `python -m <mod>` invocation in the PS script is also listed here.

Exit codes:
  0 = all steps passed
  1 = at least one step failed (log shows which)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PYTHON = sys.executable
_LOG = _REPO / "argus_flow" / "logs" / "cohort_run.log"

# Step definitions: (module, args, fatal?)
# Order matches the PS script. Fatal=True aborts on failure (like the
# refresh_managed_truth step); non-fatal steps continue so a partial
# failure doesn't block the rest of the cohort.
STEPS = [
    ("argus_flow.ops.refresh_managed_truth",
     ["--include-summary", "--accept-existing-age-s", "600"], True),
    ("helio.drift_detector",                       [],              False),
    ("apollo.runner",                              ["--dry-run", "--days", "14"], False),
    ("apollo.ops.backfill_forward_returns",        [],              False),
    ("helio.fleet_perf_summary",                   [],              False),
    ("helio.promotion_readiness",                  [],              False),
    ("helio.kill_watchdog",                        ["--no-discord"],False),
    ("apollo.execution.planned_trades",            [],              False),
    ("helio.canonical_fills",                      ["--backfill"],  False),
    ("helio.reconciliation",                       [],              False),
    ("helio.morning_brief",                        [],              False),
    ("helio.fleet_state",                          [],              False),
    ("hermes.runner",                              ["--dry-run", "--min-score", "75"], False),
]


def _log(msg: str) -> None:
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _run_step(module: str, args: list[str], fatal: bool) -> tuple[bool, float]:
    """Invoke `python -m module [args]`. Returns (success, duration_s)."""
    cmd = [_PYTHON, "-m", module, *args]
    start = time.time()
    try:
        result = subprocess.run(cmd, cwd=str(_REPO), capture_output=True,
                                 text=True, timeout=600)
    except subprocess.TimeoutExpired:
        dur = time.time() - start
        _log(f"{module}: TIMEOUT after {dur:.0f}s")
        return False, dur
    dur = time.time() - start
    # Tail the last few lines of output for the log
    tail = "\n    ".join((result.stdout + result.stderr).splitlines()[-3:])
    if result.returncode == 0:
        _log(f"{module}: OK ({dur:.1f}s)")
        if tail:
            _log(f"    {tail}")
        return True, dur
    label = "FATAL" if fatal else "WARN"
    _log(f"{module}: {label} (exit {result.returncode}, {dur:.1f}s)")
    if tail:
        _log(f"    {tail}")
    return False, dur


def run(*, stop_on_fatal: bool = True) -> int:
    """Run every step. Returns 0 on all-pass, 1 on any failure."""
    _log("=" * 60)
    _log(f"cohort_run.py starting — {len(STEPS)} steps")
    _log("=" * 60)
    overall_ok = True
    results = []
    for module, args, fatal in STEPS:
        ok, dur = _run_step(module, args, fatal)
        results.append((module, ok, dur, fatal))
        if not ok:
            overall_ok = False
            if fatal and stop_on_fatal:
                _log(f"Fatal step failed — aborting remaining {len(STEPS) - len(results)} steps")
                break
    _log("-" * 60)
    passed = sum(1 for _, ok, _, _ in results if ok)
    _log(f"Summary: {passed}/{len(results)} steps OK"
         f"{' (all passed)' if overall_ok else ' — see above for failures'}")
    _log("=" * 60)
    return 0 if overall_ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Python cohort-run orchestrator")
    ap.add_argument("--continue-on-fatal", action="store_true",
                    help="Run remaining steps even if a fatal step fails")
    args = ap.parse_args()
    return run(stop_on_fatal=not args.continue_on_fatal)


if __name__ == "__main__":
    sys.exit(main())
