"""Canonical schema definitions for signal and trade CSV artifacts.

Single source of truth for column order and required fields.
Used by runner_unified.py (writer) and daily_report.py (reader).

Rules:
- No file I/O, no runtime logic, no imports beyond stdlib
- Changes here reset the cohort (new headers = new artifact shape)
"""

# Schema version — bump when columns change
SCHEMA_VERSION = 4

# ── Signal CSV ─────────────────────────────────────────────────

SIGNAL_FIELDS_FX = [
    "ts", "price", "range_pct", "vol_z", "range_accel",
    "dist_from_low", "hour", "direction", "action",
    "config_hash", "session_id",
    "regime", "trend_strength", "efficiency_ratio",
]

SIGNAL_FIELDS_FUTURES = [
    "ts", "price", "range_pct", "vol_z", "range_accel",
    "vol_burst_z", "dist_from_low", "hour", "direction", "action",
    "config_hash", "session_id",
    "regime", "trend_strength", "efficiency_ratio",
]


def signal_header(instrument_type: str) -> list[str]:
    """Return the correct signal CSV header for an instrument type."""
    if instrument_type in ("future", "crypto"):
        return list(SIGNAL_FIELDS_FUTURES)
    return list(SIGNAL_FIELDS_FX)


def build_signal_row(features: dict, direction: str | None, action: str,
                     instrument_type: str, config_hash: str, session_id: str) -> list:
    """Build a signal CSV row matching signal_header()."""
    row = [
        features.get("ts", ""),
        features["price"],
        f"{features['range_pct']:.6f}",
        f"{features['vol_z']:.4f}",
        f"{features['range_accel']:.4f}",
    ]
    if instrument_type in ("future", "crypto"):
        row.append(f"{features.get('vol_burst_z', 0):.4f}")
    row += [
        f"{features['dist_from_low']:.4f}",
        features["hour"],
        direction or "",
        action,
        config_hash,
        session_id,
        features.get("regime", ""),
        f"{features.get('trend_strength', 0):.4f}",
        f"{features.get('efficiency_ratio', 0):.4f}",
    ]
    return row


# ── Trade CSV ──────────────────────────────────────────────────

VALIDITY_FIELDS = [
    "experiment_valid", "invalid_reason",
    "config_hash", "session_id", "runtime_epoch", "git_sha",
]

# Entry-feature + execution quality fields (v4)
ENTRY_FEATURE_FIELDS = [
    "mtf_score", "mtf_alignment", "session_score", "session_label",
    "spread_ratio", "entry_spread", "entry_bid", "entry_ask",
    "conviction_score", "bias_4h",
    "signal_mid", "fill_latency_ms", "slippage_pips",
]

TRADE_FIELDS_PIPS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pips",
    "exit_reason", "duration_min", "trade_num",
    "pnl_usd", "position_size", "risk_usd", "sizing_policy",
    "entry_regime",
] + VALIDITY_FIELDS + ENTRY_FEATURE_FIELDS

TRADE_FIELDS_POINTS = [
    "ts", "direction", "entry_px", "exit_px", "pnl_pts",
    "pnl_usd", "exit_reason", "duration_min", "trade_num",
    "position_size", "risk_usd", "sizing_policy",
    "entry_regime",
] + VALIDITY_FIELDS + ENTRY_FEATURE_FIELDS


def trade_header(uses_pips: bool) -> list[str]:
    """Return the correct trade CSV header."""
    if uses_pips:
        return list(TRADE_FIELDS_PIPS)
    return list(TRADE_FIELDS_POINTS)
