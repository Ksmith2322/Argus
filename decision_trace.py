"""decision_trace.py — First-class decision causality artifact.

Writes one row per entry-eligible tick to decision_trace.csv, capturing:
- base score, final score, gate transitions
- every blocker that fired
- every modifier that shaped the score
- enough context to answer "why didn't it trade at 3:42 PM?"

This is NOT a log. This is a canonical truth surface for decision forensics.
"""
from __future__ import annotations

import csv
import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional


TRACE_HEADER = [
    "epoch",
    "symbol",
    "decision_hash",
    "action",
    "action_reason",
    # Score pipeline
    "base_score",
    "final_score",
    "base_gate",
    "final_gate",
    # Context
    "regime",
    "session",
    "trend_ok",
    "signal",
    # Modifiers applied (delta from base)
    "adaptive_delta",
    "session_bonus",
    "liq_penalty",
    "ob_adjustment",
    "btc_lag_adjustment",
    "structure_delta",
    "trendline_delta",
    "governor_score_mod",
    # Gate results
    "governor_win_prob",
    "governor_rec",
    "ob_imbalance",
    "liq_atr_norm",
    "liq_spread_bps",
    "liq_mode",
    "liq_ok",
    # Blockers (pipe-separated list of what blocked this tick)
    "blockers",
    # Sizing
    "qty",
    "sizing_note",
    # Runtime
    "runtime_mode",
    "config_hash",
    "config_version",
]

_lock = threading.Lock()


def _compute_decision_hash(row: Dict[str, Any]) -> str:
    """Deterministic hash of decision inputs for dedup/trace linking."""
    key_fields = [
        str(row.get("epoch", "")),
        str(row.get("symbol", "")),
        str(row.get("base_score", "")),
        str(row.get("final_score", "")),
        str(row.get("final_gate", "")),
        str(row.get("blockers", "")),
        str(row.get("action", "")),
    ]
    h = hashlib.sha256("|".join(key_fields).encode()).hexdigest()[:16]
    return h


def build_trace_row(
    *,
    snap: Any,
    cfg: Dict[str, Any],
    base_score: Optional[int],
    base_gate: str,
    blockers: List[str],
    symbol: str = "",
) -> Dict[str, str]:
    """Build a decision trace row from a completed DecisionSnapshot."""
    epoch = getattr(snap, "epoch", "") or ""
    sym = symbol or getattr(snap, "symbol", "") or str(cfg.get("PRODUCT_ID", ""))

    # Score pipeline
    final_score = getattr(snap, "confluence_score", None)
    final_gate = getattr(snap, "confluence_gate", "") or ""

    # Modifiers
    ac_delta = getattr(snap, "ac_delta", 0) or 0
    sess_bonus = getattr(snap, "session_score_bonus", 0) or 0
    liq_pen = getattr(snap, "liq_penalty_points", 0) or 0
    ob_adj = getattr(snap, "ob_score_adjustment", 0) or 0
    btc_lag = getattr(snap, "btc_lag_score_adj", 0) or 0
    struct_delta = getattr(snap, "structure_score_delta", 0) or 0
    tl_delta = getattr(snap, "trendline_score_delta", 0) or 0
    gov_mod = getattr(snap, "governor_score_modifier", 0) or 0

    # Governor
    gov_prob = getattr(snap, "governor_win_prob", None)
    gov_rec = getattr(snap, "governor_recommendation", "") or ""

    # Market context
    ob_imb = getattr(snap, "ob_imbalance", None)
    atr = getattr(snap, "liq_atr_norm", None)
    spread = getattr(snap, "liq_spread_bps", None)
    liq_mode_val = getattr(snap, "liq_mode", "") or ""
    liq_ok_val = getattr(snap, "liq_ok", "") or ""

    # Config hash
    config_hash = ""
    try:
        from ops.run_manifest import _hash_config
        config_hash = _hash_config(cfg)[:12]
    except Exception:
        pass

    row = {
        "epoch": str(epoch),
        "symbol": sym,
        "decision_hash": "",  # computed below
        "action": str(getattr(snap, "action", "") or ""),
        "action_reason": str(getattr(snap, "action_reason", "") or ""),
        "base_score": str(base_score) if base_score is not None else "",
        "final_score": str(final_score) if final_score is not None else "",
        "base_gate": str(base_gate),
        "final_gate": str(final_gate),
        "regime": str(getattr(snap, "regime", "") or ""),
        "session": str(getattr(snap, "session", "") or ""),
        "trend_ok": str(getattr(snap, "trend_ok", "") or ""),
        "signal": str(getattr(snap, "signal", "") or ""),
        "adaptive_delta": str(ac_delta),
        "session_bonus": str(sess_bonus),
        "liq_penalty": str(liq_pen),
        "ob_adjustment": str(ob_adj),
        "btc_lag_adjustment": str(btc_lag),
        "structure_delta": str(struct_delta),
        "trendline_delta": str(tl_delta),
        "governor_score_mod": str(gov_mod),
        "governor_win_prob": f"{gov_prob:.4f}" if gov_prob is not None else "",
        "governor_rec": gov_rec,
        "ob_imbalance": f"{ob_imb:.4f}" if ob_imb is not None else "",
        "liq_atr_norm": f"{atr}" if atr is not None else "",
        "liq_spread_bps": f"{spread}" if spread is not None else "",
        "liq_mode": liq_mode_val,
        "liq_ok": str(liq_ok_val),
        "blockers": "|".join(blockers) if blockers else "",
        "qty": str(getattr(snap, "execution_qty", "") or ""),
        "sizing_note": str(getattr(snap, "sizing_note", "") or ""),
        "runtime_mode": str(cfg.get("_RUNTIME_MODE", "FULL")),
        "config_hash": config_hash,
        "config_version": str(cfg.get("CONFIG_VERSION", "")),
    }
    row["decision_hash"] = _compute_decision_hash(row)
    return row


def append_trace_row(row: Dict[str, str], cfg: Dict[str, Any]) -> None:
    """Append a decision trace row to the canonical trace CSV."""
    log_dir = cfg.get("RUNTIME_LOG_DIR") or cfg.get("OPS_LOG_DIR") or cfg.get("LOG_DIR", "ops/logs")
    trace_path = Path(log_dir) / "decision_trace.csv"

    with _lock:
        write_header = not trace_path.exists() or trace_path.stat().st_size == 0
        with open(trace_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=TRACE_HEADER, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)