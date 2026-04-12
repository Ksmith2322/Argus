"""
Atlas event database — schema and initialization.

SQLite with WAL mode for concurrent read access from dashboard/runners.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = r"C:\Argus\repo\forge\data\atlas.db"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_category TEXT NOT NULL,
    severity REAL DEFAULT 0.0,
    surprise_factor REAL,
    title TEXT NOT NULL,
    description TEXT,
    source TEXT,
    source_url TEXT,
    metadata TEXT,
    dedupe_hash TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    window_label TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    window_end INTEGER NOT NULL,
    raw_return REAL,
    abnormal_return REAL,
    benchmark_return REAL,
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE(event_id, asset, window_label)
);

CREATE TABLE IF NOT EXISTS cascade_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    prediction_ts TEXT NOT NULL,
    asset TEXT NOT NULL,
    wave INTEGER NOT NULL,
    predicted_direction INTEGER,
    predicted_car REAL,
    confidence REAL,
    deadline TEXT,
    actual_car REAL,
    direction_correct INTEGER,
    FOREIGN KEY (event_id) REFERENCES events(event_id),
    UNIQUE(event_id, asset, wave)
);

CREATE TABLE IF NOT EXISTS scheduled_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    description TEXT,
    importance TEXT DEFAULT 'medium'
);

CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(event_category);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_impacts_event ON impacts(event_id);
CREATE INDEX IF NOT EXISTS idx_impacts_asset ON impacts(asset);
CREATE INDEX IF NOT EXISTS idx_cascade_event ON cascade_predictions(event_id);
CREATE INDEX IF NOT EXISTS idx_scheduled_date ON scheduled_events(event_date);
"""


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Return a connection with WAL mode and row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Create the data directory (if needed), all tables, and return a connection."""
    data_dir = Path(db_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)

    conn = get_connection(db_path)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn
