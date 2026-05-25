"""Walk-forward out-of-sample test — honest sanity check on whether
backtest results survive when the test window is OUTSIDE the
training window.

The 2026-05-25 audit found: "Cohort Sharpe 1.19 is in-sample to
backtest construction. No walk-forward OOS test exists." This CLI
fills that gap.

Method:
  1. Split the 20y history into 'train' (first ~70%) and 'test'
     (last ~30%)
  2. For each universe, run the disciplined gate on the TRAIN
     window only
  3. Take the survivors of train
  4. Run the same survivors on the TEST window
  5. Report whether train survivors stay survivors in test
  6. Compute Sharpe ratio (in-sample) vs (out-of-sample)

A robust edge has OOS Sharpe ~ in-sample Sharpe (small drop is OK).
An overfit edge has OOS Sharpe << in-sample Sharpe.

USAGE
-----
    python -m ops.audit.run_walk_forward_oos
    python -m ops.audit.run_walk_forward_oos --train-frac 0.5
    python -m ops.audit.run_walk_forward_oos --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

SURVIVOR_PF_FLOOR = 1.20
SLIPPAGE_BPS = 10.0

UNIVERSES_TO_TEST = [
    ("broad_8",         None),
    ("sectors_spdr_11", "sectors_spdr_11"),
    ("style_factors_8", "style_factors_8"),
    ("legacy_15",       "legacy_sectors_countries_15"),
]


def _backtest_window_sliced(
    universe: list[str],
    *,
    period: str = "20y",
    train_frac: float = 0.7,
) -> tuple[dict, dict]:
    """Run backtest over `period`, then SPLIT the trade list into
    train/test by chronological cutoff. Return (train_stats, test_stats)."""
    try:
        from forge.xs_momentum.runner import backtest
        from helio.bootstrap_stats import bootstrap_profit_factor
    except Exception as exc:
        return {"error": f"import: {exc}"}, {"error": "import"}

    try:
        result = backtest(period=period, universe_override=universe)
    except Exception as exc:
        return {"error": f"backtest: {exc}"}, {"error": "backtest"}
    if "error" in result:
        return result, {"error": "backtest"}
    trades = result.get("trades_detail") or []
    if not trades:
        return {"error": "no trades"}, {"error": "no trades"}

    # Sort by entry_date, split chronologically
    trades.sort(key=lambda t: t.get("entry_date", ""))
    cutoff_idx = int(len(trades) * train_frac)
    train_trades = trades[:cutoff_idx]
    test_trades = trades[cutoff_idx:]
    if not train_trades or not test_trades:
        return {"error": "split empty"}, {"error": "split empty"}

    def _stats(tlist):
        drag = 2.0 * (SLIPPAGE_BPS / 100.0)
        pnls = [t["pnl_pct"] - drag for t in tlist]
        try:
            boot = bootstrap_profit_factor(pnls, n_resamples=2000)
        except Exception:
            return {"error": "bootstrap"}
        # Monthly returns
        by_month = {}
        for t in tlist:
            m = str(t.get("exit_date", ""))[:7]
            by_month.setdefault(m, []).append(t["pnl_pct"] - drag)
        rets = [sum(by_month[m]) / len(by_month[m]) / 100
                for m in sorted(by_month)]
        if len(rets) > 1:
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            sd = math.sqrt(var) if var > 0 else 0.0
            sharpe = mean / sd * math.sqrt(12) if sd > 0 else 0.0
        else:
            sharpe = 0.0
        return {
            "n_trades": len(tlist),
            "pf_point": round(boot.point, 2),
            "ci_95_lower": round(boot.ci_lower, 2),
            "ci_95_upper": round(boot.ci_upper, 2),
            "sharpe": round(sharpe, 2),
            "first_entry": train_trades[0]["entry_date"] if tlist is train_trades else test_trades[0]["entry_date"],
            "last_exit": tlist[-1]["exit_date"],
            "survivor": boot.ci_lower >= SURVIVOR_PF_FLOOR,
        }

    return _stats(train_trades), _stats(test_trades)


def _run_one(u_label: str, u_key: str | None,
             *, period: str, train_frac: float) -> dict:
    try:
        from helio.xs_momentum_universes import get_universe
        from helio.xs_momentum import DEFAULT_UNIVERSE
    except Exception as exc:
        return {"universe": u_label, "error": f"import: {exc}"}
    universe = list(get_universe(u_key)) if u_key else list(DEFAULT_UNIVERSE)
    train, test = _backtest_window_sliced(
        universe, period=period, train_frac=train_frac,
    )
    if train.get("error") or test.get("error"):
        return {"universe": u_label,
                "error": train.get("error") or test.get("error"),
                "train": train, "test": test}
    # Honest summary: how much does OOS degrade?
    sharpe_decay = (test["sharpe"] - train["sharpe"])
    pf_decay = (test["pf_point"] - train["pf_point"])
    ci_decay = (test["ci_95_lower"] - train["ci_95_lower"])
    return {
        "universe": u_label,
        "universe_size": len(universe),
        "train_frac": train_frac,
        "train": train,
        "test": test,
        "sharpe_delta_oos_vs_is": round(sharpe_decay, 2),
        "pf_delta_oos_vs_is": round(pf_decay, 2),
        "ci_lower_delta_oos_vs_is": round(ci_decay, 2),
        "verdict": _classify(train, test),
    }


def _classify(train: dict, test: dict) -> str:
    """Honest verdict on walk-forward integrity."""
    if not train.get("survivor") and not test.get("survivor"):
        return "BOTH_FAIL"  # never had edge to begin with
    if train.get("survivor") and not test.get("survivor"):
        return "OVERFIT_DEGRADED"  # in-sample edge doesn't hold OOS
    if not train.get("survivor") and test.get("survivor"):
        return "LUCKY_OOS"  # train fails, test passes -> probably noise
    # Both survive — but how much does it decay?
    is_sharpe = train["sharpe"]
    oos_sharpe = test["sharpe"]
    if is_sharpe <= 0:
        return "EDGE_INTACT"
    decay_pct = (is_sharpe - oos_sharpe) / is_sharpe
    if decay_pct > 0.5:
        return "DEGRADED_BUT_PASSING"
    if decay_pct > 0.0:
        return "MILD_DECAY"
    return "STRENGTHENED_OOS"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", default="20y")
    parser.add_argument("--train-frac", type=float, default=0.7,
                        help="Fraction of trades used for training (default 0.7)")
    parser.add_argument("--universe", default=None,
                        help="Run only one universe label")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.universe:
        universes = [u for u in UNIVERSES_TO_TEST if u[0] == args.universe]
    else:
        universes = UNIVERSES_TO_TEST

    rows: list[dict] = []
    for u_label, u_key in universes:
        print(f"  {u_label}...", file=sys.stderr, end=" ", flush=True)
        r = _run_one(u_label, u_key, period=args.period,
                     train_frac=args.train_frac)
        rows.append(r)
        if r.get("error"):
            print(f"ERR {r['error']}", file=sys.stderr)
        else:
            t = r["train"]; s = r["test"]
            print(
                f"IS Sharpe {t['sharpe']:.2f} -> OOS {s['sharpe']:.2f} "
                f"({r['verdict']})",
                file=sys.stderr,
            )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "walk_forward_oos.json").write_text(
        json.dumps(rows, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    print()
    print("# Walk-Forward Out-of-Sample Audit")
    print(f"Generated: {datetime.now(timezone.utc).isoformat()}")
    print(f"Slippage: {SLIPPAGE_BPS} bps. Train fraction: {args.train_frac}")
    print()
    print("| Universe | n_IS | n_OOS | IS PF | OOS PF | IS Sharpe | OOS Sharpe | dSharpe | Verdict |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("error"):
            print(f"| {r['universe']} | — | — | — | — | — | — | — | ERR {r['error'][:30]} |")
            continue
        t = r["train"]; s = r["test"]
        print(
            f"| {r['universe']} | "
            f"{t['n_trades']} | {s['n_trades']} | "
            f"{t['pf_point']:.2f} | {s['pf_point']:.2f} | "
            f"{t['sharpe']:.2f} | {s['sharpe']:.2f} | "
            f"{r['sharpe_delta_oos_vs_is']:+.2f} | "
            f"**{r['verdict']}** |"
        )

    print()
    print("## Verdict legend")
    print()
    print("- **BOTH_FAIL**: never had edge — backtest noise")
    print("- **OVERFIT_DEGRADED**: in-sample edge does NOT hold out-of-sample (worst)")
    print("- **LUCKY_OOS**: train fails but test passes — probably noise")
    print("- **DEGRADED_BUT_PASSING**: edge holds OOS but decays >50%")
    print("- **MILD_DECAY**: edge holds OOS with <50% Sharpe decay")
    print("- **STRENGTHENED_OOS**: OOS Sharpe > in-sample (rare — possible regime fit)")
    print()
    print("HONEST READ: only **MILD_DECAY** + **STRENGTHENED_OOS** verdicts")
    print("should justify real-money deployment. Anything else means the")
    print("backtest is in-sample to the data it was discovered on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
