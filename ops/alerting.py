#!/usr/bin/env python3
"""
ops/alerting.py  --  Phase 16 Alert Dispatch

Centralized alerting for Phase 16 events:
  - HALT events
  - Recovery contradictions
  - Drawdown limit breach
  - Invariant failures
  - Fill anomalies
  - Mode transitions

All alerts go through Discord webhook (via notify.py).
Alert deduplication prevents flooding.
"""

import time
from typing import Any, Dict, Optional, Set

# ---------------------------------------------------------------------------
# Alert severity levels
# ---------------------------------------------------------------------------

CRITICAL = "CRITICAL"   # HALT, invariant failure
WARNING = "WARNING"     # mode escalation, consecutive losses
INFO = "INFO"           # recovery pass, mode reset


# ---------------------------------------------------------------------------
# Alert deduplication
# ---------------------------------------------------------------------------

class AlertThrottle:
    """Prevent alert flooding by deduplicating within a window."""

    def __init__(self, cooldown_s: float = 300.0):
        self._cooldown_s = cooldown_s
        self._last_sent: Dict[str, float] = {}

    def should_send(self, alert_key: str) -> bool:
        now = time.time()
        last = self._last_sent.get(alert_key, 0)
        if (now - last) >= self._cooldown_s:
            self._last_sent[alert_key] = now
            return True
        return False

    def mark_sent(self, alert_key: str) -> None:
        self._last_sent[alert_key] = time.time()


# Module-level throttle (5-minute cooldown per alert type)
_throttle = AlertThrottle(cooldown_s=300.0)


# ---------------------------------------------------------------------------
# Alert dispatch
# ---------------------------------------------------------------------------

def send_alert(
    *,
    http: Any,
    webhook_url: str,
    severity: str,
    title: str,
    msg: str,
    alert_key: Optional[str] = None,
    throttle: bool = True,
) -> bool:
    """Send alert via Discord webhook with throttling.

    Returns True if alert was actually sent.
    """
    if not webhook_url:
        return False

    key = alert_key or f"{severity}:{title}"
    if throttle and not _throttle.should_send(key):
        return False

    prefix = {
        CRITICAL: "\u26a0\ufe0f",
        WARNING: "\u26a1",
        INFO: "\u2139\ufe0f",
    }.get(severity, "")

    full_msg = f"{prefix} **[{severity}] {title}**\n{msg}"

    try:
        from notify import maybe_notify_discord
        maybe_notify_discord(http, webhook_url, f"[{severity}] {title}", msg)
        _throttle.mark_sent(key)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pre-built alert helpers
# ---------------------------------------------------------------------------

def alert_halt(http: Any, webhook_url: str, reason: str) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=CRITICAL,
        title="HALT",
        msg=f"System halted: {reason}",
        alert_key="halt",
    )


def alert_mode_transition(
    http: Any, webhook_url: str, prev: str, new: str, reason: str
) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=WARNING,
        title="MODE CHANGE",
        msg=f"{prev} -> {new}: {reason}",
        alert_key=f"mode:{new}",
    )


def alert_invariant_failure(http: Any, webhook_url: str, detail: str) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=CRITICAL,
        title="INVARIANT FAILURE",
        msg=detail,
        alert_key="invariant",
    )


def alert_drawdown_breach(
    http: Any, webhook_url: str, current_dd_pct: float, limit_pct: float
) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=CRITICAL,
        title="DRAWDOWN BREACH",
        msg=f"Current drawdown: {current_dd_pct:.1f}% (limit: {limit_pct:.1f}%)",
        alert_key="drawdown",
    )


def alert_fill_anomaly(http: Any, webhook_url: str, detail: str) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=WARNING,
        title="FILL ANOMALY",
        msg=detail,
        alert_key="fill_anomaly",
    )


def alert_recovery_contradiction(http: Any, webhook_url: str, detail: str) -> bool:
    return send_alert(
        http=http,
        webhook_url=webhook_url,
        severity=CRITICAL,
        title="RECOVERY CONTRADICTION",
        msg=detail,
        alert_key="recovery_contradiction",
    )
