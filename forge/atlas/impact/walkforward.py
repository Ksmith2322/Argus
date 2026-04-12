"""
Atlas Walk-Forward Backtest — Validate cascade predictions out-of-sample
with Benjamini-Hochberg FDR correction for multiple comparisons.

Split 154 historical macro events into Train / Validate / Test periods,
build per-event-type impact models from training data only, predict
direction + magnitude for OOS events, and score rigorously.

Usage:
    python -m forge.atlas.impact.walkforward
"""

import json
import logging
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats as sp_stats

from forge.atlas.db.schema import get_connection, DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

TRAIN_CUTOFF = "2016-01-01"
VALIDATE_CUTOFF = "2019-01-01"

MIN_TRAINING_EVENTS = 10   # minimum same-type events before making a prediction
MIN_TRAINING_CARS = 5      # minimum non-null CARs per asset to predict

PRIMARY_WINDOW = "CAR(1,5)"
SECONDARY_WINDOW = "CAR(1,20)"

# Stability sub-periods
STABILITY_PERIODS = [
    ("pre-2010", None, "2010-01-01"),
    ("2010-2018", "2010-01-01", "2019-01-01"),
    ("2019+", "2019-01-01", None),
]

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
RESULTS_PATH = DATA_DIR / "atlas_walkforward_results.json"

# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Event:
    event_id: str
    timestamp: str
    event_type: str
    event_category: str
    severity: float


@dataclass
class Impact:
    event_id: str
    asset: str
    window_label: str
    abnormal_return: Optional[float]


@dataclass
class Prediction:
    event_id: str
    event_type: str
    event_date: str
    asset: str
    window_label: str
    n_training: int
    training_mean: float
    training_std: float
    training_tstat: float
    expected_direction: int     # +1 or -1
    expected_magnitude: float
    actual_car: float
    direction_correct: bool
    magnitude_error: float
    period: str  # "validate" or "test"


@dataclass
class PairResult:
    event_type: str
    asset: str
    window_label: str
    n_predictions: int
    direction_accuracy: float
    profit_factor: float
    magnitude_corr: float
    mean_actual: float
    mean_predicted: float
    sharpe: float
    p_value: float
    fdr_significant: bool
    stability_consistent: bool
    verdict: str  # PROVEN / FRAGILE / KILLED


# ──────────────────────────────────────────────────────────────────────────────
# Database loading
# ──────────────────────────────────────────────────────────────────────────────


def load_events(conn) -> list[Event]:
    """Load all events, sorted by timestamp ascending."""
    rows = conn.execute(
        "SELECT event_id, timestamp, event_type, event_category, severity "
        "FROM events ORDER BY timestamp ASC"
    ).fetchall()
    events = []
    for r in rows:
        events.append(Event(
            event_id=r["event_id"],
            timestamp=r["timestamp"],
            event_type=r["event_type"],
            event_category=r["event_category"],
            severity=r["severity"] or 0.0,
        ))
    log.info("Loaded %d events", len(events))
    return events


def load_impacts(conn) -> dict[tuple[str, str, str], float]:
    """Load all impacts as {(event_id, asset, window_label): abnormal_return}."""
    rows = conn.execute(
        "SELECT event_id, asset, window_label, abnormal_return FROM impacts "
        "WHERE abnormal_return IS NOT NULL"
    ).fetchall()
    lookup = {}
    for r in rows:
        lookup[(r["event_id"], r["asset"], r["window_label"])] = r["abnormal_return"]
    log.info("Loaded %d impact measurements", len(lookup))
    return lookup


def get_assets(conn) -> list[str]:
    """Get distinct assets from impacts table."""
    rows = conn.execute("SELECT DISTINCT asset FROM impacts ORDER BY asset").fetchall()
    return [r["asset"] for r in rows]


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward engine
# ──────────────────────────────────────────────────────────────────────────────


def event_date_str(event: Event) -> str:
    """Extract date string (YYYY-MM-DD) from event timestamp."""
    ts = event.timestamp
    # Timestamps may be ISO format with T or space
    return ts[:10]


def run_walkforward(
    events: list[Event],
    impacts: dict[tuple[str, str, str], float],
    assets: list[str],
    window_label: str,
    period_name: str,
    period_start: str,
    period_end: Optional[str],
) -> list[Prediction]:
    """
    Run walk-forward predictions for events in [period_start, period_end).

    For each OOS event, uses ONLY events strictly before it (expanding window)
    to build the prediction model.
    """
    predictions = []

    # Group events by type for fast lookup
    events_by_type: dict[str, list[Event]] = defaultdict(list)
    for ev in events:
        events_by_type[ev.event_type].append(ev)

    # Filter to OOS events in this period
    oos_events = []
    for ev in events:
        d = event_date_str(ev)
        if d >= period_start:
            if period_end is None or d < period_end:
                oos_events.append(ev)

    log.info(
        "Walk-forward [%s] window=%s: %d OOS events in [%s, %s)",
        period_name, window_label, len(oos_events),
        period_start, period_end or "end",
    )

    for ev in oos_events:
        ev_date = event_date_str(ev)

        # Get training events: same type, strictly before this event
        training = [
            e for e in events_by_type[ev.event_type]
            if event_date_str(e) < ev_date
        ]

        if len(training) < MIN_TRAINING_EVENTS:
            continue

        for asset in assets:
            # Gather training CARs for this asset
            training_cars = []
            for te in training:
                car = impacts.get((te.event_id, asset, window_label))
                if car is not None:
                    training_cars.append(car)

            if len(training_cars) < MIN_TRAINING_CARS:
                continue

            # Compute prediction from training data
            mean_car = float(np.mean(training_cars))
            std_car = float(np.std(training_cars, ddof=1)) if len(training_cars) > 1 else 0.0
            t_stat = (mean_car / (std_car / math.sqrt(len(training_cars)))) if std_car > 0 else 0.0

            expected_dir = 1 if mean_car >= 0 else -1

            # Get actual CAR
            actual = impacts.get((ev.event_id, asset, window_label))
            if actual is None:
                continue

            actual_dir = 1 if actual >= 0 else -1

            predictions.append(Prediction(
                event_id=ev.event_id,
                event_type=ev.event_type,
                event_date=ev_date,
                asset=asset,
                window_label=window_label,
                n_training=len(training_cars),
                training_mean=mean_car,
                training_std=std_car,
                training_tstat=t_stat,
                expected_direction=expected_dir,
                expected_magnitude=mean_car,
                actual_car=actual,
                direction_correct=(actual_dir == expected_dir),
                magnitude_error=abs(actual - mean_car),
                period=period_name,
            ))

    log.info("  => %d predictions generated", len(predictions))
    return predictions


# ──────────────────────────────────────────────────────────────────────────────
# Scoring & FDR correction
# ──────────────────────────────────────────────────────────────────────────────


def benjamini_hochberg(p_values: list[float], alpha: float = 0.05) -> list[bool]:
    """
    Benjamini-Hochberg FDR correction.
    Returns list of booleans (True = significant) aligned with input order.
    """
    m = len(p_values)
    if m == 0:
        return []

    # Sort p-values, keeping track of original indices
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])
    significant = [False] * m

    # Find the largest k such that p_(k) <= k/m * alpha
    max_k = -1
    for rank, (orig_idx, pval) in enumerate(indexed, start=1):
        threshold = (rank / m) * alpha
        if pval <= threshold:
            max_k = rank

    # All items with rank <= max_k are significant
    if max_k > 0:
        for rank, (orig_idx, pval) in enumerate(indexed, start=1):
            if rank <= max_k:
                significant[orig_idx] = True

    return significant


def score_pair(
    preds: list[Prediction],
) -> dict:
    """Score a single event_type x asset pair."""
    if not preds:
        return None

    n = len(preds)
    correct = sum(1 for p in preds if p.direction_correct)
    accuracy = correct / n if n > 0 else 0.0

    # Profit factor
    gain = sum(abs(p.actual_car) for p in preds if p.direction_correct)
    loss = sum(abs(p.actual_car) for p in preds if not p.direction_correct)
    profit_factor = gain / loss if loss > 0 else (float("inf") if gain > 0 else 0.0)

    # Magnitude correlation
    predicted = [p.expected_magnitude for p in preds]
    actual = [p.actual_car for p in preds]
    if n >= 3 and np.std(predicted) > 0 and np.std(actual) > 0:
        corr, _ = sp_stats.pearsonr(predicted, actual)
    else:
        corr = 0.0

    # Sharpe per trade: treat each prediction's actual CAR (signed by predicted direction) as a "return"
    trade_returns = []
    for p in preds:
        # If we predicted +1, our return is actual_car; if -1, our return is -actual_car
        trade_returns.append(p.actual_car * p.expected_direction)
    trade_returns = np.array(trade_returns)
    mean_ret = float(np.mean(trade_returns))
    std_ret = float(np.std(trade_returns, ddof=1)) if n > 1 else 0.0
    sharpe = mean_ret / std_ret if std_ret > 0 else 0.0

    # P-value via binomial test (is accuracy significantly > 50%?)
    if n >= 5:
        binom_result = sp_stats.binomtest(correct, n, 0.5, alternative="greater")
        p_value = binom_result.pvalue
    else:
        p_value = 1.0

    return {
        "n": n,
        "accuracy": accuracy,
        "profit_factor": profit_factor,
        "corr": corr,
        "mean_actual": float(np.mean(actual)),
        "mean_predicted": float(np.mean(predicted)),
        "sharpe": sharpe,
        "p_value": p_value,
    }


def compute_stability(
    events: list[Event],
    impacts: dict[tuple[str, str, str], float],
    event_type: str,
    asset: str,
    window_label: str,
) -> bool:
    """
    Check if the direction of the mean CAR is consistent across at least
    2 of 3 sub-periods.
    """
    directions = []
    for period_name, start, end in STABILITY_PERIODS:
        cars = []
        for ev in events:
            if ev.event_type != event_type:
                continue
            d = event_date_str(ev)
            if start and d < start:
                continue
            if end and d >= end:
                continue
            car = impacts.get((ev.event_id, asset, window_label))
            if car is not None:
                cars.append(car)

        if len(cars) >= 3:
            directions.append(1 if np.mean(cars) >= 0 else -1)

    if len(directions) < 2:
        return False

    # Check if at least 2 of the sub-periods agree on direction
    pos = sum(1 for d in directions if d > 0)
    neg = len(directions) - pos
    return max(pos, neg) >= 2


# ──────────────────────────────────────────────────────────────────────────────
# High-confidence subset analysis
# ──────────────────────────────────────────────────────────────────────────────


def high_confidence_analysis(preds: list[Prediction], tstat_threshold: float = 2.0):
    """Analyse the subset where training t-stat > threshold."""
    hc = [p for p in preds if abs(p.training_tstat) >= tstat_threshold]
    if not hc:
        return None
    n = len(hc)
    correct = sum(1 for p in hc if p.direction_correct)
    accuracy = correct / n
    gain = sum(abs(p.actual_car) for p in hc if p.direction_correct)
    loss = sum(abs(p.actual_car) for p in hc if not p.direction_correct)
    pf = gain / loss if loss > 0 else (float("inf") if gain > 0 else 0.0)
    return {"n": n, "accuracy": accuracy, "profit_factor": pf}


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────


def print_section(title: str, char: str = "="):
    width = 80
    print()
    print(char * width)
    print(f"  {title}")
    print(char * width)


def print_overall_metrics(preds: list[Prediction], period_name: str, window: str):
    """Print aggregate metrics for a period."""
    if not preds:
        print(f"  No predictions for {period_name} / {window}")
        return

    n = len(preds)
    correct = sum(1 for p in preds if p.direction_correct)
    accuracy = correct / n

    gain = sum(abs(p.actual_car) for p in preds if p.direction_correct)
    loss = sum(abs(p.actual_car) for p in preds if not p.direction_correct)
    pf = gain / loss if loss > 0 else float("inf")

    predicted = [p.expected_magnitude for p in preds]
    actual = [p.actual_car for p in preds]
    if n >= 3 and np.std(predicted) > 0 and np.std(actual) > 0:
        corr, _ = sp_stats.pearsonr(predicted, actual)
    else:
        corr = 0.0

    trade_returns = [p.actual_car * p.expected_direction for p in preds]
    mean_r = np.mean(trade_returns)
    std_r = np.std(trade_returns, ddof=1) if n > 1 else 0.0
    sharpe = mean_r / std_r if std_r > 0 else 0.0

    print(f"  Period:              {period_name}")
    print(f"  Window:              {window}")
    print(f"  Total predictions:   {n}")
    print(f"  Direction accuracy:  {accuracy:.1%}  ({correct}/{n})")
    print(f"  Profit factor:       {pf:.2f}")
    print(f"  Magnitude corr:      {corr:.3f}")
    print(f"  Sharpe per trade:    {sharpe:.3f}")
    print(f"  Mean |actual CAR|:   {np.mean(np.abs(actual)):.4f}")

    # High-confidence subset
    hc = high_confidence_analysis(preds)
    if hc:
        print(f"  --- High confidence (|t| >= 2.0) ---")
        print(f"  HC predictions:      {hc['n']}")
        print(f"  HC direction acc:    {hc['accuracy']:.1%}")
        print(f"  HC profit factor:    {hc['profit_factor']:.2f}")
    else:
        print(f"  No high-confidence predictions (|t| >= 2.0)")


def print_pair_results(pairs: list[PairResult]):
    """Print results table for individual pairs."""
    if not pairs:
        print("  No pairs to report.")
        return

    # Sort by verdict priority, then accuracy
    verdict_order = {"PROVEN": 0, "FRAGILE": 1, "KILLED": 2}
    pairs_sorted = sorted(
        pairs,
        key=lambda p: (verdict_order.get(p.verdict, 3), -p.direction_accuracy),
    )

    header = (
        f"  {'Event Type':<22} {'Asset':<6} {'N':>4} {'Acc%':>6} {'PF':>6} "
        f"{'Corr':>6} {'Sharpe':>7} {'pVal':>7} {'FDR':>4} {'Stable':>6} {'Verdict':<8}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    for p in pairs_sorted:
        fdr_mark = "Y" if p.fdr_significant else "N"
        stab_mark = "Y" if p.stability_consistent else "N"
        pf_str = f"{p.profit_factor:.2f}" if p.profit_factor < 100 else "Inf"
        print(
            f"  {p.event_type:<22} {p.asset:<6} {p.n_predictions:>4} "
            f"{p.direction_accuracy:>5.1%} {pf_str:>6} {p.magnitude_corr:>6.3f} "
            f"{p.sharpe:>7.3f} {p.p_value:>7.4f} {fdr_mark:>4} {stab_mark:>6} "
            f"{p.verdict:<8}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main():
    print_section("ATLAS WALK-FORWARD BACKTEST")
    print("  Validating cascade impact predictions out-of-sample")
    print("  with Benjamini-Hochberg FDR correction")
    print()

    # Load data
    conn = get_connection()
    events = load_events(conn)
    impacts = load_impacts(conn)
    assets = get_assets(conn)
    conn.close()

    if not events:
        log.error("No events in database. Run backfill first.")
        sys.exit(1)

    # Summarize data split
    train_events = [e for e in events if event_date_str(e) < TRAIN_CUTOFF]
    val_events = [e for e in events if TRAIN_CUTOFF <= event_date_str(e) < VALIDATE_CUTOFF]
    test_events = [e for e in events if event_date_str(e) >= VALIDATE_CUTOFF]
    print(f"  Events:  {len(events)} total")
    print(f"  Train:   {len(train_events)}  (before {TRAIN_CUTOFF})")
    print(f"  Validate:{len(val_events)}  ({TRAIN_CUTOFF} to {VALIDATE_CUTOFF})")
    print(f"  Test:    {len(test_events)}  ({VALIDATE_CUTOFF} onwards)")
    print(f"  Assets:  {len(assets)}")
    print(f"  Impacts: {len(impacts)}")

    # Collect event types
    event_types = sorted(set(e.event_type for e in events))
    print(f"  Event types: {len(event_types)}")

    # ──────────────────────────────────────────────────────────────────────
    # Run walk-forward for each period and window
    # ──────────────────────────────────────────────────────────────────────

    all_results = {}

    for window in [PRIMARY_WINDOW, SECONDARY_WINDOW]:
        for period_name, p_start, p_end in [
            ("validate", TRAIN_CUTOFF, VALIDATE_CUTOFF),
            ("test", VALIDATE_CUTOFF, None),
        ]:
            key = f"{period_name}_{window}"
            preds = run_walkforward(
                events, impacts, assets, window, period_name, p_start, p_end,
            )

            # ── Overall metrics ──────────────────────────────────────────
            print_section(f"OVERALL: {period_name.upper()} / {window}")
            print_overall_metrics(preds, period_name, window)

            # ── Per-pair scoring with FDR ─────────────────────────────────
            pair_preds: dict[tuple[str, str], list[Prediction]] = defaultdict(list)
            for p in preds:
                pair_preds[(p.event_type, p.asset)].append(p)

            pair_scores = []
            pair_keys = []
            p_values = []

            for (etype, asset), ppreds in sorted(pair_preds.items()):
                sc = score_pair(ppreds)
                if sc is None or sc["n"] < 3:
                    continue
                pair_scores.append(sc)
                pair_keys.append((etype, asset))
                p_values.append(sc["p_value"])

            # BH-FDR correction
            fdr_flags = benjamini_hochberg(p_values, alpha=0.05)

            # Build PairResults
            pair_results: list[PairResult] = []
            for idx, ((etype, asset), sc) in enumerate(zip(pair_keys, pair_scores)):
                stable = compute_stability(events, impacts, etype, asset, window)

                # Verdict logic
                if fdr_flags[idx] and stable and sc["accuracy"] > 0.55:
                    verdict = "PROVEN"
                elif sc["accuracy"] > 0.55 or sc["profit_factor"] > 1.3:
                    verdict = "FRAGILE"
                else:
                    verdict = "KILLED"

                pair_results.append(PairResult(
                    event_type=etype,
                    asset=asset,
                    window_label=window,
                    n_predictions=sc["n"],
                    direction_accuracy=sc["accuracy"],
                    profit_factor=sc["profit_factor"],
                    magnitude_corr=sc["corr"],
                    mean_actual=sc["mean_actual"],
                    mean_predicted=sc["mean_predicted"],
                    sharpe=sc["sharpe"],
                    p_value=sc["p_value"],
                    fdr_significant=fdr_flags[idx],
                    stability_consistent=stable,
                    verdict=verdict,
                ))

            print_section(f"PAIR RESULTS: {period_name.upper()} / {window}", "-")
            print_pair_results(pair_results)

            # Tally verdicts
            verdicts = defaultdict(int)
            for pr in pair_results:
                verdicts[pr.verdict] += 1
            print()
            print(f"  PROVEN:  {verdicts.get('PROVEN', 0)}")
            print(f"  FRAGILE: {verdicts.get('FRAGILE', 0)}")
            print(f"  KILLED:  {verdicts.get('KILLED', 0)}")
            n_fdr = sum(1 for f in fdr_flags if f)
            print(f"  FDR-significant pairs: {n_fdr}/{len(fdr_flags)}")

            # Store for JSON output
            all_results[key] = {
                "period": period_name,
                "window": window,
                "n_predictions": len(preds),
                "overall": {
                    "n": len(preds),
                    "direction_accuracy": (
                        sum(1 for p in preds if p.direction_correct) / len(preds)
                        if preds else 0.0
                    ),
                },
                "high_confidence": high_confidence_analysis(preds),
                "pairs": [asdict(pr) for pr in pair_results],
                "verdict_counts": dict(verdicts),
                "fdr_significant_count": n_fdr,
            }

    # ──────────────────────────────────────────────────────────────────────
    # Cross-period comparison (validate vs test)
    # ──────────────────────────────────────────────────────────────────────

    print_section("CROSS-PERIOD COMPARISON (PRIMARY: CAR(1,5))")

    val_key = f"validate_{PRIMARY_WINDOW}"
    test_key = f"test_{PRIMARY_WINDOW}"

    val_data = all_results.get(val_key, {})
    test_data = all_results.get(test_key, {})

    val_acc = val_data.get("overall", {}).get("direction_accuracy", 0)
    test_acc = test_data.get("overall", {}).get("direction_accuracy", 0)

    print(f"  Validate accuracy: {val_acc:.1%}")
    print(f"  Test accuracy:     {test_acc:.1%}")
    print()

    # Compare pair verdicts across periods
    val_pairs = {(p["event_type"], p["asset"]): p["verdict"]
                 for p in val_data.get("pairs", [])}
    test_pairs = {(p["event_type"], p["asset"]): p["verdict"]
                  for p in test_data.get("pairs", [])}

    both_proven = []
    val_only = []
    test_only = []
    both_killed = []

    all_pair_keys = set(val_pairs.keys()) | set(test_pairs.keys())
    for pk in sorted(all_pair_keys):
        v_verdict = val_pairs.get(pk, "N/A")
        t_verdict = test_pairs.get(pk, "N/A")
        if v_verdict == "PROVEN" and t_verdict == "PROVEN":
            both_proven.append(pk)
        elif v_verdict == "PROVEN" and t_verdict != "PROVEN":
            val_only.append(pk)
        elif v_verdict != "PROVEN" and t_verdict == "PROVEN":
            test_only.append(pk)
        elif v_verdict == "KILLED" and t_verdict == "KILLED":
            both_killed.append(pk)

    print(f"  PROVEN in BOTH periods:     {len(both_proven)}")
    for etype, asset in both_proven:
        print(f"    {etype} / {asset}")
    print(f"  PROVEN only in validate:    {len(val_only)}")
    for etype, asset in val_only:
        print(f"    {etype} / {asset}  (overfit to validation)")
    print(f"  PROVEN only in test:        {len(test_only)}")
    print(f"  KILLED in both:             {len(both_killed)}")

    # ──────────────────────────────────────────────────────────────────────
    # Final edge assessment
    # ──────────────────────────────────────────────────────────────────────

    print_section("FINAL ATLAS EDGE ASSESSMENT")

    edge_criteria = {
        "val_acc_above_55": val_acc > 0.55,
        "test_acc_above_55": test_acc > 0.55,
        "any_proven_both": len(both_proven) > 0,
        "val_fdr_pairs": val_data.get("fdr_significant_count", 0) > 0,
        "test_fdr_pairs": test_data.get("fdr_significant_count", 0) > 0,
    }

    val_hc = val_data.get("high_confidence")
    test_hc = test_data.get("high_confidence")
    edge_criteria["val_hc_above_60"] = (
        val_hc is not None and val_hc.get("accuracy", 0) > 0.60
    )
    edge_criteria["test_hc_above_60"] = (
        test_hc is not None and test_hc.get("accuracy", 0) > 0.60
    )

    for criterion, passed in edge_criteria.items():
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {criterion}")

    passes = sum(1 for v in edge_criteria.values() if v)
    total = len(edge_criteria)

    print()
    if edge_criteria["test_acc_above_55"] and edge_criteria["any_proven_both"]:
        print("  VERDICT: EDGE DETECTED")
        print("  Atlas cascade predictions show genuine out-of-sample predictive power.")
        print(f"  {len(both_proven)} event-asset pairs are PROVEN across both periods.")
        overall_verdict = "EDGE_DETECTED"
    elif edge_criteria["val_acc_above_55"] and not edge_criteria["test_acc_above_55"]:
        print("  VERDICT: OVERFIT")
        print("  Validation accuracy was above threshold but test accuracy was not.")
        print("  The impact model is likely overfit to the 2016-2018 period.")
        overall_verdict = "OVERFIT"
    elif passes >= 3:
        print("  VERDICT: WEAK EDGE")
        print(f"  Passed {passes}/{total} criteria. Some signal but not robust enough")
        print("  for standalone trading. Use as a tilt/filter only.")
        overall_verdict = "WEAK_EDGE"
    else:
        print("  VERDICT: NO EDGE")
        print(f"  Passed only {passes}/{total} criteria.")
        print("  Kill standalone Atlas trading. Consider as regime context only.")
        overall_verdict = "NO_EDGE"

    all_results["overall_verdict"] = overall_verdict
    all_results["edge_criteria"] = edge_criteria

    # ──────────────────────────────────────────────────────────────────────
    # Save JSON
    # ──────────────────────────────────────────────────────────────────────

    # Convert any numpy/inf values for JSON serialization
    def sanitize(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, float):
            if math.isinf(obj):
                return 9999.0 if obj > 0 else -9999.0
            if math.isnan(obj):
                return None
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sanitize(v) for v in obj]
        return obj

    results_clean = sanitize(all_results)
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_PATH, "w") as f:
        json.dump(results_clean, f, indent=2)
    print(f"\n  Results saved to {RESULTS_PATH}")


if __name__ == "__main__":
    main()
