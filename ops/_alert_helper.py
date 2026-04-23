"""ops/_alert_helper.py — Shared Discord-post helper + cooldown state.

Used by silent_block_check, canonical_reconcile, schema_validator. All of
them surface different flavors of "something's off" and need to respect a
per-item cooldown to avoid spam.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOGS_DIR = REPO / "argus_flow" / "logs"


def _load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def post_discord(title: str, description: str, color: int = 16753920) -> bool:
    """Best-effort Discord post. False if no webhook or any error. Never raises.
    On failure, records an entry in argus_flow/logs/alert_send_failures.jsonl
    so the dashboard can show 'alerts tried but failed' — avoids silent-fail."""
    _load_env()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        _log_send_failure(title, description, "no_webhook_configured")
        return False
    payload = {
        "embeds": [{
            "title": title,
            "description": description[:1900],
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    err_reason: str | None = None
    try:
        # Cloudflare's Discord WAF blocks Python's default urllib UA (error 1010,
        # user-agent signature ban). Browser-like UA + Discord webhook boilerplate
        # passes cleanly. Matches the pattern used by the existing `requests`-based
        # discord_alerts.py (which works because `requests` defaults to a
        # python-requests UA that Cloudflare allows).
        req = urllib.request.Request(
            webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ArgusBot/1.0 (+https://github.com/Ksmith2322/Argus)",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                return True
            err_reason = f"http_{resp.status}"
    except urllib.error.HTTPError as e:
        body_preview = ""
        try:
            body_preview = e.read()[:200].decode("utf-8", errors="replace")
        except Exception:
            pass
        err_reason = f"http_{e.code}:{body_preview[:80]}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        err_reason = f"{type(e).__name__}:{str(e)[:80]}"

    if err_reason:
        _log_send_failure(title, description, err_reason)
    return False


def _log_send_failure(title: str, description: str, reason: str) -> None:
    """Append a JSONL record of the failed alert so dashboard / user can see
    'alerts tried but Discord is blocked' — avoids the silent-fail failure mode
    that hit us on 2026-04-23 (Cloudflare error 1010 IP ban)."""
    try:
        path = LOGS_DIR / "alert_send_failures.jsonl"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "title": title,
                "description": description[:200],
                "reason": reason,
            }) + "\n")
    except Exception:
        pass


def load_cooldown_state(path_name: str) -> dict:
    """Load cooldown state JSON by short name (stored under argus_flow/logs/)."""
    p = LOGS_DIR / path_name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cooldown_state(path_name: str, state: dict) -> None:
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / path_name).write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def should_alert(state: dict, key: str, cooldown_min: int) -> bool:
    """Check if we should send an alert for this key based on cooldown.
    Returns True if cooldown elapsed (caller should send + update state)."""
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    last = state.get(key, {}).get("last_alert_epoch", 0)
    return now_epoch - last >= cooldown_min * 60


def mark_alerted(state: dict, key: str, reason: str = "") -> None:
    """Mark a key as alerted at the current time."""
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    state[key] = {
        "last_alert_epoch": now_epoch,
        "last_reason": reason,
    }
