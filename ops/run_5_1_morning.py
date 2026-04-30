"""5/1 ceremony morning runner — one command for the entire prep sequence.

Replaces the 3-step manual run with a single invocation that:
  1. Refreshes the data snapshot (run_5_1_prep)
  2. Re-runs the operational vetting auto-checker
  3. Generates the verdict skeleton with auto-fills + cross-references

Then prints the verdict-editor URL so the user goes straight from prep to
decisions. Saves ~5 minutes of context-switching on ceremony morning.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.run_5_1_morning

Idempotent — safe to run multiple times. Each step writes to its own file;
re-running just refreshes. Re-runnable on 5/15 and 5/31 ceremonies too.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

STEPS = [
    ("Snapshot ceremony data sources",   ["-m", "ops.run_5_1_prep"]),
    ("Operational vetting checklist",    ["-m", "ops.operational_vetting"]),
    ("Generate verdict skeleton",        ["-m", "ops.generate_verdict_skeleton"]),
]


def _run_step(label: str, args: list[str]) -> tuple[bool, str]:
    """Run one prep step. Returns (ok, summary_tail)."""
    try:
        result = subprocess.run(
            [PYTHON] + args,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT after 120s"
    except Exception as e:
        return False, f"EXCEPTION: {e}"
    if result.returncode != 0:
        # Show last 5 stderr lines for failure context
        err_tail = "\n    ".join((result.stderr or "").splitlines()[-5:])
        return False, f"exit={result.returncode}\n    {err_tail}"
    # Pull the last 2-3 useful lines from stdout for the summary
    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    summary = "\n    ".join(lines[-3:]) if lines else "(no output)"
    return True, summary


def main() -> int:
    started = datetime.now(timezone.utc)
    print("=" * 72)
    print("  5/1 CEREMONY MORNING PREP")
    print(f"  Started: {started.isoformat()}")
    print("=" * 72)
    print()

    overall_ok = True
    for i, (label, args) in enumerate(STEPS, start=1):
        print(f"[{i}/{len(STEPS)}] {label}...")
        ok, summary = _run_step(label, args)
        if ok:
            print(f"    OK")
            print(f"    {summary}")
        else:
            print(f"    FAILED — {summary}")
            overall_ok = False
        print()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print("=" * 72)
    if overall_ok:
        print(f"  ALL PREP STEPS COMPLETE in {elapsed:.1f}s")
        print()
        print("  Next step — open the verdict editor:")
        print()
        print("    http://localhost:8080/verdict_editor")
        print()
        print("  Most strategies are auto-filled OBSERVE. Only manual-judgment")
        print("  cards (n>=10, real edge eval) need your call. Engine recommendations")
        print("  surface inline on each card — no panel-hopping.")
        print()
        print("  After filling: click Save. Output writes to")
        print("    argus_flow/logs/verdict_<date>.json  (with .bak of any prior).")
        print()
        print("  Cheat sheet open in second window:")
        print("    C:/Users/ksmit/.claude/projects/c--Argus/memory/reference_5_1_ceremony_cheatsheet.md")
    else:
        print(f"  ONE OR MORE STEPS FAILED ({elapsed:.1f}s elapsed)")
        print()
        print("  Re-run individual steps or fix the failing dependency.")
        print("  See output above for details.")
    print("=" * 72)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
