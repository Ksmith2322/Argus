"""Prospective-holdout evaluation for scope_down validated subsets.

A scope_down subset (e.g., Tori "Dow+LONG only" at PF 3.65) is found by
scanning the historical data for which slice looks best. That's in-sample
selection — the PF you see is inflated because you picked the filter AFTER
observing the outcome. The only honest way to test it is to freeze the
filter NOW and evaluate against trades that arrive AFTER freeze date.

This module reads a validated artifact's `holdout_freeze` block (see
helio.strategy_confidence.HoldoutFreeze), finds trades since `frozen_at`
in canonical_fills.jsonl (or the strategy's trades.csv), and compares
against the in-sample claim.

Usage:
    python -m helio.holdout_eval                    # evaluate all frozen
    python -m helio.holdout_eval --strategy tori    # just one

Output: `strategy_confidence/_holdout/<strategy>_oos.json` per strategy.
Dashboard surfaces these via a future /api/holdout_status endpoint.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

ARTIFACT_DIR = _REPO / "strategy_confidence"
HOLDOUT_DIR = ARTIFACT_DIR / "_holdout"
CANONICAL_FILLS = _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"


def _load_artifact(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _canonical_oos_fills(strategy_label: str, frozen_at: str) -> list[dict]:
    """Fills for `strategy_label` with exit_ts > frozen_at. Excludes backfill."""
    if not CANONICAL_FILLS.exists():
        return []
    frozen_dt = datetime.fromisoformat(frozen_at.replace("Z", "+00:00"))
    out = []
    with open(CANONICAL_FILLS, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("strategy") != strategy_label:
                continue
            if r.get("source") == "backfill_from_trade_csv":
                continue  # backfills are historical, not real OOS
            exit_ts = r.get("exit_ts") or r.get("ts")
            if not exit_ts:
                continue
            try:
                dt = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
            except Exception:
                continue
            if dt <= frozen_dt:
                continue
            pnl = r.get("pnl_usd")
            if pnl is None:
                continue
            out.append({"ts": exit_ts, "pnl_usd": float(pnl)})
    return out


def _compute_stats(pnls: list[float]) -> dict:
    if not pnls:
        return {"n": 0, "pf": None, "wr": None, "expectancy_usd": None}
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    pf = (sum(wins) / sum(losses)) if sum(losses) > 0 else (float("inf") if wins else 0.0)
    wr = len(wins) / len(pnls) if pnls else 0.0
    return {
        "n": len(pnls),
        "pf": round(pf, 3) if pf != float("inf") else None,
        "wr": round(wr, 4),
        "expectancy_usd": round(sum(pnls) / len(pnls), 4),
    }


def _verdict(in_sample: dict, oos: dict, target_n: int, alert_pct: float) -> str:
    if oos["n"] < target_n:
        return f"INSUFFICIENT_OOS — need {target_n} trades, have {oos['n']}"
    if oos["pf"] is None:
        return "OOS_ALL_WINS_OR_NO_TRADES"
    if oos["pf"] < 1.0:
        return f"OOS_NEGATIVE — pf={oos['pf']} < 1.0 (kill candidate)"
    in_pf = in_sample.get("pf")
    if in_pf is None or in_pf <= 0:
        return "IN_SAMPLE_PF_MISSING"
    deg = 1.0 - (oos["pf"] / in_pf)
    if deg > alert_pct:
        return f"DEGRADED — pf dropped {deg:.0%} ({in_pf} → {oos['pf']})"
    return f"HOLDING — pf {oos['pf']} vs in-sample {in_pf} (deg {deg:.0%})"


def evaluate_one(artifact_path: Path) -> dict | None:
    art = _load_artifact(artifact_path)
    if not art:
        return None
    freeze = art.get("holdout_freeze")
    if not freeze:
        return None  # no frozen hypothesis — skip

    strategy_label = art.get("strategy", artifact_path.stem)
    frozen_at = freeze["frozen_at"]
    now = datetime.now(timezone.utc)

    fills = _canonical_oos_fills(strategy_label, frozen_at)
    oos = _compute_stats([f["pnl_usd"] for f in fills])

    try:
        earliest = datetime.fromisoformat(freeze["oos_eval_earliest"].replace("Z", "+00:00"))
    except Exception:
        earliest = None
    window_open = earliest is None or now >= earliest

    verdict = (
        _verdict(freeze["in_sample_claim"], oos,
                 freeze["oos_eval_target_n"], freeze["degradation_alert_pct"])
        if window_open else
        f"WAITING — eval window opens {freeze['oos_eval_earliest']}"
    )

    report = {
        "strategy": strategy_label,
        "artifact": artifact_path.name,
        "generated_at": now.isoformat(),
        "frozen_at": frozen_at,
        "filter_statement": freeze["filter_statement"],
        "in_sample_claim": freeze["in_sample_claim"],
        "oos_stats": oos,
        "window_open": window_open,
        "oos_eval_earliest": freeze["oos_eval_earliest"],
        "target_n_oos": freeze["oos_eval_target_n"],
        "verdict": verdict,
    }
    return report


def write_all() -> list[Path]:
    HOLDOUT_DIR.mkdir(parents=True, exist_ok=True)
    out_paths = []
    for p in sorted(ARTIFACT_DIR.glob("*_validated.json")) + sorted(ARTIFACT_DIR.glob("*_ym.json")):
        report = evaluate_one(p)
        if report is None:
            continue
        out_p = HOLDOUT_DIR / f"{p.stem}_oos.json"
        out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")
        out_paths.append(out_p)
    return out_paths


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", help="Evaluate just one strategy by artifact stem (e.g., tori_validated)")
    args = ap.parse_args()

    if args.strategy:
        path = ARTIFACT_DIR / f"{args.strategy}.json"
        if not path.exists():
            print(f"NOT FOUND: {path}", file=sys.stderr)
            return 1
        report = evaluate_one(path)
        if report is None:
            print(f"no holdout_freeze block in {path.name}")
            return 0
        print(json.dumps(report, indent=2))
        return 0

    written = write_all()
    if not written:
        print("No validated artifacts carry a holdout_freeze block yet.")
        return 0
    print(f"Wrote {len(written)} OOS reports:")
    for p in written:
        r = json.loads(p.read_text(encoding="utf-8"))
        print(f"  {r['strategy']:18s} {r['verdict']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
