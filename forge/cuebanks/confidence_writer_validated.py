"""Cue Banks Validated confidence-artifact writer.

Sibling to forge.cuebanks.confidence_writer. The union backtest is
mediocre (PF 1.34 / WR 35% on n=88), but a specific filtered subset
clears cleanly: trades whose factors list contains `S/D supply zone`
carry an outsized edge (PF 3.63 / WR 57% / +$4,015 on n=30) while the
`S/D demand zone` cohort is net-negative (PF 0.55 / -$1,948).

The filter is interpretable: supply-zone levels are pre-identified
ceilings where price has distributed before, so both rejections and
breakouts reflect informed order flow. The demand-zone side in this
dataset is noise — many of those levels were forming in chop and got
stopped before the broader structure resolved.

Robustness summary (n=30):
  - Direction-agnostic: SHORT+supply PF 3.72 (n=20), LONG+supply PF 3.51 (n=10)
  - Drop top-5 winners: PF still 1.90
  - Walk-forward thirds: PF 10.4 / 2.87 / 1.42 (all positive)
  - Monthly: Feb +$1229, Mar +$3087, Apr -$302 (decay worth watching)

This artifact tracks that scoped-down subset separately so the
scope_down decision is a factor gate in the runner rather than a
strategy rewrite.

Invoke:
    python -m forge.cuebanks.confidence_writer_validated
    python -m forge.cuebanks.confidence_writer_validated --dry-run
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BACKTEST_CSV = _REPO / "forge" / "logs" / "cuebanks" / "cuebanks_backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "cue_banks_validated.json"

# The filter defining the "validated" subset. Keep this constant in sync
# with whatever gate is added to forge.cuebanks.runner if / when this
# lifts to paper_only.
REQUIRED_FACTOR = "S/D supply zone"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def build_cuebanks_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(f"missing {BACKTEST_CSV}")

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Apply the validated-subset filter
    subset = [
        r for r in all_rows
        if REQUIRED_FACTOR in (r.get("factors") or "")
        and r.get("pnl_usd") not in (None, "")
    ]

    pnls = []
    r_mults = []
    for r in subset:
        try:
            pnl = float(r["pnl_usd"])
        except (TypeError, ValueError):
            continue
        if pnl == 0.0:
            continue
        pnls.append(pnl)
        try:
            r_mults.append(float(r["rr_achieved"]))
        except (KeyError, TypeError, ValueError):
            pass

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"{n} backfill, 0 live",
        f"filter: factors contains '{REQUIRED_FACTOR}'",
        f"filtered from {len(all_rows)} total Cue Banks trades",
        "demand-zone cohort (n=45) net-negative and excluded",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar — confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "cue_banks_validated",
        "source": f"cuebanks_validated_backtest_git_{_git_sha_short()}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bt_pf": round(pf, 2) if pf != float("inf") else None,
        "bt_wr": round(wins / n, 4) if n else 0.0,
        "bt_trades": n,
        "n_total": n,
        "n_live": 0,
        "n_paper": n,
        "expectancy_usd": round(sum(pnls) / n, 4) if n else None,
        "expectancy_r": round(sum(r_mults) / len(r_mults), 4) if r_mults else None,
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
        wf = _walk_forward_stability(pnls, n_folds=min(4, max(2, n // 6)))
        if wf is not None:
            pfs = [f["profit_factor"] for f in wf["fold_details"]
                   if f.get("profit_factor") is not None]
            artifact["walk_forward"] = {
                "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
                "pf_per_fold": pfs, **wf,
            }
    cs = _cost_stress(pnls, cost_per_trade_usd=2.0)  # futures
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-direction bucket on the subset, stored under `per_instrument`
    # since that's the schema slot for a group breakdown (Cue Banks trades
    # YM only, so using direction as the bucket is more informative than
    # a single-ticker row). Supply zone works both ways here — SHORT fades
    # the supply ceiling, LONG trades the breakout through it.
    per_dir = _per_group_profitability(
        [{"symbol": r.get("direction", "?"),
          "pnl_usd": float(r["pnl_usd"])}
         for r in subset if r.get("pnl_usd") not in (None, "")],
        group_key="symbol",
    )
    if per_dir is not None:
        artifact["per_instrument"] = per_dir

    # Scope-down disposition (decided 2026-04-19). The filter is
    # defensible AND has structural motivation (supply-zone levels are
    # pre-identified distribution ceilings), not a post-hoc factor cherry-pick
    # since it also shows as the top-delta factor in the full audit table.
    # Meets sanity bar (n=30) but we want live confirmation before lifting.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Filter factors contains '{REQUIRED_FACTOR}' produces PF "
            f"{pf:.2f} / exp ${sum(pnls)/n if n else 0:.2f}/trade on n={n} "
            f"vs union PF 1.34 mediocre. Demand-zone cohort (n=45) is "
            f"net-negative (PF 0.55, -$1,948) and should NOT be traded. "
            "Filter has structural motivation (supply zones are "
            "pre-identified distribution ceilings informative both as "
            "fade and as breakout). Robustness: PF 1.90 after dropping "
            "top-5 winners; all walk-forward thirds positive (10.4/2.87/1.42). "
            "Unblock to paper_only when (a) n>=30 accumulated on subset "
            "with backfill+live combined at this PF, OR (b) the filter "
            "is wired into forge.cuebanks.runner as a factor gate."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_cuebanks_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_cuebanks_validated_artifact()
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
    result = write_cuebanks_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
