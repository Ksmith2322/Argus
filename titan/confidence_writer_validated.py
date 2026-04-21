"""Titan Validated confidence-artifact writer.

Sibling to titan.confidence_writer. Titan's union backtest is mildly
positive (PF 1.52 on n=298) but spans four sub-strategies and both
directions, with BREAKOUT (PF 0.58) and TRENDLINE (PF 0.37) dragging.
A clean structural subset carves out:

  strategy == 'TREND_FOLLOW' AND direction == 'long'
    -> PF 2.09 / WR 55.7% / exp $24.05/trade / n=174

Motivation (interpretable, not cherry-pick):
  1. TREND_FOLLOW is the dominant sub-strategy (269 of 298 trades) and
     the only one with a positive PF at n>=10 (BREAKOUT n=19 PF 0.58,
     TRENDLINE n=9 PF 0.37, MEAN_REVERSION n=1 ignored).
  2. Long trades materially outperform shorts across the book
     (PF 1.87 long vs 1.13 short). Shorts on trend-followers on
     uptrending-bias stocks/ETFs is structurally suspect.

Both filter terms are known at entry (strategy label + direction),
so this subset is implementable as a scanner gate — unlike
days_held>=10 which would require post-hoc knowledge.

This artifact tracks that scoped-down subset separately so the
scope_down decision is a strategy+direction gate in
titan.ops.scanner rather than a strategy rewrite.

Invoke:
    python -m titan.confidence_writer_validated
    python -m titan.confidence_writer_validated --dry-run
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

_BACKTEST_DIR = _REPO / "titan" / "data" / "backtest_results"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "titan_validated.json"

# The filter defining the "validated" subset. Keep these constants in
# sync with whatever gate is added to titan.ops.scanner if / when this
# lifts to paper_only.
ALLOWED_STRATEGY = "TREND_FOLLOW"
ALLOWED_DIRECTION = "long"


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
    candidates = sorted(_BACKTEST_DIR.glob("trades_*.csv"))
    return candidates[-1] if candidates else None


def _fnum(v, default=0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def build_titan_validated_artifact() -> dict:
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability, _max_drawdown,
    )

    csv_path = _latest_backtest_csv()
    if csv_path is None:
        raise FileNotFoundError(
            f"no backtest trades.csv found in {_BACKTEST_DIR}. "
            f"Run `python -m titan.runner --backtest` first."
        )

    with open(csv_path, encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Apply the validated-subset filter: TREND_FOLLOW + long only.
    subset = [
        r for r in all_rows
        if (r.get("strategy") or "").strip() == ALLOWED_STRATEGY
        and (r.get("direction") or "").strip() == ALLOWED_DIRECTION
    ]

    # pnl_pct * $1000 nominal -> pnl_usd (same convention as titan.json)
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
        f"filter: strategy == '{ALLOWED_STRATEGY}' AND direction == '{ALLOWED_DIRECTION}'",
        f"filtered from {len(all_rows)} total trades",
        f"pnl synthesized from pnl_pct * ${POSITION_USD:.0f} nominal",
    ]
    if n < 10:
        warn_parts.append(f"n={n} below sanity bar -- confidence suppressed")
    elif sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "titan_validated",
        "source": f"titan_validated_{csv_path.stem}_git_{_git_sha_short()}",
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
    tns = _top_n_sensitivity(pnls, max_n=5)
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

    # Scope-down disposition. Filter has structural motivation
    # (TREND_FOLLOW is the dominant sub-strategy and only +PF at n>=10;
    # longs outperform shorts across Titan's stock/ETF universe, and
    # shorting trend-followers on upward-biased instruments is suspect).
    # Both filter terms are known at entry, so this is implementable
    # as a scanner gate.
    artifact["disposition"] = {
        "status": "scope_down",
        "reason": (
            f"Filter strategy=='{ALLOWED_STRATEGY}' AND "
            f"direction=='{ALLOWED_DIRECTION}' produces PF {pf:.2f} / "
            f"WR {wins/n*100:.1f}% / exp ${sum(pnls)/n if n else 0:.2f}/trade "
            f"on n={n} vs union PF 1.52 on n=298. BREAKOUT (PF 0.58) and "
            "TRENDLINE (PF 0.37) sub-strategies are net-losers; shorts "
            "(PF 1.13) meaningfully underperform longs (PF 1.87) across "
            "Titan's stock/ETF universe. Both filter terms are known at "
            "entry, so subset is implementable as a scanner gate. "
            "Unblock to paper_only when (a) n>=30 on subset with "
            "backfill+live combined, OR (b) the filter is wired into "
            "titan.ops.scanner as a gate."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    return artifact


def write_titan_validated_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_titan_validated_artifact()
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
    result = write_titan_validated_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
