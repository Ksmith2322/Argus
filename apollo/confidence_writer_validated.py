"""Apollo Validated confidence-artifact writer.

Sibling to apollo.confidence_writer. The union post_er backtest is
net-negative (PF 0.69 on n=205), but a specific filtered subset clears
cleanly: `surprise 10-20% + day1_gap 2%+` → PF 4.59 / WR 67% / +$625
on n=15. Two interpretable filters:
  1. Earnings surprise in the 10-20% "sweet spot" (not tiny beats that
     the market shrugs off, not unrealistic blowouts that mean-revert)
  2. Day-1 gap ≥2% — the market confirms the beat instead of fading

This artifact tracks that scoped-down subset separately so the
scope_down decision is a `score_floor` + filter change in
apollo.execution.planned_trades rather than a strategy rewrite.

Invoke:
    python -m apollo.confidence_writer_validated
    python -m apollo.confidence_writer_validated --dry-run
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

_BACKTEST_DIR = _REPO / "apollo" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "apollo_validated.json"

# The filter defining the "validated" subset. Keep these constants in
# sync with whatever gate is added to apollo.execution.planned_trades
# if / when this lifts to paper_only.
SURPRISE_MIN_PCT = 10.0
SURPRISE_MAX_PCT = 20.0
GAP_MIN_PCT = 2.0


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _latest_post_er_csv() -> Path | None:
    if not _BACKTEST_DIR.exists():
        return None
    cands = sorted(_BACKTEST_DIR.glob("post_er_*.csv"))
    return cands[-1] if cands else None


def _fnum(v, default=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_apollo_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    csv_path = _latest_post_er_csv()
    if csv_path is None:
        raise FileNotFoundError(f"no post_er_*.csv found in {_BACKTEST_DIR}")

    with open(csv_path, encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Apply the validated-subset filter
    subset = [
        r for r in all_rows
        if SURPRISE_MIN_PCT <= _fnum(r.get("surprise_pct")) < SURPRISE_MAX_PCT
        and _fnum(r.get("day1_gap_pct")) >= GAP_MIN_PCT
    ]

    # pnl_pct * $1000 nominal → pnl_usd (same convention as hermes/apollo)
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
        f"filter: surprise_pct in [{SURPRISE_MIN_PCT}%, {SURPRISE_MAX_PCT}%) AND day1_gap_pct >= {GAP_MIN_PCT}%",
        f"filtered from {len(all_rows)} total post_er rows",
        "pnl = pnl_pct * $1000 nominal",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar — confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "apollo_validated",
        "source": f"apollo_validated_{csv_path.stem}_git_{_git_sha_short()}",
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
        wf = _walk_forward_stability(pnls, n_folds=min(4, max(2, n // 5)))
        if wf is not None:
            pfs = [f["profit_factor"] for f in wf["fold_details"]
                   if f.get("profit_factor") is not None]
            artifact["walk_forward"] = {
                "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
                "pf_per_fold": pfs, **wf,
            }
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=3)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-symbol bucket on the subset
    per_sym = _per_group_profitability(
        [{"symbol": r.get("symbol", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="symbol",
    )
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Scope-down disposition (decided 2026-04-20). The filter is
    # defensible AND has structural motivation (sweet-spot beats +
    # market confirmation via gap), not just a post-hoc symbol pick.
    # Sanity bar only (n=15); needs n>=30 to lift to paper_only.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Filter surprise_pct in [{SURPRISE_MIN_PCT}%, "
            f"{SURPRISE_MAX_PCT}%) AND day1_gap_pct >= {GAP_MIN_PCT}% "
            f"produces PF {pf:.2f} / exp ${sum(pnls)/n if n else 0:.2f}/trade "
            f"on n={n} vs union PF 0.69 net-negative. Filter has "
            "structural motivation (sweet-spot beat + market "
            "confirmation). Unblock to paper_only when (a) n>=30 on "
            "subset with backfill+live combined, OR (b) the filter is "
            "wired into apollo.execution.planned_trades as a gate."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_apollo_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_apollo_validated_artifact()
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
    result = write_apollo_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
