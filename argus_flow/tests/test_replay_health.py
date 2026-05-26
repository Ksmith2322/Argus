"""Tests for the Pre-Trade Replay Bridge (helio/replay_health.py).

Verifies the asymmetric fail-open/fail-closed rationale documented in
the module:
  - DRIFT detected → REFUSE new entries (the actual guard)
  - replay_health.json missing → ALLOW (don't break fleet at boot)
  - replay_health.json stale (>max_age_hours) → REFUSE + WARN
  - REPLAY_BRIDGE_DISABLED env var → ALLOW
  - Strategy not in SUPPORTED_STRATEGIES → ALLOW
  - Strategy not yet listed → ALLOW with reason="absent"
  - Strategy status="warming" → ALLOW (fleet still in warmup window)
  - Strategy status="ok", n_blocking_mismatches=0 → ALLOW
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helio import replay_health


def _write_health(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "replay_health.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── Fail-open cases (allow=True) ────────────────────────────────────────

def test_empty_strategy_label_passes_through():
    """No strategy label → bridge skipped (legacy callers don't pass it)."""
    r = replay_health.check_replay_health("", path=Path("/nonexistent"))
    assert r.allow is True
    assert r.reason == "absent"


def test_env_disable_overrides_everything(tmp_path, monkeypatch):
    """REPLAY_BRIDGE_DISABLED=1 short-circuits even when drift is present."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {
                "n_blocking_mismatches": 99, "status": "drift",
            }
        }
    })
    monkeypatch.setenv("REPLAY_BRIDGE_DISABLED", "1")
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "disabled"


def test_strategy_not_in_supported_list_passes(tmp_path):
    """Newly-added strategies that haven't had a replay function written
    yet should not be blocked — only enforce on strategies we can
    actually verify."""
    r = replay_health.check_replay_health(
        "forge_some_new_strategy", path=tmp_path / "missing.json",
    )
    assert r.allow is True
    assert r.reason == "absent"


def test_health_file_missing_passes_with_unread(tmp_path):
    """At fleet boot before the first nightly cron has run, the health
    file may not exist yet. Don't break the fleet."""
    p = tmp_path / "never_created.json"
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "unread"


def test_health_file_unreadable_passes(tmp_path):
    """JSON typo or corruption in the health file should not break the
    fleet — log + allow."""
    p = tmp_path / "replay_health.json"
    p.write_text("{not json", encoding="utf-8")
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "unread"


def test_strategy_absent_from_health_file_passes(tmp_path):
    """Strategy in SUPPORTED list but not yet in the health-file payload
    (first cron run hasn't reached it) → allow."""
    p = _write_health(tmp_path, {
        "strategies": {"forge_gld_pm_long": {"status": "ok", "n_blocking_mismatches": 0}}
    })
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "absent"


def test_warming_status_passes(tmp_path):
    """Fleet just reset, still inside the WARMING_THRESHOLD_DAYS window.
    Replay events haven't had time to fill — allow."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {
                "status": "warming",
                "n_blocking_mismatches": 0,
                "days_since_epoch": 3.2,
            }
        }
    })
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "warming"


def test_status_ok_passes(tmp_path):
    """Happy path — strategy evaluated, zero blocking mismatches."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {"status": "ok", "n_blocking_mismatches": 0}
        }
    })
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "ok"
    assert r.n_mismatches == 0


# ── Fail-closed cases (allow=False) ────────────────────────────────────

def test_drift_detected_blocks(tmp_path):
    """Blocking mismatches above threshold → REFUSE."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {"status": "drift", "n_blocking_mismatches": 7}
        }
    })
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is False
    assert r.reason == "drift"
    assert r.n_mismatches == 7


def test_drift_with_zero_threshold_blocks_on_one_mismatch(tmp_path):
    """Default max_mismatch=0 — one is enough to block."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {"status": "drift", "n_blocking_mismatches": 1}
        }
    })
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is False
    assert r.reason == "drift"


def test_drift_at_threshold_passes(tmp_path):
    """max_mismatch=1 tolerates up to 1 anomaly."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {"status": "drift", "n_blocking_mismatches": 1}
        }
    })
    r = replay_health.check_replay_health(
        "forge_xs_momentum", path=p, max_mismatch=1,
    )
    assert r.allow is True
    assert r.reason == "ok"


def test_stale_health_file_blocks(tmp_path):
    """Health file older than max_age_hours → REFUSE (nightly cron broken)."""
    p = _write_health(tmp_path, {
        "strategies": {
            "forge_xs_momentum": {"status": "ok", "n_blocking_mismatches": 0}
        }
    })
    # Make the file appear ancient
    old_mtime = (datetime.now(timezone.utc) - timedelta(hours=72)).timestamp()
    os.utime(p, (old_mtime, old_mtime))
    r = replay_health.check_replay_health(
        "forge_xs_momentum", path=p, max_age_hours=36.0,
    )
    assert r.allow is False
    assert r.reason == "stale"
    assert r.age_hours >= 36.0


# ── Round-trip: write_health_file + read produces matching guard outputs

def test_write_then_read_consistent(tmp_path):
    """write_health_file writes the same fields check_replay_health reads."""
    healths = [
        replay_health.StrategyHealth(
            strategy="forge_xs_momentum",
            n_replay_events=5, n_ledger_fills=5, n_matched=5,
            n_mismatches=0, n_blocking_mismatches=0,
            mismatch_counts={},
            status="ok",
            last_check_ts=datetime.now(timezone.utc).isoformat(),
            days_since_epoch=42.0,
        ),
    ]
    p = tmp_path / "replay_health.json"
    replay_health.write_health_file(healths, path=p)
    r = replay_health.check_replay_health("forge_xs_momentum", path=p)
    assert r.allow is True
    assert r.reason == "ok"
