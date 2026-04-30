"""tws_recover — one-command TWS recovery after overnight disconnect.

The 4/29 + 4/30 mornings both showed the same pattern: TWS UI is up but
the API connection died overnight (likely IBKR's daily restart at 23:45 ET
killing the session). User has to manually re-auth in TWS, then run two
scripts in sequence to refresh broker state and clear the dashboard banner.

This script automates the post-reauth sequence:
  1. ops.tws_health_probe        — re-test the API connection
  2. argus_flow.ops.risk_oversight — refresh broker_equity into the report

Run it AFTER you've re-authenticated in the TWS UI. If TWS API is still
unreachable the probe will say so and exit nonzero.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.tws_recover
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

STEPS = [
    ("TWS health probe", ["-m", "ops.tws_health_probe"]),
    ("Risk oversight (broker_equity refresh)", ["-m", "argus_flow.ops.risk_oversight"]),
]


def main() -> int:
    started = datetime.now(timezone.utc)
    print("=" * 64)
    print("  TWS RECOVERY")
    print(f"  Started: {started.isoformat()}")
    print("=" * 64)
    print()

    overall_ok = True
    for i, (label, args) in enumerate(STEPS, start=1):
        print(f"[{i}/{len(STEPS)}] {label}...")
        try:
            result = subprocess.run(
                [PYTHON] + args,
                cwd=str(REPO),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            print("    TIMEOUT after 60s")
            overall_ok = False
            continue
        if result.returncode != 0:
            err_tail = "\n      ".join((result.stderr or "").splitlines()[-4:])
            print(f"    FAILED — exit={result.returncode}\n      {err_tail}")
            overall_ok = False
            # Don't bail — try the next step in case it provides more context
            continue
        # Pull last 2-3 useful lines from stdout
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
        # Filter ANSI color codes (Windows terminal renders them as bracket text)
        import re as _re
        lines = [_re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in lines]
        summary = "\n      ".join(lines[-4:]) if lines else "(no output)"
        print(f"    OK\n      {summary}")
        print()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print("=" * 64)
    if overall_ok:
        print(f"  RECOVERY COMPLETE in {elapsed:.1f}s")
        print()
        print("  Hard-refresh the dashboard (Ctrl+F5) to clear cached banners.")
        print("  Verify: http://localhost:8080/api/positions_open should now")
        print("  show the real anchor + position count.")
    else:
        print(f"  RECOVERY FAILED ({elapsed:.1f}s elapsed)")
        print()
        print("  Likely causes:")
        print("    - TWS UI not yet logged in (re-auth, then re-run)")
        print("    - Wrong port (set IBKR_PORT=7497 for paper)")
        print("    - TWS in 'reconnecting...' state — wait 30s and retry")
    print("=" * 64)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
