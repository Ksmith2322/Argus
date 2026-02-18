# session.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# -------------------------
# Data model
# -------------------------

@dataclass(frozen=True)
class SessionInfo:
    """
    Session classifier output.

    Must provide:
      - session (string) + name (canonical)
      - labels (list[str])
      - bonus_points (int)  <-- engine alias expectation
      - risk_mult (float)
      - reason (string)

    Canonical fields:
      name, is_overlap, reason, risk_mult, score_bonus

    Engine compatibility:
      - sess.session
      - sess.labels
      - sess.bonus_points
    """
    name: str                 # "ASIA", "LONDON", "NY", "OVERLAP", "OFF"
    is_overlap: bool
    reason: str
    risk_mult: float
    score_bonus: int

    @property
    def session(self) -> str:
        return self.name

    # --------- LINE ABOVE: def session(self) -> str:
    @property
    def bonus_points(self) -> int:
        return int(self.score_bonus or 0)

    @property
    def labels(self) -> List[str]:
        out: List[str] = [f"session:{self.name}"]
        if self.is_overlap:
            out.append("overlap:1")

        b = int(self.score_bonus or 0)
        if b != 0:
            out.append(f"score_bonus:{b}")

        rm = float(self.risk_mult or 1.0)
        if abs(rm - 1.0) > 1e-9:
            out.append(f"risk_mult:{rm:.3f}")

        if self.reason:
            out.append(f"reason:{self.reason}")

        return out


# -------------------------
# Internal helpers
# -------------------------

def _utc_dt_from_epoch(epoch_s: int) -> datetime:
    return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc)


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x)))
        except Exception:
            return default


def _as_float(x: Any, default: float = 1.0) -> float:
    try:
        return float(x)
    except Exception:
        try:
            return float(str(x))
        except Exception:
            return float(default)


def _clamp_i(x: int, lo: int = 0, hi: int = 100) -> int:
    try:
        x = int(x)
    except Exception:
        x = lo
    return max(lo, min(hi, x))


def _clamp_f(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    try:
        x = float(x)
    except Exception:
        x = 1.0
    return max(lo, min(hi, x))


def _session_from_utc_hour(
    h: int,
    *,
    asia_start: int,
    asia_end: int,
    london_start: int,
    london_end: int,
    ny_start: int,
    ny_end: int,
    overlap_start: int,
    overlap_end: int,
) -> Tuple[str, bool, str]:
    """
    Returns: (name, is_overlap, reason)
    All windows are inclusive bounds in UTC hours.
    """
    if overlap_start <= h <= overlap_end:
        return "OVERLAP", True, f"UTC_hour={h} overlap"
    if asia_start <= h <= asia_end:
        return "ASIA", False, f"UTC_hour={h}"
    if london_start <= h <= london_end:
        return "LONDON", False, f"UTC_hour={h}"
    if ny_start <= h <= ny_end:
        return "NY", False, f"UTC_hour={h}"
    return "OFF", False, f"UTC_hour={h}"


# -------------------------
# Public API
# -------------------------

def classify_session(epoch_s: int, cfg: Dict[str, Any]) -> SessionInfo:
    """
    UTC-based session classifier.

    Must return:
      - session/name
      - labels (property)
      - bonus_points (property)
      - risk_mult
      - reason

    Defaults (UTC):
      - ASIA:      00:00–07:59  -> hours 0..7
      - LONDON:    08:00–12:59  -> hours 8..12
      - NY:        13:00–20:59  -> hours 13..20
      - OFF:       21:00–23:59  -> hours 21..23
      - OVERLAP:   13:00–15:59  -> hours 13..15 (wins over NY)
    """
    dt = _utc_dt_from_epoch(int(epoch_s))
    h = int(dt.hour)

    # Allow config overrides for session windows (hours, inclusive)
    asia_start = _clamp_i(_as_int(cfg.get("SESSION_ASIA_START_UTC", 0), 0), 0, 23)
    asia_end = _clamp_i(_as_int(cfg.get("SESSION_ASIA_END_UTC", 7), 7), 0, 23)

    london_start = _clamp_i(_as_int(cfg.get("SESSION_LONDON_START_UTC", 8), 8), 0, 23)
    london_end = _clamp_i(_as_int(cfg.get("SESSION_LONDON_END_UTC", 12), 12), 0, 23)

    ny_start = _clamp_i(_as_int(cfg.get("SESSION_NY_START_UTC", 13), 13), 0, 23)
    ny_end = _clamp_i(_as_int(cfg.get("SESSION_NY_END_UTC", 20), 20), 0, 23)

    overlap_start = _clamp_i(_as_int(cfg.get("SESSION_OVERLAP_START_UTC", 13), 13), 0, 23)
    overlap_end = _clamp_i(_as_int(cfg.get("SESSION_OVERLAP_END_UTC", 15), 15), 0, 23)

    name, is_overlap, base_reason = _session_from_utc_hour(
        h,
        asia_start=asia_start,
        asia_end=asia_end,
        london_start=london_start,
        london_end=london_end,
        ny_start=ny_start,
        ny_end=ny_end,
        overlap_start=overlap_start,
        overlap_end=overlap_end,
    )

    use = _as_bool(cfg.get("USE_SESSION_MODIFIERS", "false"), False)

    # Score bonuses (points)
    overlap_bonus = _clamp_i(_as_int(cfg.get("SESSION_OVERLAP_BONUS", 2), 2), -100, 100)
    asia_bonus = _clamp_i(_as_int(cfg.get("SESSION_ASIA_BONUS", 0), 0), -100, 100)
    london_bonus = _clamp_i(_as_int(cfg.get("SESSION_LONDON_BONUS", 0), 0), -100, 100)
    ny_bonus = _clamp_i(_as_int(cfg.get("SESSION_NY_BONUS", 0), 0), -100, 100)
    off_bonus = _clamp_i(_as_int(cfg.get("SESSION_OFF_BONUS", 0), 0), -100, 100)

    # Risk multipliers (float)
    asia_rm = _clamp_f(_as_float(cfg.get("SESSION_ASIA_RISK_MULT", 1.0), 1.0), 0.0, 10.0)
    london_rm = _clamp_f(_as_float(cfg.get("SESSION_LONDON_RISK_MULT", 1.0), 1.0), 0.0, 10.0)
    ny_rm = _clamp_f(_as_float(cfg.get("SESSION_NY_RISK_MULT", 1.0), 1.0), 0.0, 10.0)
    overlap_rm = _clamp_f(_as_float(cfg.get("SESSION_OVERLAP_RISK_MULT", 1.0), 1.0), 0.0, 10.0)
    off_rm = _clamp_f(_as_float(cfg.get("SESSION_OFF_RISK_MULT", 1.0), 1.0), 0.0, 10.0)

    if not use:
        # Still return the detected session, but neutral modifiers.
        return SessionInfo(
            name=name,
            is_overlap=is_overlap,
            reason=f"{base_reason} use=0",
            risk_mult=1.0,
            score_bonus=0,
        )

    if name == "OVERLAP":
        return SessionInfo(
            name="OVERLAP",
            is_overlap=True,
            reason=base_reason,
            risk_mult=overlap_rm,
            score_bonus=overlap_bonus,
        )
    if name == "ASIA":
        return SessionInfo(
            name="ASIA",
            is_overlap=False,
            reason=base_reason,
            risk_mult=asia_rm,
            score_bonus=asia_bonus,
        )
    if name == "LONDON":
        return SessionInfo(
            name="LONDON",
            is_overlap=False,
            reason=base_reason,
            risk_mult=london_rm,
            score_bonus=london_bonus,
        )
    if name == "NY":
        return SessionInfo(
            name="NY",
            is_overlap=False,
            reason=base_reason,
            risk_mult=ny_rm,
            score_bonus=ny_bonus,
        )

    return SessionInfo(
        name="OFF",
        is_overlap=False,
        reason=base_reason,
        risk_mult=off_rm,
        score_bonus=off_bonus,
    )


def apply_session_to_score(
    score: Optional[int],
    session_info: Optional[SessionInfo],
    cfg: Dict[str, Any],
) -> Optional[int]:
    """
    Apply session bonus to a score with:
      - None-safe behavior (returns None if score is None)
      - clamping to [0, 100]
      - respects USE_SESSION_MODIFIERS

    This is the *canonical* function your engine should call.

    Note:
      - This function only changes score (not gate).
      - Session risk_mult is for sizing/risk layer, not score.
    """
    if score is None:
        return None

    use = _as_bool(cfg.get("USE_SESSION_MODIFIERS", "false"), False)
    if (not use) or (session_info is None):
        return _clamp_i(int(score), 0, 100)

    bonus = int(getattr(session_info, "bonus_points", 0) or 0)
    return _clamp_i(int(score) + int(bonus), 0, 100)


# -------------------------
# Compatibility wrapper (optional)
# -------------------------

def apply_session_to_score_compat(
    *,
    score: Optional[int],
    gate: Optional[str] = None,
    reasons: str = "",
    session: Optional[SessionInfo] = None,
    session_info: Optional[SessionInfo] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Supports BOTH older tuple style and newer score-only engine style.

    Old style (returns tuple):
        new_score, new_gate, new_reasons = apply_session_to_score_compat(
            score=score, gate=gate, reasons=reasons, session=sess, cfg=cfg
        )

    New style (returns score only):
        eff_score = apply_session_to_score_compat(score=eff_score, session_info=sess, cfg=cfg)

    Prefer calling apply_session_to_score(score, session_info, cfg) directly in new code.
    """
    sess = session_info or session
    cfg = cfg or {}

    # Score-only path (new engine call)
    if session_info is not None or (gate is None and reasons == ""):
        return apply_session_to_score(score, sess, cfg)

    # Tuple path (legacy)
    new_score = apply_session_to_score(score, sess, cfg)

    tag = f"session={getattr(sess, 'session', 'NA')}"
    if sess is not None:
        b = int(getattr(sess, "bonus_points", 0) or 0)
        if _as_bool(cfg.get("USE_SESSION_MODIFIERS", "false"), False) and b:
            tag += f" bonus=+{b}"
        rm = float(getattr(sess, "risk_mult", 1.0) or 1.0)
        if abs(rm - 1.0) > 1e-9:
            tag += f" risk_mult={rm:.3f}"

    new_reasons = (reasons + " | " if reasons else "") + tag
    return new_score, (gate or ""), new_reasons
