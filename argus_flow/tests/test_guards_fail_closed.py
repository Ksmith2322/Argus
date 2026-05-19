"""Guards must fail closed (Codex audit 2026-05-18 X5).

Several entry-time guards used to swallow exceptions and log "allowing
trade" — which means a bug in the guard itself silently disabled it.
The new policy: on exception, the guard returns a refusal so the trade
is blocked. The operator can then investigate why the guard broke."""
from __future__ import annotations

import pytest


def test_would_breach_cluster_cap_refuses_when_anchor_raises(monkeypatch):
    """If get_sizing_anchor_usd raises, cluster cap check must refuse the
    trade (was: returned None → "OK to trade")."""
    import helio.cluster_exposure as ce
    import helio.fleet_sizing as fs

    def _broken_anchor():
        raise RuntimeError("synthetic: broker connection broken")

    monkeypatch.setattr(fs, "get_sizing_anchor_usd", _broken_anchor)

    result = ce.would_breach_cluster_cap(
        symbol="USDJPY", direction="long", notional_usd=10_000.0,
    )
    assert result == "ANCHOR_UNAVAILABLE", (
        f"Expected fail-closed sentinel 'ANCHOR_UNAVAILABLE', got {result!r}. "
        f"The guard must refuse the trade, not return None."
    )


def test_would_breach_cluster_cap_refuses_when_anchor_zero(monkeypatch):
    """An anchor of $0 effectively disables every multiplicative cap; refuse."""
    import helio.cluster_exposure as ce
    import helio.fleet_sizing as fs

    monkeypatch.setattr(fs, "get_sizing_anchor_usd", lambda: 0.0)
    result = ce.would_breach_cluster_cap(
        symbol="USDJPY", direction="long", notional_usd=10_000.0,
    )
    assert result == "ANCHOR_NON_POSITIVE"


def test_would_breach_cluster_cap_refuses_when_margin_check_raises(monkeypatch):
    """The margin-aware arm used to swallow exceptions. Now it must refuse."""
    import helio.cluster_exposure as ce
    import helio.fleet_sizing as fs

    monkeypatch.setattr(fs, "get_sizing_anchor_usd", lambda: 30_000.0)
    # Force the margin-aware arm to raise by breaking its dependency.
    def _broken_ratio(_):
        raise RuntimeError("synthetic: margin ratio table missing")
    monkeypatch.setattr(ce, "_maint_margin_ratio", _broken_ratio)
    # Pre-clusters/per-strat caps need positive notional but small enough
    # not to trip earlier caps. 1 USD won't breach anything.
    result = ce.would_breach_cluster_cap(
        symbol="USDJPY", direction="long", notional_usd=1.0,
    )
    assert result == "MARGIN_CHECK_ERROR", (
        f"Expected fail-closed sentinel 'MARGIN_CHECK_ERROR', got {result!r}."
    )


def test_cluster_exposure_module_no_longer_logs_allowing_trade():
    """Source-level regression: the substring 'allowing trade' must not
    appear in any guard's exception path. Catches anyone restoring the
    old fail-open behaviour by reflex."""
    from pathlib import Path

    paths = [
        Path(__file__).resolve().parents[2] / "helio" / "cluster_exposure.py",
        Path(__file__).resolve().parents[2] / "helio" / "ibkr_execution.py",
        Path(__file__).resolve().parents[2] / "helio" / "ibkr_executor.py",
    ]
    offenders: list[str] = []
    for p in paths:
        text = p.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "allowing trade" in line.lower():
                offenders.append(f"{p.name}:{lineno}  {line.strip()[:120]}")
    assert not offenders, (
        "Guard exception path still logs 'allowing trade' — that is the "
        "fail-OPEN pattern Codex X5 flagged. Replace with 'REFUSING' and "
        "return a sentinel/raise:\n  " + "\n  ".join(offenders)
    )
