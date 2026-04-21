"""VIX Mean-Reversion confidence-artifact writer.

Reads cached backtest trades from `forge/data/vix_revert/backtest_trades.csv`
(seeded by `python -m forge.run_macro_backtests`) and writes
`strategy_confidence/vix_revert.json`.

The VIX Revert strategy buys SPY when VIX closes above 30 and exits at
VIX < 20 / 60 days / -10% stop. Historical sample is small (~15-25 trades
since 2020) because VIX > 30 is a rare event, so evidence_bar will be
"sanity" at best — the sample_warning surfaces this honestly.

pnl_usd is synthesized as return_pct * $1,000 nominal SPY position. PF
and bootstrap shape are preserved regardless of the exact assumed size.

Invoke:
    python -m forge.vix_revert_confidence_writer
    python -m forge.vix_revert_confidence_writer --dry-run
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

BACKTEST_CSV = _REPO / "forge" / "data" / "vix_revert" / "backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "vix_revert.json"


def _git_sha_short() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def build_vix_revert_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(
            f"missing {BACKTEST_CSV}. "
            f"Run `python -m forge.run_macro_backtests` first."
        )

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    POSITION_USD = 1000.0
    processed = []
    for r in rows:
        try:
            ret_pct = float(r.get("return_pct") or 0)
        except (TypeError, ValueError):
            ret_pct = 0.0
        r["_pnl_usd"] = ret_pct / 100.0 * POSITION_USD
        processed.append(r)

    n = len(processed)
    pnls = [r["_pnl_usd"] for r in processed]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"{n} backfill, 0 live",
        f"pnl = return_pct * ${POSITION_USD:.0f} nominal SPY position",
        "VIX > 30 is rare — sample is structurally small; treat evidence as sanity-tier",
    ]
    if sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "vix_revert",
        "source": f"vix_revert_backtest_csv_git_{_git_sha_short()}",
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
    dd = _max_drawdown(pnls, starting_equity_usd=1000.0)
    if dd is not None:
        artifact["drawdown"] = dd
    wf = _walk_forward_stability(pnls, n_folds=4)
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

    # Per-exit-reason bucketing: surfaces whether STOP / VIX_BELOW_20 /
    # TIME_EXIT trades each make money independently.
    per_exit = _per_group_profitability(
        [{"exit_reason": r.get("exit_reason", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="exit_reason",
    )
    if per_exit is not None:
        artifact["per_instrument"] = per_exit

    return artifact


def write_vix_revert_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_vix_revert_artifact()
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
    result = write_vix_revert_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
