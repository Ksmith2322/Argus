"""Apollo confidence-artifact writer.

Apollo is an earnings/catalyst scanner. The runner's live-trading path
(`python -m apollo.runner --live`) only enters trades via the post-ER drift
rule in `apollo/strategies/position_rules.py::should_enter_post_er` (Day-2
entry after a confirmed beat with a gap-up). That behavior is simulated by
`apollo/ops/post_er_backtest.py`, which writes `post_er_*.csv` — so that
subset is what actually reflects Apollo's primary edge.

Choice of CSV (decision rationale):
    The backtest_results/ directory carries four sub-strategy CSVs:
      - drift_*.csv      (runner --backtest output: hindsight-biased
                          "buy after surprise is known" — not what we trade)
      - honest_bt_*.csv  (blind pre-ER entry — risk profile Apollo does
                          NOT currently run in live mode)
      - post_er_*.csv    (Day-2 entry after confirmed beat — what the
                          runner executes via should_enter_post_er)
      - runup_play.csv   (separate pre-ER run-up play, not live)
    We union ONLY the latest `post_er_*.csv`. Unioning across sub-strategies
    with different entry rules would blend distributions and bias PF.

Schema of post_er_*.csv:
    symbol,earnings_date,entry_date,entry_price,exit_price,pre_er_close,
    day1_gap_pct,surprise_pct,pnl_pct,exit_reason,days_held

pnl_usd is synthesized as pnl_pct * $1,000 nominal — linear transform that
preserves PF and bootstrap shape regardless of actual position size.

Invoke:
    python -m apollo.confidence_writer
    python -m apollo.confidence_writer --dry-run
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
ARTIFACT_PATH = _REPO / "strategy_confidence" / "apollo.json"

# Which sub-strategy CSV we read — see module docstring for reasoning.
_CSV_GLOB = "post_er_*.csv"
_SUBSET_NAME = "post_er"


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
    candidates = sorted(_BACKTEST_DIR.glob(_CSV_GLOB))
    return candidates[-1] if candidates else None


def build_apollo_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    csv_path = _latest_backtest_csv()
    if csv_path is None:
        raise FileNotFoundError(
            f"no {_CSV_GLOB} found in {_BACKTEST_DIR}. "
            f"Run `python -m apollo.ops.post_er_backtest` first."
        )

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    POSITION_USD = 1000.0
    processed = []
    for r in rows:
        try:
            pct = float(r.get("pnl_pct") or 0)
        except (TypeError, ValueError):
            continue
        if pct == 0.0:
            continue
        r["_pnl_usd"] = pct / 100.0 * POSITION_USD
        processed.append(r)

    n = len(processed)
    pnls = [r["_pnl_usd"] for r in processed]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [
        f"using {_SUBSET_NAME} subset, n={n}",
        f"{n} backfill, 0 live",
        f"pnl synthesized from pnl_pct * ${POSITION_USD:.0f} nominal",
        "other Apollo sub-strategies (drift/honest_bt/runup) NOT included — "
        "post_er is the only rule the live runner executes",
    ]
    if sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "apollo",
        "source": f"apollo_backtest_{csv_path.stem}_git_{_git_sha_short()}",
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
    wf = _walk_forward_stability(pnls, n_folds=4)
    if wf is not None:
        pfs = [f["profit_factor"] for f in wf["fold_details"]
               if f.get("profit_factor") is not None]
        artifact["walk_forward"] = {
            "folds": wf["n_folds"], "stable_folds": wf["positive_folds"],
            "pf_per_fold": pfs, **wf,
        }
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)  # stocks
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-symbol profitability — Apollo's universe is ~110 stocks so this
    # surfaces which names dominated the edge.
    per_sym = _per_group_profitability(
        [{"symbol": r.get("symbol", "?"), "pnl_usd": r["_pnl_usd"]}
         for r in processed],
        group_key="symbol",
    )
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    return artifact


def write_apollo_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_apollo_artifact()
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
    result = write_apollo_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
