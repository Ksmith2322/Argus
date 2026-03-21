"""Classify edge from sweep results: DEAD / ILLUSION / WEAK_BUT_REAL / PROMISING.

Applies the kill-or-continue framework. No "maybe."

Usage:
    python -m argus_flow.analytics.classify_edge --input argus_flow/replay_out/sweep_summary.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def classify(sweep_path: str) -> dict:
    data = json.loads(Path(sweep_path).read_text())
    runs = data.get("runs", [])

    if not runs:
        return {"classification": "NO_DATA", "reason": "No sweep runs found."}

    # Extract key metrics
    valid_runs = [r for r in runs if r.get("expectancy_pct") is not None]
    if not valid_runs:
        return {"classification": "NO_DATA", "reason": "No runs with valid expectancy."}

    n = len(valid_runs)
    positive_exp = [r for r in valid_runs if r["expectancy_pct"] > 0]
    n_positive = len(positive_exp)

    fbr_values = [r["false_breakout_rate"] for r in valid_runs if r.get("false_breakout_rate") is not None]
    exp_values = [r["expectancy_pct"] for r in valid_runs]
    event_counts = [r["event_count"] for r in valid_runs]
    median_mfe = [r["median_mfe_pct"] for r in valid_runs if r.get("median_mfe_pct") is not None]
    median_mae = [r["median_mae_pct"] for r in valid_runs if r.get("median_mae_pct") is not None]

    avg_fbr = sum(fbr_values) / len(fbr_values) if fbr_values else 1.0
    avg_exp = sum(exp_values) / len(exp_values) if exp_values else 0.0
    min_events = min(event_counts) if event_counts else 0
    median_events = sorted(event_counts)[len(event_counts) // 2] if event_counts else 0

    reasons = []

    # ── Kill checks ─────────────────────────────────────────
    # 1. Insufficient data
    if median_events < 20:
        return {
            "classification": "INSUFFICIENT_DATA",
            "reason": f"Median event count too low ({median_events}). Need 20+ for statistical validity.",
            "metrics": {"median_events": median_events, "n_runs": n},
        }

    # 2. False breakout rate too high
    high_fbr_count = sum(1 for f in fbr_values if f > 0.65)
    if high_fbr_count >= n * 0.7:
        reasons.append(f"False breakout rate >65% in {high_fbr_count}/{n} configs (avg {avg_fbr:.3f})")

    # 3. Median MAE > median MFE across most configs
    mae_dominates = sum(1 for m, a in zip(median_mfe, median_mae) if a >= m)
    if mae_dominates >= n * 0.7:
        reasons.append(f"Median MAE >= median MFE in {mae_dominates}/{n} configs — no asymmetry")

    # 4. All configs negative expectancy
    if n_positive == 0:
        reasons.append("ALL configs have negative post-cost expectancy")
        return {
            "classification": "DEAD",
            "reason": " | ".join(reasons),
            "metrics": {
                "n_runs": n,
                "n_positive": 0,
                "avg_expectancy": avg_exp,
                "avg_false_breakout_rate": avg_fbr,
            },
        }

    # If major kill conditions hit
    if len(reasons) >= 2:
        return {
            "classification": "DEAD",
            "reason": " | ".join(reasons),
            "metrics": {
                "n_runs": n,
                "n_positive": n_positive,
                "avg_expectancy": avg_exp,
                "avg_false_breakout_rate": avg_fbr,
            },
        }

    # ── Illusion check ──────────────────────────────────────
    # Only 1-2 configs positive, neighbors collapse
    if n_positive <= 2:
        # Check if positive configs are isolated (neighbors negative)
        return {
            "classification": "ILLUSION",
            "reason": f"Only {n_positive}/{n} configs positive. Likely overfit to specific threshold.",
            "metrics": {
                "n_runs": n,
                "n_positive": n_positive,
                "avg_expectancy": avg_exp,
                "positive_configs": [
                    {"comp": r["compression_threshold_pct"], "dz": r["flow_confirm_delta_z"],
                     "exp": r["expectancy_pct"]}
                    for r in positive_exp
                ],
            },
        }

    # ── Weak but real ───────────────────────────────────────
    # Positive but thin margin or parameter-sensitive
    exp_range = max(exp_values) - min(exp_values)
    avg_positive_exp = sum(r["expectancy_pct"] for r in positive_exp) / n_positive

    if n_positive <= n * 0.5 or avg_positive_exp < 0.0005:
        return {
            "classification": "WEAK_BUT_REAL",
            "reason": (
                f"{n_positive}/{n} configs positive (avg {avg_positive_exp:.6f}%). "
                f"Edge exists but thin — sensitive to parameters."
            ),
            "metrics": {
                "n_runs": n,
                "n_positive": n_positive,
                "avg_positive_expectancy": avg_positive_exp,
                "avg_false_breakout_rate": avg_fbr,
                "expectancy_range": exp_range,
            },
        }

    # ── Promising ───────────────────────────────────────────
    return {
        "classification": "PROMISING",
        "reason": (
            f"{n_positive}/{n} configs positive (avg {avg_positive_exp:.6f}%). "
            f"Gradual degradation across grid. FBR avg {avg_fbr:.3f}."
        ),
        "metrics": {
            "n_runs": n,
            "n_positive": n_positive,
            "avg_positive_expectancy": avg_positive_exp,
            "avg_false_breakout_rate": avg_fbr,
            "expectancy_range": exp_range,
            "best_config": max(valid_runs, key=lambda r: r["expectancy_pct"]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify edge from sweep results")
    parser.add_argument("--input", required=True, help="Path to sweep_summary.json")
    parser.add_argument("--output", default=None, help="Output path for classification JSON")
    args = parser.parse_args()

    result = classify(args.input)

    out_path = Path(args.output) if args.output else Path(args.input).parent / "edge_classification.json"
    out_path.write_text(json.dumps(result, indent=2))

    # Print verdict
    cls = result["classification"]
    colors = {
        "DEAD": "\033[91m",       # red
        "ILLUSION": "\033[93m",   # yellow
        "WEAK_BUT_REAL": "\033[96m",  # cyan
        "PROMISING": "\033[92m",  # green
        "INSUFFICIENT_DATA": "\033[90m",  # gray
        "NO_DATA": "\033[90m",
    }
    reset = "\033[0m"
    color = colors.get(cls, "")

    print(f"\n{'='*60}")
    print(f"EDGE CLASSIFICATION: {color}{cls}{reset}")
    print(f"{'='*60}")
    print(f"Reason: {result['reason']}")
    if "metrics" in result:
        print(f"\nMetrics:")
        for k, v in result["metrics"].items():
            if isinstance(v, dict):
                print(f"  {k}: {json.dumps(v, indent=4)}")
            else:
                print(f"  {k}: {v}")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()