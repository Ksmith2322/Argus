"""Tori confidence-artifact writer.

Reads forge/logs/tori/backtest_trades.csv, computes the overall confidence
block, and writes strategy_confidence/tori.json. The artifact describes
the CURRENT bot behaviour (take-everything union), not the narrowed
subset. The subset edge — Bounce,A = PF 3.56 and Dow-only = PF 1.85 —
goes into the `sample_warning` field so anyone reading the artifact sees
the action is to narrow scope, not to trust the headline PF.

Invoke:
    python -m forge.tori.confidence_writer            # write
    python -m forge.tori.confidence_writer --dry-run  # print only
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

BACKTEST_CSV = _REPO / "forge" / "logs" / "tori" / "backtest_trades.csv"
ARTIFACT_PATH = _REPO / "strategy_confidence" / "tori.json"


def _git_sha_short() -> str:
    """Best-effort short git sha so the artifact's `source` is identifying.
    Falls back to "unknown" silently if git isn't available."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_REPO), stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii", errors="replace").strip() or "unknown"
    except Exception:
        return "unknown"


def _bucket_stats(rows: list[dict], key_fn) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        groups[key_fn(r)].append(r)
    out = {}
    for k, grp in groups.items():
        pnls = [float(r["pnl_usd"]) for r in grp]
        wins = sum(1 for p in pnls if p > 0)
        gross_w = sum(p for p in pnls if p > 0)
        gross_l = abs(sum(p for p in pnls if p < 0))
        pf = gross_w / gross_l if gross_l else float("inf")
        out[k] = {
            "trades": len(grp),
            "wr": wins / len(grp) if grp else 0.0,
            "pnl_usd": round(sum(pnls), 2),
            "pf": round(pf, 2) if pf != float("inf") else None,
        }
    return out


def build_tori_artifact() -> dict:
    """Returns the artifact dict. Raises FileNotFoundError if the CSV is
    missing so the caller (or CLI) gets a clear error rather than a
    silently-broken artifact."""
    from helio.fleet_state import (
        _bootstrap_p_positive, _evidence_bar, _monte_carlo_shuffle,
        _walk_forward_stability, _cost_stress, _top_n_sensitivity,
        _per_group_profitability,
    )

    if not BACKTEST_CSV.exists():
        raise FileNotFoundError(f"missing {BACKTEST_CSV}")

    with open(BACKTEST_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    n = len(rows)
    pnls = [float(r["pnl_usd"]) for r in rows]
    r_mults = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") not in (None, "")]
    wins = sum(1 for p in pnls if p > 0)
    gross_w = sum(p for p in pnls if p > 0)
    gross_l = abs(sum(p for p in pnls if p < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    # Subset breakdowns for the sample_warning headline
    by_setup_grade = _bucket_stats(rows, lambda r: f"{r.get('setup','?')}_{r.get('grade','?')}")
    by_instrument = _bucket_stats(rows, lambda r: r.get("name") or r.get("ticker") or "?")

    def _best(buckets: dict[str, dict]) -> tuple[str, dict] | None:
        candidates = [(k, v) for k, v in buckets.items()
                      if v["pnl_usd"] > 0 and v["pf"] is not None]
        if not candidates:
            return None
        return max(candidates, key=lambda kv: kv[1]["pf"])

    best_setup = _best(by_setup_grade)
    best_instrument = _best(by_instrument)
    warning_parts = [f"{n} backfill, 0 live"]
    if best_setup:
        warning_parts.append(
            f"subset edge: {best_setup[0]} (n={best_setup[1]['trades']}, PF={best_setup[1]['pf']})"
        )
    if best_instrument:
        warning_parts.append(
            f"best instrument: {best_instrument[0]} (n={best_instrument[1]['trades']}, PF={best_instrument[1]['pf']})"
        )
    # Union sign check — only flag "narrow scope" if the bot's all-setups
    # all-instruments union is actually losing. Gated after the v2 exit-logic
    # fix flipped the union positive on 2026-04-19.
    if sum(pnls) < 0:
        warning_parts.append("union (current bot) is negative -- narrow scope")
    else:
        warning_parts.append(f"union (current bot) is positive: PF={pf:.2f}")

    artifact = {
        "schema_version": 1,
        "strategy": "tori",
        "source": f"tori_backtest_trades_git_{_git_sha_short()}",
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
        "sample_warning": " | ".join(warning_parts),
    }
    mc = _monte_carlo_shuffle(pnls)
    if mc is not None:
        artifact["mc_stress"] = mc

    # Full stress battery (2026-04-19 discovery-test phase)
    wf = _walk_forward_stability(pnls, n_folds=4)
    if wf is not None:
        # Map helper output to schema shape — legacy pf_per_fold kept for
        # any existing consumer, plus the new extended fields from the
        # stability helper.
        pfs = [f["profit_factor"] for f in wf["fold_details"]
               if f.get("profit_factor") is not None]
        artifact["walk_forward"] = {
            "folds": wf["n_folds"],
            "stable_folds": wf["positive_folds"],
            "pf_per_fold": pfs,
            "n_folds": wf["n_folds"],
            "fold_size": wf["fold_size"],
            "positive_folds": wf["positive_folds"],
            "stability_score": wf["stability_score"],
            "all_folds_positive": wf["all_folds_positive"],
            "fold_details": wf["fold_details"],
        }

    cs = _cost_stress(pnls, cost_per_trade_usd=5.0)
    if cs is not None:
        artifact["cost_stress"] = cs

    tns = _top_n_sensitivity(pnls, max_n=5)
    if tns is not None:
        artifact["top_n_sensitivity"] = tns

    # Per-instrument gate — every symbol must independently earn its keep
    per_inst = _per_group_profitability(rows, group_key="name")
    if per_inst is not None:
        artifact["per_instrument"] = per_inst

    # Per-day-of-week
    # Derive a day-of-week field from entry_date
    from datetime import datetime as _dt
    rows_with_dow = []
    for r in rows:
        try:
            dt = _dt.fromisoformat(r["entry_date"].replace(" ", "T").split("+")[0].split("-0")[0]
                                    if "T" not in r["entry_date"] else r["entry_date"].split("+")[0].split("-0")[0])
        except Exception:
            continue
        dow_name = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]
        rows_with_dow.append({**r, "_dow": dow_name})
    if rows_with_dow:
        per_dow = _per_group_profitability(rows_with_dow, group_key="_dow")
        if per_dow is not None:
            artifact["per_day_of_week"] = per_dow

    return artifact


def write_tori_artifact(dry_run: bool = False) -> Path | dict:
    """Build + validate + write. Validation happens through the same
    pydantic model the loader uses so a bad writer fails loudly here
    instead of writing a bad file that the loader will silently drop."""
    from helio.strategy_confidence import StrategyConfidenceArtifact

    data = build_tori_artifact()
    # Fail-fast: validate before writing
    StrategyConfidenceArtifact.model_validate(data)
    if dry_run:
        return data
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ARTIFACT_PATH


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the artifact instead of writing")
    args = ap.parse_args()
    result = write_tori_artifact(dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(result, indent=2))
    else:
        print(f"Wrote: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
