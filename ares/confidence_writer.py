"""Ares confidence-artifact writer.

Ares is a sector-rotation strategy (long top-K ETFs from {SPY, QQQ, SMH,
GDX, XLE, XBI, ...}). Backtest output comes as JSON from
`python -m ares.runner --backtest` and lives at
`ares/data/backtest_results/backtest_<timestamp>.json`.

Schema at read time:
    {
      "trades": int, "wins": int, "losses": int,
      "win_rate": pct, "profit_factor": float, "total_return_pct": float,
      "trade_list": [{pnl_usd, pnl_pct, symbol, exit_reason, ...}, ...]
    }

Invoke:
    python -m ares.confidence_writer
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
ARTIFACT_PATH = _REPO / "strategy_confidence" / "ares.json"


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


def build_ares_artifact() -> dict:
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

    trades = data.get("trade_list") or []
    pnls = [float(t.get("pnl_usd", 0)) for t in trades if t.get("pnl_usd") is not None]
    pnls = [p for p in pnls if p != 0.0]
    n = len(pnls)
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    warn_parts = [f"{n} backfill, 0 live",
                  f"total_return_pct={data.get('total_return_pct')}%",
                  f"max_drawdown_pct={data.get('max_drawdown_pct')}%"]
    if sum(pnls) < 0:
        warn_parts.append("union negative")
    else:
        warn_parts.append(f"union positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "ares",
        "source": f"ares_backtest_{json_path.stem}_git_{_git_sha_short()}",
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
    cs = _cost_stress(pnls, cost_per_trade_usd=1.0)  # ETF — low commission
    if cs is not None:
        artifact["cost_stress"] = cs
    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns
    per_sym = _per_group_profitability(trades, group_key="symbol")
    if per_sym is not None:
        artifact["per_instrument"] = per_sym

    # Monitor disposition (decided 2026-04-19). Union is already strong
    # (PF 2.70 / WR 63% / exp $198 on n=35), but a clean subset-edge
    # exists when we exclude risk_off exits: rotation-only trades show
    # PF 4.63 / exp $306 on n=27 (see strategy_confidence/ares_validated.json).
    # The full union continues to pass evidence bar; the sibling tracks
    # the scope_down path in case we later wire a regime gate into
    # ares.runner to skip entries during risk_off classification.
    artifact["disposition"] = {
        "status": "paper_only",
        "reason": (
            f"Union: PF {pf:.2f} / exp ${sum(pnls)/n if n else 0:.2f}/trade "
            f"on n={n}. Strong enough to run as-is in paper while live "
            "confirmation accumulates. A cleaner scope_down sibling "
            "exists on rotation-only exits (see ares_validated.json, "
            "PF 4.63 / exp $306 on n=27); track separately and lift to "
            "promote_candidate if the filter holds on live trades."
        ),
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    return artifact


def write_ares_artifact(dry_run: bool = False) -> Path | dict:
    from helio.strategy_confidence import StrategyConfidenceArtifact
    data = build_ares_artifact()
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = write_ares_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
