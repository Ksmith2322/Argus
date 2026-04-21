"""Hermes Validated confidence-artifact writer.

Sibling to hermes.confidence_writer. The union backtest is weakly
positive (PF 1.11 / WR 52% / n=386), but the edge concentrates sharply
at score>=80 with long gap-DOWN fills — which is precisely what the
live runner takes. This sibling surfaces that live-mirror subset as its
own artifact so the `scope_down` decision is auditable.

Filter (matches hermes.runner.execute_entries):
    score >= 80  AND  direction == "long"  AND  gap_type == "GAP_DOWN"

Per-score bucket analysis (2026-04-19 backtest):
    Score <60:    PF 0.41 (losing)
    Score 60-70:  PF 0.70 (losing)
    Score 70-80:  PF 1.12 (break-even)
    Score 80+:    PF 1.91, WR 63% — real edge
    Score >=85:   PF 2.57, WR 68% (tighter, n=56)
    Score 80+ long GAP_DOWN (RUNNER): PF 2.06, WR 65%, n=92

Invoke:
    python -m hermes.confidence_writer_validated
    python -m hermes.confidence_writer_validated --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_BACKTEST_DIR = _REPO / "hermes" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "hermes_validated.json"

# The filter defining the "validated" subset. Must stay in sync with the
# gate in hermes.runner.execute_entries (score>=MIN_SCORE_TO_ENTER AND
# direction=="long", which for gap fills means GAP_DOWN → long fill up).
SCORE_MIN = 80
DIRECTION = "long"
GAP_TYPE = "GAP_DOWN"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _latest_backtest_csv() -> Path | None:
    if not _BACKTEST_DIR.exists():
        return None
    cands = sorted(_BACKTEST_DIR.glob("trades_*.csv"))
    return cands[-1] if cands else None


def _fnum(v, default=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_hermes_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    csv_path = _latest_backtest_csv()
    if csv_path is None:
        raise FileNotFoundError(
            f"no backtest trades.csv found in {_BACKTEST_DIR}. "
            f"Run `python -m hermes.runner --backtest` first."
        )

    with open(csv_path, encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Apply the validated-subset filter — must mirror runner gate.
    subset = [
        r for r in all_rows
        if _fnum(r.get("score")) >= SCORE_MIN
        and r.get("direction") == DIRECTION
        and r.get("gap_type") == GAP_TYPE
    ]

    # pnl_pct * $1000 nominal → pnl_usd (same convention as hermes base)
    POSITION_USD = 1000.0
    processed = []
    pnls = []
    for r in subset:
        pnl = _fnum(r.get("pnl_pct")) / 100.0 * POSITION_USD
        if pnl == 0.0:
            continue
        r["_pnl_usd"] = pnl
        processed.append(r)
        pnls.append(pnl)

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"{n} backfill, 0 live",
        (f"filter: score >= {SCORE_MIN} AND direction == {DIRECTION!r} "
         f"AND gap_type == {GAP_TYPE!r} (mirrors runner gate)"),
        f"filtered from {len(all_rows)} total trades",
        f"pnl = pnl_pct * ${POSITION_USD:.0f} nominal",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar — confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"subset positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "hermes_validated",
        "source": f"hermes_validated_{csv_path.stem}_git_{_git_sha_short()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bt_pf": round(pf, 2) if pf != float("inf") else None,
        "bt_wr": round(wins / n, 4) if n else 0.0,
        "bt_trades": n,
        "n_total": n,
        "n_live": 0,
        "n_paper": n,
        "expectancy_usd": round(sum(pnls) / n, 4) if n else None,
        "p_expectancy_positive": _bootstrap_p_positive(pnls) if n >= 10 else None,
        "evidence_bar": _evidence_bar(n),
        "sample_warning": " | ".join(warn_parts),
    }
    mc = _monte_carlo_shuffle(pnls)
    if mc is not None:
        artifact["mc_stress"] = mc
    dd = _max_drawdown(pnls)
    if dd is not None:
        artifact["drawdown"] = dd
    if n >= 8:
        wf = _walk_forward_stability(pnls, n_folds=min(4, max(2, n // 20)))
        if wf is not None:
            pfs = [f["profit_factor"] for f in wf["fold_details"]
                   if f.get("profit_factor") is not None]
            artifact["walk_forward"] = {
                "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
                "pf_per_fold": pfs, **wf,
            }
    cs = _cost_stress(pnls, cost_per_trade_usd=2.0)  # stocks
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-symbol bucket on the subset — surfaces which tickers carry edge
    per_sym = _per_group_profitability(
        [{"symbol": r.get("symbol", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="symbol",
    )
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Scope-down disposition. The runner already enforces this filter
    # today (hermes.runner MIN_SCORE_TO_ENTER=80 + long-only + gap-down),
    # so this artifact simply surfaces the true live-mirror edge instead
    # of the diluted union metric. Unblock to paper_only when n>=30
    # accumulates on backfill+live combined with PF holding >=1.5.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Subset (score >= {SCORE_MIN} AND {DIRECTION} AND {GAP_TYPE}) "
            f"produces PF {pf:.2f} / exp ${sum(pnls)/n if n else 0:.2f}/trade "
            f"/ WR {wins/n*100 if n else 0:.0f}% on n={n} vs union PF 1.11. "
            f"Filter already enforced by hermes.runner today "
            f"(MIN_SCORE_TO_ENTER={SCORE_MIN}, long-only, gap-down). "
            "Per-bucket analysis shows score 60-70 losing (PF 0.70), "
            "70-80 break-even (PF 1.12), 80+ the real edge (PF 1.91). "
            "Unblock to paper_only when n>=30 accumulated on subset "
            "backfill+live combined with PF holding >= 1.5."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_hermes_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_hermes_validated_artifact()
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = write_hermes_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
