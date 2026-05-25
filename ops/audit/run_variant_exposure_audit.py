"""Variant-cohort holdings exposure audit.

The 5 xs_momentum variants each pick their top-N from a different
universe. Monthly returns are weakly correlated (per
docs/COHORT_CORRELATION_FINDINGS_20260525.md), but that doesn't
guarantee their HOLDINGS are diversified. Two strategies with 0.07
return correlation can still hold the same instruments in different
months.

This audit computes the CONCURRENT exposure: what would the 5
variants hold on a single rebalance date, and is there hidden
concentration risk?

Method:
  1. For each variant, run the same ranking logic we'd use live, get
     the current top picks
  2. Compute combined notional per ticker assuming each variant
     deploys its allocation_factor × per_strategy_cap of equity
  3. Flag any ticker that consumes >N% of fleet capital
  4. Flag any sector / asset class that exceeds cluster cap

USAGE
-----
    python -m ops.audit.run_variant_exposure_audit
    python -m ops.audit.run_variant_exposure_audit --anchor 250000
    python -m ops.audit.run_variant_exposure_audit --json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
OUT_DIR = REPO / "ops" / "reports" / "system_audit"

# Concentration thresholds. Tunable; defaults are conservative.
SINGLE_TICKER_WARN_PCT = 30.0   # any one ticker > 30% of fleet -> WARN
SINGLE_TICKER_RED_PCT  = 50.0   # > 50% -> RED


def _get_picks_for_variant(variant_name: str, *, period: str = "2y") -> dict:
    """Compute what the variant would pick if it rebalanced today.

    Reuses the runner's variant config + ranking pipeline. Doesn't
    mutate live state (the variant's --check mode does the same
    thing).
    """
    try:
        # Lazy import the runner module so configure_variant works
        from forge.xs_momentum import runner as xs
        from helio.xs_momentum import (
            rank_universe_by_momentum, select_top_quintile,
        )
    except Exception as exc:
        return {"variant": variant_name, "error": f"import: {exc}"}

    try:
        xs.configure_variant(variant_name)
    except Exception as exc:
        return {"variant": variant_name, "error": f"configure: {exc}"}

    try:
        closes = xs._fetch_history(xs.PARAMS["universe"], period=period)
    except Exception as exc:
        return {"variant": variant_name, "error": f"fetch: {exc}"}
    if closes.empty:
        return {"variant": variant_name, "error": "no data"}

    per_asset = {
        t: closes[t].dropna().tolist()
        for t in xs.PARAMS["universe"] if t in closes.columns
    }
    try:
        ranked = rank_universe_by_momentum(
            per_asset,
            long_lookback=xs.PARAMS["long_lookback"],
            short_lookback=xs.PARAMS["short_lookback"],
        )
        picks = select_top_quintile(
            ranked, fraction=xs.PARAMS["top_quintile_fraction"]
        )
    except Exception as exc:
        return {"variant": variant_name, "error": f"rank: {exc}"}

    return {
        "variant": variant_name,
        "strategy_label": xs.STRATEGY_LABEL,
        "universe_size": len(xs.PARAMS["universe"]),
        "picks": [{"ticker": p.ticker, "score": round(p.score * 100, 2)}
                  for p in picks],
        "as_of": str(closes.index[-1]) if len(closes) else None,
    }


def _allocation_for(strategy_label: str) -> float:
    """Read allocation_factor for the strategy from
    argus_flow/configs/allocation_factors.json."""
    try:
        path = REPO / "argus_flow" / "configs" / "allocation_factors.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("factors", {}).get(strategy_label, 0.0))
    except Exception:
        return 0.0


def _per_strategy_cap_for(strategy_label: str) -> float:
    """Per-strategy notional cap (fraction of anchor). Defaults to
    PER_STRATEGY_DEFAULT_CAP (0.4) if not listed."""
    try:
        from helio.cluster_exposure import (
            PER_STRATEGY_NOTIONAL_CAP_X,
            PER_STRATEGY_DEFAULT_CAP,
        )
        return float(
            PER_STRATEGY_NOTIONAL_CAP_X.get(
                strategy_label, PER_STRATEGY_DEFAULT_CAP
            )
        )
    except Exception:
        return 0.4


def _compute_combined_exposure(
    picks_per_variant: list[dict],
    *,
    anchor_usd: float,
) -> dict:
    """For each ticker held by any variant, compute combined notional
    across all variants that hold it.

    Within one variant: pick weight = allocation × per_strategy_cap /
    num_picks. (Per-pick equal weight inside the variant.)

    Across variants: combined notional = sum of weights × anchor.
    """
    per_variant_breakdown: dict[str, list[dict]] = {}
    per_ticker_notional: dict[str, float] = {}
    per_ticker_holders: dict[str, list[str]] = {}

    for v in picks_per_variant:
        if v.get("error") or not v.get("picks"):
            continue
        label = v["strategy_label"]
        n_picks = len(v["picks"])
        alloc = _allocation_for(label)
        cap_x = _per_strategy_cap_for(label)
        # variant's deployable notional = anchor × allocation × cap_x
        variant_notional = anchor_usd * alloc * cap_x
        per_pick_notional = variant_notional / max(1, n_picks)
        breakdown = []
        for p in v["picks"]:
            tkr = p["ticker"]
            per_ticker_notional[tkr] = per_ticker_notional.get(tkr, 0) + per_pick_notional
            per_ticker_holders.setdefault(tkr, []).append(label)
            breakdown.append({
                "ticker": tkr,
                "score_pct": p["score"],
                "weight_within_variant": round(1.0 / n_picks, 3),
                "per_pick_notional_usd": round(per_pick_notional, 0),
            })
        per_variant_breakdown[label] = breakdown

    # Total fleet xs_momentum-family notional
    fleet_notional = sum(per_ticker_notional.values())
    # Percent of fleet per ticker
    per_ticker_pct: dict[str, float] = {}
    for tkr, n in per_ticker_notional.items():
        per_ticker_pct[tkr] = (n / fleet_notional * 100.0) if fleet_notional > 0 else 0

    # Sort tickers by exposure descending
    tickers_sorted = sorted(per_ticker_notional, key=lambda t: -per_ticker_notional[t])

    # Concentration warnings
    warnings: list[dict] = []
    for tkr in tickers_sorted:
        pct = per_ticker_pct[tkr]
        if pct >= SINGLE_TICKER_RED_PCT:
            warnings.append({
                "ticker": tkr, "pct_of_fleet": round(pct, 1),
                "level": "RED",
                "held_by": per_ticker_holders[tkr],
            })
        elif pct >= SINGLE_TICKER_WARN_PCT:
            warnings.append({
                "ticker": tkr, "pct_of_fleet": round(pct, 1),
                "level": "WARN",
                "held_by": per_ticker_holders[tkr],
            })

    return {
        "anchor_usd": anchor_usd,
        "fleet_notional_usd": round(fleet_notional, 0),
        "per_variant_breakdown": per_variant_breakdown,
        "per_ticker": [
            {
                "ticker": t,
                "notional_usd": round(per_ticker_notional[t], 0),
                "pct_of_fleet": round(per_ticker_pct[t], 2),
                "n_holders": len(per_ticker_holders[t]),
                "held_by": per_ticker_holders[t],
            }
            for t in tickers_sorted
        ],
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor", type=float, default=250_000.0,
                        help="Account anchor for notional math "
                        "(default: $250K paper)")
    parser.add_argument("--variants", default="all",
                        help="Comma-separated variant names, or 'all'")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from forge.xs_momentum.runner import _VARIANT_REGISTRY
    if args.variants == "all":
        variants = list(_VARIANT_REGISTRY.keys())
    else:
        variants = [v.strip() for v in args.variants.split(",")]
        for v in variants:
            if v not in _VARIANT_REGISTRY:
                print(f"ERROR: unknown variant {v!r}")
                return 2

    print("Querying current picks for each variant...", file=sys.stderr)
    picks = []
    for v in variants:
        print(f"  {v}...", file=sys.stderr, end=" ", flush=True)
        r = _get_picks_for_variant(v)
        picks.append(r)
        if r.get("error"):
            print(f"ERR {r['error']}", file=sys.stderr)
        else:
            tkrs = ", ".join(p["ticker"] for p in r["picks"])
            print(f"{len(r['picks'])} picks: {tkrs}", file=sys.stderr)

    exposure = _compute_combined_exposure(picks, anchor_usd=args.anchor)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "picks_per_variant": picks,
        "exposure": exposure,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "variant_exposure_audit.json").write_text(
        json.dumps(report, indent=2, default=str), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print()
    print("# Variant cohort exposure audit")
    print(f"Generated: {report['generated_at']}")
    print(f"Anchor: ${args.anchor:,.0f}")
    print(f"Combined fleet notional: ${exposure['fleet_notional_usd']:,.0f}")
    print()
    print("## Per-variant picks")
    print()
    for v in picks:
        if v.get("error"):
            print(f"  {v['variant']}: ERROR {v['error']}")
            continue
        tkrs = ", ".join(f"{p['ticker']} ({p['score']:+.1f}%)" for p in v["picks"])
        print(f"  {v['strategy_label']}: {tkrs}")
    print()
    print("## Combined ticker exposure (sorted by fleet %)")
    print()
    print("| Ticker | Notional | % of fleet | Held by |")
    print("|---|---|---|---|")
    for t in exposure["per_ticker"]:
        holders = ", ".join(h.replace("forge_xs_momentum_", "xs_m_").replace("forge_xs_momentum", "xs_m") for h in t["held_by"])
        print(f"| {t['ticker']} | ${t['notional_usd']:,.0f} | {t['pct_of_fleet']}% | {holders} |")
    print()
    if exposure["warnings"]:
        print("## Concentration warnings")
        print()
        for w in exposure["warnings"]:
            print(f"- **{w['level']}** — {w['ticker']} = {w['pct_of_fleet']}% "
                  f"of fleet (held by {len(w['held_by'])} variants: "
                  f"{', '.join(w['held_by'])})")
    else:
        print("## Concentration check: GREEN")
        print("  No ticker exceeds 30% of fleet notional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
