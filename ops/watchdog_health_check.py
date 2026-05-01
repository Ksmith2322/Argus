"""watchdog_health_check — detect and re-launch a dead ArgusWatchdog.

The ArgusWatchdog scheduled task is a Windows BootTrigger — it only fires
when the machine boots. If the long-running supervisor process crashes
mid-session (as happened 2026-04-20 → 2026-04-30, 10 days unsupervised),
nothing brings it back until the next reboot.

This helper is the auto-recover for that. Logic:

  1. Check `argus_flow/logs/watchdog_managed.log` last-modified age.
  2. If stale > STALE_THRESHOLD_MIN (default 10), the supervisor loop is
     dead — every iteration writes either a HEARTBEAT line or alert.
  3. Relaunch via `Start-Process powershell.exe -File ops/watchdog_managed.ps1`.
     The watchdog's internal Global mutex (`ArgusManagedWatchdog`) prevents
     double-launch — if a process IS still alive but just paused, the
     mutex check in the script makes the new instance exit cleanly.
  4. Discord-alert on respawn so we know it happened.

Cooldown: don't respawn more than once per RESPAWN_COOLDOWN_MIN (30 min).
Prevents thrash if the watchdog crashes immediately on startup.

Usage:
    cd c:/Argus/repo
    C:/Argus/.venv/Scripts/python.exe -m ops.watchdog_health_check

Schedule: hourly via managed_truth_loop.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WATCHDOG_LOG = REPO / "argus_flow" / "logs" / "watchdog_managed.log"
WATCHDOG_SCRIPT = REPO / "ops" / "watchdog_managed.ps1"
STATE_PATH = REPO / "argus_flow" / "logs" / "watchdog_health_state.json"

STALE_THRESHOLD_MIN = 10
RESPAWN_COOLDOWN_MIN = 30


def _post_discord(title: str, msg: str, color: int = 16753920) -> None:
    """Best-effort Discord alert. Mirrors silent_block_check pattern."""
    import urllib.request, urllib.error  # type: ignore
    env_path = REPO / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    payload = {"embeds": [{
        "title": title, "description": msg[:1900], "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }]}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            _ = r.read()
    except Exception:
        pass


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    if sys.platform != "win32":
        print("watchdog_health_check: Windows-only; skipping")
        return 0
    if not WATCHDOG_SCRIPT.exists():
        print(f"watchdog_health_check: script missing at {WATCHDOG_SCRIPT}")
        return 1

    now = datetime.now(timezone.utc)
    log_exists = WATCHDOG_LOG.exists()
    if log_exists:
        mtime = datetime.fromtimestamp(WATCHDOG_LOG.stat().st_mtime, tz=timezone.utc)
        age_min = (now - mtime).total_seconds() / 60.0
    else:
        mtime = None
        age_min = float("inf")

    if log_exists and age_min < STALE_THRESHOLD_MIN:
        print(f"watchdog_health_check: log fresh ({age_min:.1f}min old) — supervisor alive")
        return 0

    # Stale or missing — check cooldown before respawning
    state = _load_state()
    last_respawn = state.get("last_respawn_epoch", 0)
    cooldown_remaining = (last_respawn + RESPAWN_COOLDOWN_MIN * 60) - now.timestamp()
    if cooldown_remaining > 0:
        print(
            f"watchdog_health_check: log stale ({age_min:.1f}min) but cooldown "
            f"active ({cooldown_remaining/60:.1f}min remaining)"
        )
        return 0

    age_str = f"{age_min:.0f}min" if age_min != float("inf") else "missing"
    print(f"watchdog_health_check: log stale ({age_str}) — relaunching supervisor")

    try:
        subprocess.Popen(
            [
                "powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(WATCHDOG_SCRIPT),
            ],
            cwd=str(REPO),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        print(f"watchdog_health_check: launch failed: {e}")
        _post_discord(
            "Watchdog relaunch FAILED",
            f"Tried to respawn ArgusWatchdog after {age_str} of staleness — `{e}`",
            color=16711680,  # red
        )
        return 1

    state["last_respawn_epoch"] = int(now.timestamp())
    state["last_respawn_iso"] = now.isoformat()
    state["last_respawn_log_age_min"] = age_min if age_min != float("inf") else None
    _save_state(state)

    _post_discord(
        "Watchdog auto-respawned",
        f"Detected stale supervisor log ({age_str}) and relaunched "
        f"`watchdog_managed.ps1`. Internal mutex prevents double-start; "
        f"if a process was somehow still alive, this is a no-op.",
        color=16776960,  # yellow
    )
    print("watchdog_health_check: launched; Discord-notified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
