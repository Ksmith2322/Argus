"""Active alpha readiness report.

This is the operator-facing "why are we not making more money yet?" view:
active roster, preflight verdicts, canonical evidence, current exposure, and
the blocker ledger in one JSON artifact.

Output:
  argus_flow/logs/active_alpha_readiness.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
OUT_PATH = REPO / "argus_flow" / "logs" / "active_alpha_readiness.json"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _active_roster() -> list[str]:
    from argus_flow.tests.test_sunset_roster import ACTIVE_ROSTER

    return sorted(ACTIVE_ROSTER)


def _epoch() -> dict[str, Any]:
    try:
        from helio.evidence_epoch import current_epoch

        ep = current_epoch()
        return {
            "id": ep.id,
            "label": ep.label,
            "started_at": ep.started_at.isoformat(),
            "is_clean": ep.is_clean,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _canonical_counts(active: list[str], epoch: dict[str, Any]) -> dict[str, Any]:
    since = _parse_ts(epoch.get("started_at")) if "error" not in epoch else None
    rows_by_strategy: dict[str, Counter] = defaultdict(Counter)
    pnl_by_strategy: dict[str, float] = defaultdict(float)
    latest_by_strategy: dict[str, str] = {}
    total = 0
    malformed = 0

    try:
        from helio.canonical_fills import _iter_canonical_paths

        paths = _iter_canonical_paths()
    except Exception as exc:
        return {"error": f"canonical path scan failed: {exc}"}

    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            ts = _parse_ts(row.get("ts") or row.get("exit_ts") or row.get("entry_ts"))
            if since is not None and (ts is None or ts < since):
                continue
            strategy = str(row.get("strategy") or "unknown")
            if strategy not in active:
                continue
            side = str(row.get("side") or "UNKNOWN").upper()
            rows_by_strategy[strategy][side] += 1
            total += 1
            latest_by_strategy[strategy] = ts.isoformat() if ts else ""
            if side == "EXIT":
                try:
                    pnl_by_strategy[strategy] += float(row.get("pnl_usd") or 0.0)
                except (TypeError, ValueError):
                    pass

    per_strategy = {}
    for strategy in active:
        counts = rows_by_strategy.get(strategy, Counter())
        per_strategy[strategy] = {
            "entries": int(counts.get("ENTRY", 0)),
            "exits": int(counts.get("EXIT", 0)),
            "other": int(sum(v for k, v in counts.items()
                             if k not in {"ENTRY", "EXIT"})),
            "closed_pnl_usd": round(pnl_by_strategy.get(strategy, 0.0), 2),
            "latest_fill_ts": latest_by_strategy.get(strategy),
        }

    return {
        "since": since.isoformat() if since else None,
        "total_active_rows": total,
        "malformed_rows": malformed,
        "per_strategy": per_strategy,
    }


def _preflight(active: list[str]) -> dict[str, Any]:
    from helio.real_money_preflight import evaluate_strategy

    out = {}
    for strategy in active:
        try:
            result = evaluate_strategy(strategy)
            out[strategy] = result.to_dict()
        except Exception as exc:
            out[strategy] = {"error": str(exc)}
    return out


def _blockers() -> dict[str, Any]:
    from helio.blocker_ledger import summarize_blockers

    return summarize_blockers(days=30)


def _exposure() -> dict[str, Any]:
    try:
        from helio.cluster_exposure import compute_cluster_exposure

        return compute_cluster_exposure()
    except Exception as exc:
        return {"error": str(exc)}


def _roles(active: list[str]) -> dict[str, Any]:
    """Per-strategy role + role-aware PF floor (Codex gap #10)."""
    try:
        from helio.strategy_roles import (
            STRATEGY_ROLES,
            get_pf_floor,
            get_role,
        )
    except Exception as exc:
        return {"error": str(exc)}
    out: dict[str, Any] = {}
    for strategy in active:
        out[strategy] = {
            "role": get_role(strategy),
            "pf_floor": get_pf_floor(strategy),
            "explicit": strategy in STRATEGY_ROLES,
        }
    return out


def _strategy_status(
    active: list[str],
    preflight: dict[str, Any],
    canonical: dict[str, Any],
    blockers: dict[str, Any],
    roles: dict[str, Any],
) -> dict[str, Any]:
    blocker_by_strategy = blockers.get("by_strategy") or {}
    canonical_by_strategy = canonical.get("per_strategy") or {}
    statuses = {}
    for strategy in active:
        pf = preflight.get(strategy) or {}
        checks = pf.get("checks") or []
        red_checks = [c.get("name") for c in checks if c.get("verdict") == "RED"]
        yellow_checks = [c.get("name") for c in checks if c.get("verdict") == "YELLOW"]
        blocker_counts = blocker_by_strategy.get(strategy, {})
        cap_blocks = int(blocker_counts.get("CAP_EXCEEDED", 0))
        data_blocks = int(blocker_counts.get("DATA_UNAVAILABLE", 0))
        evidence = canonical_by_strategy.get(strategy, {})
        exits = int(evidence.get("exits") or 0)

        verdict = "READY_TO_SCALE"
        reasons: list[str] = []
        if pf.get("error"):
            verdict = "BLOCKED"
            reasons.append(f"preflight_error:{pf['error']}")
        elif red_checks:
            verdict = "BLOCKED"
            reasons.extend(f"preflight_red:{name}" for name in red_checks)
        elif exits < 20:
            verdict = "EVIDENCE_BUILDING"
            reasons.append(f"closed_trades:{exits}<20")
        elif cap_blocks or data_blocks:
            verdict = "GATED_REVIEW"
            if cap_blocks:
                reasons.append(f"cap_blocks_30d:{cap_blocks}")
            if data_blocks:
                reasons.append(f"data_blocks_30d:{data_blocks}")
        elif yellow_checks:
            verdict = "REVIEW_BEFORE_SCALE"
            reasons.extend(f"preflight_yellow:{name}" for name in yellow_checks)

        role_info = (roles.get(strategy) or {}) if isinstance(roles, dict) else {}
        statuses[strategy] = {
            "verdict": verdict,
            "reasons": reasons,
            "blockers_30d": blocker_counts,
            "canonical_evidence": evidence,
            "role": role_info.get("role"),
            "pf_floor": role_info.get("pf_floor"),
        }
    return statuses


def build_report() -> dict[str, Any]:
    active = _active_roster()
    epoch = _epoch()
    preflight = _preflight(active)
    canonical = _canonical_counts(active, epoch)
    blockers = _blockers()
    exposure = _exposure()
    roles = _roles(active)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_roster": active,
        "epoch": epoch,
        "preflight": preflight,
        "canonical_evidence": canonical,
        "blockers": blockers,
        "exposure": exposure,
        "roles": roles,
        "strategy_status": _strategy_status(
            active, preflight, canonical, blockers, roles
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true",
                        help="Print JSON report to stdout")
    args = parser.parse_args(argv)

    report = build_report()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    if args.stdout:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"Persisted: {OUT_PATH}")
        for strategy, row in report.get("strategy_status", {}).items():
            reasons = ", ".join(row.get("reasons") or ["none"])
            print(f"{strategy}: {row.get('verdict')} ({reasons})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
