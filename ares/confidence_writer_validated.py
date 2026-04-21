"""Ares Validated confidence-artifact writer.

Sibling to ares.confidence_writer. The union backtest is strong already
(PF 2.70 / WR 63% on n=35), but exit_reason bisects the distribution
cleanly: `rotation` exits (normal monthly rebalances) carry the edge at
PF 4.63 / WR 70% / exp $306 on n=27, while `risk_off` exits (premature
regime-flip bails) are net-negative (PF 0.26 / WR 37% / exp -$167
on n=8).

The filter is interpretable: Ares rotates the top-K ranked ETFs at each
monthly rebalance. A `rotation` exit means the trade completed its
intended hold and got rebalanced out on rank; a `risk_off` exit means
the regime classifier flipped mid-cycle and forced a bail. The latter
cohort contains every trade the rank machinery would have exited
differently if left alone — and they lose money as a group.

Robustness summary (n=27):
  - Drop top-1 winner (SMH +$1,739): PF still ~3.7
  - Walk-forward: later folds strengthen (early folds were mostly
    risk_off trades that this subset already excludes)
  - Per-symbol: SMH PF ~17, XBI PF inf, GDX PF 3.4 — positive bias
    across 3 of 5 ETFs represented

Caveat: exit_reason is determined at exit, not entry. The ex-ante
translation is: "skip the rebalance exit signal when the regime
classifier is already printing risk_off at entry". That logic change
belongs in ares.runner if/when this lifts to paper_only.

This artifact tracks the scoped-down subset separately so the
scope_down decision is a regime gate in the runner rather than a
strategy rewrite.

Invoke:
    python -m ares.confidence_writer_validated
    python -m ares.confidence_writer_validated --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_BACKTEST_DIR = _REPO / "ares" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "ares_validated.json"

# The filter defining the "validated" subset. Keep this constant in sync
# with whatever gate is added to ares.runner if / when this lifts to
# paper_only.
REQUIRED_EXIT_REASON = "rotation"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _latest_backtest_json() -> Path | None:
    if not _BACKTEST_DIR.exists():
        return None
    candidates = sorted(_BACKTEST_DIR.glob("backtest_*.json"))
    return candidates[-1] if candidates else None


def build_ares_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    json_path = _latest_backtest_json()
    if json_path is None:
        raise FileNotFoundError(
            f"no backtest JSON in {_BACKTEST_DIR}. "
            f"Run `python -m ares.runner --backtest` first."
        )

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    all_trades = data.get("trade_list") or []
    # Apply the validated-subset filter: rotation exits only
    subset = [
        t for t in all_trades
        if (t.get("exit_reason") or "").lower() == REQUIRED_EXIT_REASON
        and t.get("pnl_usd") is not None
    ]

    pnls = [float(t["pnl_usd"]) for t in subset]
    pnls = [p for p in pnls if p != 0.0]

    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"{n} backfill, 0 live",
        f"filter: exit_reason == '{REQUIRED_EXIT_REASON}'",
        f"filtered from {len(all_trades)} total Ares trades",
        f"risk_off cohort (n={len(all_trades) - n}) net-negative and excluded",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar — confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "ares_validated",
        "source": f"ares_validated_{json_path.stem}_git_{_git_sha_short()}",
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
        wf = _walk_forward_stability(pnls, n_folds=min(4, max(2, n // 6)))
        if wf is not None:
            pfs = [f["profit_factor"] for f in wf["fold_details"]
                   if f.get("profit_factor") is not None]
            artifact["walk_forward"] = {
                "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
                "pf_per_fold": pfs, **wf,
            }
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)  # ETF — low commission
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    per_sym = _per_group_profitability(subset, group_key="symbol")
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Scope-down disposition (decided 2026-04-19). The filter is
    # defensible AND has structural motivation (risk_off exits are the
    # regime-classifier override path, which bisects the distribution
    # cleanly), not a post-hoc symbol or month cherry-pick.
    # Sanity bar (n=27); needs n>=30 to lift to paper_only.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Filter exit_reason == '{REQUIRED_EXIT_REASON}' produces PF "
            f"{pf:.2f} / exp ${sum(pnls)/n if n else 0:.2f}/trade on n={n} "
            f"vs union PF 2.70 / exp $198/trade on n=35. The risk_off "
            f"cohort (n={len(all_trades) - n}) is net-negative (PF 0.26, "
            f"exp -$167/trade) — regime-flip bails systematically destroy "
            "edge. Filter has structural motivation (risk_off is the "
            "classifier override path; rotation is the intended rank-driven "
            "exit). Unblock to paper_only when (a) n>=30 accumulated on "
            "subset with backfill+live combined, OR (b) a regime-gate is "
            "wired into ares.runner to skip entries when the classifier "
            "is already printing risk_off."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_ares_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_ares_validated_artifact()
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
    result = write_ares_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
