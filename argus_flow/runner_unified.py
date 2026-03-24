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
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

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
IBKR_CLIENT_ID = 1  # single shared connection

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

    def save(self) -> None:
        self.file.write_text(json.dumps({
            "position": self.position,
            "entry_price": self.entry_price,
            "entry_time": str(self.entry_time) if self.entry_time else None,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "trade_count": self.trade_count,
            "pnl_pips": self.pnl_pips,
            "pnl_points": self.pnl_points,
        }, indent=2))

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
        if d.get("entry_time"):
            try:
                self.entry_time = datetime.fromisoformat(d["entry_time"])
            except (ValueError, TypeError):
                pass
        # Safety: if restored in-position but stops are zero, force FLAT
        if self.position != "FLAT" and (self.stop_price == 0 or self.target_price == 0):
            log.warning(f"[{sym_label}] Restored {self.position} but stop/target=0 — forcing FLAT")
            self.position = "FLAT"
        if self.position != "FLAT":
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

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "dist_from_low": dist_from_low,
        "hour": datetime.now(timezone.utc).hour,
        "price": current_px,
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

    return {
        "range_pct": range_pct,
        "vol_z": vol_z,
        "range_accel": range_accel,
        "vol_burst_z": vol_burst_z,
        "dist_from_low": dist_from_low,
        "hour": datetime.now(timezone.utc).hour,
        "price": current_px,
    }


# ═════════════════════════════════════════════════════════════
# Trigger checks
# ═════════════════════════════════════════════════════════════
def check_trigger_fx(features: dict, cfg: dict) -> Optional[str]:
    """FX trigger: range_pct + range_accel + vol_z + session."""
    trigger = cfg.get("trigger", {})
    if features["range_pct"] < trigger.get("range_pct_min", 0.0012):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    if features.get("vol_z", 0) <= trigger.get("vol_z_min", -999):
        return None
    h = features["hour"]
    if not (trigger.get("session_start_utc", 0) <= h <= trigger.get("session_end_utc", 23)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return "long"


def check_trigger_futures(features: dict, cfg: dict) -> Optional[str]:
    """Futures trigger: vol_burst + range_accel + session."""
    trigger = cfg.get("trigger", {})
    if features.get("vol_burst_z", 0) <= trigger.get("vol_burst_z_min", 1.0):
        return None
    if features["range_accel"] <= trigger.get("range_accel_min", 0.0):
        return None
    h = features["hour"]
    if not (trigger.get("session_start_utc", 13) <= h <= trigger.get("session_end_utc", 20)):
        return None

    dist = features["dist_from_low"]
    direction_cfg = cfg.get("direction", {})
    if dist < direction_cfg.get("dist_long_threshold", 0.4):
        return "long"
    elif dist > direction_cfg.get("dist_short_threshold", 0.6):
        return "short"
    return "long"


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
        t = self.ticker
        vol = getattr(t, "volume", None) or getattr(t, "delayedVolume", None)
        return float(vol) if vol and vol > 0 else 0.0

    # ── CSV helpers ──────────────────────────────────────────
    def _ensure_signal_header(self) -> None:
        if not self.signal_log.exists():
            with open(self.signal_log, "w", newline="") as f:
                w = csv.writer(f)
                if self.instrument_type in ("future", "crypto"):
                    w.writerow([
                        "ts", "price", "range_pct", "vol_z", "range_accel",
                        "vol_burst_z", "dist_from_low", "hour", "direction", "action",
                    ])
                else:
                    w.writerow([
                        "ts", "price", "range_pct", "vol_z", "range_accel",
                        "dist_from_low", "hour", "direction", "action",
                    ])

    def _log_signal(self, features: dict, direction: Optional[str], action: str) -> None:
        self._ensure_signal_header()
        with open(self.signal_log, "a", newline="") as f:
            w = csv.writer(f)
            row = [
                datetime.now(timezone.utc).isoformat(),
                features["price"],
                f"{features['range_pct']:.6f}",
                f"{features['vol_z']:.4f}",
                f"{features['range_accel']:.4f}",
            ]
            if self.instrument_type in ("future", "crypto"):
                row.append(f"{features.get('vol_burst_z', 0):.4f}")
            row += [
                f"{features['dist_from_low']:.4f}",
                features["hour"],
                direction or "",
                action,
            ]
            w.writerow(row)

    def _ensure_trade_header(self) -> None:
        if not self.trade_log.exists():
            with open(self.trade_log, "w", newline="") as f:
                w = csv.writer(f)
                if self.uses_pips:
                    w.writerow([
                        "ts", "direction", "entry_px", "exit_px", "pnl_pips",
                        "exit_reason", "duration_min", "trade_num",
                    ])
                else:
                    w.writerow([
                        "ts", "direction", "entry_px", "exit_px", "pnl_pts",
                        "pnl_usd", "exit_reason", "duration_min", "trade_num",
                    ])

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

        self._ensure_trade_header()
        with open(self.trade_log, "a", newline="") as f:
            w = csv.writer(f)
            if self.uses_pips:
                w.writerow([
                    now.isoformat(), s.position.lower(),
                    f"{s.entry_price:.5f}",
                    f"{exit_price:.5f}", f"{pnl:.2f}",
                    exit_reason, f"{dur:.1f}", s.trade_count,
                ])
            else:
                pnl_usd = pnl * self.multiplier
                # Determine precision: futures use .2f, FX/crypto use .5f
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
                ])
        return pnl

    # ── Feature & trigger dispatch ───────────────────────────
    def _compute_features(self) -> Optional[dict]:
        if self.instrument_type in ("future", "crypto"):
            return compute_features_futures(self.buf)
        return compute_features_fx(self.buf)

    def _check_trigger(self, features: dict) -> Optional[str]:
        if self.instrument_type in ("future", "crypto"):
            return check_trigger_futures(features, self.cfg)
        return check_trigger_fx(features, self.cfg)

    # ── Stop/target computation ──────────────────────────────
    def _compute_stops(self, entry_px: float, direction: str) -> tuple[float, float]:
        """Return (stop_price, target_price) for a new entry."""
        if self.uses_pips:
            stop_dist = self.stop_pips * self.pip_size
            target_dist = self.target_pips * self.pip_size
        else:
            # bps-based
            stop_dist = entry_px * (self.stop_bps / 10000)
            target_dist = entry_px * (self.target_bps / 10000)

        if direction == "long":
            return entry_px - stop_dist, entry_px + target_dist
        else:
            return entry_px + stop_dist, entry_px - target_dist

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
                self.current_bar["volume"] = vol

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
                self._log.info(
                    f"EXIT {s.position} @ {mid} reason={exit_reason} "
                    f"pnl={pnl:+.2f}{unit} total={total:+.2f}{unit} "
                    f"trades={s.trade_count}"
                )
                s.position = "FLAT"
                s.entry_time = None
                s.timeout_time = None
                s.save()
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

        # Min gap between signals
        if direction and s.last_signal_time:
            gap = (now - s.last_signal_time).total_seconds() / 60
            if gap < self.min_gap:
                direction = None

        if direction:
            entry_px = mid
            stop_px, target_px = self._compute_stops(entry_px, direction)

            s.position = direction.upper()
            s.entry_price = entry_px
            s.entry_time = now
            s.stop_price = stop_px
            s.target_price = target_px
            s.timeout_time = now.replace(second=0) + timedelta(minutes=self.timeout_min)
            s.last_signal_time = now
            s.trade_count += 1
            s.direction_str = direction
            s.save()

            self._log_signal(features, direction, "ENTRY")

            extra = ""
            if not self.uses_pips:
                extra = f" vol_burst={features.get('vol_burst_z', 0):.2f}"
            self._log.info(
                f"ENTRY {direction.upper()} @ {entry_px} "
                f"stop={stop_px} target={target_px} "
                f"rng={features['range_pct']:.4f} accel={features['range_accel']:.3f}"
                f"{extra}"
            )
        else:
            # Periodic NO_TRIGGER log every 5 minutes
            if now.minute % 5 == 0 and now.second < 2:
                self._log_signal(features, None, "NO_TRIGGER")

    # ── Seed historical bars ─────────────────────────────────
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
    """Derive per-instrument log directory."""
    sym = cfg["symbol"].lower()
    strategy = cfg.get("strategy", "default")
    # Use symbol as directory (matches existing pattern)
    return LOGS_ROOT / sym


# ═════════════════════════════════════════════════════════════
# Main loop
# ═════════════════════════════════════════════════════════════
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

    # ── Build instrument runners ─────────────────────────────
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

    log.info(f"Loaded {len(instruments)} instruments ({skipped} skipped)")
    log.info("-" * 70)

    # ── Seed historical data ─────────────────────────────────
    log.info("Seeding historical bars...")
    for inst in instruments:
        inst.seed(ib)
        ib.sleep(0.5)  # rate-limit historical data requests

    # ── Main loop ────────────────────────────────────────────
    log.info("Starting main loop (Ctrl+C to stop)...")
    heartbeat_interval = 300  # log heartbeat every 5 min
    last_heartbeat = time.time()

    try:
        while True:
            ib.sleep(1)  # process all IB events for all instruments
            now = datetime.now(timezone.utc)

            for inst in instruments:
                try:
                    inst.tick(now)
                except Exception as e:
                    inst._log.error(f"Tick error: {e}", exc_info=True)

            # Periodic heartbeat
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

    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
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

    for attempt in range(max_retries):
        if attempt > 0:
            log.info(f"Reconnect attempt {attempt}/{max_retries} in {retry_delay:.0f}s...")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 120)  # cap at 2 min
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
    args = parser.parse_args()
    run_with_reconnect(config_paths=args.configs, exclude=args.exclude)


if __name__ == "__main__":
    cli()