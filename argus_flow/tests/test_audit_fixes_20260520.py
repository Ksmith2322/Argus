"""Tests for the 2026-05-20 operational audit fixes.

Covers 9 fixes that landed after the 5-agent audit caught patterns the
prior 5/19 cleanup missed:

  P0 #1 — entry-timeout broker-truth check + pending-stops preservation
  P0 #2 — bracket half-armed: cancel stop on target placeOrder failure
  P0 #3 — helio/ibkr_executor: fail-CLOSED on unexpected boundary errors
  P0 #4 — flatten_eod_executor: JPY-aware decimals + qualify contracts
  P0 #7 — _broker_state_allows_exit: fail-CLOSED on broker-unreachable
  P1 #6 — wire pending_fills.write_pending + clear_pending into argus
  P1 #8 — adopt_orphan_position: reject corrupted broker snapshots
  P1 #9 — exit cascade re-checks broker after cancel sleep

These are mostly source-level regression tests because the live behaviour
(IBKR callbacks, multi-process state) is not unit-testable. The point is
to catch silent removal of any of these guards in a future refactor."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNNER_UNIFIED = REPO / "argus_flow" / "runner_unified.py"
IBKR_EXECUTOR = REPO / "helio" / "ibkr_executor.py"
FLATTEN_EOD = REPO / "ops" / "flatten_eod_executor.py"


def _runner_src() -> str:
    return RUNNER_UNIFIED.read_text(encoding="utf-8")


def _executor_src() -> str:
    return IBKR_EXECUTOR.read_text(encoding="utf-8")


def _flatten_src() -> str:
    return FLATTEN_EOD.read_text(encoding="utf-8")


# ── P0 #1: entry-timeout broker-truth check ────────────────────────────────

def test_entry_timeout_has_broker_truth_check():
    """The entry-timeout cascade must query broker BEFORE cancelling +
    clearing state. If broker shows position, the order filled and the
    runner should adopt it (with strategy-intended stops) instead of
    cancel-and-clear."""
    src = _runner_src()
    idx = src.find("def _check_order_timeouts(")
    assert idx >= 0
    # The entry-side block is the first if-block after the def
    block_end = src.find("if s.exit_pending", idx)
    entry_block = src[idx:block_end]
    assert "ENTRY_RACE_RESOLVED" in entry_block, (
        "Entry-timeout cascade lost its broker-truth check. Without it, "
        "every FX entry that hits the 60s timeout (24 instances on 5/19-20) "
        "cancels-and-clears local state, then adopts via reconciliation "
        "with synthetic stops 2-5× wider than the strategy intended."
    )
    assert "_read_broker_position" in entry_block
    # Must use _pending_stop_px / _pending_target_px when adopting
    assert "_pending_stop_px" in entry_block
    assert "_pending_target_px" in entry_block


# ── P0 #2: bracket half-armed cancel stop ─────────────────────────────────

def test_bracket_cancels_stop_if_target_placeOrder_fails():
    """_submit_bracket_orders must cancel the already-live stop if the
    target placeOrder throws. Otherwise the caller's emergency MKT exit
    fires alongside the live OCA stop → LONG→SHORT cascade."""
    src = _runner_src()
    idx = src.find("def _submit_bracket_orders(")
    assert idx >= 0
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end]
    # Must have separate try around target placement
    assert "target placeOrder failed" in body or "BRACKET HALF-ARMED" in body, (
        "_submit_bracket_orders has no inner try around target placeOrder. "
        "If target throws (Error 110, throttle, network), stop is live alone "
        "→ caller emergency-exits alongside the stop → LONG→SHORT cascade."
    )
    # Must cancel the stop on target failure
    target_fail_idx = body.find("target_exc")
    assert target_fail_idx >= 0
    cancel_region = body[target_fail_idx:target_fail_idx + 500]
    assert "_cancel_order_by_id(s.stop_order_id" in cancel_region


# ── P0 #3: Greek-family real-money fail-closed ────────────────────────────

def test_ibkr_executor_real_money_fails_closed_on_unexpected():
    """helio/ibkr_executor.py:190 + :277 used to log 'allowing paper path'
    on unexpected exceptions in enforce_real_money_boundary. Codex X5
    sweep missed this file. Must now return None (refuse trade) on any
    boundary-check error."""
    src = _executor_src()
    # The old fail-open log line must be gone from active code (allow
    # the phrase in comments since the BUGFIX docstring references it).
    active_lines = [
        ln for ln in src.splitlines()
        if "allowing paper path" in ln and not ln.lstrip().startswith("#")
    ]
    assert not active_lines, (
        "helio/ibkr_executor.py has an active line containing 'allowing "
        "paper path' (not a comment). That was the X5 fail-open pattern. "
        f"Lines: {active_lines}"
    )
    # Must REFUSE on unexpected errors (BOTH bracket + market paths)
    assert src.count("REAL_MONEY_BOUNDARY: REFUSING") >= 2, (
        "Expected REFUSING log in both submit_bracket and submit_market "
        "boundary-error paths."
    )


# ── P0 #4: flatten_eod JPY decimal + qualify contracts ───────────────────

def test_flatten_eod_jpy_aware_decimals():
    """ops/flatten_eod_executor.py:83 used `round(ref * buffer, 5)` —
    same JPY bug as runner_unified.py before the 5/19 fix. Must now
    check for JPY and use 3 decimals."""
    src = _flatten_src()
    # The old hardcoded ,5) on FX path must be gone
    # (might still appear elsewhere — focus on the FX branch)
    cash_idx = src.find('if sec_type == "CASH"')
    assert cash_idx >= 0
    cash_block_end = src.find("else:", cash_idx)
    cash_block = src[cash_idx:cash_block_end]
    assert "is_jpy" in cash_block or "JPY" in cash_block, (
        "flatten_eod_executor.py CASH branch must detect JPY pairs. "
        "Without it, USDJPY/CADJPY/EURJPY flatten orders get Warning 110."
    )
    # Must qualify contracts (Error 321 fix from emergency_close)
    assert "qualifyContracts" in src, (
        "flatten_eod_executor.py must qualify positions() contracts to "
        "avoid Error 321 'Missing order exchange' on FX."
    )


def test_flatten_eod_uses_gtc_for_fx():
    """Same TIF fix as runner_unified — DAY on FX silently expires
    at broker roll boundary. Use GTC."""
    src = _flatten_src()
    cash_idx = src.find('if sec_type == "CASH"')
    cash_end = src.find("else:", cash_idx)
    cash_block = src[cash_idx:cash_end]
    assert 'tif = "GTC"' in cash_block or "'GTC'" in cash_block, (
        "FX flatten must use TIF=GTC to survive broker-day rollover. "
        "DAY-flagged FX exits expire mid-flatten."
    )


# ── P0 #7: _broker_state_allows_exit fail-closed ─────────────────────────

def test_broker_state_unreachable_fails_closed():
    """When _read_broker_position returns None (network blip, IB
    disconnect), _broker_state_allows_exit must REFUSE the exit — not
    pass through. The 5/20 'passthrough' was itself fail-open and
    introduces the exact catastrophe the helper was supposed to prevent
    in the cascade-retry path."""
    src = _runner_src()
    idx = src.find("def _broker_state_allows_exit(")
    assert idx >= 0
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end]
    assert "broker_unreachable_passthrough" not in body, (
        "_broker_state_allows_exit still passes through on broker-unreachable. "
        "That's fail-OPEN inside the cascade — network blip → duplicate exit."
    )
    assert "broker_unreachable_fail_closed" in body, (
        "_broker_state_allows_exit must return False on broker-unreachable "
        "with reason 'broker_unreachable_fail_closed'."
    )


# ── P1 #6: pending_fills wired into argus ─────────────────────────────────

def test_submit_real_entry_writes_pending_fills():
    """_submit_real_entry must call pending_fills.write_pending() so a
    runner crash during the 60s pending-entry window has a recovery
    record. argus_flow/logs/_pending_fills.jsonl was 0 bytes until this
    fix."""
    src = _runner_src()
    idx = src.find("def _submit_real_entry(")
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end]
    assert "write_pending" in body, (
        "_submit_real_entry has no write_pending call. Crash during "
        "pending-entry window = no recovery record, falls back to "
        "synthetic-stop orphan adoption (which is broken)."
    )
    assert "from helio.pending_fills import write_pending" in body


def test_on_fill_clears_pending_fills_on_entry():
    """_on_fill entry branch must call clear_pending(order_id) so the
    queue doesn't accumulate already-filled entries."""
    src = _runner_src()
    idx = src.find("def _on_fill(")
    assert idx >= 0
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end]
    assert "clear_pending" in body, (
        "_on_fill entry branch must clear filled entries from "
        "pending_fills queue or reconcile_pending will re-adopt them "
        "on next runner restart."
    )


# ── P1 #8: adopt_orphan_position sanity check ─────────────────────────────

def test_adopt_orphan_rejects_garbage_prices():
    """IBKR sent corrupted snapshots on 5/19 (position=-1.0 avgCost=-202).
    adopt_orphan_position blindly accepted them and submitted orders
    against negative prices. Must now sanity-check before adoption."""
    src = _runner_src()
    idx = src.find("def adopt_orphan_position(")
    assert idx >= 0
    end = src.find("\n    def ", idx + 10)
    body = src[idx:end]
    assert "broker_avg_cost <= 0" in body, (
        "adopt_orphan_position has no negative-price guard. Will re-accept "
        "corrupted IBKR snapshots and submit orders against bad data."
    )
    assert "ADOPT_ORPHAN REFUSED" in body
    # Must also reject sub-lot phantom positions
    assert "abs(broker_qty) < 1" in body


# ── P1 #9: cascade re-checks broker after cancel sleep ───────────────────

def test_exit_cascade_rechecks_broker_after_cancel_30s_arm():
    """The 30s-elapsed retry arm cancels then re-submits. _cancel_order_by_id
    sleeps 2s during which the original order can fill. Must re-check
    broker before submitting the IOC retry."""
    src = _runner_src()
    idx = src.find("EXIT TIMEOUT:")
    assert idx >= 0
    # Look ahead 2000 chars for the 30s-arm region
    arm = src[idx:idx + 2500]
    assert "EXIT_FILLED_DURING_CANCEL" in arm, (
        "30s-elapsed retry arm has no fill-during-cancel re-check. The 2s "
        "sleep in _cancel_order_by_id can swallow a fill; without this "
        "guard the IOC retry duplicates the exit."
    )


def test_exit_cascade_rechecks_broker_after_cancel_60s_arm():
    """Same fix applied to the 60s-elapsed GTC retry arm."""
    src = _runner_src()
    idx = src.find("EXIT STUCK: IOC retry also failed")
    assert idx >= 0
    arm = src[idx:idx + 2500]
    assert "EXIT_FILLED_DURING_CANCEL" in arm, (
        "60s GTC retry arm has no fill-during-cancel re-check."
    )


# ── Cross-cutting: no 'allowing trade' / 'allowing paper path' remain ────

def test_no_allowing_trade_in_execution_modules():
    """After the X5 sweep + today's follow-up, no execution-path file
    should contain 'allowing trade' or 'allowing paper path' anymore."""
    forbidden = ["allowing trade", "allowing paper path"]
    files = [
        REPO / "argus_flow" / "runner_unified.py",
        REPO / "helio" / "ibkr_executor.py",
        REPO / "helio" / "ibkr_execution.py",
        REPO / "helio" / "cluster_exposure.py",
    ]
    offenders: list[str] = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        for phrase in forbidden:
            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                # Skip if the line is a comment OR a string literal
                # (docstrings/log messages referencing the old phrase are
                # allowed; only ACTIVE log statements are forbidden).
                if phrase not in line.lower():
                    continue
                if stripped.startswith("#"):
                    continue
                if stripped.startswith(('"', "'")) or stripped.startswith(('f"', "f'")):
                    continue
                # Allow it inside a comment-block / docstring referencing
                # the old behaviour: detect by checking if line contains
                # 'BUGFIX' or 'previously' or 'used to'
                low = line.lower()
                if any(k in low for k in ("bugfix", "previously", "used to", "old fail-open", "was the x5")):
                    continue
                offenders.append(f"{p.name}:{lineno}  {line.strip()[:100]}")
    assert not offenders, (
        "Fail-OPEN phrases found in active execution paths:\n  " + "\n  ".join(offenders)
    )
