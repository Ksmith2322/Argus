"""Alert Escalation — consolidated Discord alerts for system events.
Checks all monitoring reports and sends alerts on issues.

Usage:
    python -m argus_flow.ops.alert_escalation
"""
from __future__ import annotations
import json, os, time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ALERT_COOLDOWN_S = 3600  # 1 hour per category
ALERT_HISTORY_PATH = LOGS / "alert_history.json"

# ---------------------------------------------------------------------------
# Report readers — each returns a list of (category, message, color) tuples
# ---------------------------------------------------------------------------

def _check_kill_discipline() -> list[tuple[str, str, int]]:
    """Check kill_discipline_report.json for KILL or WATCH verdicts."""
    path = LOGS / "kill_discipline_report.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    alerts: list[tuple[str, str, int]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        verdict = item.get("verdict", "").upper()
        pair = item.get("pair", item.get("symbol", "unknown"))
        if verdict == "KILL":
            alerts.append((
                "kill_discipline",
                f"KILL triggered for **{pair}**: {item.get('reason', 'no reason')}",
                0xFF0000,  # red
            ))
        elif verdict == "WATCH":
            alerts.append((
                "kill_discipline",
                f"WATCH flag for **{pair}**: {item.get('reason', 'no reason')}",
                0xFFA500,  # orange
            ))
    return alerts


def _check_divergence() -> list[tuple[str, str, int]]:
    """Check divergence_report.json for KILL or WATCH."""
    path = LOGS / "divergence_report.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    alerts: list[tuple[str, str, int]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        verdict = item.get("verdict", "").upper()
        pair = item.get("pair", item.get("symbol", "unknown"))
        if verdict == "KILL":
            alerts.append((
                "divergence",
                f"Divergence KILL for **{pair}**: {item.get('reason', '')}",
                0xFF0000,
            ))
        elif verdict == "WATCH":
            alerts.append((
                "divergence",
                f"Divergence WATCH for **{pair}**: {item.get('reason', '')}",
                0xFFA500,
            ))
    return alerts


def _check_artifact_divergence() -> list[tuple[str, str, int]]:
    """Check artifact_divergence_report.json for DIVERGENT status."""
    path = LOGS / "artifact_divergence_report.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    alerts: list[tuple[str, str, int]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        status = item.get("status", "").upper()
        if status == "DIVERGENT":
            pair = item.get("pair", item.get("symbol", "unknown"))
            alerts.append((
                "artifact_divergence",
                f"Artifact DIVERGENT for **{pair}**: {item.get('details', '')}",
                0xFF0000,
            ))
    return alerts


def _check_risk_oversight() -> list[tuple[str, str, int]]:
    """Check risk_oversight_report.json for RED or YELLOW flags."""
    path = LOGS / "risk_oversight_report.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    alerts: list[tuple[str, str, int]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        level = item.get("level", item.get("status", "")).upper()
        metric = item.get("metric", item.get("check", "unknown"))
        if level == "RED":
            alerts.append((
                "risk_oversight",
                f"Risk RED — **{metric}**: {item.get('message', '')}",
                0xFF0000,
            ))
        elif level == "YELLOW":
            alerts.append((
                "risk_oversight",
                f"Risk YELLOW — **{metric}**: {item.get('message', '')}",
                0xFFFF00,
            ))
    return alerts


def _check_promotion_gate() -> list[tuple[str, str, int]]:
    """Check promotion_gate_report.json for PROMOTE verdicts."""
    path = LOGS / "promotion_gate_report.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    alerts: list[tuple[str, str, int]] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        verdict = item.get("verdict", "").upper()
        if verdict == "PROMOTE":
            pair = item.get("pair", item.get("symbol", "unknown"))
            alerts.append((
                "promotion_gate",
                f"PROMOTE eligible: **{pair}** passed all gates!",
                0x00FF00,  # green
            ))
    return alerts


# ---------------------------------------------------------------------------
# Cooldown tracking
# ---------------------------------------------------------------------------

def _load_alert_history() -> dict:
    """Load alert history from disk."""
    if ALERT_HISTORY_PATH.exists():
        try:
            return json.loads(ALERT_HISTORY_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_alert_history(history: dict) -> None:
    """Persist alert history to disk."""
    ALERT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_HISTORY_PATH.write_text(json.dumps(history, indent=2))


def _is_cooled_down(history: dict, category: str, now_ts: float) -> bool:
    """Return True if category is still in cooldown."""
    last_ts = history.get(category, 0)
    return (now_ts - last_ts) < ALERT_COOLDOWN_S


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    """Check all reports and send consolidated Discord alert."""
    from argus_flow.ops.discord_alerts import send_discord

    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    history = _load_alert_history()

    # Collect alerts from all sources
    checkers = [
        _check_kill_discipline,
        _check_divergence,
        _check_artifact_divergence,
        _check_risk_oversight,
        _check_promotion_gate,
    ]
    all_alerts: list[tuple[str, str, int]] = []
    for checker in checkers:
        try:
            all_alerts.extend(checker())
        except Exception as exc:
            print(f"Warning: {checker.__name__} failed: {exc}")

    if not all_alerts:
        print(f"[{now.isoformat()}] No alert conditions found.")
        return

    # Filter by cooldown
    actionable: list[tuple[str, str, int]] = []
    for category, message, color in all_alerts:
        if _is_cooled_down(history, category, now_ts):
            print(f"  Cooldown active for '{category}', skipping: {message}")
            continue
        actionable.append((category, message, color))

    if not actionable:
        print(f"[{now.isoformat()}] All alerts in cooldown — nothing sent.")
        return

    # Build Discord embeds — one per alert
    embeds = []
    categories_sent: set[str] = set()
    for category, message, color in actionable:
        embeds.append({
            "title": f"Argus Alert: {category.replace('_', ' ').title()}",
            "description": message,
            "color": color,
            "timestamp": now.isoformat(),
        })
        categories_sent.add(category)

    # Discord allows max 10 embeds per message
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        ok = send_discord(
            content=f"**Argus Alert Escalation** — {now.strftime('%Y-%m-%d %H:%M UTC')}",
            embeds=batch,
        )
        if ok:
            print(f"  Sent {len(batch)} embed(s) to Discord.")
        else:
            print(f"  Failed to send Discord alert batch.")

    # Update cooldown timestamps
    for cat in categories_sent:
        history[cat] = now_ts
    history["_last_run"] = now.isoformat()
    _save_alert_history(history)

    print(f"[{now.isoformat()}] Sent {len(actionable)} alert(s), "
          f"updated cooldown for: {', '.join(sorted(categories_sent))}")


if __name__ == "__main__":
    run()
