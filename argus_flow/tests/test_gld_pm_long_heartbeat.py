"""Heartbeat regressions for forge.gld_pm_long.runner."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from forge.gld_pm_long import runner as gld


def test_write_heartbeat_marks_current_git_sha(monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat.json"
    monkeypatch.setattr(gld, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(gld, "_git_sha", lambda: "testsha")
    monkeypatch.setattr(gld, "_config_hash", lambda: "testcfg")

    state = {"trade_count": 0, "open_trade": None}
    gld._write_heartbeat(state, "2026-05-22 19:30:00+00:00")

    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["git_sha"] == "testsha"
    assert data["config_hash"] == "testcfg"
    assert data["last_eval_ts"] == "2026-05-22 19:30:00+00:00"


def test_last_eval_ts_from_heartbeat_preserves_previous_eval(monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat.json"
    hb.write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "last_eval_ts": "2026-05-22 19:30:00+00:00",
    }), encoding="utf-8")
    monkeypatch.setattr(gld, "HEARTBEAT_PATH", hb)

    assert gld._last_eval_ts_from_heartbeat() == "2026-05-22 19:30:00+00:00"


def test_heartbeat_refresh_updates_timestamp(monkeypatch, tmp_path):
    hb = tmp_path / "heartbeat.json"
    monkeypatch.setattr(gld, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(gld, "_git_sha", lambda: "testsha")
    monkeypatch.setattr(gld, "_config_hash", lambda: "testcfg")

    state = {"trade_count": 0, "open_trade": None}
    gld._write_heartbeat(state, "")
    first = json.loads(hb.read_text(encoding="utf-8"))["ts"]
    time.sleep(0.01)
    gld._write_heartbeat(state, "")
    second = json.loads(hb.read_text(encoding="utf-8"))["ts"]

    assert second > first
