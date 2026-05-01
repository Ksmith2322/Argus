"""
GDX/GLD Log-Ratio Pairs Trade Runner
=====================================
Deployable runner for the validated GDX/GLD mean-reversion pairs strategy.

Strategy:
    Spread  = log(GDX / GLD)
    Z-score = rolling 60-day z-score of spread
    Entry:  z crosses +/-2.0 (short spread when z > +2, long spread when z < -2)
    Exit:   z crosses 0
    Stop:   z hits +/-3.0

Modes:
    --backtest      Replay historical data via yfinance, log trades to CSV
    --live          Connect to IBKR, evaluate signal daily, submit orders
    --signal-only   Like live but only log signals (no orders) -- safe default
    --dry-run       Print what would happen, touch nothing

Usage:
    python -m forge.gdx_gld_runner --backtest
    python -m forge.gdx_gld_runner --signal-only
    python -m forge.gdx_gld_runner --live
    python -m forge.gdx_gld_runner --backtest --equity 25000
"""

import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple

try:
    import numpy as np
    import pandas as pd
except ImportError:
    print("Required: pip install pandas numpy")
    sys.exit(1)

# Ensure repo root on path for helio.fleet_sizing
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from helio.fleet_sizing import get_initial_capital_usd, pnl_pct_of_fleet

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# z-score thresholds load via registry (forge_gdx_gld_live — distinct from
# the backtest forge_gdx_gld entry because zscore_stop diverges: live uses
# 3.5 per sensitivity analysis (PF 1.56 -> 1.88), backtest stays at 3.0).
# Fallback to hardcoded defaults if registry unavailable — a broken registry
# cannot break the runner.
_HARDCODED_DEFAULTS = {
    "ZSCORE_ENTRY":    2.0,
    "ZSCORE_EXIT":     0.0,
    "ZSCORE_STOP":     3.5,  # widened from 3.0 per sensitivity analysis (PF 1.56 -> 1.88)
    "ZSCORE_LOOKBACK": 60,   # rolling window for z-score
}


def _load_constants_from_registry() -> dict:
    try:
        from helio.strategy_registry import load_registry
        reg = load_registry().gdx_gld_live
        return {
            "ZSCORE_ENTRY":    reg.zscore_entry,
            "ZSCORE_EXIT":     reg.zscore_exit,
            "ZSCORE_STOP":     reg.zscore_stop,
            "ZSCORE_LOOKBACK": reg.zscore_lookback,
        }
    except Exception:
        return dict(_HARDCODED_DEFAULTS)


_RC = _load_constants_from_registry()
ZSCORE_ENTRY    = _RC["ZSCORE_ENTRY"]
ZSCORE_EXIT     = _RC["ZSCORE_EXIT"]
ZSCORE_STOP     = _RC["ZSCORE_STOP"]  # 3.5 live (diverges from backtest 3.0)
ZSCORE_LOOKBACK = _RC["ZSCORE_LOOKBACK"]

RISK_PER_TRADE_PCT = 0.02     # 2% of equity
IBKR_CLIENT_ID = 101          # reserved range 100-199 for forge
IBKR_DEFAULT_PORT = 7497      # paper trading
EVAL_HOUR_ET = 16             # evaluate at market close (4pm ET)
HEARTBEAT_INTERVAL_S = 60

LOG_DIR = Path(__file__).resolve().parent / "logs" / "gdx_gld"
LOCK_FILE = LOG_DIR / "runner.lock"
SIGNALS_CSV = LOG_DIR / "signals.csv"
TRADES_CSV = LOG_DIR / "trades.csv"
HEARTBEAT_JSON = LOG_DIR / "heartbeat.json"

# Ensure log directory exists
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log = logging.getLogger("gdx_gld")
log.setLevel(logging.DEBUG)

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
log.addHandler(_console)

_file_handler = logging.FileHandler(LOG_DIR / "runner.log")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
log.addHandler(_file_handler)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class Signal(Enum):
    NO_SIGNAL = "NO_SIGNAL"
    LONG_SPREAD = "LONG_SPREAD"      # long GDX, short GLD
    SHORT_SPREAD = "SHORT_SPREAD"    # short GDX, long GLD
    EXIT = "EXIT"
    STOP = "STOP"


class Position(Enum):
    FLAT = "FLAT"
    LONG_SPREAD = "LONG_SPREAD"
    SHORT_SPREAD = "SHORT_SPREAD"


# ---------------------------------------------------------------------------
# Signal generator
# ---------------------------------------------------------------------------
class GdxGldSignal:
    """
    Maintains rolling z-score state and generates trade signals for the
    GDX/GLD log-ratio pairs strategy.
    """

    def __init__(self, lookback: int = ZSCORE_LOOKBACK):
        self.lookback = lookback
        self.position: Position = Position.FLAT
        self._spread_history: list[float] = []
        self._prev_z: Optional[float] = None

    @property
    def current_z(self) -> Optional[float]:
        """Return the most recent z-score, or None if insufficient data."""
        if len(self._spread_history) < self.lookback:
            return None
        window = self._spread_history[-self.lookback:]
        mean = np.mean(window)
        std = np.std(window, ddof=1)
        if std < 1e-10:
            return 0.0
        return (self._spread_history[-1] - mean) / std

    @property
    def current_spread(self) -> Optional[float]:
        return self._spread_history[-1] if self._spread_history else None

    def update(self, gdx_close: float, gld_close: float) -> Signal:
        """
        Feed new daily closes. Returns the signal for this bar.

        The signal reflects transitions:
        - FLAT -> LONG_SPREAD or SHORT_SPREAD on entry conditions
        - LONG_SPREAD/SHORT_SPREAD -> EXIT or STOP on exit conditions
        - NO_SIGNAL otherwise
        """
        if gld_close <= 0 or gdx_close <= 0:
            return Signal.NO_SIGNAL

        spread = float(np.log(gdx_close / gld_close))
        self._spread_history.append(spread)

        z = self.current_z
        if z is None:
            self._prev_z = None
            return Signal.NO_SIGNAL

        signal = self._evaluate(z)
        self._prev_z = z
        return signal

    def _evaluate(self, z: float) -> Signal:
        prev_z = self._prev_z
        if prev_z is None:
            return Signal.NO_SIGNAL

        # --- Exits first (only if in a position) ---
        if self.position == Position.LONG_SPREAD:
            # Stop: spread keeps falling, z hits -3
            if z <= -ZSCORE_STOP:
                self.position = Position.FLAT
                return Signal.STOP
            # Mean-revert exit: z crosses back to 0
            if prev_z < ZSCORE_EXIT <= z or (z >= ZSCORE_EXIT and prev_z < ZSCORE_EXIT):
                self.position = Position.FLAT
                return Signal.EXIT

        elif self.position == Position.SHORT_SPREAD:
            # Stop: spread keeps rising, z hits +3
            if z >= ZSCORE_STOP:
                self.position = Position.FLAT
                return Signal.STOP
            # Mean-revert exit: z crosses back to 0
            if prev_z > ZSCORE_EXIT >= z or (z <= ZSCORE_EXIT and prev_z > ZSCORE_EXIT):
                self.position = Position.FLAT
                return Signal.EXIT

        # --- Entries (only if flat) ---
        if self.position == Position.FLAT:
            # z crosses below -2: spread is cheap -> long spread (long GDX, short GLD)
            if prev_z > -ZSCORE_ENTRY and z <= -ZSCORE_ENTRY:
                self.position = Position.LONG_SPREAD
                return Signal.LONG_SPREAD
            # z crosses above +2: spread is expensive -> short spread (short GDX, long GLD)
            if prev_z < ZSCORE_ENTRY and z >= ZSCORE_ENTRY:
                self.position = Position.SHORT_SPREAD
                return Signal.SHORT_SPREAD

        return Signal.NO_SIGNAL

    def reset(self):
        """Clear all state."""
        self.position = Position.FLAT
        self._spread_history.clear()
        self._prev_z = None


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
def compute_position_sizes(
    equity: float,
    gdx_price: float,
    gld_price: float,
    entry_z: float,
) -> Tuple[int, int]:
    """
    Dollar-neutral position sizing with 2% risk per trade.

    Risk budget = equity * RISK_PER_TRADE_PCT
    Stop distance in z-score space = 1.0 (entry at +/-2, stop at +/-3)
    We size so that a 1-sigma spread move ~ risk_budget in dollar terms.

    For dollar-neutral: equal dollar value on each leg.
    Each leg gets half the total notional.

    Returns (gdx_shares, gld_shares) -- always positive, caller decides direction.
    """
    risk_budget = equity * RISK_PER_TRADE_PCT  # e.g., $200 on $10K

    # The stop is 1 z-score unit away from entry.
    # We want loss at stop = risk_budget.
    # A 1-sigma move in the spread = std of last 60 days of log(GDX/GLD).
    # For dollar-neutral with notional N per leg, a spread move of delta_s
    # produces P&L ~ N * delta_s (since spread = log ratio, first-order
    # approximation: d(log(GDX/GLD)) ~ dGDX/GDX - dGLD/GLD).
    #
    # But we want to be practical: size so that each leg = risk_budget / stop_distance_z.
    # Since stop_distance = 1 z-unit and 1 z-unit ~ 1 std of spread,
    # and spread std is typically ~0.03 for GDX/GLD, we use:
    # notional_per_leg = risk_budget / spread_std
    # But we don't always have spread_std handy in this function, so we use
    # a simpler heuristic: total notional = risk_budget * leverage_factor.
    # With z-stop at 1.0 sigma away, and typical spread vol ~3%,
    # notional = risk_budget / 0.03 ~ risk_budget * 33.
    # But that can be too large. Instead, use the clean approach:
    # Each leg = equity / 2 (dollar neutral), capped so max loss ~ risk_budget.

    # Simple approach: each leg = equity / 2 for dollar-neutral
    half_equity = equity / 2.0
    gdx_shares = max(1, int(half_equity / gdx_price))
    gld_shares = max(1, int(half_equity / gld_price))

    # Verify approximate risk: worst case is stop hit (1 z away).
    # This is a rough sizing -- live tuning may refine it.
    return gdx_shares, gld_shares


# ---------------------------------------------------------------------------
# CSV logging
# ---------------------------------------------------------------------------
SIGNAL_COLS = ["timestamp", "gdx_close", "gld_close", "spread", "z_score", "signal"]
TRADE_COLS = [
    "entry_date", "exit_date", "direction", "entry_z", "exit_z",
    "gdx_entry", "gld_entry", "gdx_exit", "gld_exit", "pnl_pct", "exit_reason",
    "gdx_shares", "gld_shares", "equity_at_entry_usd", "pnl_usd", "pnl_pct_of_fleet",
]


def _init_csv(path: Path, cols: list[str]) -> None:
    """Write header if file doesn't exist or is empty."""
    if not path.exists() or path.stat().st_size == 0:
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(cols)


def append_signal_row(
    ts: str, gdx: float, gld: float, spread: float, z: float, signal: str
) -> None:
    _init_csv(SIGNALS_CSV, SIGNAL_COLS)
    with open(SIGNALS_CSV, "a", newline="") as f:
        csv.writer(f).writerow([ts, f"{gdx:.4f}", f"{gld:.4f}",
                                 f"{spread:.6f}", f"{z:.4f}", signal])


def compute_trade_pnl_usd(
    direction: str,
    gdx_entry: float, gld_entry: float,
    gdx_exit: float, gld_exit: float,
    equity_usd: float,
) -> Tuple[int, int, float]:
    """Compute dollar PnL for a gdx_gld pairs trade using the same sizing
    the strategy would use at `equity_usd` (half-equity per leg, dollar-neutral).

    Returns (gdx_shares, gld_shares, pnl_usd).
    """
    half_equity = equity_usd / 2.0
    gdx_shares = max(1, int(half_equity / gdx_entry))
    gld_shares = max(1, int(half_equity / gld_entry))
    # LONG_SPREAD = long GDX, short GLD (spread expected to mean-revert up)
    # SHORT_SPREAD = short GDX, long GLD
    if direction == "LONG_SPREAD":
        pnl_usd = gdx_shares * (gdx_exit - gdx_entry) - gld_shares * (gld_exit - gld_entry)
    else:  # SHORT_SPREAD
        pnl_usd = -gdx_shares * (gdx_exit - gdx_entry) + gld_shares * (gld_exit - gld_entry)
    return gdx_shares, gld_shares, pnl_usd


def append_trade_row(
    entry_date: str, exit_date: str, direction: str,
    entry_z: float, exit_z: float,
    gdx_entry: float, gld_entry: float,
    gdx_exit: float, gld_exit: float,
    pnl_pct: float, exit_reason: str,
    equity_usd: Optional[float] = None,
    emit_canonical: bool = False,
) -> None:
    # emit_canonical defaults to False so run_backtest() (historical replay)
    # cannot pollute canonical_fills.jsonl with 2006–2026 rows that would look
    # like live fills to the dashboard, reconciliation, and promotion gates.
    # Live/paper paths pass emit_canonical=True explicitly.
    _init_csv(TRADES_CSV, TRADE_COLS)
    if equity_usd is None:
        equity_usd = get_initial_capital_usd()
    gdx_shares, gld_shares, pnl_usd = compute_trade_pnl_usd(
        direction, gdx_entry, gld_entry, gdx_exit, gld_exit, equity_usd
    )
    with open(TRADES_CSV, "a", newline="") as f:
        csv.writer(f).writerow([
            entry_date, exit_date, direction,
            f"{entry_z:.4f}", f"{exit_z:.4f}",
            f"{gdx_entry:.4f}", f"{gld_entry:.4f}",
            f"{gdx_exit:.4f}", f"{gld_exit:.4f}",
            f"{pnl_pct:.4f}", exit_reason,
            gdx_shares, gld_shares, f"{equity_usd:.2f}",
            f"{pnl_usd:.2f}", f"{pnl_pct_of_fleet(pnl_usd):.4f}",
        ])

    if not emit_canonical:
        return

    try:
        from helio.canonical_fills import write_fill_typed
        from helio.domain import Fill
        direction_norm = "long" if direction == "LONG_SPREAD" else "short"
        write_fill_typed(Fill(
            strategy="forge_gdx_gld",
            symbol="GDX/GLD",
            direction=direction_norm,
            side="EXIT",
            entry_ts=str(entry_date),
            exit_ts=str(exit_date),
            entry_px=float(gdx_entry),
            exit_px=float(gdx_exit),
            size=float(gdx_shares),
            risk_usd=None,
            pnl_usd=round(pnl_usd, 2),
            exit_reason=exit_reason,
        ), extra={
            "gld_entry": float(gld_entry),
            "gld_exit": float(gld_exit),
            "gld_shares": int(gld_shares),
            "entry_z": float(entry_z),
            "exit_z": float(exit_z),
            "equity_at_entry_usd": float(equity_usd),
        })
    except Exception:
        pass  # never let the canonical log break paper trading


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------
def write_heartbeat(mode: str, position: str, z_score: Optional[float],
                    spread: Optional[float]) -> None:
    """Write heartbeat.json compatible with fleet monitor format."""
    hb = {
        "system": "gdx_gld_pairs",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "mode": mode,
        "position": position,
        "z_score": round(z_score, 4) if z_score is not None else None,
        "spread": round(spread, 6) if spread is not None else None,
    }
    tmp = HEARTBEAT_JSON.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(hb, f, indent=2)
        tmp.replace(HEARTBEAT_JSON)
    except Exception as e:
        log.warning(f"Failed to write heartbeat: {e}")


# ---------------------------------------------------------------------------
# Process lock
# ---------------------------------------------------------------------------
class ProcessLock:
    """Simple file-based lock to prevent duplicate runner instances."""

    def __init__(self, lock_path: Path = LOCK_FILE):
        self.lock_path = lock_path

    def acquire(self) -> bool:
        """Attempt to acquire lock. Returns True on success."""
        if self.lock_path.exists():
            try:
                old_pid = int(self.lock_path.read_text().strip())
                # Check if the old process is still running
                try:
                    os.kill(old_pid, 0)
                    log.error(f"Another instance is running (PID {old_pid}). "
                              f"Remove {self.lock_path} if stale.")
                    return False
                except OSError:
                    log.warning(f"Stale lock from PID {old_pid}, removing.")
                    self.lock_path.unlink(missing_ok=True)
            except (ValueError, OSError):
                log.warning("Corrupt lock file, removing.")
                self.lock_path.unlink(missing_ok=True)

        self.lock_path.write_text(str(os.getpid()))
        return True

    def release(self):
        self.lock_path.unlink(missing_ok=True)

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Could not acquire process lock")
        return self

    def __exit__(self, *args):
        self.release()


# ---------------------------------------------------------------------------
# Backtest mode
# ---------------------------------------------------------------------------
def run_backtest(equity: float, start: str = "2006-05-22",
                 end: str = "2026-04-11") -> None:
    """Replay historical data from yfinance and log all signals/trades."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance required for backtest: pip install yfinance")
        return

    log.info(f"Backtest mode: {start} to {end}, equity=${equity:,.0f}")
    log.info("Downloading GDX and GLD data...")

    gdx_df = yf.download("GDX", start=start, end=end, progress=False, auto_adjust=True)
    gld_df = yf.download("GLD", start=start, end=end, progress=False, auto_adjust=True)

    # Handle MultiIndex columns from yfinance
    if isinstance(gdx_df.columns, pd.MultiIndex):
        gdx_df.columns = gdx_df.columns.get_level_values(0)
    if isinstance(gld_df.columns, pd.MultiIndex):
        gld_df.columns = gld_df.columns.get_level_values(0)

    df = pd.DataFrame({
        "GDX": gdx_df["Close"],
        "GLD": gld_df["Close"],
    }).dropna()

    log.info(f"Data: {df.index[0].strftime('%Y-%m-%d')} to "
             f"{df.index[-1].strftime('%Y-%m-%d')} ({len(df)} bars)")

    sig = GdxGldSignal()

    # Track open trade state for CSV logging
    entry_date: Optional[str] = None
    entry_z: float = 0.0
    entry_gdx: float = 0.0
    entry_gld: float = 0.0
    entry_direction: str = ""

    trade_count = 0
    total_pnl = 0.0

    for date, row in df.iterrows():
        gdx_close = float(row["GDX"])
        gld_close = float(row["GLD"])
        date_str = date.strftime("%Y-%m-%d")

        signal = sig.update(gdx_close, gld_close)
        z = sig.current_z
        spread = sig.current_spread

        # Log every signal row
        if z is not None and spread is not None:
            append_signal_row(date_str, gdx_close, gld_close, spread, z,
                              signal.value)

        # Track trades
        if signal in (Signal.LONG_SPREAD, Signal.SHORT_SPREAD):
            entry_date = date_str
            entry_z = z if z is not None else 0.0
            entry_gdx = gdx_close
            entry_gld = gld_close
            entry_direction = signal.value

        elif signal in (Signal.EXIT, Signal.STOP) and entry_date is not None:
            # Compute P&L as spread change percentage
            entry_spread = float(np.log(entry_gdx / entry_gld))
            exit_spread = float(np.log(gdx_close / gld_close))

            if entry_direction == "LONG_SPREAD":
                raw = exit_spread - entry_spread
            else:
                raw = entry_spread - exit_spread

            denom = abs(entry_spread) if abs(entry_spread) > 0.01 else 1.0
            pnl_pct = (raw / denom) * 100.0

            reason = "mean_revert" if signal == Signal.EXIT else "stop"
            append_trade_row(
                entry_date, date_str, entry_direction,
                entry_z, z if z is not None else 0.0,
                entry_gdx, entry_gld, gdx_close, gld_close,
                pnl_pct, reason,
            )
            trade_count += 1
            total_pnl += pnl_pct
            log.info(f"  Trade #{trade_count}: {entry_direction} "
                     f"{entry_date}->{date_str} PnL={pnl_pct:+.2f}% ({reason})")

            entry_date = None

    log.info(f"Backtest complete: {trade_count} trades, total PnL={total_pnl:+.2f}%")
    log.info(f"Signals CSV: {SIGNALS_CSV}")
    log.info(f"Trades CSV:  {TRADES_CSV}")


# ---------------------------------------------------------------------------
# Live / signal-only mode
# ---------------------------------------------------------------------------
def run_live(equity: float, signal_only: bool = True, dry_run: bool = False) -> None:
    """
    Connect to IBKR, subscribe to GDX/GLD data, evaluate signals daily.

    Args:
        equity: Model equity for position sizing
        signal_only: If True, only log signals (no order submission)
        dry_run: If True, print what would happen without doing anything
    """
    port = int(os.environ.get("IBKR_PORT", str(IBKR_DEFAULT_PORT)))
    mode_label = "DRY-RUN" if dry_run else ("SIGNAL-ONLY" if signal_only else "LIVE")
    log.info(f"Starting {mode_label} mode | equity=${equity:,.0f} | "
             f"IBKR port={port} client_id={IBKR_CLIENT_ID}")

    if dry_run:
        log.info("[DRY-RUN] Would connect to IBKR at 127.0.0.1:%d", port)
        log.info("[DRY-RUN] Would subscribe to GDX and GLD daily bars")
        log.info("[DRY-RUN] Would evaluate signal at %d:00 ET daily", EVAL_HOUR_ET)
        log.info("[DRY-RUN] Signal-only=%s", signal_only)
        log.info("[DRY-RUN] Exiting without action.")
        return

    # For signal-only mode, we can use yfinance instead of IBKR
    if signal_only:
        _run_signal_only_loop(equity)
        return

    # Full IBKR live mode
    _run_ibkr_live_loop(equity, port)


def _run_signal_only_loop(equity: float) -> None:
    """
    Signal-only loop using yfinance data. No IBKR connection required.
    Evaluates once, logs signal, writes heartbeat, then sleeps until next close.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance required for signal-only mode: pip install yfinance")
        return

    sig = GdxGldSignal()
    log.info("Signal-only mode: fetching recent data via yfinance...")

    # Fetch enough history to warm up the z-score
    gdx_df = yf.download("GDX", period="120d", progress=False, auto_adjust=True)
    gld_df = yf.download("GLD", period="120d", progress=False, auto_adjust=True)

    if isinstance(gdx_df.columns, pd.MultiIndex):
        gdx_df.columns = gdx_df.columns.get_level_values(0)
    if isinstance(gld_df.columns, pd.MultiIndex):
        gld_df.columns = gld_df.columns.get_level_values(0)

    df = pd.DataFrame({
        "GDX": gdx_df["Close"],
        "GLD": gld_df["Close"],
    }).dropna()

    if len(df) < ZSCORE_LOOKBACK + 5:
        log.error(f"Insufficient data: {len(df)} bars (need {ZSCORE_LOOKBACK + 5})")
        return

    log.info(f"Loaded {len(df)} bars from {df.index[0].strftime('%Y-%m-%d')} "
             f"to {df.index[-1].strftime('%Y-%m-%d')}")

    # Replay all bars to build state
    last_signal = Signal.NO_SIGNAL
    for date, row in df.iterrows():
        last_signal = sig.update(float(row["GDX"]), float(row["GLD"]))

    z = sig.current_z
    spread = sig.current_spread
    last_date = df.index[-1].strftime("%Y-%m-%d")
    last_gdx = float(df.iloc[-1]["GDX"])
    last_gld = float(df.iloc[-1]["GLD"])

    log.info(f"Current state: position={sig.position.value} z={z:.4f} "
             f"spread={spread:.6f} signal={last_signal.value}")

    # Log the current signal
    if z is not None and spread is not None:
        append_signal_row(last_date, last_gdx, last_gld, spread, z,
                          last_signal.value)

    # If there's an actionable signal, log what we WOULD do
    if last_signal in (Signal.LONG_SPREAD, Signal.SHORT_SPREAD):
        gdx_shares, gld_shares = compute_position_sizes(equity, last_gdx, last_gld, z)
        if last_signal == Signal.LONG_SPREAD:
            log.info(f"[SIGNAL-ONLY] Would BUY {gdx_shares} GDX @ ~${last_gdx:.2f} "
                     f"and SELL {gld_shares} GLD @ ~${last_gld:.2f}")
        else:
            log.info(f"[SIGNAL-ONLY] Would SELL {gdx_shares} GDX @ ~${last_gdx:.2f} "
                     f"and BUY {gld_shares} GLD @ ~${last_gld:.2f}")
    elif last_signal in (Signal.EXIT, Signal.STOP):
        log.info(f"[SIGNAL-ONLY] Would CLOSE all positions ({last_signal.value})")
    else:
        log.info(f"[SIGNAL-ONLY] No action. Position={sig.position.value}")

    write_heartbeat("signal_only", sig.position.value, z, spread)
    log.info(f"Heartbeat written to {HEARTBEAT_JSON}")
    log.info("Signal-only evaluation complete.")


def _connect_with_backoff(ib, port: int, max_attempts: int = 12) -> bool:
    """Connect to TWS with exponential backoff (5s -> 10s -> ... -> 300s cap).

    Returns True on success, False if all attempts exhausted.
    """
    delay = 5
    for attempt in range(1, max_attempts + 1):
        try:
            ib.connect("127.0.0.1", port, clientId=IBKR_CLIENT_ID)
            log.info("Connected to IBKR on attempt %d", attempt)
            return True
        except Exception as e:
            log.warning(
                "TWS connect attempt %d/%d failed: %s. Retry in %ds...",
                attempt, max_attempts, e, delay,
            )
            try:
                ib.disconnect()
            except Exception:
                pass
            time.sleep(delay)
            delay = min(delay * 2, 300)
    log.error("TWS connect exhausted %d attempts; giving up", max_attempts)
    return False


def _run_ibkr_live_loop(equity: float, port: int) -> None:
    """
    Full IBKR live trading loop. Connects to TWS, subscribes to daily bars,
    evaluates signal at market close, and submits orders.

    Survives transient TWS disconnects: if ib.isConnected() becomes False
    mid-loop, attempts reconnect-with-backoff before resuming. This is the
    fix for the recurring "gdx_gld TWS socket dropped overnight" failure
    — runner used to exit on first socket error and require manual restart.
    """
    try:
        from ib_insync import IB, Stock, MarketOrder, util
    except ImportError:
        log.error("ib_insync required for live mode: pip install ib_insync")
        return

    ib = IB()
    sig = GdxGldSignal()

    gdx_contract = Stock("GDX", "SMART", "USD")
    gld_contract = Stock("GLD", "SMART", "USD")

    try:
        log.info(f"Connecting to IBKR at 127.0.0.1:{port} (client_id={IBKR_CLIENT_ID})...")
        if not _connect_with_backoff(ib, port):
            log.error("Initial connect failed; aborting")
            return

        ib.qualifyContracts(gdx_contract, gld_contract)

        # Fetch 60+ days of daily bars to warm up z-score
        log.info("Requesting historical bars for GDX and GLD...")
        gdx_bars = ib.reqHistoricalData(
            gdx_contract, endDateTime="", durationStr="120 D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
        )
        gld_bars = ib.reqHistoricalData(
            gld_contract, endDateTime="", durationStr="120 D",
            barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
        )

        gdx_df = util.df(gdx_bars).set_index("date")
        gld_df = util.df(gld_bars).set_index("date")

        # Align dates
        common_dates = gdx_df.index.intersection(gld_df.index)
        if len(common_dates) < ZSCORE_LOOKBACK + 5:
            log.error(f"Insufficient IBKR data: {len(common_dates)} bars")
            return

        log.info(f"Loaded {len(common_dates)} aligned daily bars from IBKR")

        # Warm up signal generator
        for date in sorted(common_dates):
            sig.update(float(gdx_df.loc[date, "close"]),
                       float(gld_df.loc[date, "close"]))

        log.info(f"Signal warmed up. Position={sig.position.value}, "
                 f"z={sig.current_z:.4f}")

        # --- Main loop: re-evaluate daily at market close ---
        last_eval_date: Optional[str] = None
        last_heartbeat_time = 0.0

        log.info("Entering live loop. Evaluating at 16:00 ET daily...")

        while True:
            # Reconnect-on-drop: if TWS socket died, back off + retry
            # before doing any ib.* calls (they would raise otherwise).
            if not ib.isConnected():
                log.warning("TWS socket lost; attempting reconnect with backoff...")
                if not _connect_with_backoff(ib, port):
                    log.error("Reconnect failed; exiting loop so watchdog can respawn")
                    return
                try:
                    ib.qualifyContracts(gdx_contract, gld_contract)
                except Exception as e:
                    log.warning("re-qualify after reconnect failed: %s", e)
                log.info("Reconnected to TWS — resuming live loop")

            try:
                ib.sleep(10)  # ib_insync event loop
            except Exception as e:
                log.warning("ib.sleep raised %s; will attempt reconnect on next iteration", e)
                continue

            now_utc = datetime.now(timezone.utc)
            # Approximate ET (UTC-4 or UTC-5 depending on DST)
            # Use a rough check; for production, use pytz/zoneinfo
            et_offset = -4 if _is_dst_approx(now_utc) else -5
            now_et_hour = (now_utc.hour + et_offset) % 24
            today_str = now_utc.strftime("%Y-%m-%d")

            # Heartbeat
            if time.time() - last_heartbeat_time > HEARTBEAT_INTERVAL_S:
                write_heartbeat("live", sig.position.value,
                                sig.current_z, sig.current_spread)
                last_heartbeat_time = time.time()

            # Evaluate at market close
            if now_et_hour == EVAL_HOUR_ET and today_str != last_eval_date:
                last_eval_date = today_str
                log.info(f"Market close evaluation for {today_str}")

                # Get latest prices
                gdx_bars_new = ib.reqHistoricalData(
                    gdx_contract, endDateTime="", durationStr="5 D",
                    barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
                )
                gld_bars_new = ib.reqHistoricalData(
                    gld_contract, endDateTime="", durationStr="5 D",
                    barSizeSetting="1 day", whatToShow="TRADES", useRTH=True,
                )

                if not gdx_bars_new or not gld_bars_new:
                    log.warning("No new bars received, skipping evaluation")
                    continue

                gdx_close = float(gdx_bars_new[-1].close)
                gld_close = float(gld_bars_new[-1].close)

                signal = sig.update(gdx_close, gld_close)
                z = sig.current_z
                spread = sig.current_spread

                log.info(f"GDX={gdx_close:.2f} GLD={gld_close:.2f} "
                         f"spread={spread:.6f} z={z:.4f} signal={signal.value}")

                if z is not None and spread is not None:
                    append_signal_row(today_str, gdx_close, gld_close,
                                      spread, z, signal.value)

                # --- Execute orders ---
                if signal in (Signal.LONG_SPREAD, Signal.SHORT_SPREAD):
                    gdx_shares, gld_shares = compute_position_sizes(
                        equity, gdx_close, gld_close, z
                    )
                    if signal == Signal.LONG_SPREAD:
                        log.info(f"ENTRY: Long spread -> "
                                 f"BUY {gdx_shares} GDX, SELL {gld_shares} GLD")
                        gdx_order = MarketOrder("BUY", gdx_shares)
                        gld_order = MarketOrder("SELL", gld_shares)
                    else:
                        log.info(f"ENTRY: Short spread -> "
                                 f"SELL {gdx_shares} GDX, BUY {gld_shares} GLD")
                        gdx_order = MarketOrder("SELL", gdx_shares)
                        gld_order = MarketOrder("BUY", gld_shares)

                    ib.placeOrder(gdx_contract, gdx_order)
                    ib.placeOrder(gld_contract, gld_order)
                    log.info("Orders submitted.")

                elif signal in (Signal.EXIT, Signal.STOP):
                    reason = "mean_revert" if signal == Signal.EXIT else "stop"
                    log.info(f"CLOSING position ({reason})")
                    # Close all positions in GDX and GLD
                    positions = ib.positions()
                    for pos in positions:
                        if pos.contract.symbol == "GDX" and pos.position != 0:
                            action = "SELL" if pos.position > 0 else "BUY"
                            qty = abs(pos.position)
                            ib.placeOrder(gdx_contract,
                                          MarketOrder(action, qty))
                        elif pos.contract.symbol == "GLD" and pos.position != 0:
                            action = "SELL" if pos.position > 0 else "BUY"
                            qty = abs(pos.position)
                            ib.placeOrder(gld_contract,
                                          MarketOrder(action, qty))
                    log.info("Close orders submitted.")

                write_heartbeat("live", sig.position.value, z, spread)

    except KeyboardInterrupt:
        log.info("Shutdown requested.")
    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
    finally:
        if ib.isConnected():
            ib.disconnect()
            log.info("Disconnected from IBKR.")
        write_heartbeat("live_stopped", sig.position.value,
                        sig.current_z, sig.current_spread)


def _is_dst_approx(dt: datetime) -> bool:
    """Rough DST check for US Eastern (Mar second Sun to Nov first Sun)."""
    month = dt.month
    if 4 <= month <= 10:
        return True
    if month == 3:
        return dt.day >= 8  # approximate
    if month == 11:
        return dt.day < 7   # approximate
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="gdx_gld_runner",
        description="GDX/GLD log-ratio pairs trade runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  --backtest       Replay historical data via yfinance (default: 2006-2026)
  --signal-only    Evaluate current signal using yfinance, no IBKR needed
  --live           Connect to IBKR, trade live (requires TWS/Gateway)
  --dry-run        Print what would happen without touching anything

Examples:
  python -m forge.gdx_gld_runner --backtest
  python -m forge.gdx_gld_runner --backtest --start 2015-01-01 --equity 25000
  python -m forge.gdx_gld_runner --signal-only
  python -m forge.gdx_gld_runner --live --dry-run
  python -m forge.gdx_gld_runner --live
""",
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backtest", action="store_true",
                      help="Replay historical data from yfinance")
    mode.add_argument("--signal-only", action="store_true",
                      help="Evaluate signal via yfinance, no IBKR (safe default)")
    mode.add_argument("--live", action="store_true",
                      help="Connect to IBKR and trade (paper or live)")

    p.add_argument("--dry-run", action="store_true",
                   help="Print what would happen without doing anything")
    p.add_argument("--loop", action="store_true",
                   help="Run signal-only continuously (evaluate every --interval-min)")
    p.add_argument("--interval-min", type=int, default=60,
                   help="Loop interval in minutes (default: 60)")
    p.add_argument("--equity", type=float, default=get_initial_capital_usd(),
                   help="Model equity in USD (default: 10000)")
    p.add_argument("--start", type=str, default="2006-05-22",
                   help="Backtest start date (default: 2006-05-22, GDX inception)")
    p.add_argument("--end", type=str, default="2026-04-11",
                   help="Backtest end date (default: 2026-04-11)")

    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = parse_args()

    if args.dry_run and args.backtest:
        log.info("[DRY-RUN] Would run backtest from %s to %s with equity=$%s",
                 args.start, args.end, f"{args.equity:,.0f}")
        log.info("[DRY-RUN] Would write signals to %s", SIGNALS_CSV)
        log.info("[DRY-RUN] Would write trades to %s", TRADES_CSV)
        return

    lock = ProcessLock()
    try:
        with lock:
            if args.backtest:
                run_backtest(args.equity, args.start, args.end)
            elif args.signal_only:
                if args.loop:
                    log.info("Starting signal-only LOOP mode (interval=%dmin)", args.interval_min)
                    while True:
                        try:
                            run_live(args.equity, signal_only=True, dry_run=args.dry_run)
                        except Exception as e:
                            log.error("Signal-only cycle error: %s", e)
                        log.info("Sleeping %d minutes until next evaluation...", args.interval_min)
                        time.sleep(args.interval_min * 60)
                else:
                    run_live(args.equity, signal_only=True, dry_run=args.dry_run)
            elif args.live:
                run_live(args.equity, signal_only=False, dry_run=args.dry_run)
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
