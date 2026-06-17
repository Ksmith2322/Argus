"""Runtime invariant for killed strategies (Codex audit 2026-05-18 X6).

Tests two enforcement paths:

  1. **Entry-time refusal** in `helio.ibkr_execution.submit_bracket` —
     if `strategy_label` is on the kill registry, the executor refuses
     the entry. Belt-and-suspenders against the static-config invariant
     (allocation_factor=0.0, no_restart=True) drifting out of sync.

  2. **Exposure detection** in `helio.killed_strategy_invariant.find_killed_strategy_orphans` —
     scans broker positions and flags any owned by a killed strategy
     without a matching active unwind ticket."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── 1. Entry-time refusal ──────────────────────────────────────────────────

def test_submit_bracket_source_refuses_killed_strategy():
    """Source-level verification: the executor's killed-strategy guard
    exists and routes through KILLED_STRATEGY_CUTOFFS."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "helio" / "ibkr_execution.py"
    text = src.read_text(encoding="utf-8")
    assert "KILLED_STRATEGY_CUTOFFS" in text, (
        "submit_bracket lost its killed-strategy guard. Re-add the check "
        "that refuses entries when strategy_label is in the kill registry."
    )
    assert "killed_strategy:" in text, (
        "killed_strategy reject_reason missing from submit_bracket; "
        "this is what observability/alerts look for."
    )


# ── 2. Exposure detection ──────────────────────────────────────────────────

def test_find_orphans_returns_empty_when_no_killed_exposure():
    """Positions only in symbols NOT claimed by any killed strategy → no
    orphans. Note that as the kill registry grows, more symbols become
    'claimed'; use a clearly-untouched ticker (FOO_TEST) for this test."""
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {
        "FOO_TEST": {"direction": "LONG", "qty": 100},
    }
    orphans = find_killed_strategy_orphans(
        positions, active_strategy_symbols={"FOO_TEST"},
    )
    assert orphans == []


def test_find_orphans_flags_killed_strategy_position():
    """A position in a symbol claimed by AT LEAST ONE killed strategy
    must be flagged. Multiple killed strategies can claim the same
    symbol (e.g., UVXY → forge_vix_intraday + forge_vix_revert) — the
    test asserts forge_vix_intraday is in the list, not exact count."""
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {
        "UVXY": {"direction": "LONG", "qty": 283},
    }
    orphans = find_killed_strategy_orphans(positions, active_strategy_symbols=())
    assert len(orphans) >= 1
    strategies = {o["strategy"] for o in orphans}
    assert "forge_vix_intraday" in strategies
    target = next(o for o in orphans
                    if o["strategy"] == "forge_vix_intraday")
    assert target["symbol"] == "UVXY"
    assert target["direction"] == "LONG"
    assert target["qty"] == 283
    assert target["has_unwind_ticket"] is False


def test_find_orphans_respects_active_unwind_ticket():
    """An unwind ticket for ONE killed-strategy mapping doesn't affect
    other killed strategies that also claim the symbol — each is
    evaluated independently."""
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {
        "UVXY": {"direction": "LONG", "qty": 283},
    }
    now = datetime.now(timezone.utc)
    tickets = [{
        "strategy": "forge_vix_intraday",
        "symbol": "UVXY",
        "max_qty": 283,
        "operator": "ksmith2322",
        "signed_at": (now - timedelta(hours=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "reason": "Manual flatten of 5/12 orphan",
    }]
    orphans = find_killed_strategy_orphans(
        positions, active_strategy_symbols=(), tickets=tickets, now=now,
    )
    # forge_vix_intraday's orphan now has a ticket
    target = next(o for o in orphans
                    if o["strategy"] == "forge_vix_intraday")
    assert target["has_unwind_ticket"] is True


def test_find_orphans_ignores_expired_ticket():
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {"UVXY": {"direction": "LONG", "qty": 283}}
    now = datetime.now(timezone.utc)
    tickets = [{
        "strategy": "forge_vix_intraday",
        "symbol": "UVXY",
        "max_qty": 283,
        "operator": "ksmith2322",
        "signed_at": (now - timedelta(days=2)).isoformat(),
        "expires_at": (now - timedelta(days=1)).isoformat(),  # expired
        "reason": "old ticket",
    }]
    orphans = find_killed_strategy_orphans(
        positions, active_strategy_symbols=(), tickets=tickets, now=now,
    )
    assert orphans[0]["has_unwind_ticket"] is False


def test_find_orphans_skips_when_active_strategy_claims_symbol():
    """If an active strategy currently trades SPY, a SPY position isn't a
    forge_spy_mean_rev orphan even though spy_mean_rev (killed) traded SPY."""
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {"SPY": {"direction": "LONG", "qty": 13}}
    orphans = find_killed_strategy_orphans(
        positions, active_strategy_symbols=("SPY",),
    )
    assert orphans == []


def test_find_orphans_ignores_flat_positions():
    from helio.killed_strategy_invariant import find_killed_strategy_orphans

    positions = {"UVXY": {"direction": "FLAT", "qty": 0}}
    orphans = find_killed_strategy_orphans(positions, active_strategy_symbols=())
    assert orphans == []


def test_unwind_ticket_file_exists_and_is_empty_by_default():
    """The committed scaffolding file must exist + start with zero active
    tickets so the operator has to consciously add one."""
    import json
    from pathlib import Path

    p = Path(__file__).resolve().parents[2] / "argus_flow" / "configs" / "killed_strategy_unwind_tickets.json"
    assert p.exists(), f"Unwind ticket scaffold missing at {p}"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert "tickets" in data
    assert data["tickets"] == [], (
        f"Unwind tickets file ships non-empty: {data['tickets']!r}. "
        f"Tickets should be added per-incident by the operator, not committed "
        f"as defaults."
    )


def test_killed_symbol_map_aligned_with_kill_registry():
    """Every strategy in KILLED_STRATEGY_CUTOFFS must have a symbol mapping
    in KILLED_STRATEGY_SYMBOLS (so the runtime check can find orphans for
    that strategy)."""
    from helio.killed_strategy_invariant import KILLED_STRATEGY_SYMBOLS
    from helio.roi_filter import KILLED_STRATEGY_CUTOFFS

    missing = [s for s in KILLED_STRATEGY_CUTOFFS if s not in KILLED_STRATEGY_SYMBOLS]
    assert not missing, (
        f"Strategies on the kill registry without a symbol mapping: {missing}. "
        f"Add an entry to helio.killed_strategy_invariant.KILLED_STRATEGY_SYMBOLS "
        f"so the runtime exposure check can detect orphans for these."
    )
