"""Tests for ops.audit.order_lifecycle_audit (Codex X7).

Validates the heuristic classifier against synthetic source files and
runs an integration smoke against the real repo.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _classify_text(text: str, call_line_idx: int = None):
    """Helper: classify a fake source file. Call line auto-located if not given."""
    from ops.audit.order_lifecycle_audit import (
        classify_call_site, PLACE_ORDER_RE, _enclosing_function,
    )
    lines = text.splitlines(keepends=True)
    if call_line_idx is None:
        for i, l in enumerate(lines):
            if PLACE_ORDER_RE.search(l):
                call_line_idx = i
                break
    if call_line_idx is None:
        return None
    site = classify_call_site(lines, call_line_idx)
    site.function = _enclosing_function(lines, call_line_idx)
    return site


# ─── classification cases ──────────────────────────────────────────

def test_classifies_protected_pattern():
    """Pre-allocate orderId + state-write before placeOrder + try/except."""
    text = """
def submit():
    try:
        oid = self._pre_allocate_order_id(ib)
        order.orderId = oid
        s.entry_order_id = str(oid)
        s.save()
        trade = ib.placeOrder(self.contract, order)
    except Exception as e:
        pass
"""
    site = _classify_text(text)
    assert site.classification == "PROTECTED"
    assert site.has_pre_allocate
    assert site.has_state_write_before
    assert site.has_try_except


def test_classifies_unprotected_naked_call():
    """No try/except, no pre-alloc."""
    text = """
def submit():
    trade = ib.placeOrder(self.contract, order)
    s.entry_order_id = str(trade.order.orderId)
"""
    site = _classify_text(text)
    assert site.classification == "UNPROTECTED"
    assert not site.has_try_except


def test_classifies_partial_protection():
    """Pre-alloc + try/except but no state-write before placeOrder."""
    text = """
def submit():
    try:
        oid = self._pre_allocate_order_id(ib)
        order.orderId = oid
        trade = ib.placeOrder(self.contract, order)
        s.entry_order_id = str(trade.order.orderId)
    except Exception:
        pass
"""
    site = _classify_text(text)
    assert site.classification == "PARTIAL"
    assert site.has_pre_allocate
    assert site.has_try_except


def test_classifies_unverified_only_try_except():
    """Has try/except but no clear pre-alloc pattern."""
    text = """
def submit():
    try:
        trade = ib.placeOrder(self.contract, order)
    except Exception:
        pass
"""
    site = _classify_text(text)
    assert site.classification == "UNVERIFIED"
    assert site.has_try_except
    assert not site.has_pre_allocate


def test_function_name_extraction():
    text = """
class X:
    def my_submit_func(self):
        ib.placeOrder(contract, order)
"""
    site = _classify_text(text)
    assert site.function == "my_submit_func"


def test_no_call_sites_in_empty_file():
    from ops.audit.order_lifecycle_audit import audit_file
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as f:
        f.write("# nothing to see\nx = 1\n")
        path = Path(f.name)
    try:
        sites = audit_file(path)
        assert sites == []
    finally:
        path.unlink()


# ─── integration smoke ─────────────────────────────────────────────

def test_audit_repo_runs_without_error():
    from ops.audit.order_lifecycle_audit import audit_repo
    report = audit_repo()
    assert report["total_call_sites"] > 0
    assert "PROTECTED" in report["classification_counts"] or \
           "PARTIAL" in report["classification_counts"] or \
           "UNVERIFIED" in report["classification_counts"] or \
           "UNPROTECTED" in report["classification_counts"]


def test_audit_repo_finds_runner_unified_calls():
    """The runner_unified.py file has ~14 placeOrder calls — audit should
    find them."""
    from ops.audit.order_lifecycle_audit import audit_repo
    report = audit_repo()
    files = [s["file"] for s in report["sites"]]
    assert any("runner_unified.py" in f for f in files), (
        "audit failed to find argus_flow/runner_unified.py call sites"
    )


def test_render_markdown_returns_string():
    from ops.audit.order_lifecycle_audit import render_markdown
    fake_report = {
        "generated_at": "now",
        "total_call_sites": 1,
        "files_with_calls": ["x.py"],
        "classification_counts": {"PROTECTED": 1},
        "sites": [{
            "file": "x.py", "line": 1, "function": "f",
            "classification": "PROTECTED", "notes": "ok",
            "context_excerpt": "",
            "has_try_except": True, "has_pre_allocate": True,
            "has_state_write_before": True, "has_trade_capture": True,
        }],
    }
    md = render_markdown(fake_report)
    assert "# Order Lifecycle Audit" in md
    assert "x.py" in md
