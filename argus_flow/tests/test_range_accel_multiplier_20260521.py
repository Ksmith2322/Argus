"""Regression test for the 2026-05-21 GBPUSD range_accel paper_stress wiring.

Background: the paper_stress multiplier was wired into the MTF strategy
(USDJPY, CADJPY) via InstrumentRunner.__init__ on 2026-05-20, but GBPUSD
uses the range_accel strategy which has its own threshold knobs and
wasn't covered. That gap was flagged in the activation commit and is
closed in this batch.

The wiring scales `trigger.range_pct_min` (the barrier filter that
determines whether ANY signal can fire for that bar). The dist
thresholds aren't touched — they route direction (long vs short),
not whether to enter, so they're not the right knob to multiply.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from argus_flow.runner_unified import check_trigger_fx


# ─── source-level checks ─────────────────────────────────────────────────

def test_check_trigger_fx_reads_paper_stress_multiplier():
    """Source-level: the new wiring must read paper_stress_multiplier
    from cfg and pass it to helio.paper_stress.apply."""
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def check_trigger_fx(")
    assert idx >= 0
    # Body up to next def
    end = src.find("\ndef ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    assert 'cfg.get("paper_stress_multiplier", 1.0)' in body, (
        "paper_stress_multiplier read missing from check_trigger_fx — "
        "GBPUSD won't respond to the activity-multiplication knob"
    )
    assert "from helio.paper_stress import apply" in body
    assert 'knob="range_pct_min"' in body, (
        "knob label missing — paper_stress logs will be unattributed"
    )


def test_check_trigger_fx_does_not_scale_dist_thresholds():
    """Direction thresholds (dist_long_threshold, dist_short_threshold)
    must NOT be scaled by the multiplier — they route direction, not
    whether to enter. Scaling them would either narrow or widen the
    long/short zones, which changes the strategy semantically."""
    src = (Path(__file__).resolve().parents[2] / "argus_flow" / "runner_unified.py").read_text(encoding="utf-8")
    idx = src.find("def check_trigger_fx(")
    end = src.find("\ndef ", idx + 10)
    body = src[idx:end] if end > idx else src[idx:]
    # The dist lookups should be unmodified — no paper_stress_apply around them
    assert 'direction_cfg.get("dist_long_threshold"' in body
    assert 'direction_cfg.get("dist_short_threshold"' in body
    # Verify the only knob name passed to paper_stress is range_pct_min
    import re
    knobs = re.findall(r'knob="([^"]+)"', body)
    assert knobs == ["range_pct_min"], (
        f"Unexpected paper_stress knob targets in check_trigger_fx: {knobs}. "
        f"Only range_pct_min should be multiplier-scaled."
    )


# ─── behavioral checks ──────────────────────────────────────────────────

def _features(range_pct: float, range_accel: float = 0.5,
              hour: int = 10, dist_from_low: float = 0.2) -> dict:
    return {
        "range_pct": range_pct,
        "range_accel": range_accel,
        "vol_z": 1.0,
        "hour": hour,
        "dist_from_low": dist_from_low,
    }


def _cfg(range_pct_min: float = 0.0005,
         paper_stress_multiplier: float = 1.0,
         symbol: str = "GBPUSD") -> dict:
    return {
        "symbol": symbol,
        "trigger": {
            "range_pct_min": range_pct_min,
            "range_accel_min": 0.0,
            "session_start_utc": 0,
            "session_end_utc": 23,
        },
        "direction": {
            "dist_long_threshold": 0.35,
            "dist_short_threshold": 0.65,
        },
        "paper_stress_multiplier": paper_stress_multiplier,
    }


def test_mult_1_preserves_default_behavior():
    """mult=1.0 must produce identical output to no-multiplier baseline."""
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=1.0)
    # Just under threshold — should reject
    assert check_trigger_fx(_features(range_pct=0.00049), cfg) is None
    # At threshold — should fire (long because dist_from_low=0.2 < 0.35)
    assert check_trigger_fx(_features(range_pct=0.0005), cfg) == "long"


def test_mult_below_1_lowers_threshold_on_paper(monkeypatch):
    """mult=0.5 on paper port halves the effective range_pct_min.
    A range that wouldn't qualify at the base threshold now does."""
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=0.5)
    # range_pct=0.0003 is BELOW base 0.0005 but ABOVE effective 0.00025
    # → should fire under multiplier, would reject without
    assert check_trigger_fx(_features(range_pct=0.0003), cfg) == "long"


def test_mult_above_1_raises_threshold_on_paper(monkeypatch):
    """mult=2.0 on paper port doubles the effective range_pct_min,
    making the strategy STRICTER (fewer fires). Sanity case."""
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=2.0)
    # range_pct=0.0008 ABOVE base 0.0005 but BELOW effective 0.001
    # → should reject under stricter multiplier
    assert check_trigger_fx(_features(range_pct=0.0008), cfg) is None
    # range_pct=0.0011 ABOVE effective 0.001 → should fire
    assert check_trigger_fx(_features(range_pct=0.0011), cfg) == "long"


def test_mult_ignored_on_live_port(monkeypatch, caplog):
    """Safety contract: multiplier MUST be a no-op when not on paper port.
    Mirrors the safety check in helio.paper_stress.apply."""
    monkeypatch.setenv("IBKR_PORT", "7496")  # live port
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=0.5)
    with caplog.at_level(logging.WARNING):
        # range_pct=0.0003 below base 0.0005, multiplier would lower to
        # 0.00025; but on live port the multiplier is ignored so this
        # should reject
        result = check_trigger_fx(_features(range_pct=0.0003), cfg)
    assert result is None
    # And the helper should have logged the IGNORED reason
    assert any("paper_stress IGNORED" in r.message for r in caplog.records)


def test_short_direction_still_routes_correctly(monkeypatch):
    """When the multiplier lets a signal through, the dist-based direction
    routing must still apply — the multiplier does NOT affect direction."""
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=0.5)
    # range qualifies under multiplier; dist=0.8 > 0.65 → short
    feats = _features(range_pct=0.0003, dist_from_low=0.8)
    assert check_trigger_fx(feats, cfg) == "short"


def test_ambiguous_zone_still_rejects(monkeypatch):
    """Multiplier lets the signal through the barrier filter, but if dist
    is in the 0.35-0.65 ambiguous zone, no entry. Multiplier doesn't widen
    the direction zone."""
    monkeypatch.setenv("IBKR_PORT", "7497")
    monkeypatch.delenv("REAL_MONEY_ENABLED", raising=False)
    cfg = _cfg(range_pct_min=0.0005, paper_stress_multiplier=0.5)
    feats = _features(range_pct=0.0003, dist_from_low=0.5)
    assert check_trigger_fx(feats, cfg) is None


# ─── config-level check ─────────────────────────────────────────────────

def test_gbpusd_config_has_paper_stress_multiplier_field():
    """The GBPUSD config file must have the field exposed so the
    operator can flip it without code changes. Default 1.0 = no-op
    until allocation is raised."""
    import json
    cfg_path = Path(__file__).resolve().parents[2] / "argus_flow" / "configs" / "gbpusd_range_paper_v1.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "paper_stress_multiplier" in cfg, (
        "paper_stress_multiplier field missing from GBPUSD config; "
        "operator has no way to scale activity without editing code"
    )
    mult = cfg["paper_stress_multiplier"]
    # Should be in [0.1, 10.0] sanity bounds matching helio.paper_stress
    assert 0.1 <= float(mult) <= 10.0, (
        f"GBPUSD paper_stress_multiplier={mult} outside [0.1, 10.0]"
    )
