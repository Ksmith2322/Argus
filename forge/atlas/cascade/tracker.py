"""
Atlas Cascade Tracker
=====================
Tracks cascade predictions vs actual outcomes.
When an event is detected and a cascade forecast is generated,
the tracker records the predictions and scores them after the deadline.

This is Atlas's accountability layer — if predictions don't track,
the cascade templates are wrong and need updating.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    yf = None
    pd = None


def create_predictions(event_id: str, event_type: str, severity: float,
                       event_date: str, conn) -> int:
    """
    Create cascade predictions for a detected event and store them in the DB.

    Returns the number of predictions created.
    """
    from forge.atlas.cascade.templates import get_cascade_template
    from forge.atlas.db.queries import insert_cascade_prediction

    template = get_cascade_template(event_type)
    if not template:
        return 0

    count = 0
    for entry in template:
        # Scale expected CAR by severity
        expected_car = entry.get("expected_car_5d", 0) * severity
        direction = entry.get("direction", 0)
        wave = entry.get("wave", 1)
        lag = entry.get("lag_days", 0)
        confidence = {"high": 0.8, "medium": 0.5, "low": 0.3}.get(
            entry.get("confidence", "low"), 0.3
        )

        # Deadline = event_date + lag_days + 5 trading days (for measurement)
        try:
            dt = datetime.strptime(event_date[:10], "%Y-%m-%d")
            deadline = dt + timedelta(days=lag + 7)  # +7 calendar days ≈ 5 trading days
            deadline_str = deadline.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            deadline_str = ""

        try:
            insert_cascade_prediction(
                conn,
                event_id=event_id,
                asset=entry["asset"],
                wave=wave,
                direction=direction,
                car=expected_car,
                confidence=confidence,
                deadline=deadline_str,
            )
            count += 1
        except Exception as e:
            log.debug("Prediction insert failed (likely duplicate): %s", e)

    return count


def score_due_predictions(conn) -> dict:
    """
    Find predictions past their deadline, fetch actual returns, and score them.

    Returns summary: {scored: int, correct: int, total_checked: int}
    """
    if yf is None or pd is None:
        log.warning("yfinance/pandas not available — cannot score predictions")
        return {"scored": 0, "correct": 0, "total_checked": 0}

    from forge.atlas.db.queries import score_cascade_prediction

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Find unscored predictions past deadline
    cursor = conn.execute("""
        SELECT cp.id, cp.event_id, cp.asset, cp.wave, cp.predicted_direction,
               cp.predicted_car, cp.deadline, e.timestamp as event_ts
        FROM cascade_predictions cp
        JOIN events e ON cp.event_id = e.event_id
        WHERE cp.actual_car IS NULL
          AND cp.deadline IS NOT NULL
          AND cp.deadline < ?
        LIMIT 50
    """, (today,))

    rows = cursor.fetchall()
    if not rows:
        return {"scored": 0, "correct": 0, "total_checked": 0}

    # Group by asset to minimize yfinance downloads
    assets_needed = set()
    for row in rows:
        assets_needed.add(row[2])  # asset column

    # Download price data for needed assets
    price_cache = {}
    for asset in assets_needed:
        try:
            df = yf.download(asset, period="60d", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.index = pd.to_datetime(df.index).tz_localize(None)
            price_cache[asset] = df["Close"]
        except Exception as e:
            log.warning("Failed to download %s for scoring: %s", asset, e)

    scored = 0
    correct = 0

    for row in rows:
        pred_id = row[0]
        asset = row[2]
        wave = row[3]
        pred_direction = row[4]
        event_ts = row[7]

        if asset not in price_cache:
            continue

        prices = price_cache[asset]

        # Find the event date in the price series
        try:
            event_date = datetime.fromisoformat(event_ts).replace(tzinfo=None)
            event_ts_pd = pd.Timestamp(event_date.date())
        except (ValueError, TypeError):
            continue

        # Find nearest trading day on or after event
        future_dates = prices.index[prices.index >= event_ts_pd]
        if len(future_dates) < 6:  # need at least 5 trading days after
            continue

        # Compute CAR based on wave
        if wave == 1:
            # CAR(0,1) — day of + next day
            start_idx = 0
            end_idx = min(2, len(future_dates) - 1)
        elif wave == 2:
            # CAR(1,5)
            start_idx = 1
            end_idx = min(5, len(future_dates) - 1)
        else:
            # CAR(1,20) for wave 3+
            start_idx = 1
            end_idx = min(20, len(future_dates) - 1)

        if end_idx <= start_idx:
            continue

        start_price = float(prices.iloc[prices.index.get_indexer([future_dates[start_idx]], method="nearest")[0]])
        end_price = float(prices.iloc[prices.index.get_indexer([future_dates[end_idx]], method="nearest")[0]])

        actual_car = (end_price - start_price) / start_price

        try:
            score_cascade_prediction(conn, pred_id, actual_car)
            scored += 1
            if (pred_direction > 0 and actual_car > 0) or (pred_direction < 0 and actual_car < 0):
                correct += 1
        except Exception as e:
            log.warning("Failed to score prediction %d: %s", pred_id, e)

    return {"scored": scored, "correct": correct, "total_checked": len(rows)}


def get_scorecard(conn) -> dict:
    """
    Get Atlas's overall prediction accuracy scorecard.

    Returns dict with hit rates by event type and overall.
    """
    cursor = conn.execute("""
        SELECT e.event_type,
               COUNT(*) as total,
               SUM(CASE WHEN cp.direction_correct = 1 THEN 1 ELSE 0 END) as correct,
               AVG(cp.predicted_car) as avg_predicted,
               AVG(cp.actual_car) as avg_actual
        FROM cascade_predictions cp
        JOIN events e ON cp.event_id = e.event_id
        WHERE cp.actual_car IS NOT NULL
        GROUP BY e.event_type
        ORDER BY total DESC
    """)

    rows = cursor.fetchall()
    scorecard = {
        "by_type": {},
        "overall_total": 0,
        "overall_correct": 0,
        "overall_hit_rate": 0.0,
    }

    for row in rows:
        etype = row[0]
        total = row[1]
        correct = row[2]
        avg_pred = row[3]
        avg_actual = row[4]
        hit_rate = correct / total if total > 0 else 0

        scorecard["by_type"][etype] = {
            "total": total,
            "correct": correct,
            "hit_rate": round(hit_rate, 3),
            "avg_predicted_car": round(avg_pred, 5) if avg_pred else 0,
            "avg_actual_car": round(avg_actual, 5) if avg_actual else 0,
        }
        scorecard["overall_total"] += total
        scorecard["overall_correct"] += correct

    if scorecard["overall_total"] > 0:
        scorecard["overall_hit_rate"] = round(
            scorecard["overall_correct"] / scorecard["overall_total"], 3
        )

    return scorecard


def print_scorecard(conn):
    """Print a formatted scorecard to stdout."""
    sc = get_scorecard(conn)

    if sc["overall_total"] == 0:
        print("No scored predictions yet.")
        return

    print(f"\n{'='*70}")
    print(f"ATLAS CASCADE PREDICTION SCORECARD")
    print(f"{'='*70}")
    print(f"Overall: {sc['overall_correct']}/{sc['overall_total']} correct "
          f"({sc['overall_hit_rate']*100:.1f}% hit rate)")
    print()

    print(f"{'Event Type':<28} {'Total':>6} {'Correct':>8} {'Hit%':>6} {'AvgPred':>9} {'AvgActual':>10}")
    print("-" * 70)
    for etype, stats in sorted(sc["by_type"].items(), key=lambda x: -x[1]["total"]):
        print(f"{etype:<28} {stats['total']:>6} {stats['correct']:>8} "
              f"{stats['hit_rate']*100:>5.1f}% {stats['avg_predicted_car']:>+8.4f} "
              f"{stats['avg_actual_car']:>+9.4f}")

    print(f"\nThreshold: >55% hit rate = edge exists, >60% on high-confidence = tradeable")
