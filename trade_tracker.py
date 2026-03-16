# trade_tracker.py
"""
Operator-facing hold / missed-buy counter for Argus.

Purpose
-------
This module is intentionally *not* the canonical trade lifecycle record.

It exists only to provide lightweight operator feedback such as:
- why entries were skipped
- why the engine is holding
- high-level counts of non-trade outcomes

Non-goals
---------
Do not use this file as authoritative accounting truth for:
- closed trade lifecycle reconstruction
- realized PnL reconciliation
- entry/exit fill lineage
- trade journal reporting

Those belong in the dedicated execution / reconciliation artifact surface
(e.g. trade_journal_<run>.csv, fills, orders, positions, daily summary).

Design rule
-----------
Keep this module simple, side-effect-light, and non-blocking.
It must never become a mixed responsibility "journal + counters" hybrid.
"""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional


# -------------------------
# Constants
# -------------------------
DEFAULT_KEYS = [
    # --- Missed buys (existing) ---
    "MISSED_BUY_NO_CASH",
    "MISSED_BUY_WATCH_GATED",
    "MISSED_BUY_EXPOSURE_CAP",
    "MISSED_BUY_MIN_ORDER",
    "MISSED_BUY_RISK_LOCKOUT",
    "MISSED_BUY_MAX_TRADES_PER_DAY",
    "MISSED_BUY_DAILY_MAX_LOSS",
    "MISSED_BUY_CONFLUENCE",
    "MISSED_BUY_ENTRY_BLOCKED",

    # --- Holds (new) ---
    "HOLD_WARMUP_NO_1M_STATE",
    "HOLD_PAUSED",
    "HOLD_STALE_DATA",
    "HOLD_COOLDOWN",
    "HOLD_NO_SIGNAL",
    "HOLD_NO_TREND_OK",
    "HOLD_CONFLUENCE",
    "HOLD_ENTRY_SIGNAL_FALSE",
    "HOLD_ENTRY_NO_SIZE",
    "HOLD_IN_POSITION",
    "HOLD_OTHER",
]

MISSED_BUY_PREFIX = "MISSED_BUY_"
HOLD_PREFIX = "HOLD_"

# Austin, TX local time (CST/CDT auto-handled)
LOCAL_TZ = ZoneInfo("America/Chicago")


def local_now_str() -> str:
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


_COUNT_LINE_RE = re.compile(r"^\s*-\s*([A-Z0-9_]+)\s*:\s*([0-9]+)\s*$")
_LAST_UPDATE_RE = re.compile(r"^\s*Last update:\s*(.+?)\s*$")
_LAST_REASON_RE = re.compile(r"^\s*reason:\s*(.*?)\s*$")
_LAST_DETAIL_RE = re.compile(r"^\s*detail:\s*(.*?)\s*$")


def _is_missed_buy_key(k: str) -> bool:
    return (k or "").startswith(MISSED_BUY_PREFIX)


def _is_hold_key(k: str) -> bool:
    return (k or "").startswith(HOLD_PREFIX)


def _sum_keys(counts: Dict[str, int], keys: List[str]) -> int:
    total = 0
    for k in keys:
        total += int(counts.get(k, 0) or 0)
    return total


@dataclass
class TradeTracker:
    """
    Lightweight operator-feedback tracker.

    This class deliberately tracks *reasons* and *counts*, not trade lifecycle truth.
    It should never be treated as the source of record for closed trades, fills, or PnL.
    """

    filepath: str
    counts: Dict[str, int] = field(default_factory=dict)
    last_reason: str = ""
    last_detail: str = ""
    last_update_local: str = ""

    # Optional behavior controls
    enabled: bool = True

    # If True, avoid atomic replace and instead do a best-effort overwrite.
    # Useful on Windows when destination file is locked (Notepad / VSCode / AV).
    # You lose atomicity, but you also avoid crashing the runtime.
    non_atomic_writes: bool = True

    @classmethod
    def from_config(cls, cfg: Dict) -> "TradeTracker":
        fp = cfg.get("TRADE_TRACKER_FILE", "trade_tracker.txt")

        # Support "logs/..." relative paths (recommended) and plain relative paths.
        if not os.path.isabs(fp):
            fp = os.path.join(os.path.dirname(__file__), fp)

        tracker = cls(
            filepath=fp,
            enabled=bool(cfg.get("TRADE_TRACKER_ENABLED", True)),
            non_atomic_writes=bool(cfg.get("TRADE_TRACKER_NON_ATOMIC_WRITES", True)),
        )

        for k in DEFAULT_KEYS:
            tracker.counts.setdefault(k, 0)

        tracker._load_existing_state()

        for k in DEFAULT_KEYS:
            tracker.counts.setdefault(k, 0)

        # Ensure file exists on startup when enabled.
        tracker.write()
        return tracker

    def _load_existing_state(self) -> None:
        if not os.path.exists(self.filepath):
            return

        try:
            in_last_event = False
            with open(self.filepath, "r", encoding="utf-8") as f:
                for line in f:
                    m = _COUNT_LINE_RE.match(line)
                    if m:
                        key = m.group(1).strip()
                        val = int(m.group(2).strip())
                        self.counts[key] = val
                        continue

                    mu = _LAST_UPDATE_RE.match(line)
                    if mu:
                        self.last_update_local = mu.group(1).strip()
                        continue

                    if line.strip().startswith("Last event:"):
                        in_last_event = True
                        continue

                    if in_last_event:
                        mr = _LAST_REASON_RE.search(line)
                        if mr:
                            self.last_reason = mr.group(1).strip().strip('"')
                            continue

                        md = _LAST_DETAIL_RE.search(line)
                        if md:
                            self.last_detail = md.group(1).strip().strip('"')
                            continue
        except Exception:
            # Never crash the runtime because an operator-feedback file is malformed.
            return

    def bump(self, key: str, detail: str = "") -> None:
        """
        Generic counter bump.

        Use bump_hold() / bump_missed_buy() when possible for clearer intent.
        """
        if not self.enabled:
            return

        if not key:
            key = "HOLD_OTHER"

        if key not in self.counts:
            self.counts[key] = 0

        self.counts[key] += 1
        self.last_reason = key
        self.last_detail = detail
        self.last_update_local = local_now_str()

        self.write()

    def bump_hold(self, reason: str, detail: str = "") -> None:
        """
        Bump a HOLD_* reason (adds prefix if missing).
        """
        if not self.enabled:
            return

        if not reason:
            reason = "OTHER"

        key = reason if reason.startswith(HOLD_PREFIX) else f"{HOLD_PREFIX}{reason}"
        if key not in self.counts:
            self.counts[key] = 0

        self.bump(key, detail)

    def bump_missed_buy(self, reason: str, detail: str = "") -> None:
        """
        Bump a MISSED_BUY_* reason (adds prefix if missing).
        """
        if not self.enabled:
            return

        if not reason:
            reason = "RISK_LOCKOUT"

        key = reason if reason.startswith(MISSED_BUY_PREFIX) else f"{MISSED_BUY_PREFIX}{reason}"
        if key not in self.counts:
            self.counts[key] = 0

        self.bump(key, detail)

    def reset(self) -> None:
        """
        Reset all tracked counters.

        This resets operator-feedback counts only.
        It must not be confused with resetting trade/accounting artifacts.
        """
        if not self.enabled:
            return

        self.counts = {k: 0 for k in DEFAULT_KEYS}
        self.last_reason = ""
        self.last_detail = ""
        self.last_update_local = local_now_str()
        self.write()

    def snapshot(self) -> Dict[str, object]:
        """
        Return a lightweight in-memory snapshot for debugging / UI use.

        This snapshot is informational only and is not accounting truth.
        """
        for k in DEFAULT_KEYS:
            self.counts.setdefault(k, 0)

        missed_keys = [k for k in DEFAULT_KEYS if _is_missed_buy_key(k)]
        hold_keys = [k for k in DEFAULT_KEYS if _is_hold_key(k)]

        return {
            "last_update_local": self.last_update_local or local_now_str(),
            "last_reason": self.last_reason,
            "last_detail": self.last_detail,
            "missed_buy_total": _sum_keys(self.counts, missed_keys),
            "hold_total": _sum_keys(self.counts, hold_keys),
            "counts": dict(self.counts),
        }

    def _build_lines(self) -> List[str]:
        for k in DEFAULT_KEYS:
            self.counts.setdefault(k, 0)

        missed_keys = [k for k in DEFAULT_KEYS if _is_missed_buy_key(k)]
        hold_keys = [k for k in DEFAULT_KEYS if _is_hold_key(k)]

        missed_total = _sum_keys(self.counts, missed_keys)
        hold_total = _sum_keys(self.counts, hold_keys)

        lines: List[str] = []
        lines.append("=== ARGUS TRADE TRACKER (OPERATOR FEEDBACK ONLY) ===")
        lines.append("NOTE: This file is not authoritative trade lifecycle or PnL truth.")
        lines.append(f"Last update: {self.last_update_local or local_now_str()}")
        lines.append("")
        lines.append("Totals:")
        lines.append(f"  - MISSED_BUY total: {missed_total}")
        lines.append(f"  - HOLD total:       {hold_total}")

        lines.append("")
        lines.append("Missed buy counters:")
        for k in missed_keys:
            lines.append(f"  - {k}: {self.counts.get(k, 0)}")

        lines.append("")
        lines.append("Hold reason counters:")
        for k in hold_keys:
            lines.append(f"  - {k}: {self.counts.get(k, 0)}")

        extra = sorted(k for k in self.counts.keys() if k not in DEFAULT_KEYS)
        if extra:
            lines.append("")
            lines.append("Other counters:")
            for k in extra:
                lines.append(f"  - {k}: {self.counts.get(k, 0)}")

        lines.append("")
        lines.append("Last event:")
        lines.append(f"  reason: {self.last_reason}")
        if self.last_detail:
            lines.append(f"  detail: {self.last_detail}")

        return lines

    def _atomic_replace_write(self, text: str) -> Optional[Exception]:
        """
        Atomic write via tmp file then os.replace().

        On Windows, os.replace() can raise PermissionError if destination is open/locked.
        """
        try:
            tmp = self.filepath + ".tmp"
            parent = os.path.dirname(self.filepath)
            if parent:
                os.makedirs(parent, exist_ok=True)

            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)

            os.replace(tmp, self.filepath)
            return None
        except Exception as e:
            return e

    def _non_atomic_write(self, text: str) -> Optional[Exception]:
        """
        Best-effort overwrite. Not atomic, but avoids crashing when file is locked.
        """
        try:
            parent = os.path.dirname(self.filepath)
            if parent:
                os.makedirs(parent, exist_ok=True)

            with open(self.filepath, "w", encoding="utf-8") as f:
                f.write(text)

            return None
        except Exception as e:
            return e

    def write(self) -> None:
        if not self.enabled:
            return

        text = "\n".join(self._build_lines()) + "\n"

        err = self._atomic_replace_write(text)
        if err is None:
            return

        if self.non_atomic_writes:
            _ = self._non_atomic_write(text)
            return

        # Strict mode still must not crash runtime.
        return