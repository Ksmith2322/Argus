from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Any


def utc_ts() -> str:
    """UTC ISO-8601 timestamp used across logs."""
    return datetime.now(timezone.utc).isoformat()


def now_unix() -> int:
    """UTC unix epoch seconds."""
    return int(datetime.now(timezone.utc).timestamp())


def pct_dist(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    """Absolute percent distance |a-b| / b."""
    if a is None or b is None:
        return None
    if b == 0:
        return None
    try:
        return abs(a - b) / b
    except Exception:
        return None


def safe_str(x: Optional[Any]) -> str:
    """Stringify safely for CSV/log output."""
    if x is None:
        return ""
    try:
        return str(x)
    except Exception:
        return ""
