"""
Atlas event database — common query functions.

All functions use parameterized queries and return dicts (not tuples).
"""

import hashlib
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [_row_to_dict(r) for r in rows]


def compute_dedupe_hash(title: str) -> str:
    """SHA256 of lowercased, stripped title for deduplication."""
    normalized = title.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def insert_event(conn: sqlite3.Connection, event_dict: dict) -> str:
    """
    Insert an event. Returns the event_id.

    If dedupe_hash already exists, the insert is skipped and the existing
    event_id is returned.

    Required keys: timestamp, detected_at, event_type, event_category, title.
    Optional keys: severity, surprise_factor, description, source, source_url,
                   metadata, event_id, dedupe_hash.
    """
    event_id = event_dict.get("event_id") or str(uuid.uuid4())

    # Auto-compute dedupe_hash if not provided
    dedupe_hash = event_dict.get("dedupe_hash") or compute_dedupe_hash(event_dict["title"])

    # Check for existing event with same dedupe_hash
    existing = conn.execute(
        "SELECT event_id FROM events WHERE dedupe_hash = ?",
        (dedupe_hash,),
    ).fetchone()
    if existing:
        return existing["event_id"]

    conn.execute(
        """
        INSERT INTO events
            (event_id, timestamp, detected_at, event_type, event_category,
             severity, surprise_factor, title, description, source, source_url,
             metadata, dedupe_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_dict["timestamp"],
            event_dict["detected_at"],
            event_dict["event_type"],
            event_dict["event_category"],
            event_dict.get("severity", 0.0),
            event_dict.get("surprise_factor"),
            event_dict["title"],
            event_dict.get("description"),
            event_dict.get("source"),
            event_dict.get("source_url"),
            event_dict.get("metadata"),
            dedupe_hash,
        ),
    )
    conn.commit()
    return event_id


def get_recent_events(conn: sqlite3.Connection, hours: int = 24) -> list[dict]:
    """Return events detected within the last N hours, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT * FROM events WHERE detected_at >= ? ORDER BY detected_at DESC",
        (cutoff,),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_events_by_type(
    conn: sqlite3.Connection, event_type: str, limit: int = 100
) -> list[dict]:
    """Return events matching a given event_type, newest first."""
    rows = conn.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY timestamp DESC LIMIT ?",
        (event_type, limit),
    ).fetchall()
    return _rows_to_dicts(rows)


def get_events_by_category(
    conn: sqlite3.Connection, category: str, limit: int = 100
) -> list[dict]:
    """Return events matching a given event_category, newest first."""
    rows = conn.execute(
        "SELECT * FROM events WHERE event_category = ? ORDER BY timestamp DESC LIMIT ?",
        (category, limit),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Impacts
# ---------------------------------------------------------------------------

def insert_impact(
    conn: sqlite3.Connection,
    event_id: str,
    asset: str,
    window_label: str,
    window_start: int,
    window_end: int,
    raw_return: Optional[float] = None,
    abnormal_return: Optional[float] = None,
    benchmark_return: Optional[float] = None,
) -> None:
    """Insert or replace an impact row."""
    conn.execute(
        """
        INSERT OR REPLACE INTO impacts
            (event_id, asset, window_label, window_start, window_end,
             raw_return, abnormal_return, benchmark_return)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (event_id, asset, window_label, window_start, window_end,
         raw_return, abnormal_return, benchmark_return),
    )
    conn.commit()


def get_impact_matrix(conn: sqlite3.Connection, event_type: str) -> dict:
    """
    Aggregate impact stats for a given event type.

    Returns: {asset: {window_label: {mean, std, n, min, max}}}
    """
    rows = conn.execute(
        """
        SELECT i.asset, i.window_label,
               AVG(i.abnormal_return) AS mean,
               COUNT(*) AS n,
               MIN(i.abnormal_return) AS min_ar,
               MAX(i.abnormal_return) AS max_ar
        FROM impacts i
        JOIN events e ON e.event_id = i.event_id
        WHERE e.event_type = ?
          AND i.abnormal_return IS NOT NULL
        GROUP BY i.asset, i.window_label
        """,
        (event_type,),
    ).fetchall()

    # Also compute std dev in a second pass (SQLite has no built-in stdev)
    std_rows = conn.execute(
        """
        SELECT i.asset, i.window_label,
               AVG(i.abnormal_return) AS mean,
               AVG(i.abnormal_return * i.abnormal_return) AS mean_sq,
               COUNT(*) AS n
        FROM impacts i
        JOIN events e ON e.event_id = i.event_id
        WHERE e.event_type = ?
          AND i.abnormal_return IS NOT NULL
        GROUP BY i.asset, i.window_label
        """,
        (event_type,),
    ).fetchall()

    std_lookup: dict[tuple[str, str], float] = {}
    for r in std_rows:
        n = r["n"]
        if n > 1:
            variance = r["mean_sq"] - r["mean"] ** 2
            # Clamp to zero to handle floating-point rounding
            std_lookup[(r["asset"], r["window_label"])] = max(0.0, variance) ** 0.5
        else:
            std_lookup[(r["asset"], r["window_label"])] = 0.0

    matrix: dict[str, dict] = {}
    for r in rows:
        asset = r["asset"]
        wl = r["window_label"]
        if asset not in matrix:
            matrix[asset] = {}
        matrix[asset][wl] = {
            "mean": r["mean"],
            "std": std_lookup.get((asset, wl), 0.0),
            "n": r["n"],
            "min": r["min_ar"],
            "max": r["max_ar"],
        }
    return matrix


# ---------------------------------------------------------------------------
# Cascade predictions
# ---------------------------------------------------------------------------

def insert_cascade_prediction(
    conn: sqlite3.Connection,
    event_id: str,
    asset: str,
    wave: int,
    direction: Optional[int],
    car: Optional[float],
    confidence: Optional[float],
    deadline: Optional[str],
) -> int:
    """Insert a cascade prediction. Returns the row id."""
    cur = conn.execute(
        """
        INSERT OR REPLACE INTO cascade_predictions
            (event_id, prediction_ts, asset, wave,
             predicted_direction, predicted_car, confidence, deadline)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            datetime.now(timezone.utc).isoformat(),
            asset,
            wave,
            direction,
            car,
            confidence,
            deadline,
        ),
    )
    conn.commit()
    return cur.lastrowid


def score_cascade_prediction(
    conn: sqlite3.Connection,
    prediction_id: int,
    actual_car: float,
) -> None:
    """Fill in actual results for a cascade prediction."""
    row = conn.execute(
        "SELECT predicted_direction FROM cascade_predictions WHERE id = ?",
        (prediction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"No cascade_prediction with id={prediction_id}")

    predicted_dir = row["predicted_direction"]
    if predicted_dir is not None:
        actual_dir = 1 if actual_car >= 0 else -1
        direction_correct = 1 if actual_dir == predicted_dir else 0
    else:
        direction_correct = None

    conn.execute(
        """
        UPDATE cascade_predictions
        SET actual_car = ?, direction_correct = ?
        WHERE id = ?
        """,
        (actual_car, direction_correct, prediction_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Scheduled events
# ---------------------------------------------------------------------------

def insert_scheduled_event(
    conn: sqlite3.Connection,
    date: str,
    event_type: str,
    description: Optional[str] = None,
    importance: str = "medium",
) -> int:
    """Insert a scheduled event. Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO scheduled_events (event_date, event_type, description, importance)
        VALUES (?, ?, ?, ?)
        """,
        (date, event_type, description, importance),
    )
    conn.commit()
    return cur.lastrowid


def get_upcoming_events(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return scheduled events within the next N days, soonest first."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cutoff = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute(
        """
        SELECT * FROM scheduled_events
        WHERE event_date >= ? AND event_date <= ?
        ORDER BY event_date ASC
        """,
        (today, cutoff),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def event_count_by_type(conn: sqlite3.Connection) -> dict:
    """Return {event_type: count} for quick stats."""
    rows = conn.execute(
        "SELECT event_type, COUNT(*) AS cnt FROM events GROUP BY event_type"
    ).fetchall()
    return {r["event_type"]: r["cnt"] for r in rows}
