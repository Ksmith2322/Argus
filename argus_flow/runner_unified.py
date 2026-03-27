"""Unified Multi-Instrument Paper Trading Runner via IBKR TWS.

Single process, single IB connection, manages ALL instruments simultaneously.
Loads every config from argus_flow/configs/*.json, creates the appropriate
contract (Forex / Future / Crypto-as-CFD), subscribes to market data, and
runs each strategy in lock-step inside one event loop.

Usage:
    python -m argus_flow.runner_unified
    python -m argus_flow.runner_unified --configs argus_flow/configs/eurusd_t4_paper_v1.json argus_flow/configs/mnq_vol_burst_paper_v1.json
    python -m argus_flow.runner_unified --exclude eth_range btc_range
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import logging
import os
import uuid
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from dotenv import load_dotenv

from argus_flow.schemas import signal_header, build_signal_row, trade_header

load_dotenv()

try:
    from ib_insync import IB, Forex, Future, util
except ImportError:
    print("ERROR: pip install ib_insync")
    sys.exit(1)

# ── Logging ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("unified")

# ── Global connection settings from .env ─────────────────────
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "7496"))
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))

CONFIGS_DIR = Path("argus_flow/configs")
LOGS_ROOT = Path("argus_flow/logs")


# ═════════════════════════════════════════════════════════════
# BarBuffer — rolling 1-minute bar window
# ═════════════════════════════════════════════════════════════
class BarBuffer:
    """Rolling buffer of 1-minute OHLCV bars."""

    def __init__(self, maxlen: int = 300):
        self.maxlen = maxlen
        self.bars: list[dict] = []

    def add(self, bar: dict) -> None:
        self.bars.append(bar)
        if len(self.bars) > self.maxlen:
            self.bars = self.bars[-self.maxlen :]

    def to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.bars) if self.bars else pd.DataFrame()

    def __len__(self) -> int:
        return len(self.bars)


# ═════════════════════════════════════════════════════════════
# State — per-instrument position persistence
# ═════════════════════════════════════════════════════════════
class State:
    """Tracks current position and trade lifecycle for one instrument."""

    def __init__(self, state_file: Path):
        self.file = state_file
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.position = "FLAT"  # FLAT | LONG | SHORT
        self.entry_price = 0.0
        self.entry_time: Optional[datetime] = None
        self.stop_price = 0.0
        self.target_price = 0.0
        self.timeout_time: Optional[datetime] = None
        self.last_signal_time: Optional[datetime] = None
        self.trade_count = 0
        self.pnl_pips = 0.0    # FX
        self.pnl_points = 0.0  # Futures / bps-based
        self.direction_str = ""  # "long" or "short" for logging
        self.restored_this_session = False  # True if loaded from file
        self.had_zero_stops = False  # True if stop/target were 0 at any point
        self.pyramid_adds = 0        # Number of scale-in adds this trade
        self.avg_entry_price = 0.0   # Volume-weighted average entry price

    def save(self) -> None:
        """Atomic state write: write to temp file, flush, then rename."""
        data = json.dumps({
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "timeout_time": self.timeout_time.isoformat() if self.timeout_time else None,
            "trade_count": self.trade_count,
            "pnl_pips": self.pnl_pips,
            "pnl_points": self.pnl_points,
            "pyramid_adds": self.pyramid_adds,
            "avg_entry_price": self.avg_entry_price,
        }, indent=2)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(self.file)  # atomic on same filesystem

    def load(self, sym_label: str) -> None:
        if not self.file.exists():
            return
        try:
            d = json.loads(self.file.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning(f"[{sym_label}] Corrupt state file — starting FLAT")
            return
        self.position = d.get("position", "FLAT")
        self.entry_price = d.get("entry_price", 0.0)
        self.stop_price = d.get("stop_price", 0.0)
        self.target_price = d.get("target_price", 0.0)
        self.trade_count = d.get("trade_count", 0)
        self.pnl_pips = d.get("pnl_pips", 0.0)
        self.pnl_points = d.get("pnl_points", 0.0)
        self.pyramid_adds = d.get("pyramid_adds", 0)
        self.avg_entry_price = d.get("avg_entry_price", 0.0)
        if d.get("entry_time"):
            try:
                self.entry_time = datetime.fromisoformat(d["entry_time"])
            except (ValueError, TypeError):
                pass
        if d.get("timeout_time"):
            try:
                self.timeout_time = datetime.fromisoformat(d["timeout_time"])
            except (ValueError, TypeError):
                pass
        # Safety: if restored in-position but stops are zero, force FLAT locally.
        # WARNING: This is a LOCAL-ONLY override. If broker has a real position,
        # this creates a phantom mismatch that reconciliation MUST catch.
        # The runner will log this as had_zero_stops=True for trade validity.
        if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
            log.warning(
                f"[{sym_label}] Restored {self.position} but stop/target=0 — forcing FLAT (LOCAL ONLY). "
                f"If broker has a real position, reconciliation will flag PHANTOM mismatch."
            )
            self.had_zero_stops = True
            self.position = "FLAT"
        # Safety: if restored in-position but timeout missing, mark invalid
        if self.position != "FLAT" and self.timeout_time is None:
            log.warning(f"[{sym_label}] Restored {self.position} but timeout_time missing — trade validity compromised")
            self.had_zero_stops = True  # triggers invalid flag on close
        if self.position != "FLAT":
            self.restored_this_session = True
            log.info(
                f"[{sym_label}] Restored {self.position} entry={self.entry_price} "
                f"stop={self.stop_price} target={self.target_price} "
                f"trades={self.trade_count}"
            )


# ═════════════════════════════════════════════════════════════
# Feature computation
# ═════════════════════════════════════════════════════════════
def compute_features_fx(buf: BarBuffer) -> Optional[dict]:
    """Compute range/accel features for FX strategies."""
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))
    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    range_pct = (pre["high"].max() - pre["low"].min()) / pre["close"].iloc[-1]
    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    current_px = float(pre["close"].iloc[-1])
    dist_from_low = (current_px - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    # Use last bar timestamp for hour, not wall-clock (avoids drift on stall/reconnect)
    try:
        bar_hour = int(str(df["ts"].iloc[-1])[11:13])
    except (ValueError, IndexError):
        bar_hour = datetime.now(timezone.utc).hour

    regime = classify_regime(buf)

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "dist_from_low": dist_from_low,
        "hour": bar_hour,
        "price": current_px,
        "regime": regime["regime"],
        "trend_strength": regime["trend_strength"],
        "efficiency_ratio": regime["efficiency_ratio"],
        "regime_confidence": regime["regime_confidence"],
    }


def compute_features_futures(buf: BarBuffer) -> Optional[dict]:
    """Compute range/accel + vol_burst features for futures strategies."""
    df = buf.to_df()
    if len(df) < 60:
        return None

    lookback = 30
    ctx_win = min(240, len(df))
    pre = df.iloc[-lookback:]
    context = df.iloc[-ctx_win:]

    range_pct = (pre["high"].max() - pre["low"].min()) / pre["close"].iloc[-1]
    pre_vol = (pre["high"] - pre["low"]).mean()
    ctx_vol = (context["high"] - context["low"]).mean()
    vol_z = (pre_vol - ctx_vol) / ctx_vol if ctx_vol > 0 else 0

    first15 = df.iloc[-30:-15]
    last15 = df.iloc[-15:]
    r1 = (first15["high"] - first15["low"]).mean()
    r2 = (last15["high"] - last15["low"]).mean()
    range_accel = (r2 - r1) / r1 if r1 > 0 else 0

    # Volume burst z-score (futures-specific)
    vol_burst_z = 0.0
    if "volume" in df.columns and df["volume"].sum() > 0:
        vol_5m = df["volume"].iloc[-5:].sum()
        vol_hist = df["volume"].rolling(5).sum().iloc[-ctx_win:-5]
        if len(vol_hist) > 10 and vol_hist.mean() > 0:
            vol_burst_z = (vol_5m - vol_hist.mean()) / vol_hist.mean()

    ctx_high = context["high"].max()
    ctx_low = context["low"].min()
    ctx_range = ctx_high - ctx_low
    current_px = float(pre["close"].iloc[-1])
    dist_from_low = (current_px - ctx_low) / ctx_range if ctx_range > 0 else 0.5

    # Use last bar timestamp for hour, not wall-clock
    try:
        bar_hour = int(str(df["ts"].iloc[-1])[11:13])
    except (ValueError, IndexError):
        bar_hour = datetime.now(timezone.utc).hour

    regime = classify_regime(buf)

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "vol_burst_z": vol_burst_z,
        "dist_from_low": dist_from_low,
        "hour": bar_hour,
        "price": current_px,
        "regime": regime["regime"],
        "trend_strength": regime["trend_strength"],
        "efficiency_ratio": regime["efficiency_ratio"],
        "regime_confidence": regime["regime_confidence"],
    }


# ═════════════════════════════════════════════════════════════
# Regime classification
# ═════════════════════════════════════════════════════════════
def classify_regime(buf: BarBuffer) -> dict:
    """Classify current market regime from bar buffer.

    Returns dict with:
        regime: TRENDING | RANGING | CHOPPY
        trend_strength: float (-1 to +1, negative = downtrend)
        volatility_rank: float (0 to 1, percentile of current vol vs history)
        regime_confidence: float (0 to 1, how clearly the regime is defined)
        regime_suitable_for: list of strategy types that fit this regime
    """
    df = buf.to_df()
    if len(df) < 120:
        return {"regime": "UNKNOWN", "trend_strength": 0, "volatility_rank": 0.5,
                "efficiency_ratio": 0, "regime_confidence": 0, "regime_suitable_for": ["range_accel"]}

    closes = df["close"].astype(float)
    highs = df["high"].astype(float)
    lows = df["low"].astype(float)

    # ── Trend detection via linear regression slope ──
    # Use last 60 bars (~1 hour) for trend
    recent = closes.iloc[-60:]
    x = np.arange(len(recent))
    slope = np.polyfit(x, recent.values, 1)[0]
    # Normalize slope by ATR to make it comparable across assets
    atr = (highs.iloc[-60:] - lows.iloc[-60:]).mean()
    trend_strength = (slope * 60) / atr if atr > 0 else 0  # slope over 60 bars, normalized
    trend_strength = max(-1.0, min(1.0, trend_strength))   # clamp to [-1, 1]

    # ── Volatility rank (current vs historical) ──
    bar_ranges = highs - lows
    recent_vol = bar_ranges.iloc[-30:].mean()
    hist_vol = bar_ranges.mean()
    vol_ratio = recent_vol / hist_vol if hist_vol > 0 else 1.0
    volatility_rank = max(0.0, min(1.0, vol_ratio / 2.0))  # 0-1 scale, 0.5 = normal

    # ── Choppiness index (Kaufman efficiency ratio) ──
    # direction / total_path — high = trending, low = choppy
    window = min(60, len(closes) - 1)
    direction = abs(float(closes.iloc[-1]) - float(closes.iloc[-window - 1]))
    total_path = closes.diff().abs().iloc[-window:].sum()
    efficiency = direction / total_path if total_path > 0 else 0

    # ── Classify ──
    abs_trend = abs(trend_strength)
    if efficiency > 0.35 and abs_trend > 0.3:
        regime = "TRENDING"
        confidence = min(1.0, efficiency * 1.5)
        suitable = ["momentum", "breakout", "trend_follow"]
    elif efficiency < 0.15 or volatility_rank > 0.7:
        regime = "CHOPPY"
        confidence = min(1.0, (1 - efficiency) * 0.8)
        suitable = ["mean_revert", "scalp"]  # range_accel is risky here
    else:
        regime = "RANGING"
        confidence = min(1.0, (0.35 - efficiency) / 0.2) if efficiency < 0.35 else 0.5
        suitable = ["range_accel", "mean_revert"]

    return {
        "regime": regime,
        "trend_strength": round(trend_strength, 4),
        "volatility_rank": round(volatility_rank, 4),
        "efficiency_ratio": round(efficiency, 4),
        "regime_confidence": round(confidence, 3),
        "regime_suitable_for": suitable,
    }


# ═════════════════════════════════════════════════════════════
# Trigger checks
# ═════════════════════════════════════════════════════════════
def _in_session(hour: int, start: int, end: int) -> bool:
    """Check if hour is within session window, supporting wrap-around (e.g. 22-08)."""
    if start <= end:
        return start <= hour <= end
    else:
        # Wrap-around: e.g. 22-08 means 22,23,0,1,...,8
        return hour >= start or hour <= end


def check_trigger_fx(features: dict, cfg: dict) -> Optional[str]:
    """FX trigger: range_pct + range_accel + vol_z + session."""
    trigger = cfg.get("trigger", {})
    if features["range_pct"] < trigger.get("range_pct_min", 0.0012):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    vol_z_min = trigger.get("vol_z_min")
    if vol_z_min is not None and features.get("vol_z", 0) <= vol_z_min:
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 0), trigger.get("session_end_utc", 23)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade (was: hidden long bias)


def check_trigger_futures(features: dict, cfg: dict) -> Optional[str]:
    """Futures trigger — dispatches by strategy field in config."""
    strategy = cfg.get("strategy", "vol_burst")
    if strategy.startswith("range_accel"):
        return _check_trigger_futures_range(features, cfg)
    return _check_trigger_futures_vol_burst(features, cfg)


def _check_trigger_futures_vol_burst(features: dict, cfg: dict) -> Optional[str]:
    """Original vol_burst: volume spike + range accel + session."""
    trigger = cfg.get("trigger", {})
    if features.get("vol_burst_z", 0) <= trigger.get("vol_burst_z_min", 1.0):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade


def _check_trigger_futures_range(features: dict, cfg: dict) -> Optional[str]:
    """Range-accel for futures: same logic as FX but uses futures features."""
    trigger = cfg.get("trigger", {})
    if features["range_pct"] < trigger.get("range_pct_min", 0.0008):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    vol_z_min = trigger.get("vol_z_min")
    if vol_z_min is not None and features.get("vol_z", 0) <= vol_z_min:
        return None
    h = features["hour"]
    if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return None  # ambiguous zone — no trade


# ═════════════════════════════════════════════════════════════
# InstrumentRunner — one per config
# ═════════════════════════════════════════════════════════════
class InstrumentRunner:
    """Manages one instrument's strategy within the unified process."""

    def __init__(self, config: dict, contract, ticker, log_dir: Path, config_path: str):
        self.cfg = config
        self.contract = contract
        self.ticker = ticker
        self.config_path = config_path

        self.symbol: str = config["symbol"]
        self.instrument_type: str = config.get("instrument_type", "forex")
        self.strategy: str = config.get("strategy", "unknown")
        self.label = f"{self.symbol}:{self.strategy}"

        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self.buf = BarBuffer(maxlen=300)
        self.state = State(log_dir / "state.json")
        self.signal_log = log_dir / "signals.csv"
        self.trade_log = log_dir / "trades.csv"

        self.current_bar_minute: Optional[datetime] = None
        self.current_bar: dict = {}

        # Risk params
        risk = config.get("risk", {})
        self.timeout_min = risk.get("timeout_minutes", 60)
        self.min_gap = risk.get("min_signal_gap_minutes", 15)

        # FX-specific
        self.stop_pips = risk.get("stop_pips", 0)
        self.target_pips = risk.get("target_pips", 0)
        self.lot_size = risk.get("lot_size", 20000)

        # Futures/bps-specific
        self.stop_bps = risk.get("stop_bps", 0)
        self.target_bps = risk.get("target_bps", 0)
        self.num_contracts = risk.get("num_contracts", 1)

        # Derived: pip size for FX
        self.pip_size = 0.01 if "JPY" in self.symbol.upper() else 0.0001

        # Uses pips (FX) or bps (futures/crypto)?
        self.uses_pips = self.instrument_type == "forex" and self.stop_pips > 0

        # Pyramiding / scale-in (opt-in, defaults OFF — does not affect cohort)
        pyramid = config.get("pyramid", {})
        self.pyramid_enabled = pyramid.get("enabled", False)
        self.pyramid_trigger_pips = pyramid.get("trigger_pips", 0)   # FX: add when price moves N pips in our favor
        self.pyramid_trigger_bps = pyramid.get("trigger_bps", 0)     # Futures: add when price moves N bps in our favor
        self.pyramid_max_adds = pyramid.get("max_adds", 1)           # Max scale-in entries (1 = double position)
        self.pyramid_move_stop_breakeven = pyramid.get("move_stop_breakeven", True)  # Move stop to avg entry on add

        # Futures multiplier
        self.multiplier = 1.0
        if hasattr(contract, "multiplier") and contract.multiplier:
            try:
                self.multiplier = float(contract.multiplier)
            except (ValueError, TypeError):
                pass

        # Per-instrument logger
        self._log = logging.getLogger(f"inst.{self.symbol.lower()}")

        # Track last feature eval minute to avoid double-evaluation
        self._last_eval_minute: Optional[datetime] = None

    # ── Price extraction ─────────────────────────────────────
    def _get_mid(self) -> Optional[float]:
        """Extract mid price from ticker. FX: bid/ask mid. Futures: last or delayed."""
        t = self.ticker
        if self.instrument_type == "forex":
            bid = getattr(t, "bid", None)
            ask = getattr(t, "ask", None)
            if bid and bid > 0 and ask and ask > 0:
                return (bid + ask) / 2
            last = getattr(t, "last", None)
            if last and last > 0:
                return last
            return None
        else:
            # Futures / Crypto: prefer last, then delayedLast, then bid/ask mid
            last = getattr(t, "last", None) or getattr(t, "delayedLast", None)
            if last and last > 0:
                return float(last)
            bid = getattr(t, "bid", None) or getattr(t, "delayedBid", None)
            ask = getattr(t, "ask", None) or getattr(t, "delayedAsk", None)
            if bid and bid > 0 and ask and ask > 0:
                return (bid + ask) / 2
            return None

    def _get_volume(self) -> float:
        """Get INCREMENTAL volume since last read (not cumulative session volume).

        IBKR ticker.volume is cumulative for the session. We track the previous
        value and return the delta, which gives per-bar volume when sampled once
        per bar close.
        """
        t = self.ticker
        raw_vol = getattr(t, "volume", None) or getattr(t, "delayedVolume", None)
        raw_vol = float(raw_vol) if raw_vol and raw_vol > 0 else 0.0

        prev = getattr(self, '_prev_cumulative_vol', 0.0)
        self._prev_cumulative_vol = raw_vol

        if prev <= 0 or raw_vol < prev:
            # First read or session reset — can't compute delta
            return 0.0
        return raw_vol - prev

    # ── CSV helpers ──────────────────────────────────────────
    def _ensure_signal_header(self) -> None:
        if not self.signal_log.exists():
            with open(self.signal_log, "w", newline="") as f:
                csv.writer(f).writerow(signal_header(self.instrument_type))

    def _log_signal(self, features: dict, direction: Optional[str], action: str) -> None:
        self._ensure_signal_header()
        features["ts"] = datetime.now(timezone.utc).isoformat()
        with open(self.signal_log, "a", newline="") as f:
            csv.writer(f).writerow(build_signal_row(
                features, direction, action, self.instrument_type,
                getattr(self, '_config_hash', ''), getattr(self, '_session_id', ''),
            ))

    def _evaluate_validity(self) -> tuple[bool, str]:
        """Determine if this trade is experimentally valid.

        Taxonomy (ordered by priority):
        - restored_from_file: state loaded from disk (first trade after restart)
        - zero_stops_during_trade: stop/target were 0 at any point
        - timeout_restoration_failure: timeout_time missing after restore
        - reconnect_during_session: IBKR reconnect occurred (session-wide)
        - repeated_tick_failures: runner had N+ consecutive errors during trade
        """
        s = self.state
        if s.restored_this_session:
            return False, "restored_from_file"
        if s.had_zero_stops:
            return False, "zero_stops_or_timeout_missing"
        if getattr(self, '_reconnected', False):
            return False, "reconnect_during_session"
        if getattr(self, '_consecutive_errors', 0) >= 3:
            return False, "repeated_tick_failures"
        return True, ""

    def _ensure_trade_header(self) -> None:
        if not self.trade_log.exists():
            with open(self.trade_log, "w", newline="") as f:
                csv.writer(f).writerow(trade_header(self.uses_pips))

    def _log_trade(self, exit_price: float, exit_reason: str, now: datetime) -> float:
        """Log closed trade, return PnL in pips (FX) or points (futures)."""
        s = self.state
        dur = (now - s.entry_time).total_seconds() / 60 if s.entry_time else 0

        if self.uses_pips:
            if s.position == "LONG":
                pnl = (exit_price - s.entry_price) / self.pip_size
            else:
                pnl = (s.entry_price - exit_price) / self.pip_size
        else:
            if s.position == "LONG":
                pnl = exit_price - s.entry_price
            else:
                pnl = s.entry_price - exit_price

        # Evaluate experiment validity
        valid, invalid_reason = self._evaluate_validity()
        runtime_epoch = int(time.time() - self._runtime_start) if hasattr(self, '_runtime_start') else 0
        validity_fields = [
            str(valid).lower(), invalid_reason or "",
            getattr(self, '_config_hash', ''), getattr(self, '_session_id', ''),
            str(runtime_epoch), getattr(self, '_git_sha', ''),
        ]

        self._ensure_trade_header()
        with open(self.trade_log, "a", newline="") as f:
            w = csv.writer(f)
            if self.uses_pips:
                w.writerow([
                    now.isoformat(), s.position.lower(),
                    f"{s.entry_price:.5f}",
                    f"{exit_price:.5f}", f"{pnl:.2f}",
                    exit_reason, f"{dur:.1f}", s.trade_count,
                ] + validity_fields)
            else:
                pnl_usd = pnl * self.multiplier
                if self.instrument_type == "future":
                    ep = f"{s.entry_price:.2f}"
                    xp = f"{exit_price:.2f}"
                else:
                    ep = f"{s.entry_price:.5f}"
                    xp = f"{exit_price:.5f}"
                w.writerow([
                    now.isoformat(), s.position.lower(),
                    ep, xp, f"{pnl:.2f}", f"{pnl_usd:.2f}",
                    exit_reason, f"{dur:.1f}", s.trade_count,
                ] + validity_fields)

        # Reset validity flags after trade closes
        s.restored_this_session = False
        s.had_zero_stops = False

        return pnl

    # ── Feature & trigger dispatch ───────────────────────────
    def _compute_features(self) -> Optional[dict]:
        if self.instrument_type in ("future", "crypto"):
            return compute_features_futures(self.buf)
        return compute_features_fx(self.buf)

    def _check_trigger(self, features: dict) -> Optional[str]:
        strategy = self.cfg.get("strategy", "range_accel")
        # Advanced strategies: FVG, liquidity sweep, volume profile
        if strategy == "fvg":
            return self._check_trigger_fvg(features)
        elif strategy == "liquidity_sweep":
            return self._check_trigger_sweep(features)
        elif strategy == "volume_profile":
            return self._check_trigger_vp(features)
        # Default: range-based triggers
        if self.instrument_type in ("future", "crypto"):
            return check_trigger_futures(features, self.cfg)
        return check_trigger_fx(features, self.cfg)

    def _check_trigger_fvg(self, features: dict) -> Optional[str]:
        """Fair Value Gap strategy: enter on retrace to fill 3-candle imbalance."""
        from argus_flow.strategies.fvg_detector import detect_fvgs, find_fvg_fill_entries
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        if len(df) < 60:
            return None
        df = df.reset_index(drop=True)
        fvgs = detect_fvgs(df, min_displacement_mult=trigger.get("min_displacement_mult", 1.5))
        entries = find_fvg_fill_entries(
            df, fvgs, max_wait_bars=trigger.get("max_wait_bars", 60),
            session_start=trigger.get("session_start_utc", 8),
            session_end=trigger.get("session_end_utc", 20),
        )
        if entries:
            return entries[-1]["direction"]  # latest signal
        return None

    def _check_trigger_sweep(self, features: dict) -> Optional[str]:
        """Liquidity sweep: enter opposite direction after wick beyond swing point."""
        from argus_flow.strategies.liquidity_sweep import find_swing_points, detect_sweeps
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 8), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        if len(df) < 60:
            return None
        df = df.reset_index(drop=True)
        sh, sl = find_swing_points(df, lookback=trigger.get("swing_lookback", 20))
        sweeps = detect_sweeps(
            df, sh, sl, wick_ratio_min=trigger.get("wick_ratio_min", 0.3),
            session_start=trigger.get("session_start_utc", 8),
            session_end=trigger.get("session_end_utc", 20),
        )
        if sweeps:
            return sweeps[-1]["direction"]
        return None

    def _check_trigger_vp(self, features: dict) -> Optional[str]:
        """Volume profile: mean-revert from VAH/VAL toward POC."""
        from argus_flow.strategies.volume_profile import calculate_volume_profile
        trigger = self.cfg.get("trigger", {})
        h = features["hour"]
        if not _in_session(h, trigger.get("session_start_utc", 13), trigger.get("session_end_utc", 20)):
            return None
        df = self.buf.to_df()
        lookback = trigger.get("vp_lookback", 240)
        if len(df) < lookback:
            return None
        df = df.reset_index(drop=True)
        profile = calculate_volume_profile(df, len(df) - lookback, len(df))
        if not profile:
            return None
        price = features["price"]
        buffer = trigger.get("entry_buffer_pct", 0.0002)
        vah = profile["vah"]
        val = profile["val"]
        if price >= vah * (1 - buffer):
            return "short"  # at value area high, mean-revert down
        elif price <= val * (1 + buffer):
            return "long"   # at value area low, mean-revert up
        return None

    @staticmethod
    def _regime_compatible(regime: str, strategy: str) -> bool:
        """Check if the current regime suits the strategy type."""
        compatibility = {
            "range_accel":      {"RANGING", "UNKNOWN"},
            "T4_full_stack":    {"RANGING", "TRENDING", "UNKNOWN"},
            "range_accel_NY":   {"RANGING", "UNKNOWN"},
            "momentum":         {"TRENDING", "UNKNOWN"},
            "vol_burst":        {"TRENDING", "CHOPPY", "UNKNOWN"},
            "fvg":              {"RANGING", "TRENDING", "UNKNOWN"},
            "liquidity_sweep":  {"RANGING", "CHOPPY", "UNKNOWN"},
            "volume_profile":   {"RANGING", "UNKNOWN"},
        }
        allowed = compatibility.get(strategy, {"RANGING", "TRENDING", "UNKNOWN"})
        return regime in allowed

    # ── Stop/target computation ──────────────────────────────
    def _compute_atr(self) -> float:
        """Compute 14-period ATR from bar buffer."""
        df = self.buf.to_df()
        if len(df) < 14:
            return 0.0
        highs = df["high"].astype(float).iloc[-14:]
        lows = df["low"].astype(float).iloc[-14:]
        closes = df["close"].astype(float).iloc[-15:-1]  # previous closes
        if len(closes) < 14:
            return float((highs - lows).mean())
        tr = pd.concat([
            highs - lows,
            (highs - closes).abs(),
            (lows - closes).abs(),
        ], axis=1).max(axis=1)
        return float(tr.mean())

    def _compute_stops(self, entry_px: float, direction: str) -> tuple[float, float]:
        """Return (stop_price, target_price) for a new entry.

        Uses ATR-scaled stops if atr_stop_mult is in config, otherwise fixed pip/bps.
        """
        risk_cfg = self.cfg.get("risk", {})
        atr_stop_mult = risk_cfg.get("atr_stop_mult", 0)
        atr_target_mult = risk_cfg.get("atr_target_mult", 0)

        if atr_stop_mult > 0:
            atr = self._compute_atr()
            if atr > 0:
                stop_dist = atr * atr_stop_mult
                target_dist = atr * (atr_target_mult if atr_target_mult > 0 else atr_stop_mult * 2)
            else:
                # Fallback to fixed if ATR can't be computed
                stop_dist = self.stop_pips * self.pip_size if self.uses_pips else entry_px * (self.stop_bps / 10000)
                target_dist = self.target_pips * self.pip_size if self.uses_pips else entry_px * (self.target_bps / 10000)
        elif self.uses_pips:
            stop_dist = self.stop_pips * self.pip_size
            target_dist = self.target_pips * self.pip_size
        else:
            stop_dist = entry_px * (self.stop_bps / 10000)
            target_dist = entry_px * (self.target_bps / 10000)

        if direction == "long":
            return entry_px - stop_dist, entry_px + target_dist
        else:
            return entry_px + stop_dist, entry_px - target_dist

    def _check_trailing_stop(self, mid: float) -> None:
        """Move stop to breakeven after price moves 1R in our favor.

        1R = original risk distance (entry to stop).
        After 1.5R, trail at entry + 0.5R.
        """
        s = self.state
        if s.entry_price == 0 or s.stop_price == 0:
            return

        risk_dist = abs(s.entry_price - s.stop_price)
        if risk_dist == 0:
            return

        if s.position == "LONG":
            favorable = mid - s.entry_price
            if favorable >= risk_dist * 1.5:
                # Trail at entry + 0.5R
                new_stop = s.entry_price + risk_dist * 0.5
                if new_stop > s.stop_price:
                    s.stop_price = new_stop
            elif favorable >= risk_dist:
                # Move to breakeven
                if s.stop_price < s.entry_price:
                    self._log.info(f"TRAILING: stop moved to breakeven {s.entry_price:.5f}")
                    s.stop_price = s.entry_price
        elif s.position == "SHORT":
            favorable = s.entry_price - mid
            if favorable >= risk_dist * 1.5:
                new_stop = s.entry_price - risk_dist * 0.5
                if new_stop < s.stop_price:
                    s.stop_price = new_stop
            elif favorable >= risk_dist:
                if s.stop_price > s.entry_price:
                    self._log.info(f"TRAILING: stop moved to breakeven {s.entry_price:.5f}")
                    s.stop_price = s.entry_price

    # ── Pyramiding / scale-in ────────────────────────────────
    def _check_pyramid(self, mid: float, now: datetime) -> None:
        """Check if we should add to the current position (scale-in on strength)."""
        if not self.pyramid_enabled:
            return
        s = self.state
        if s.pyramid_adds >= self.pyramid_max_adds:
            return

        # Compute how far price has moved in our favor
        if self.uses_pips:
            trigger_dist = self.pyramid_trigger_pips * self.pip_size
        else:
            trigger_dist = s.entry_price * (self.pyramid_trigger_bps / 10000)

        if trigger_dist <= 0:
            return

        if s.position == "LONG":
            favorable_move = mid - s.entry_price
        else:  # SHORT
            favorable_move = s.entry_price - mid

        if favorable_move < trigger_dist:
            return

        # Scale-in: add to position
        old_entry = s.entry_price
        # Average entry: equal-weight average of original + add
        n_entries = s.pyramid_adds + 1  # entries so far (original + prior adds)
        s.avg_entry_price = (old_entry * n_entries + mid) / (n_entries + 1)
        s.pyramid_adds += 1

        # Move stop to breakeven (avg entry) if configured
        if self.pyramid_move_stop_breakeven and s.avg_entry_price > 0:
            if s.position == "LONG":
                new_stop = s.avg_entry_price - (1 * self.pip_size if self.uses_pips else 0)
                s.stop_price = max(s.stop_price, new_stop)  # only tighten, never loosen
            else:
                new_stop = s.avg_entry_price + (1 * self.pip_size if self.uses_pips else 0)
                s.stop_price = min(s.stop_price, new_stop)  # only tighten, never loosen

        s.save()
        self._log.info(
            f"PYRAMID ADD #{s.pyramid_adds} {s.position} @ {mid:.5f} "
            f"avg_entry={s.avg_entry_price:.5f} "
            f"new_stop={s.stop_price:.5f}"
        )

    # ── Main tick (called every second) ──────────────────────
    def tick(self, now: datetime) -> None:
        """Process one tick: update bar, check exits, evaluate signals."""
        mid = self._get_mid()
        if mid is None or mid <= 0:
            return

        vol = self._get_volume()
        bar_minute = now.replace(second=0, microsecond=0)
        new_bar_closed = False

        # ── Bar construction ─────────────────────────────────
        if self.current_bar_minute is None:
            self.current_bar_minute = bar_minute
            self.current_bar = {
                "open": mid, "high": mid, "low": mid, "close": mid, "volume": vol,
            }
        elif bar_minute > self.current_bar_minute:
            # Close prior bar, push to buffer
            self.current_bar["ts"] = str(self.current_bar_minute)
            self.buf.add(self.current_bar)
            new_bar_closed = True
            self.current_bar_minute = bar_minute
            self.current_bar = {
                "open": mid, "high": mid, "low": mid, "close": mid, "volume": vol,
            }
        else:
            self.current_bar["high"] = max(self.current_bar["high"], mid)
            self.current_bar["low"] = min(self.current_bar["low"], mid)
            self.current_bar["close"] = mid
            if vol > 0:
                self.current_bar["volume"] = self.current_bar.get("volume", 0) + vol  # accumulate, not overwrite

        # ── Position management (every tick) ─────────────────
        s = self.state
        if s.position != "FLAT":
            exit_reason = None

            if s.position == "LONG":
                if mid <= s.stop_price:
                    exit_reason = "stop"
                elif mid >= s.target_price:
                    exit_reason = "target"
            elif s.position == "SHORT":
                if mid >= s.stop_price:
                    exit_reason = "stop"
                elif mid <= s.target_price:
                    exit_reason = "target"

            if s.timeout_time and now >= s.timeout_time:
                exit_reason = "timeout"

            # ── Trailing stop: move stop to breakeven after 1R ──
            if s.position != "FLAT" and not exit_reason:
                self._check_trailing_stop(mid)

            if exit_reason:
                pnl = self._log_trade(mid, exit_reason, now)
                if self.uses_pips:
                    s.pnl_pips += pnl
                    unit = "pip"
                    total = s.pnl_pips
                else:
                    s.pnl_points += pnl
                    unit = "pts"
                    total = s.pnl_points
                # Record trade PnL in R-multiples for daily risk limits
                if hasattr(self, '_risk_mgr'):
                    stop_size = self.stop_pips if self.uses_pips else self.stop_bps
                    pnl_r = pnl / stop_size if stop_size > 0 else pnl
                    self._risk_mgr.record_trade_pnl(self.symbol, pnl_r)
                self._log.info(
                    f"EXIT {s.position} @ {mid} reason={exit_reason} "
                    f"pnl={pnl:+.2f}{unit} total={total:+.2f}{unit} "
                    f"trades={s.trade_count}"
                )
                s.position = "FLAT"
                s.entry_time = None
                s.timeout_time = None
                s.pyramid_adds = 0
                s.avg_entry_price = 0.0
                s.save()
            else:
                # ── Pyramiding: scale-in on confirmed move ────
                self._check_pyramid(mid, now)
            return  # don't evaluate new signals while in position

        # ── Signal evaluation (once per new bar, when FLAT) ──
        if not new_bar_closed:
            # Also allow eval if we haven't evaluated this minute yet
            if self._last_eval_minute == bar_minute:
                return
        if len(self.buf) < 60:
            return

        self._last_eval_minute = bar_minute
        features = self._compute_features()
        if features is None:
            return

        direction = self._check_trigger(features)

        # ── Regime gate ──────────────────────────────────────
        # Stamps every signal with regime. In GATE mode, blocks entries
        # when regime doesn't match strategy type.
        regime = features.get("regime", "UNKNOWN")
        regime_mode = self.cfg.get("regime_gate", "LOG_ONLY")  # LOG_ONLY | GATE
        if direction and regime_mode == "GATE":
            strategy = self.cfg.get("strategy", "range_accel")
            regime_ok = self._regime_compatible(regime, strategy)
            if not regime_ok:
                self._log.info(
                    f"REGIME_BLOCK {direction.upper()} | regime={regime} "
                    f"strategy={strategy} eff={features.get('efficiency_ratio', 0):.3f} "
                    f"trend={features.get('trend_strength', 0):.3f}"
                )
                self._log_signal(features, direction, "REGIME_BLOCKED")
                direction = None

        # ── Maintenance blackout (TWS restart window) ────
        # Block new entries during TWS restart period to avoid tainted trades.
        # Conservative window: 01:30-04:30 UTC covers both CDT and CST restart times.
        if direction:
            h_utc = now.hour
            m_utc = now.minute
            utc_minutes = h_utc * 60 + m_utc
            blackout_start = 1 * 60 + 30   # 01:30 UTC
            blackout_end = 4 * 60 + 30     # 04:30 UTC
            if blackout_start <= utc_minutes <= blackout_end:
                self._log.info(f"MAINTENANCE_BLACKOUT: {direction.upper()} blocked during TWS restart window")
                self._log_signal(features, direction, "MAINTENANCE_BLACKOUT")
                direction = None

        # Min gap between signals
        if direction and s.last_signal_time:
            gap = (now - s.last_signal_time).total_seconds() / 60
            if gap < self.min_gap:
                direction = None

        # Block new entries if reconciliation requires recovery
        if direction and getattr(self, '_entries_blocked', False):
            self._log.warning(f"Entry BLOCKED ({direction}) -- reconciliation recovery required")
            direction = None

        # ── Portfolio risk gate ─────────────────────────────
        if direction and hasattr(self, '_risk_mgr'):
            allowed, reason = self._risk_mgr.can_enter(
                self.symbol, direction, getattr(self, '_all_instruments', [])
            )
            if not allowed:
                self._log.info(f"RISK_BLOCK {direction.upper()} | reason={reason}")
                self._log_signal(features, direction, f"RISK_BLOCKED_{reason}")
                direction = None

        if direction:
            entry_px = mid
            stop_px, target_px = self._compute_stops(entry_px, direction)

            s.position = direction.upper()
            s.entry_price = entry_px
            s.avg_entry_price = entry_px
            s.pyramid_adds = 0
            s.entry_time = now
            s.stop_price = stop_px
            s.target_price = target_px
            s.timeout_time = now.replace(second=0, microsecond=0) + timedelta(minutes=self.timeout_min)
            s.last_signal_time = now
            s.trade_count += 1
            s.direction_str = direction
            s.save()

            # Capture spread at entry for toxicity analysis
            t = self.ticker
            bid = getattr(t, "bid", None) or getattr(t, "delayedBid", None)
            ask = getattr(t, "ask", None) or getattr(t, "delayedAsk", None)
            if bid and ask and bid > 0 and ask > 0:
                spread_pips = (ask - bid) / self.pip_size if self.uses_pips else (ask - bid)
                features["entry_spread"] = round(spread_pips, 2)
                features["entry_bid"] = bid
                features["entry_ask"] = ask
            else:
                features["entry_spread"] = 0
                features["entry_bid"] = 0
                features["entry_ask"] = 0

            self._log_signal(features, direction, "ENTRY")

            extra = ""
            if not self.uses_pips:
                extra = f" vol_burst={features.get('vol_burst_z', 0):.2f}"
            spread_str = f" spread={features.get('entry_spread', 0):.1f}pip" if self.uses_pips else ""
            self._log.info(
                f"ENTRY {direction.upper()} @ {entry_px} "
                f"stop={stop_px} target={target_px} "
                f"rng={features['range_pct']:.4f} accel={features['range_accel']:.3f}"
                f" regime={features.get('regime', '?')} eff={features.get('efficiency_ratio', 0):.3f}"
                f"{spread_str}{extra}"
            )
        else:
            # Periodic NO_TRIGGER log every 5 minutes
            if now.minute % 5 == 0 and now.second < 2:
                self._log_signal(features, None, "NO_TRIGGER")

    # ── Seed historical bars ───────────────────────────��─────
    def seed(self, ib: IB) -> None:
        """Load 5D of 1-min history to bootstrap feature computation."""
        what = "MIDPOINT" if self.instrument_type == "forex" else "TRADES"
        try:
            bars = ib.reqHistoricalData(
                self.contract,
                endDateTime="",
                durationStr="5 D",
                barSizeSetting="1 min",
                whatToShow=what,
                useRTH=False,
            )
        except Exception as e:
            self._log.warning(f"Historical data request failed: {e}")
            bars = []
        for b in bars:
            self.buf.add({
                "ts": str(b.date),
                "open": b.open, "high": b.high,
                "low": b.low, "close": b.close,
                "volume": getattr(b, "volume", 0),
            })
        self._log.info(f"Seeded {len(self.buf)} bars")


# ═════════════════════════════════════════════════════════════
# Contract creation helpers
# ═════════════════════════════════════════════════════════════
def create_contract(cfg: dict):
    """Create ib_insync contract from config dict."""
    itype = cfg.get("instrument_type", "forex")
    sym = cfg["symbol"]

    if itype == "forex":
        return Forex(sym)
    elif itype == "future":
        return Future(
            symbol=sym,
            exchange=cfg.get("exchange", "CME"),
            lastTradeDateOrContractMonth=cfg.get("expiry", ""),
        )
    elif itype == "crypto":
        # Crypto CFDs via IBKR use the Crypto contract type if available,
        # but ib_insync may not have it. Fall back to generic Forex-style.
        # For IBKR paper, crypto pairs are typically not available — use
        # bps-based stops like futures.
        try:
            from ib_insync import Crypto
            return Crypto(sym, currency="USD")
        except ImportError:
            # Fallback: treat as forex pair  sym + "USD"
            return Forex(sym + "USD")
    else:
        raise ValueError(f"Unknown instrument_type: {itype} for {sym}")


def log_dir_for(cfg: dict) -> Path:
    """Derive per-instrument log directory from symbol.

    NOTE: if you ever run two configs with the same symbol (e.g. two EURUSD strategies),
    this will collide. The main() function checks for duplicates at startup.
    """
    return LOGS_ROOT / cfg["symbol"].lower()


# =================================================================
# Runtime state enum -- replaces scattered booleans
# =================================================================
class RuntimeMode:
    """Process-level runtime state."""
    BOOTING = "BOOTING"
    RECONCILING = "RECONCILING"
    READY = "READY"
    DEGRADED = "DEGRADED"       # at least one instrument quarantined
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"  # unresolved broker mismatch


class ReconcileResult:
    """Per-instrument broker reconciliation outcome."""
    CLEAN_FLAT = "CLEAN_FLAT"
    CLEAN_OPEN_MATCHED = "CLEAN_OPEN_MATCHED"
    LOCAL_FLAT_BROKER_OPEN = "LOCAL_FLAT_BROKER_OPEN"
    LOCAL_OPEN_BROKER_FLAT = "LOCAL_OPEN_BROKER_FLAT"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    UNRESOLVED = "UNRESOLVED"


# =================================================================
# Broker reconciliation gate
# =================================================================
def _normalize_ib_key(contract) -> str:
    """Normalize IB contract to canonical instrument key."""
    sec_type = getattr(contract, "secType", "")
    if sec_type == "CASH":
        return f"{contract.symbol}.{contract.currency}"
    elif sec_type == "FUT":
        return contract.symbol
    return getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "")


def _runner_to_ib_key(runner: 'InstrumentRunner') -> str:
    """Convert runner's contract to canonical key for broker matching."""
    return _normalize_ib_key(runner.contract)


def reconcile_instruments(ib, instruments: list) -> dict:
    """Compare local state vs broker truth for all instruments.

    Returns dict mapping runner.symbol -> {result, local_pos, broker_pos, detail}.
    Writes incident artifacts for non-clean results.
    """
    results = {}

    # Fetch broker positions
    try:
        broker_positions = {}
        for p in ib.positions():
            key = _normalize_ib_key(p.contract)
            qty = float(p.position)
            broker_positions[key] = {
                "qty": qty,
                "direction": "LONG" if qty > 0 else ("SHORT" if qty < 0 else "FLAT"),
                "avg_cost": float(p.avgCost),
            }
        broker_ok = True
    except Exception as e:
        log.error(f"Reconciliation: failed to fetch broker positions: {e}")
        broker_ok = False
        broker_positions = {}

    for inst in instruments:
        ib_key = _runner_to_ib_key(inst)
        local_pos = inst.state.position
        broker_info = broker_positions.get(ib_key, {"qty": 0, "direction": "FLAT"})
        broker_dir = broker_info["direction"]

        if not broker_ok:
            result = ReconcileResult.BROKER_UNAVAILABLE
            detail = "Could not query broker positions"
        elif local_pos == "FLAT" and broker_dir == "FLAT":
            result = ReconcileResult.CLEAN_FLAT
            detail = "Both local and broker flat"
        elif local_pos == broker_dir:
            # Direction matches — also verify qty is nonzero and reasonable
            broker_qty = abs(broker_info.get("qty", 0))
            if broker_qty > 0:
                result = ReconcileResult.CLEAN_OPEN_MATCHED
                detail = f"Both agree: {local_pos} qty={broker_qty}"
            else:
                # Direction matches but qty is 0 — trust broker
                result = ReconcileResult.LOCAL_OPEN_BROKER_FLAT
                detail = f"Direction matches but broker qty=0 — forcing FLAT"
                inst.state.position = "FLAT"
                inst.state.entry_price = 0.0
                inst.state.stop_price = 0.0
                inst.state.target_price = 0.0
                inst.state.timeout_time = None
                inst.state.entry_time = None
                inst.state.avg_entry_price = 0.0
                inst.state.pyramid_adds = 0
                inst.state.save()
        elif local_pos == "FLAT" and broker_dir in ("LONG", "SHORT"):
            result = ReconcileResult.LOCAL_FLAT_BROKER_OPEN
            detail = f"Orphan: broker has {broker_dir} qty={broker_info['qty']} but runner is FLAT"
        elif local_pos in ("LONG", "SHORT") and broker_dir == "FLAT":
            result = ReconcileResult.LOCAL_OPEN_BROKER_FLAT
            detail = f"Phantom: runner says {local_pos} but broker is FLAT -- forcing local FLAT"
            # Auto-correct: trust broker truth — clear ALL lifecycle state
            inst.state.position = "FLAT"
            inst.state.entry_price = 0.0
            inst.state.stop_price = 0.0
            inst.state.target_price = 0.0
            inst.state.timeout_time = None
            inst.state.entry_time = None
            inst.state.avg_entry_price = 0.0
            inst.state.pyramid_adds = 0
            inst.state.save()
        else:
            result = ReconcileResult.UNRESOLVED
            detail = f"local={local_pos} broker={broker_dir} -- cannot auto-resolve"

        results[inst.symbol] = {
            "result": result,
            "local_position": local_pos,
            "broker_position": broker_dir,
            "broker_qty": broker_info.get("qty", 0),
            "detail": detail,
            "ib_key": ib_key,
        }

        # Store reconciliation result on the runner
        inst._reconciliation = result

        if result not in (ReconcileResult.CLEAN_FLAT, ReconcileResult.CLEAN_OPEN_MATCHED):
            log.warning(f"[{inst.symbol}] RECONCILE: {result} -- {detail}")
            _write_incident(inst, result, detail, local_pos, broker_info)
        else:
            log.info(f"[{inst.symbol}] RECONCILE: {result}")

    # Check for orphaned broker positions not tracked by any runner
    tracked_keys = {_runner_to_ib_key(inst) for inst in instruments}
    for key, info in broker_positions.items():
        if key not in tracked_keys and info["direction"] != "FLAT":
            log.warning(f"ORPHAN DETECTED: broker has {info['direction']} in {key} -- not tracked by any runner")
            results[f"_orphan_{key}"] = {
                "result": ReconcileResult.UNRESOLVED,
                "local_position": "NONE",
                "broker_position": info["direction"],
                "broker_qty": info["qty"],
                "detail": f"Untracked broker position in {key}",
                "ib_key": key,
            }

    return results


def _write_incident(inst, result: str, detail: str,
                    local_pos: str, broker_info: dict) -> None:
    """Persist an incident artifact for audit trail."""
    incident_dir = inst.log_dir / "incidents"
    incident_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = getattr(inst, '_session_id', 'unknown')
    incident_file = incident_dir / f"incident_{session_id}_{ts}.json"
    incident_file.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "instrument": inst.symbol,
        "session_id": session_id,
        "severity": "CRITICAL" if result in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED) else "WARNING",
        "reconciliation_result": result,
        "detail": detail,
        "local_state": {
            "position": local_pos,
            "entry_price": inst.state.entry_price,
            "stop_price": inst.state.stop_price,
            "target_price": inst.state.target_price,
        },
        "broker_state": broker_info,
        "action_taken": "forced_flat" if result == ReconcileResult.LOCAL_OPEN_BROKER_FLAT else "none",
        "requires_manual_review": result in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED),
    }, indent=2))
    log.info(f"  Incident written: {incident_file}")


def _determine_runtime_mode(recon_results: dict) -> str:
    """Determine overall runtime mode from reconciliation results."""
    has_critical = any(
        r["result"] in (ReconcileResult.LOCAL_FLAT_BROKER_OPEN, ReconcileResult.UNRESOLVED)
        for r in recon_results.values()
    )
    if has_critical:
        return RuntimeMode.RECOVERY_REQUIRED

    has_degraded = any(
        r["result"] == ReconcileResult.BROKER_UNAVAILABLE
        for r in recon_results.values()
    )
    if has_degraded:
        return RuntimeMode.DEGRADED

    return RuntimeMode.READY


# ═════════════════════════════════════════════════════════════
# Portfolio Risk Manager
# ═════════════════════════════════════════════════════════════
class PortfolioRiskManager:
    """Cross-instrument risk guards. Shared by all runners in a process."""

    # Currency exposure map: which currencies each symbol exposes you to
    CURRENCY_MAP = {
        "EURUSD": {"EUR": +1, "USD": -1},
        "GBPUSD": {"GBP": +1, "USD": -1},
        "AUDUSD": {"AUD": +1, "USD": -1},
        "USDJPY": {"USD": +1, "JPY": -1},
        "EURJPY": {"EUR": +1, "JPY": -1},
        "GBPJPY": {"GBP": +1, "JPY": -1},
        "AUDJPY": {"AUD": +1, "JPY": -1},
        "CADJPY": {"CAD": +1, "JPY": -1},
        "MES": {"USD_EQUITY": +1}, "MNQ": {"USD_EQUITY": +1},
        "MYM": {"USD_EQUITY": +1}, "M2K": {"USD_EQUITY": +1},
        "MGC": {"GOLD": +1}, "MCL": {"OIL": +1}, "NKD": {"JPY_EQUITY": +1},
    }

    def __init__(self, max_same_currency: int = 3, max_drawdown_pct: float = 0.03,
                 daily_max_loss: float = 3.0, portfolio_daily_max_loss: float = 10.0):
        self.max_same_currency = max_same_currency
        self.max_drawdown_pct = max_drawdown_pct
        self.daily_max_loss = daily_max_loss  # per instrument, in R
        self.portfolio_daily_max_loss = portfolio_daily_max_loss  # fleet-wide, in R
        self._peak_pnl: float = 0.0
        self._current_pnl: float = 0.0
        self._drawdown_pause = False
        self._daily_pnl: dict[str, float] = {}
        self._daily_paused: set[str] = set()
        self._portfolio_daily_paused: bool = False
        self._current_day: str = ""

    def update(self, instruments: list) -> None:
        """Update portfolio PnL tracking.

        Normalizes to R-multiples (multiples of initial risk) per instrument
        to avoid mixing pips and points. 1R = one stop-loss distance of PnL.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._current_day:
            self._current_day = today
            self._daily_pnl.clear()
            self._daily_paused.clear()
            self._portfolio_daily_paused = False
            log.info("RISK_MGR: daily limits reset")
        # Normalize each instrument's PnL by its stop distance to get R-multiples
        total_r = 0.0
        for i in instruments:
            s = i.state
            raw_pnl = s.pnl_pips if i.uses_pips else s.pnl_points
            # Normalize by stop size to get R-units
            stop = i.stop_pips if i.uses_pips else i.stop_bps
            if stop > 0:
                total_r += raw_pnl / stop
            elif raw_pnl != 0:
                # Zero stop = broken config. Don't contaminate R-aggregate.
                log.warning(f"RISK_MGR: {i.symbol} has stop=0, PnL={raw_pnl} excluded from R-total")
                pass  # skip this instrument's contribution
        self._current_pnl = total_r
        self._peak_pnl = max(self._peak_pnl, total_r)

    def record_trade_pnl(self, symbol: str, pnl_r: float) -> None:
        """Record a completed trade's PnL in R-multiples for daily tracking."""
        self._daily_pnl[symbol] = self._daily_pnl.get(symbol, 0.0) + pnl_r

    def can_enter(self, symbol: str, direction: str, instruments: list) -> tuple[bool, str]:
        """Master gate: check all portfolio-level risk guards."""
        # 1. Drawdown breaker
        if self._peak_pnl > 0:
            dd = (self._peak_pnl - self._current_pnl) / abs(self._peak_pnl)
            if dd >= self.max_drawdown_pct:
                if not self._drawdown_pause:
                    log.warning(f"DRAWDOWN BREAKER: {dd:.1%} from peak. ALL entries paused.")
                    self._drawdown_pause = True
                return False, "DRAWDOWN_PAUSE"
            if self._drawdown_pause and dd < 0.01:
                log.info("DRAWDOWN BREAKER: recovered. Entries resumed.")
                self._drawdown_pause = False
        if self._drawdown_pause:
            return False, "DRAWDOWN_PAUSE"

        # 2. Daily max loss per instrument
        if symbol in self._daily_paused:
            return False, "DAILY_LIMIT"
        daily = self._daily_pnl.get(symbol, 0.0)
        if daily <= -self.daily_max_loss:
            log.warning(f"DAILY LIMIT: {symbol} lost {daily:+.1f} today. Paused.")
            self._daily_paused.add(symbol)
            return False, "DAILY_LIMIT"

        # 3. Portfolio daily max loss (sum of all instrument daily losses)
        total_daily_r = sum(self._daily_pnl.values())
        if total_daily_r <= -self.portfolio_daily_max_loss:
            if not self._portfolio_daily_paused:
                log.warning(f"PORTFOLIO DAILY LIMIT: fleet lost {total_daily_r:+.1f}R today. ALL entries paused.")
                self._portfolio_daily_paused = True
            return False, "PORTFOLIO_DAILY_LIMIT"

        # 4. Correlation / currency exposure
        cmap = self.CURRENCY_MAP.get(symbol.upper(), {})
        if not cmap:
            # Unknown symbol — fail closed, do not silently skip correlation checks
            log.warning(f"CORRELATION FAIL_CLOSED: {symbol} not in CURRENCY_MAP — entry blocked")
            return False, "UNKNOWN_SYMBOL_EXPOSURE"

        exposure: dict[str, int] = {}
        for inst in instruments:
            if inst.state.position == "FLAT":
                continue
            ic = self.CURRENCY_MAP.get(inst.symbol.upper(), {})
            pm = 1 if inst.state.position == "LONG" else -1
            for ccy, dm in ic.items():
                exposure[ccy] = exposure.get(ccy, 0) + (dm * pm)
        new_mult = 1 if direction == "long" else -1
        for ccy, dm in cmap.items():
            new_exp = exposure.get(ccy, 0) + (dm * new_mult)
            if abs(new_exp) > self.max_same_currency:
                log.info(f"CORRELATION BLOCK: {symbol} {direction} -> {ccy}={new_exp:+d} (max={self.max_same_currency})")
                return False, "CORRELATION_LIMIT"

        return True, ""


# ═════════════════════════════════════════════════════════════
# Main loop
# ══════════════════════════════════════════════════════���══════
def main(config_paths: Optional[list[str]] = None, exclude: Optional[list[str]] = None) -> bool:
    """
    Run all instruments in a single event loop.
    Returns True to signal reconnect, False for clean exit.
    """
    # ── Discover configs ─────────────────────────────────────
    if config_paths:
        cfg_files = [Path(p) for p in config_paths]
    else:
        cfg_files = sorted(CONFIGS_DIR.glob("*_paper_v1.json"))

    if not cfg_files:
        log.error("No config files found!")
        return False

    exclude_set = set(e.lower() for e in (exclude or []))

    # ── Banner ───────────────────────────────────────────────
    log.info("=" * 70)
    log.info("ARGUS Unified Multi-Instrument Runner")
    log.info(f"Gateway: {IBKR_HOST}:{IBKR_PORT}  clientId={IBKR_CLIENT_ID}")
    log.info(f"Configs: {len(cfg_files)} files discovered")
    log.info("=" * 70)

    # ── Connect ──────────────────────────────────────────────
    ib = IB()
    try:
        ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=15)
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return True

    accounts = ib.managedAccounts()
    log.info(f"Connected. Accounts: {accounts}")

    # Request delayed data fallback (needed for futures outside RTH)
    ib.reqMarketDataType(3)

    # ── Build instrument runners ────────���────────────────────
    instruments: list[InstrumentRunner] = []
    skipped = 0

    for cfg_path in cfg_files:
        try:
            cfg = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Skipping {cfg_path.name}: {e}")
            skipped += 1
            continue

        sym = cfg.get("symbol", "???")

        # Apply exclusion filter
        if sym.lower() in exclude_set or cfg_path.stem.lower() in exclude_set:
            log.info(f"  SKIP {sym} (excluded)")
            skipped += 1
            continue

        # Skip non-instrument files (e.g. hashes.json)
        if "instrument_type" not in cfg:
            continue

        try:
            contract = create_contract(cfg)
        except Exception as e:
            log.warning(f"  SKIP {sym}: contract creation failed: {e}")
            skipped += 1
            continue

        # Qualify contract
        try:
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                log.warning(f"  SKIP {sym}: qualification returned empty")
                skipped += 1
                continue
            contract = qualified[0]
        except Exception as e:
            log.warning(f"  SKIP {sym}: qualification failed: {e}")
            skipped += 1
            continue

        # Subscribe to market data
        try:
            ticker = ib.reqMktData(contract, genericTickList="", snapshot=False)
        except Exception as e:
            log.warning(f"  SKIP {sym}: market data subscription failed: {e}")
            skipped += 1
            continue

        ldir = log_dir_for(cfg)
        runner = InstrumentRunner(cfg, contract, ticker, ldir, str(cfg_path))
        runner.state.load(runner.label)

        # Cohort tracking fields
        cfg_content = cfg_path.read_text()
        runner._config_hash = hashlib.sha256(cfg_content.encode()).hexdigest()[:16]
        runner._session_id = getattr(main, '_session_id', str(uuid.uuid4())[:8])
        runner._runtime_start = getattr(main, '_runtime_start', time.time())
        runner._reconnected = getattr(main, '_is_reconnect', False)
        runner._git_sha = getattr(main, '_git_sha', 'unknown')

        instruments.append(runner)

        itype = cfg.get("instrument_type", "?")
        risk = cfg.get("risk", {})
        if itype == "forex":
            risk_str = f"SL={risk.get('stop_pips', '?')}pip TP={risk.get('target_pips', '?')}pip"
        else:
            risk_str = f"SL={risk.get('stop_bps', '?')}bps TP={risk.get('target_bps', '?')}bps"
        log.info(f"  OK {sym:8s} [{itype:6s}] {cfg.get('strategy', '?'):20s} {risk_str}  -> {contract}")

    if not instruments:
        log.error("No instruments loaded! Check configs and IBKR connection.")
        ib.disconnect()
        return False

    # Check for duplicate symbols (would cause log/state dir collision)
    seen_syms = {}
    for inst in instruments:
        sym = inst.symbol.lower()
        if sym in seen_syms:
            log.error(f"FATAL: duplicate symbol '{inst.symbol}' — configs '{seen_syms[sym]}' and '{inst.config_path}' would share log dir")
            ib.disconnect()
            return False
        seen_syms[sym] = inst.config_path

    log.info(f"Loaded {len(instruments)} instruments ({skipped} skipped)")
    log.info("-" * 70)

    # -- Broker reconciliation gate ------------------------------------
    log.info("Running broker reconciliation...")
    runtime_mode = RuntimeMode.RECONCILING
    recon_results = reconcile_instruments(ib, instruments)
    runtime_mode = _determine_runtime_mode(recon_results)
    log.info(f"Reconciliation complete. Runtime mode: {runtime_mode}")

    if runtime_mode == RuntimeMode.RECOVERY_REQUIRED:
        log.error("RECOVERY REQUIRED: unresolved broker mismatch detected")
        log.error("New entries BLOCKED until manual review. Monitor will continue.")
        # Don't exit -- keep running for monitoring, but block entries
        for inst in instruments:
            inst._entries_blocked = True
    else:
        for inst in instruments:
            inst._entries_blocked = False

    # -- Portfolio risk manager ----------------------------------------
    risk_mgr = PortfolioRiskManager(
        max_same_currency=3,
        max_drawdown_pct=0.03,
        daily_max_loss=3.0,           # per instrument: 3R/day (3 full stop-losses)
        portfolio_daily_max_loss=10.0, # fleet-wide: 10R/day total across all instruments
    )
    for inst in instruments:
        inst._risk_mgr = risk_mgr
        inst._all_instruments = instruments  # reference for correlation checks

    # -- Seed historical data -----------------------------------------
    log.info("Seeding historical bars...")
    for inst in instruments:
        inst.seed(ib)
        ib.sleep(0.5)  # rate-limit historical data requests

    # -- Main loop ---------------------------------------------------���───────────────
    log.info("Starting main loop (Ctrl+C to stop)...")
    heartbeat_interval = 300  # log heartbeat every 5 min
    last_heartbeat = time.time()

    try:
        while True:
            ib.sleep(1)  # process all IB events for all instruments
            now = datetime.now(timezone.utc)

            risk_mgr.update(instruments)

            for inst in instruments:
                # Skip quarantined runners
                if getattr(inst, '_quarantined', False):
                    continue
                try:
                    inst.tick(now)
                    inst._consecutive_errors = 0  # reset on success
                except Exception as e:
                    inst._consecutive_errors = getattr(inst, '_consecutive_errors', 0) + 1
                    inst._log.error(f"Tick error ({inst._consecutive_errors}x): {e}")
                    if inst._consecutive_errors >= 10:
                        inst._log.error(f"QUARANTINED after {inst._consecutive_errors} consecutive errors")
                        inst._quarantined = True
                        runtime_mode = RuntimeMode.DEGRADED
                        _write_incident(inst, "QUARANTINED", f"{inst._consecutive_errors} consecutive tick errors",
                                        inst.state.position, {"direction": "unknown", "qty": 0})

            # Periodic broker reconciliation (every 5 min, same key logic as startup)
            if time.time() - last_heartbeat > heartbeat_interval and ib.isConnected():
                try:
                    # Use same normalized key as startup reconciliation
                    broker_positions = {}
                    for p in ib.positions():
                        key = _normalize_ib_key(p.contract)
                        broker_positions[key] = float(p.position)
                    for inst in instruments:
                        ib_key = _runner_to_ib_key(inst)
                        bp = broker_positions.get(ib_key, 0)
                        local_pos = inst.state.position
                        broker_flat = (bp == 0)
                        local_flat = (local_pos == "FLAT")
                        if broker_flat != local_flat:
                            log.warning(
                                f"RECON_DRIFT: {inst.symbol} local={local_pos} "
                                f"broker_qty={bp} key={ib_key} — mismatch detected"
                            )
                            _write_incident(inst, "RECON_DRIFT",
                                            f"local={local_pos} broker_qty={bp}",
                                            local_pos, {"direction": "unknown", "qty": bp})
                except Exception as e:
                    log.warning(f"Periodic reconciliation failed: {e}")

            # Periodic heartbeat (log + per-instrument heartbeat files)
            if time.time() - last_heartbeat > heartbeat_interval:
                last_heartbeat = time.time()
                positions = [
                    f"{i.symbol}={i.state.position}"
                    for i in instruments
                    if i.state.position != "FLAT"
                ]
                flat_count = sum(1 for i in instruments if i.state.position == "FLAT")
                pos_str = ", ".join(positions) if positions else "all FLAT"
                log.info(
                    f"HEARTBEAT | {len(instruments)} instruments | "
                    f"{flat_count} flat | {pos_str}"
                )
                # Write per-instrument heartbeat files (used by position_monitor)
                for inst in instruments:
                    hb_file = inst.log_dir / "heartbeat.json"
                    try:
                        hb_file.write_text(json.dumps({
                            "ts": now.isoformat(),
                            "pid": os.getpid(),
                            "session_id": getattr(inst, '_session_id', ''),
                            "instrument": inst.symbol,
                            "runtime_mode": runtime_mode,
                            "position": inst.state.position,
                            "entries_blocked": getattr(inst, '_entries_blocked', False),
                            "quarantined": getattr(inst, '_quarantined', False),
                            "consecutive_errors": getattr(inst, '_consecutive_errors', 0),
                            "reconciliation": getattr(inst, '_reconciliation', ''),
                            "last_bar_ts": str(inst.current_bar_minute) if inst.current_bar_minute else None,
                            "broker_connected": ib.isConnected(),
                        }))
                    except Exception:
                        pass

    except KeyboardInterrupt:
        log.info("Shutting down (Ctrl+C)...")
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        _log_summary(instruments)
        return False  # clean exit

    except (ConnectionError, OSError, asyncio.CancelledError) as e:
        log.warning(f"Connection lost: {e}. Will reconnect...")
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return True  # reconnect

    except (TypeError, ValueError, KeyError, AttributeError, IndexError) as e:
        # Logic/programming errors — do NOT reconnect, fail loudly
        log.critical(f"CODE DEFECT (not a connection issue): {type(e).__name__}: {e}", exc_info=True)
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return False  # do NOT reconnect — fix the bug

    except Exception as e:
        log.error(f"Unexpected error (will reconnect): {e}", exc_info=True)
        for inst in instruments:
            inst.state.save()
        try:
            ib.disconnect()
        except Exception:
            pass
        return True  # reconnect


def _log_summary(instruments: list[InstrumentRunner]) -> None:
    """Print final summary of all instruments."""
    log.info("=" * 70)
    log.info("SESSION SUMMARY")
    log.info(f"{'Symbol':<10} {'Strategy':<20} {'Trades':>6} {'PnL':>12}")
    log.info("-" * 70)
    for inst in instruments:
        s = inst.state
        if inst.uses_pips:
            pnl_str = f"{s.pnl_pips:+.1f} pip"
        else:
            pnl_str = f"{s.pnl_points:+.2f} pts"
        log.info(f"{inst.symbol:<10} {inst.strategy:<20} {s.trade_count:>6} {pnl_str:>12}")
    log.info("=" * 70)


# ═════════════════════════════════════════════════════════════
# Auto-reconnect wrapper
# ═════════════════════════════════════════════════════════════
def run_with_reconnect(
    config_paths: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> None:
    """Outer loop: reconnect on connection failures with exponential backoff."""
    max_retries = 200
    retry_delay = 10.0

    # Generate session/cohort tracking once for entire session
    main._session_id = str(uuid.uuid4())[:8]
    main._runtime_start = time.time()
    main._is_reconnect = False

    # Capture git sha for cohort auditing
    import subprocess as _sp
    try:
        main._git_sha = _sp.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=_sp.DEVNULL, text=True
        ).strip()
    except Exception:
        main._git_sha = "unknown"

    for attempt in range(max_retries):
        if attempt > 0:
            log.info(f"Reconnect attempt {attempt}/{max_retries} in {retry_delay:.0f}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 120)  # cap at 2 min
            # DESIGN DOCTRINE (2026-03-25): _is_reconnect is intentionally False here.
            # Bar buffer rebuilds from scratch on reconnect (300 bars seeded).
            # Only the trade open DURING disconnect is tainted (via restored_from_file).
            # Post-reconnect entries on fresh bar data are valid by design.
            # Previous session-wide tainting killed ALL trades after any single blip,
            # making it impossible to accumulate 30 valid trades for promotion.
            # See COHORT_SPEC.md "Trade-scoped with session reset" for full rationale.
            main._session_id = str(uuid.uuid4())[:8]
            main._runtime_start = time.time()
            main._is_reconnect = False
            log.info(f"New session after reconnect: {main._session_id}")
        else:
            retry_delay = 10.0

        should_reconnect = main(config_paths=config_paths, exclude=exclude)

        if should_reconnect is False:
            break  # clean exit
        if should_reconnect is True:
            log.info("Preparing to reconnect...")
            continue

    log.info("Unified runner stopped.")


# ═════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════
def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Argus Unified Multi-Instrument Runner — single process, single connection",
    )
    parser.add_argument(
        "--configs", nargs="*", default=None,
        help="Specific config file paths (default: all *_paper_v1.json in configs dir)",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=None,
        help="Symbols or config stems to exclude (e.g., eth_range btc_range sol)",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="IBKR client ID (default: from IBKR_CLIENT_ID env or 1). Use different IDs for parallel runners.",
    )
    args = parser.parse_args()
    if args.client_id is not None:
        global IBKR_CLIENT_ID
        IBKR_CLIENT_ID = args.client_id
    run_with_reconnect(config_paths=args.configs, exclude=args.exclude)


if __name__ == "__main__":
    cli()