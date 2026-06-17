"""Epoch reset script for the 2026-05-31 cutover.

Implements the procedure documented in
``project_2026_05_31_clean_reset_plan.md``: archive pre-reset trade
evidence, reset per-strategy state counters that were COUNTER_AHEAD,
and prepare a clean slate for post-5/31 evidence accumulation.

**Default behavior is dry-run.** The script lists every move it would
make and writes nothing. Pass ``--execute`` to actually perform the
moves. Idempotent: re-running on the same target is a no-op (already
moved files are skipped).

Preserved (NOT moved): allocation_factors.json, real_money_allowlist.json,
shadow_strategies.json, all code, all configs, all memory files. See the
plan memory for the full preservation list.

Files moved go under
``argus_flow/logs/_archive/pre_reset_<TARGET>/<original_path>``. A
manifest of every move is written to
``argus_flow/logs/_archive/pre_reset_<TARGET>/_manifest.jsonl`` so a
rollback restore is straightforward.

Usage::

    python -m ops.maintenance.epoch_reset --target 20260531
    python -m ops.maintenance.epoch_reset --target 20260531 --execute
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

REPO = Path(__file__).resolve().parents[2]
LOGS = REPO / "argus_flow" / "logs"


# Files to archive (moved to _archive/<target>/<orig>). Globs allowed.
ARCHIVE_GLOBS: tuple[str, ...] = (
    "argus_flow/logs/canonical_fills.jsonl",
    "argus_flow/logs/strategy_scorecard.csv",
    "argus_flow/logs/promotion_gate_report.json",
    "argus_flow/logs/promotion_gate_history.jsonl",
    "argus_flow/logs/promotion_readiness_report.json",
    "argus_flow/logs/evidence_registry.json",
    "argus_flow/logs/artifact_divergence_report.json",
    "argus_flow/logs/decision_engine_report.json",
    "forge/logs/*/trades.csv",
    "forge/logs/*/trades_pre_*.csv",
    "argus_flow/logs/*_trades.csv",
    "argus_flow/logs/risk_oversight_report.json",
    "argus_flow/logs/position_monitor.json",
    "argus_flow/logs/_risk/portfolio_risk_state.json",
)


# State files whose trade_serial / trade_count must reset to 0 after
# archive. Currently the argus pairs whose COUNTER_AHEAD_WHILE_FLAT
# alarm has been firing all week.
COUNTER_RESETS: tuple[tuple[str, ...], ...] = (
    ("argus_flow/logs/cadjpy/state.json",),
    ("argus_flow/logs/gbpusd/state.json",),
    ("argus_flow/logs/usdjpy/state.json",),
)


# Position-state fields to clear on every KILLED strategy's state.json
# during the epoch reset. Without this step, phantom open_trade entries
# survive across resets and inflate cluster_exposure (the 5/22 reset
# left a forge_spy_mean_rev phantom from 4/30 visible all the way
# through 5/24 — this fixes that class of bug).
#
# SAFETY: clearing state.open_trade without verifying the broker is flat
# would create an unmanaged position. The reset assumes:
#   (a) the strategy is in KILLED_STRATEGY_CUTOFFS (caller pre-checked)
#   (b) operator ran emergency_close.py for any real broker positions
#       BEFORE invoking the reset
# A pre-snapshot of the cleared values goes into the archive manifest
# so a post-mortem can always recover what was there.
POSITION_FIELDS_TO_CLEAR: tuple[str, ...] = (
    "open_trade", "open_trades", "open_positions", "current_picks",
)


# Files that MUST exist clean (empty / not present) at start of the new
# epoch. The script will rewrite each as an empty file with a header.
CLEAN_TEMPLATES: tuple[tuple[str, str], ...] = (
    # canonical_fills.jsonl is consumed line-by-line; an empty file is fine.
    ("argus_flow/logs/canonical_fills.jsonl", ""),
)


# Files that should be CONFIRMED absent at start (operator must clear before
# proceeding — the executor refuses to run while these are present).
ABSENT_REQUIRED: tuple[str, ...] = (
    "argus_flow/logs/HALT.flag",
    "argus_flow/logs/FLATTEN_EOD.flag",
)


@dataclass
class PlannedMove:
    src: str
    dst: str
    kind: str  # "archive" | "counter_reset" | "rewrite_template"
    note: str = ""


def _archive_root(target: str) -> Path:
    return LOGS / "_archive" / f"pre_reset_{target}"


def _expand_globs(globs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for pattern in globs:
        out.extend(sorted(REPO.glob(pattern)))
    return [p for p in out if p.is_file()]


def _plan_archive_moves(target: str) -> list[PlannedMove]:
    archive = _archive_root(target)
    moves: list[PlannedMove] = []
    for src in _expand_globs(ARCHIVE_GLOBS):
        rel = src.relative_to(REPO)
        dst = archive / rel
        if dst.exists():
            continue  # idempotent: already archived
        moves.append(PlannedMove(
            src=str(rel), dst=str(dst.relative_to(REPO)), kind="archive",
        ))
    return moves


def _plan_counter_resets(target: str) -> list[PlannedMove]:
    """For each state file in COUNTER_RESETS, plan a counter-only rewrite.
    The state file itself is not moved — only `trade_count` /
    `trade_serial` / `next_trade_num` are zeroed. A pre-reset snapshot of
    the values goes into the archive."""
    moves: list[PlannedMove] = []
    for paths in COUNTER_RESETS:
        for rel in paths:
            src = REPO / rel
            if not src.exists():
                continue
            moves.append(PlannedMove(
                src=rel, dst=rel,
                kind="counter_reset",
                note="zero trade_count / trade_serial / next_trade_num",
            ))
    return moves


def _plan_template_rewrites(target: str) -> list[PlannedMove]:
    moves: list[PlannedMove] = []
    for rel, _content in CLEAN_TEMPLATES:
        moves.append(PlannedMove(
            src=rel, dst=rel, kind="rewrite_template",
            note="rewrite to empty/header",
        ))
    return moves


def _plan_killed_state_clears(target: str) -> list[PlannedMove]:
    """For every strategy in KILLED_STRATEGY_CUTOFFS, plan a clear of
    state.open_trade / open_trades / open_positions / current_picks.
    Skip strategies whose state.json doesn't exist or whose fields are
    already empty (idempotent)."""
    moves: list[PlannedMove] = []
    try:
        from helio.roi_filter import KILLED_STRATEGY_CUTOFFS
    except Exception:
        return moves
    for strategy in sorted(KILLED_STRATEGY_CUTOFFS):
        short = strategy.replace("forge_", "")
        rel = f"forge/logs/{short}/state.json"
        path = REPO / rel
        if not path.exists():
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        has_phantom = any(
            isinstance(state.get(k), dict) and state.get(k)
            for k in POSITION_FIELDS_TO_CLEAR
        )
        if not has_phantom:
            continue
        moves.append(PlannedMove(
            src=rel, dst=rel, kind="killed_state_clear",
            note=f"clear position fields for killed strategy {strategy}",
        ))
    return moves


def _verify_absent_required() -> list[str]:
    """Return any required-absent files that are currently present."""
    bad: list[str] = []
    for rel in ABSENT_REQUIRED:
        if (REPO / rel).exists():
            bad.append(rel)
    return bad


def plan_reset(target: str) -> dict:
    """Build the full reset plan without doing any I/O writes. Returns a
    structured dict the CLI prints and tests can assert against."""
    archive_moves = _plan_archive_moves(target)
    counter_moves = _plan_counter_resets(target)
    template_moves = _plan_template_rewrites(target)
    killed_clear_moves = _plan_killed_state_clears(target)
    blockers = _verify_absent_required()
    return {
        "target": target,
        "archive_root": str(_archive_root(target).relative_to(REPO)),
        "n_archive_moves": len(archive_moves),
        "n_counter_resets": len(counter_moves),
        "n_template_rewrites": len(template_moves),
        "n_killed_state_clears": len(killed_clear_moves),
        "archive_moves": [asdict(m) for m in archive_moves],
        "counter_resets": [asdict(m) for m in counter_moves],
        "template_rewrites": [asdict(m) for m in template_moves],
        "killed_state_clears": [asdict(m) for m in killed_clear_moves],
        "blockers_present": blockers,
        "ready_to_execute": not blockers,
    }


def _do_archive(plan: dict, target: str) -> list[dict]:
    archive = _archive_root(target)
    archive.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    for m in plan["archive_moves"]:
        src = REPO / m["src"]
        dst = REPO / m["dst"]
        if not src.exists():
            log.append({"kind": "archive_skip_missing", "src": m["src"]})
            continue
        if dst.exists():
            log.append({"kind": "archive_skip_already_archived", "src": m["src"]})
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        log.append({"kind": "archived", "src": m["src"], "dst": m["dst"]})
    return log


def _reset_counter_in_state(state_path: Path) -> dict:
    """Reset only the trade-counter fields. Preserves position, deployment
    stage, config_hash, etc. Snapshot of pre-reset values is returned for
    the manifest."""
    if not state_path.exists():
        return {"reset": False, "reason": "missing"}
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    pre = {k: raw.get(k) for k in ("trade_count", "trade_serial", "next_trade_num")}
    for k in ("trade_count", "trade_serial", "next_trade_num"):
        if k in raw:
            raw[k] = 0
    state_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    return {"reset": True, "pre": pre}


def _do_counter_resets(plan: dict) -> list[dict]:
    log: list[dict] = []
    for m in plan["counter_resets"]:
        result = _reset_counter_in_state(REPO / m["src"])
        log.append({"kind": "counter_reset", "src": m["src"], **result})
    return log


def _do_template_rewrites(plan: dict) -> list[dict]:
    log: list[dict] = []
    for rel, content in CLEAN_TEMPLATES:
        path = REPO / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        log.append({"kind": "template_rewritten", "path": rel})
    return log


def _clear_killed_state(state_path: Path) -> dict:
    """Clear position-state fields in a killed strategy's state.json.
    Returns a snapshot of pre-cleared values for the archive manifest."""
    if not state_path.exists():
        return {"cleared": False, "reason": "missing"}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"cleared": False, "reason": f"unreadable:{exc}"}
    pre: dict = {}
    cleared: list[str] = []
    for k in POSITION_FIELDS_TO_CLEAR:
        if k in raw and isinstance(raw[k], dict) and raw[k]:
            pre[k] = raw[k]
            raw[k] = None if k == "open_trade" else {}
            cleared.append(k)
    if not cleared:
        return {"cleared": False, "reason": "already_empty"}
    state_path.write_text(
        json.dumps(raw, indent=2, default=str), encoding="utf-8"
    )
    return {"cleared": True, "fields": cleared, "pre": pre}


def _do_killed_state_clears(plan: dict) -> list[dict]:
    log: list[dict] = []
    for m in plan.get("killed_state_clears") or []:
        result = _clear_killed_state(REPO / m["src"])
        log.append({"kind": "killed_state_cleared", "src": m["src"], **result})
    return log


def _write_manifest(target: str, manifest: list[dict]) -> None:
    archive = _archive_root(target)
    archive.mkdir(parents=True, exist_ok=True)
    out = archive / "_manifest.jsonl"
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "target": target,
            "events": manifest,
        }) + "\n")


def execute_reset(target: str, plan: Optional[dict] = None) -> dict:
    """Perform the planned moves. Returns the manifest of what was done.
    Refuses to run if HALT/FLATTEN flags are still present (operator
    must clear before reset)."""
    if plan is None:
        plan = plan_reset(target)
    if not plan["ready_to_execute"]:
        return {
            "status": "blocked",
            "reason": "required-absent files present",
            "blockers": plan["blockers_present"],
            "events": [],
        }
    events: list[dict] = []
    events.extend(_do_archive(plan, target))
    events.extend(_do_counter_resets(plan))
    events.extend(_do_template_rewrites(plan))
    events.extend(_do_killed_state_clears(plan))
    _write_manifest(target, events)
    return {"status": "executed", "target": target, "events": events}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        help="Reset target tag, e.g. 20260531. Used in archive path.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually perform the moves. Default is dry-run.")
    args = parser.parse_args(argv)

    plan = plan_reset(args.target)
    if not args.execute:
        print(json.dumps({"mode": "dry-run", **plan}, indent=2, default=str))
        if not plan["ready_to_execute"]:
            print(f"\nBLOCKED — clear these files first: {plan['blockers_present']}", flush=True)
            return 2
        print("\nDry-run complete. Re-run with --execute to perform the moves.", flush=True)
        return 0

    result = execute_reset(args.target, plan=plan)
    print(json.dumps({"mode": "execute", **result}, indent=2, default=str))
    return 0 if result["status"] == "executed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
