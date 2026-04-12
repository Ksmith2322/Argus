"""Themis — Congressional trading tracker database layer."""

import re
import sqlite3
from pathlib import Path

DB_PATH = r"C:\Argus\repo\forge\data\themis.db"


def init_db(path: str = DB_PATH) -> None:
    """Create tables if they don't exist, enable WAL mode."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            representative TEXT NOT NULL,
            bioguide_id TEXT,
            ticker TEXT NOT NULL,
            transaction_type TEXT NOT NULL,
            transaction_date TEXT NOT NULL,
            disclosure_date TEXT NOT NULL,
            amount_range TEXT,
            amount_low REAL,
            amount_high REAL,
            chamber TEXT,
            party TEXT,
            description TEXT,
            excess_return REAL,
            price_change REAL,
            spy_change REAL,
            fetched_at TEXT NOT NULL,
            UNIQUE(representative, ticker, transaction_date, transaction_type)
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_type TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            members TEXT NOT NULL,
            member_count INTEGER,
            avg_amount_low REAL,
            description TEXT,
            status TEXT DEFAULT 'active',
            entry_price REAL,
            return_30d REAL,
            return_60d REAL,
            return_90d REAL,
            spy_return_30d REAL,
            spy_return_60d REAL,
            spy_return_90d REAL,
            alpha_30d REAL,
            alpha_60d REAL,
            alpha_90d REAL,
            score_date_30d TEXT,
            score_date_60d TEXT,
            score_date_90d TEXT
        );

        CREATE TABLE IF NOT EXISTS tracked_returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            ticker TEXT NOT NULL,
            transaction_date TEXT NOT NULL,
            transaction_type TEXT NOT NULL,
            check_date TEXT NOT NULL,
            days_after INTEGER NOT NULL,
            stock_return REAL,
            spy_return REAL,
            alpha REAL,
            UNIQUE(trade_id, days_after)
        );
    """)
    conn.close()


def get_connection(path: str = DB_PATH) -> sqlite3.Connection:
    """Return a connection with row_factory set to sqlite3.Row."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def parse_amount_range(range_str: str | None) -> tuple[float, float]:
    """Parse '$1,001 - $15,000' into (1001.0, 15000.0). Returns (0,0) on failure."""
    if not range_str:
        return (0.0, 0.0)
    nums = re.findall(r"[\d,]+", range_str.replace("$", ""))
    if len(nums) >= 2:
        return (float(nums[0].replace(",", "")), float(nums[1].replace(",", "")))
    if len(nums) == 1:
        v = float(nums[0].replace(",", ""))
        return (v, v)
    return (0.0, 0.0)


def insert_trade(conn: sqlite3.Connection, trade: dict) -> int | None:
    """Insert a trade, return row id or None if duplicate."""
    try:
        cur = conn.execute("""
            INSERT INTO trades (
                representative, bioguide_id, ticker, transaction_type,
                transaction_date, disclosure_date, amount_range,
                amount_low, amount_high, chamber, party, description,
                excess_return, price_change, spy_change, fetched_at
            ) VALUES (
                :representative, :bioguide_id, :ticker, :transaction_type,
                :transaction_date, :disclosure_date, :amount_range,
                :amount_low, :amount_high, :chamber, :party, :description,
                :excess_return, :price_change, :spy_change, :fetched_at
            )
        """, trade)
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def get_recent_trades(conn: sqlite3.Connection, days: int = 30) -> list[dict]:
    """Return trades from the last N days."""
    rows = conn.execute(
        "SELECT * FROM trades WHERE transaction_date >= date('now', ?)",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def get_trades_by_ticker(conn: sqlite3.Connection, ticker: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE ticker = ? ORDER BY transaction_date DESC", (ticker,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_trades_by_member(conn: sqlite3.Connection, member: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM trades WHERE representative = ? ORDER BY transaction_date DESC",
        (member,),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_signal(conn: sqlite3.Connection, signal: dict) -> int:
    """Insert a signal, return row id."""
    cur = conn.execute("""
        INSERT INTO signals (
            signal_type, detected_at, ticker, members, member_count,
            avg_amount_low, description, status, entry_price
        ) VALUES (
            :signal_type, :detected_at, :ticker, :members, :member_count,
            :avg_amount_low, :description, :status, :entry_price
        )
    """, signal)
    conn.commit()
    return cur.lastrowid


def get_active_signals(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM signals WHERE status = 'active' ORDER BY detected_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def update_signal_returns(
    conn: sqlite3.Connection, signal_id: int, days: int,
    stock_return: float, spy_return: float
) -> None:
    """Update return columns for a given horizon (30, 60, or 90)."""
    from datetime import datetime
    alpha = stock_return - spy_return
    today = datetime.utcnow().strftime("%Y-%m-%d")
    conn.execute(f"""
        UPDATE signals
        SET return_{days}d = ?, spy_return_{days}d = ?,
            alpha_{days}d = ?, score_date_{days}d = ?
        WHERE id = ?
    """, (stock_return, spy_return, alpha, today, signal_id))
    conn.commit()


def get_signal_scorecard(conn: sqlite3.Connection) -> dict:
    """Return aggregate signal performance stats."""
    signals = conn.execute("SELECT * FROM signals").fetchall()
    signals = [dict(s) for s in signals]

    total = len(signals)
    scored_30 = [s for s in signals if s.get("alpha_30d") is not None]
    scored_60 = [s for s in signals if s.get("alpha_60d") is not None]
    scored_90 = [s for s in signals if s.get("alpha_90d") is not None]

    def avg(lst: list[float]) -> float | None:
        return sum(lst) / len(lst) if lst else None

    by_type: dict[str, list[dict]] = {}
    for s in signals:
        by_type.setdefault(s["signal_type"], []).append(s)

    type_stats = {}
    for stype, sigs in by_type.items():
        a30 = [s["alpha_30d"] for s in sigs if s.get("alpha_30d") is not None]
        type_stats[stype] = {
            "count": len(sigs),
            "scored_30d": len(a30),
            "avg_alpha_30d": avg(a30),
        }

    return {
        "total_signals": total,
        "scored_30d": len(scored_30),
        "scored_60d": len(scored_60),
        "scored_90d": len(scored_90),
        "avg_alpha_30d": avg([s["alpha_30d"] for s in scored_30]),
        "avg_alpha_60d": avg([s["alpha_60d"] for s in scored_60]),
        "avg_alpha_90d": avg([s["alpha_90d"] for s in scored_90]),
        "by_type": type_stats,
    }
