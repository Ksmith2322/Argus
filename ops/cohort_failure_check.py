"""ops/cohort_failure_check.py — Detect silent cohort_report failures.

Scans `argus_flow/logs/cohort_report.log` for "WITH FAILURES" markers in the
last 25h. If found, posts a Discord alert with 24h cooldown to avoid spam.

Why: 2026-04-22 → 2026-04-25, the nightly ArgusCohortReport task failed every
night (duplicate managed_truth daemons → lock contention → refresh exit 2 →
cohort script throws → apollo/hermes/titan/ares/gdx_gld never run). Nobody
noticed for 4 days because no alert fired. This script + its hourly invocation
from managed_truth_loop catches the next instance immediately.

Run via managed_truth_loop hourly check OR standalone:
    python -m ops.cohort_failure_check
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COHORT_LOG = REPO / "argus_flow" / "logs" / "cohort_report.log"
LOOKBACK_HOURS = 25  # nightly task fires once/24h, allow buffer
COOLDOWN_MIN = 24 * 60  # alert at most once per day
ALERT_KEY = "cohort_report_failure"

sys.path.insert(0, str(REPO / "ops"))
from _alert_helper import (
    post_discord,
    load_cooldown_state,
    save_cooldown_state,
    should_alert,
    mark_alerted,
)


def _line_ts(line: str) -> datetime | None:
    """Extract leading timestamp like '[2026-04-24 23:00:01]' from a log line."""
    m = re.match(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _scan_for_failures() -> list[tuple[datetime, str]]:
    """Return list of (ts, line) for any 'WITH FAILURES' marker in the lookback window."""
    if not COHORT_LOG.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    hits: list[tuple[datetime, str]] = []
    try:
        # Read tail only to avoid loading huge log
        with COHORT_LOG.open(encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-500:]
    except Exception:
        return []
    for line in lines:
        if "WITH FAILURES" not in line:
            continue
        ts = _line_ts(line)
        if ts is None or ts < cutoff:
            continue
        hits.append((ts, line.strip()))
    return hits


def main() -> int:
    hits = _scan_for_failures()
    if not hits:
        return 0

    state = load_cooldown_state("cohort_failure_alert_state.json")
    if not should_alert(state, ALERT_KEY, COOLDOWN_MIN):
        return 0  # in cooldown — already alerted

    # Build a compact alert message
    most_recent_ts, most_recent_line = hits[-1]
    age_h = (datetime.now(timezone.utc) - most_recent_ts).total_seconds() / 3600.0
    description = (
        f"Cohort report failed {len(hits)} time(s) in last {LOOKBACK_HOURS}h.\n"
        f"Most recent: {most_recent_ts.isoformat()} ({age_h:.1f}h ago)\n"
        f"Line: {most_recent_line[:300]}\n\n"
        f"Likely cause: duplicate managed_truth daemons OR refresh_managed_truth lock.\n"
        f"Check: `Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        f"Where-Object {{$_.CommandLine -like '*managed_truth*'}}`\n"
        f"See: memory/reference_failure_modes.md mode #1"
    )
    posted = post_discord(
        title="ARGUS COHORT REPORT FAILURE",
        description=description,
        color=0xE53935,  # red
    )
    if posted:
        mark_alerted(state, ALERT_KEY, reason=f"{len(hits)} failures, latest {most_recent_ts.isoformat()}")
        save_cooldown_state("cohort_failure_alert_state.json", state)
    return 1


if __name__ == "__main__":
    sys.exit(main())
