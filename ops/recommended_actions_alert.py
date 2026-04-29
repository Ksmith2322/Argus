"""recommended_actions_alert — push notifications for changes to the
Recommended Actions panel.

Reads /api/recommended_actions, diffs against the last-seen state stored on
disk, and posts to Discord for:
  - New high-priority actions (P1 KILL_CANDIDATE, QUARANTINE) — operator
    needs to know without watching the dashboard
  - Resolutions of previously-flagged actions — closes the loop

State stored at argus_flow/logs/recommended_actions_seen.json. First run
just records the current state without alerting (no false alarms on
script first-deployment).

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.recommended_actions_alert

Schedule alongside the other hourly checks in managed_truth_loop. Idempotent
— same state in/out = no alerts.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SEEN_PATH = REPO / "argus_flow" / "logs" / "recommended_actions_seen.json"
DASHBOARD_URL = "http://localhost:8080"

# Severity floor for sending alerts. P1 always alerts; P2 alerts only on
# new SCOPE_DOWN/PROMOTE_REVIEW (actionable, not just informational); P3
# (alpha-negative) does NOT alert — it's a watchlist signal, not urgent.
ALERT_PRIORITIES = {1, 2}


def _load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {"actions": {}, "first_run": True}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"actions": {}, "first_run": True}


def _save_seen(state: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["last_run_utc"] = datetime.now(timezone.utc).isoformat()
    SEEN_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _action_key(a: dict) -> str:
    """Stable key for diffing — strategy + action type. Reason is prose so
    we don't include it; reason changes shouldn't trigger re-alerts."""
    return f"{a.get('strategy', '?')}|{a.get('action', '?')}"


def _fetch_recommendations() -> dict:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{DASHBOARD_URL}/api/recommended_actions", timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"status": "fetch_error", "error": str(e), "actions": []}


def _format_alert(a: dict, kind: str) -> str:
    """Discord-friendly message body."""
    pri = a.get("priority", 9)
    action = a.get("action", "?")
    strat = a.get("strategy", "?")
    reason = a.get("reason", "")
    cites = a.get("data_citations", []) or []
    if kind == "new":
        emoji = "🚨" if pri == 1 else "⚠️"
        head = f"{emoji} **NEW [P{pri}] {action}** — `{strat}`"
    elif kind == "resolved":
        head = f"✅ **RESOLVED [P{pri}] {action}** — `{strat}`"
    else:
        head = f"**[{kind.upper()}] [P{pri}] {action}** — `{strat}`"
    body = reason
    cite_block = "\n" + "\n".join(f"  • {c}" for c in cites[:4]) if cites else ""
    return f"{head}\n{body}{cite_block}"


def main() -> int:
    seen = _load_seen()
    seen_keys: set[str] = set((seen.get("actions") or {}).keys())
    first_run = bool(seen.get("first_run"))

    data = _fetch_recommendations()
    if data.get("status") == "fetch_error":
        print(f"ERROR: could not fetch recommendations: {data.get('error')}", file=sys.stderr)
        return 1

    current_actions = {_action_key(a): a for a in data.get("actions", [])}
    current_keys = set(current_actions.keys())

    new_keys = current_keys - seen_keys
    resolved_keys = seen_keys - current_keys

    sent_count = 0
    if first_run:
        print(f"First run — recording {len(current_keys)} current actions; no alerts sent")
    else:
        # Send alerts for NEW high-priority actions
        for k in sorted(new_keys):
            a = current_actions[k]
            if a.get("priority", 9) not in ALERT_PRIORITIES:
                continue
            msg = _format_alert(a, "new")
            try:
                from argus_flow.ops.discord_alerts import send_discord
                ok = send_discord(content=msg)
                print(f"  NEW alert sent={ok}: {k}")
                if ok:
                    sent_count += 1
            except Exception as e:
                print(f"  NEW alert FAILED for {k}: {e}", file=sys.stderr)
        # Send alerts for RESOLVED previously-flagged actions
        for k in sorted(resolved_keys):
            prev = (seen.get("actions") or {}).get(k, {})
            if prev.get("priority", 9) not in ALERT_PRIORITIES:
                continue
            msg = _format_alert(prev, "resolved")
            try:
                from argus_flow.ops.discord_alerts import send_discord
                ok = send_discord(content=msg)
                print(f"  RESOLVED alert sent={ok}: {k}")
                if ok:
                    sent_count += 1
            except Exception as e:
                print(f"  RESOLVED alert FAILED for {k}: {e}", file=sys.stderr)

    # Persist the new state
    _save_seen({
        "actions": {k: a for k, a in current_actions.items()},
        "first_run": False,
    })
    print(f"Done. Current: {len(current_keys)} actions (new={len(new_keys)}, resolved={len(resolved_keys)}, alerts_sent={sent_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
