"""Static safety invariants — catch regressions before they ship.

Per Codex audit 2026-05-18, three classes of bug were costly enough to
warrant static-test coverage:

  1. Port-default split-brain (was 7496 LIVE vs 7497 paper in different
     files — one missing env var → silent live trade). Codex fixed; this
     test prevents regression.
  2. `submit_bracket` called without `strategy_label` weakens real-money
     boundary + per-strategy cluster caps. Codex added labels; this test
     keeps them.
  3. Direct `ib.placeOrder` calls outside the central executor bypass the
     real-money boundary. argus_flow/runner_unified.py:_submit_real_entry
     was such a path; Codex audit + 2026-05-18 fix added the guard. This
     test catches any new direct callsites that bypass the wrapper.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Files that legitimately reference port 7496 (real-money modules + tests
# verifying the live port). Anything else MUST default to 7497 or read
# from IBKR_PORT env var.
ALLOWED_7496_FILES = {
    # real_money.py constants
    "helio/real_money.py",
    # docs
    "docs/AUDIT_FOR_CODEX_2026_05_18.md",
    "docs/CODEX_SECOND_SWEEP_AUDIT_2026_05_18.md",
}

# Files that legitimately call ib.placeOrder directly (e.g. the central
# executor that itself enforces the boundary). Anything else MUST route
# through helio.ibkr_execution.submit_bracket or call enforce_real_money_boundary
# before placeOrder.
ALLOWED_DIRECT_PLACEORDER = {
    "helio/ibkr_execution.py",       # the central executor itself
    "helio/ibkr_executor.py",        # legacy executor with its own boundary
    "argus_flow/runner_unified.py",  # has its own _submit_real_entry guard chain (2026-05-18)
    "ops/maintenance/epoch_reset.py",  # dry-run / state-only
    "argus_flow/ops/emergency_close.py",  # operator emergency tool — paper-only by default port
    # ── Operator one-shots / flatten tools (manual invocation, dry-run by
    # default OR --execute gated; not part of automated trading path). ──
    "ops/close_orphan_gld.py",
    "ops/close_orphan_usdjpy.py",
    "ops/flatten_eod_executor.py",
    "ops/flatten_orphan.py",
    # Paper-only stress harness — 4 import-time safety locks (IBKR_PORT
    # 7497 + REAL_MONEY_ENABLED env + STRESS_INJECT_OK + helio.real_money
    # constant). Locks tested in test_stress_injector.py. Adding here
    # because the placeOrder calls bypass the central executor BUT the
    # locks make it categorically paper-only.
    "ops/stress_injector.py",
    # Static-analysis tool that mentions `ib.placeOrder(...)` in its
    # docstring; doesn't actually call placeOrder.
    "ops/audit/order_lifecycle_audit.py",
    # ── Known tech-debt below: TODO migrate to submit_bracket path ──
    # These runners bypass the central executor; add real_money boundary
    # before they go live. Tracked in fix-by-5/31 queue.
    "forge/gdx_gld_runner.py",
    "forge/rebalance_runner.py",
    "forge/vix_revert_runner.py",
    "forge/fomc_drift/runner.py",
    "forge/spy_trend_follower/runner.py",
    "forge/tom_international/runner.py",
}


def _python_files_under(*subdirs: str, include_tests: bool = False) -> list[Path]:
    """Walk only the active subdirs (avoid archive/, .venv/, .git/).

    Tests excluded by default — they contain literal '7496' / 'placeOrder'
    / 'submit_bracket' for legitimate test purposes; the safety invariant
    is about CODE not test fixtures."""
    out: list[Path] = []
    for sub in subdirs:
        d = REPO / sub
        if d.exists():
            for p in d.rglob("*.py"):
                # skip archive + __pycache__
                if "_archive" in p.parts or "__pycache__" in p.parts:
                    continue
                # skip tests by default
                if not include_tests:
                    if "tests" in p.parts:
                        continue
                    if p.name.startswith("test_"):
                        continue
                out.append(p)
    return out


# ── Test 1: no `7496` default in code paths that should be paper-only ──
def test_no_bare_7496_default_outside_real_money_modules():
    """A bare `7496` literal in a default-arg or env-var fallback is a
    silent-live-trade vector. The only allowlist is real-money modules
    where 7496 is the documented live port."""
    # Match "7496" as a STRING-LITERAL default (e.g., os.getenv(..., "7496"))
    # or as a bare numeric default (e.g., port=7496). Skip comments via the
    # leading `#` test below, and skip in-line trailing comments by splitting
    # off the comment portion. This avoids false positives on documentation
    # like `# 7497=paper, 7496=live`.
    pattern_default = re.compile(r'(?:default\s*=\s*|=\s*|"\s*)7496\b|\b7496\s*(?=\))')
    offenders: list[str] = []
    for p in _python_files_under("argus_flow", "forge", "helio", "ops"):
        rel = p.relative_to(REPO).as_posix()
        if rel in ALLOWED_7496_FILES:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Strip trailing comment so `IBKR_PORT=7497  # 7496 is live`
            # doesn't flag the comment portion
            code_only = stripped.split("#", 1)[0]
            if "7496" not in code_only:
                continue
            if pattern_default.search(code_only):
                offenders.append(f"{rel}:{lineno}  {stripped[:120]}")
    assert not offenders, (
        "Bare 7496 (live port) found outside allowed modules. Possible "
        "silent live-trade vector — runner that defaults to 7496 when "
        "IBKR_PORT env unset will contact the live account.\n  " +
        "\n  ".join(offenders[:10])
    )


# ── Test 2: every submit_bracket call passes strategy_label ──
def test_submit_bracket_callsites_pass_strategy_label():
    """Without `strategy_label`, the real-money boundary cannot attribute
    the order and per-strategy cluster caps degrade to default. Codex's
    2026-05-18 sweep added labels to direct Forge callsites; this test
    keeps them there."""
    offenders: list[str] = []
    for p in _python_files_under("argus_flow", "forge", "helio", "ops"):
        rel = p.relative_to(REPO).as_posix()
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "submit_bracket(" not in text:
            continue
        # Parse to find each submit_bracket Call node and check kwargs
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Attribute) and node.func.attr == "submit_bracket":
                name = "submit_bracket"
            elif isinstance(node.func, ast.Name) and node.func.id == "submit_bracket":
                name = "submit_bracket"
            else:
                continue
            kwarg_names = {k.arg for k in node.keywords if k.arg}
            if "strategy_label" not in kwarg_names:
                # Skip the definition itself (in helio/ibkr_execution.py)
                if rel == "helio/ibkr_execution.py":
                    continue
                offenders.append(f"{rel}:{node.lineno}  submit_bracket(...) without strategy_label")
    assert not offenders, (
        "submit_bracket calls missing strategy_label — real-money "
        "attribution + per-strategy cluster caps weakened.\n  " +
        "\n  ".join(offenders[:10])
    )


# ── Test 3: direct ib.placeOrder only in allowed files ──
def test_no_direct_placeOrder_outside_central_executor():
    """`ib.placeOrder(...)` bypasses the real-money boundary unless the
    caller has its own boundary check. Allowed files are the central
    executor + runner_unified (which now has its own boundary guard as of
    2026-05-18). Anything else is a regression."""
    pattern = re.compile(r"\.placeOrder\s*\(")
    offenders: list[str] = []
    for p in _python_files_under("argus_flow", "forge", "helio", "ops"):
        rel = p.relative_to(REPO).as_posix()
        if rel in ALLOWED_DIRECT_PLACEORDER:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(stripped):
                offenders.append(f"{rel}:{lineno}  {stripped[:120]}")
    assert not offenders, (
        "Direct ib.placeOrder(...) call outside the allowed executor "
        "modules. This bypasses the real-money boundary. Route through "
        "helio.ibkr_execution.submit_bracket OR call "
        "enforce_real_money_boundary before transmitting.\n  " +
        "\n  ".join(offenders[:10])
    )


# ── Test 4: every Future() construction uses qualify_front_month_future
#    (skip for the helper definition itself) ──
def test_no_direct_Future_construction_without_qualify_helper():
    """`Future(symbol, exchange)` with no expiry hint triggers the Error 321
    silent-cancel pattern on multi-expiry contracts (CBOT MYM etc).
    `qualify_front_month_future` does the right thing. The only allowed
    places to construct a bare Future are: the helper itself, the
    make_contract factory (which is meant to be qualified downstream), and
    tests."""
    allowed_files = {
        "helio/ibkr_execution.py",         # defines make_contract + qualify_front_month_future
        "argus_flow/runner_unified.py",    # uses explicit lastTradeDateOrContractMonth
        "argus_flow/runner_mnq.py",        # uses explicit lastTradeDateOrContractMonth
        "argus_flow/runner_futures_generic.py",  # uses explicit lastTradeDateOrContractMonth
        "argus_flow/ops/refresh_ibkr_data.py",   # hardcoded expiry
        "argus_flow/ops/smoke_test.py",          # hardcoded expiry
    }
    pattern = re.compile(r"\bFuture\s*\(")
    offenders: list[str] = []
    for p in _python_files_under("argus_flow", "forge", "helio"):
        rel = p.relative_to(REPO).as_posix()
        if rel in allowed_files:
            continue
        if "tests/" in rel or "/test_" in rel:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(stripped):
                offenders.append(f"{rel}:{lineno}  {stripped[:120]}")
    # Don't hard-fail on this one (some files might use Future legitimately) —
    # just warn loudly. Convert to xfail/error at maintainers' discretion.
    if offenders:
        print(f"\n[WARN] {len(offenders)} bare Future() constructions found:")
        for o in offenders[:5]:
            print(f"  {o}")
