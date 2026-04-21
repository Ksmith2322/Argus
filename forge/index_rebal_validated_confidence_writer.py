"""Index Rebal Validated confidence-artifact writer.

Sibling to forge.index_rebalance_confidence_writer. The union backtest
is positive (PF 2.08 on n=31), but the per-action bucket shows a clean
structural split: ADD PF 7.04 / WR 68% / +$609.7 on n=19 vs
DELETE PF 0.49 / WR 33% / -$159.0 on n=12.

The ADD-only subset captures the classic "index-inclusion effect"
(Shleifer 1986, Harris-Gurel 1986): forced index-fund buying creates
temporary price pressure on additions around the announce->effective
window. Deletions have much noisier post-event behaviour (index funds
sell over a distributed window, and many deletions are forced by
distress — already priced in at announcement).

This artifact tracks the ADD-only subset separately so the scope_down
decision is a filter change in any execution layer rather than a
strategy rewrite.

Invoke:
    python -m forge.index_rebal_validated_confidence_writer
    python -m forge.index_rebal_validated_confidence_writer --dry-run
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

BACKTEST_CSV = _REPO / "forge" / "index_rebalance_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "index_rebal_validated.json"

# The filter defining the "validated" subset. Keep in sync with any
# gate added to execution if / when this lifts to paper_only.
REQUIRED_ACTION = "ADD"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def build_index_rebal_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(f"missing {BACKTEST_CSV}")

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Apply the validated-subset filter: ADD only
    subset = [r for r in all_rows if (r.get("action") or "").strip() == REQUIRED_ACTION]

    POSITION_USD = 1000.0
    processed = []
    pnls = []
    for r in subset:
        try:
            alpha = float(r.get("alpha_ann_pct") or 0)
        except (TypeError, ValueError):
            alpha = 0.0
        if alpha == 0.0:
            continue
        pnl = alpha / 100.0 * POSITION_USD
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
        f"filter: action == {REQUIRED_ACTION!r} (index-inclusion effect)",
        f"filtered from {len(all_rows)} total rows",
        f"pnl = alpha_ann_pct * ${POSITION_USD:.0f} nominal (alpha is SPY-relative)",
        "small sample at index-event frequency",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar — confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "index_rebal_validated",
        "source": f"index_rebal_validated_trades_csv_git_{_git_sha_short()}",
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

    # Per-ticker bucket on the subset (sanity check — no single name
    # should carry all the edge)
    per_sym = _per_group_profitability(
        [{"ticker": r.get("ticker", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="ticker",
    )
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Scope-down disposition (decided 2026-04-19). The filter is
    # defensible AND has structural motivation (index-inclusion effect —
    # forced index-fund buying on additions, well-documented in academic
    # literature). Deletions are structurally different (distributed
    # selling, distress pre-pricing) and carry no reliable edge in our
    # sample. Sanity bar only (n=19); needs n>=30 to lift to paper_only.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Filter action == {REQUIRED_ACTION!r} produces PF {pf:.2f} / "
            f"exp ${sum(pnls)/n if n else 0:.2f}/trade on n={n} vs union "
            f"PF 2.08 and DELETE-side PF 0.49 on n=12. Filter has "
            "structural motivation (index-inclusion effect: forced "
            "index-fund buying creates temporary price pressure on "
            "S&P 500 additions; deletions are structurally different). "
            "Unblock to paper_only when (a) n>=30 on ADD subset with "
            "backfill+live combined, OR (b) the filter is wired into "
            "execution as a gate."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_index_rebal_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_index_rebal_validated_artifact()
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
    result = write_index_rebal_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
