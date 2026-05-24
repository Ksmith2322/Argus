"""ops/daily_health_check.py — single daily-cron entrypoint that
composes every standalone safety/audit check into one report.

Designed to be the canonical scheduled task: replaces the original
ArgusAutoPause cron and folds in tonight's additions (orphan phantom
detector, real-money preflight, halt-flag check, recommendations).

What it runs (in order, each best-effort with try/except so a single
component failure doesn't block the rest):

  1. helio.auto_pause.run() — snapshot live gate, append history,
     write recommendations, post Discord on fresh PAUSE_RECOMMENDED
     crossings. (Existing behavior.)

  2. ops.audit.run_orphan_phantom_check — scan killed-strategy state.json
     files for phantom open_trade / current_picks / etc. that survived
     past their kill date.

  3. helio.real_money_preflight — run the 12-point card against every
     active strategy. Summary line only (don't spam Discord with full
     check details).

  4. Halt-flag / Flatten-flag presence check (visible signal).

OUTPUT

  - stdout: human-readable combined summary
  - argus_flow/logs/daily_health_check.json: machine-readable for cron
  - Discord: ONE consolidated message per problem class (not one per
    issue) so the channel doesn't flood

EXIT CODES

  0  no issues
  1  WARNING-tier issues (consecutive-WARNING gate, preflight YELLOW
     on active strategies, etc.)
  2  RED-tier issues (PAUSE_RECOMMENDED gate, phantoms found, HALT.flag
     present, preflight BLOCKED on active strategies)

USAGE

  python -m ops.daily_health_check                # default
  python -m ops.daily_health_check --no-discord   # silent
  python -m ops.daily_health_check --json         # JSON to stdout

CRON (Windows scheduled task) — replaces the ArgusAutoPause task:

  $action = New-ScheduledTaskAction `
      -Execute "C:\\Argus\\.venv\\Scripts\\python.exe" `
      -Argument "-m ops.daily_health_check" `
      -WorkingDirectory "C:\\Argus\\repo"
  $trigger = New-ScheduledTaskTrigger -Daily -At "10:00 PM"
  Register-ScheduledTask -TaskName "ArgusDailyHealth" `
      -Action $action -Trigger $trigger -Force
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[1]
LOG_PATH = REPO / "argus_flow" / "logs" / "daily_health_check.json"
HALT_FLAG = REPO / "argus_flow" / "logs" / "HALT.flag"
FLATTEN_FLAG = REPO / "argus_flow" / "logs" / "FLATTEN_EOD.flag"


def _run_auto_pause() -> dict:
    """Run helio.auto_pause.run() — snapshot live gate + write
    recommendations. Returns a summary."""
    try:
        from helio.auto_pause import run as _ap_run, RECOMMENDATIONS_PATH
        rc = _ap_run()
        if RECOMMENDATIONS_PATH.exists():
            data = json.loads(RECOMMENDATIONS_PATH.read_text(encoding="utf-8"))
            counts = data.get("alert_tier_counts", {})
        else:
            counts = {}
        return {
            "component": "auto_pause",
            "exit_code": int(rc),
            "alert_tier_counts": counts,
            "status": ("RED" if rc == 2
                       else "YELLOW" if rc == 1
                       else "GREEN"),
        }
    except Exception as exc:
        return {"component": "auto_pause", "error": str(exc), "status": "ERROR"}


def _run_orphan_check() -> dict:
    """Run the orphan phantom detector. Returns summary + phantom list."""
    try:
        from ops.audit.run_orphan_phantom_check import (
            _load_killed_strategies, _scan_state, _has_phantom,
        )
        killed = sorted(_load_killed_strategies())
        phantoms = []
        for strategy in killed:
            state = _scan_state(strategy)
            if state is None:
                continue
            has, desc = _has_phantom(state)
            if has:
                phantoms.append({"strategy": strategy, "phantom": desc})
        return {
            "component": "orphan_phantom",
            "n_killed_scanned": len(killed),
            "n_phantoms": len(phantoms),
            "phantoms": phantoms,
            "status": "RED" if phantoms else "GREEN",
        }
    except Exception as exc:
        return {"component": "orphan_phantom", "error": str(exc),
                "status": "ERROR"}


def _run_preflight() -> dict:
    """Run the 12-point preflight against every active strategy.
    Returns summary only (one line per strategy)."""
    try:
        from helio.real_money_preflight import evaluate_strategy
        try:
            from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER
            strategies = sorted(ACTIVE_ROSTER)
        except Exception:
            strategies = []
        summaries = []
        worst = "GREEN"
        for s in strategies:
            try:
                p = evaluate_strategy(s)
                summaries.append({
                    "strategy": s,
                    "verdict": p.verdict,
                    "green": p.n_green, "yellow": p.n_yellow, "red": p.n_red,
                })
                if p.verdict == "BLOCKED":
                    worst = "RED"
                elif p.verdict == "BLOCKED_PENDING_REVIEW" and worst != "RED":
                    worst = "YELLOW"
            except Exception as exc:
                summaries.append({"strategy": s, "error": str(exc)})
                worst = "RED"
        return {
            "component": "preflight",
            "strategies": summaries,
            "worst_verdict": worst,
            "status": worst,
        }
    except Exception as exc:
        return {"component": "preflight", "error": str(exc),
                "status": "ERROR"}


def _run_flag_check() -> dict:
    """Check for fleet-halt / flatten flags. RED if either present."""
    halt = HALT_FLAG.exists()
    flatten = FLATTEN_FLAG.exists()
    return {
        "component": "flag_check",
        "halt_flag": halt,
        "flatten_flag": flatten,
        "status": "RED" if (halt or flatten) else "GREEN",
    }


def _post_discord(reports: list[dict]) -> bool:
    """Post a single combined Discord message summarising RED+YELLOW
    findings only. Best-effort; failures swallowed."""
    red = [r for r in reports if r.get("status") == "RED"]
    yellow = [r for r in reports if r.get("status") == "YELLOW"]
    error = [r for r in reports if r.get("status") == "ERROR"]
    if not (red or yellow or error):
        return False  # all GREEN — no need to spam channel
    try:
        from ops.notify import send_discord
        lines = [f"**Daily health check — {datetime.now(timezone.utc).date()}**"]
        for r in red:
            lines.append(f"[RED] `{r['component']}`: {_one_liner(r)}")
        for r in yellow:
            lines.append(f"[YELLOW] `{r['component']}`: {_one_liner(r)}")
        for r in error:
            lines.append(f"[ERR] `{r['component']}`: {r.get('error', '?')}")
        send_discord("\n".join(lines))
        return True
    except Exception:
        return False


def _one_liner(report: dict) -> str:
    component = report.get("component", "?")
    if component == "auto_pause":
        return f"verdict tiers: {report.get('alert_tier_counts', {})}"
    if component == "orphan_phantom":
        n = report.get("n_phantoms", 0)
        names = [p["strategy"] for p in report.get("phantoms", [])]
        return f"{n} phantom(s) found: {names}"
    if component == "preflight":
        return (f"worst: {report.get('worst_verdict', '?')}; "
                f"{[s.get('strategy') + '=' + s.get('verdict', '?') for s in report.get('strategies', [])]}")
    if component == "flag_check":
        flags = []
        if report.get("halt_flag"): flags.append("HALT.flag")
        if report.get("flatten_flag"): flags.append("FLATTEN_EOD.flag")
        return f"flags present: {flags}" if flags else "no flags"
    return str(report)


def evaluate(post_discord: bool = True) -> dict:
    """Run every component + return the combined report. The function
    is unit-testable; main() wraps it for CLI ergonomics."""
    reports = [
        _run_auto_pause(),
        _run_orphan_check(),
        _run_preflight(),
        _run_flag_check(),
    ]
    if post_discord:
        _post_discord(reports)
    # Worst-status determines exit code
    statuses = {r.get("status") for r in reports}
    if "RED" in statuses or "ERROR" in statuses:
        worst = "RED"
    elif "YELLOW" in statuses:
        worst = "YELLOW"
    else:
        worst = "GREEN"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "worst_status": worst,
        "reports": reports,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-discord", action="store_true",
                          help="Don't post to Discord (silent mode)")
    parser.add_argument("--json", action="store_true",
                          help="Emit JSON to stdout instead of human-readable")
    args = parser.parse_args(argv)

    result = evaluate(post_discord=not args.no_discord)

    # Always persist the JSON log
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text(json.dumps(result, indent=2, default=str),
                          encoding="utf-8")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print("=" * 72)
        print(f"DAILY HEALTH CHECK — {datetime.now(timezone.utc).isoformat()}")
        print("=" * 72)
        print(f"Worst status: {result['worst_status']}")
        print()
        for r in result["reports"]:
            sym = {"GREEN": " + ", "YELLOW": " ~ ", "RED": " ! ",
                     "ERROR": " X "}.get(r.get("status"), " ? ")
            print(f" {sym} {r['component']:<18} {r.get('status', '?'):<6}  "
                  f"{_one_liner(r)}")
        print()
        print(f"JSON log: {LOG_PATH}")

    # Exit code maps to worst status (cron-friendly)
    if result["worst_status"] in ("RED", "ERROR"):
        return 2
    if result["worst_status"] == "YELLOW":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
