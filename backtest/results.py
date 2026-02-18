# backtest/results.py
from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import List, Optional, Dict, Any, Tuple


_BUY_RE = re.compile(r"qty=([0-9.]+)")
_SELL_RE = re.compile(r"realized_trade=([-0-9.]+)")
_FILL_RE = re.compile(r"fill_px=([-0-9.]+)")
_PX_RE = re.compile(r"px=([-0-9.]+)")
_PROCEEDS_RE = re.compile(r"proceeds=([-0-9.]+)")
_EFF_SELL_RE = re.compile(r"eff_sell=([-0-9.]+)")

# Optional block / reason extraction (best-effort, tolerant of message evolution)
_ACTION_RE = re.compile(r"\baction=([A-Z0-9_]+)")
_REASON_RE = re.compile(r"\breason=([^|]+)")
_RISK_BLOCK_RE = re.compile(r"\brisk_blocked_reason=([^|]+)")
_LIQ_RE = re.compile(r"\bliq_([a-zA-Z0-9_]+)=([-0-9.]+|[A-Za-z_]+)")
_GATE_HINT_RE = re.compile(r"\bgate=([A-Z0-9_]+)")


def _safe_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        s = str(x).strip()
        if s == "":
            return Decimal(default)
        return Decimal(s)
    except Exception:
        return Decimal(default)


def _maybe_decimal(x: Any) -> Optional[Decimal]:
    """
    Strict Decimal parse for measurability fields.

    Critical behavior:
      - None / "" / "none"/"null"/"nan" => None (missing)
      - Non-numeric tokens => None (missing)
      - Numeric strings/ints/floats/Decimal => Decimal(...)
    """
    if x is None:
        return None
    if isinstance(x, Decimal):
        return x
    try:
        s = str(x).strip()
        if s == "":
            return None
        sl = s.lower()
        if sl in ("none", "null", "nan", "na", "n/a"):
            return None
        return Decimal(s)
    except Exception:
        return None


def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x)))
        except Exception:
            return default


def _median_dec(xs: List[Decimal]) -> Optional[Decimal]:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2 == 1:
        return ys[mid]
    return (ys[mid - 1] + ys[mid]) / Decimal("2")


def _quantile_dec(xs: List[Decimal], q: float) -> Optional[Decimal]:
    """
    Simple nearest-rank quantile. q in [0,1].
    """
    if not xs:
        return None
    if q <= 0:
        return min(xs)
    if q >= 1:
        return max(xs)
    ys = sorted(xs)
    idx = int(round(q * (len(ys) - 1)))
    idx = max(0, min(len(ys) - 1, idx))
    return ys[idx]


@dataclass
class Trade:
    entry_epoch: int
    entry_px: Decimal
    qty: Decimal
    exit_epoch: Optional[int] = None
    exit_px: Optional[Decimal] = None
    realized_usd: Optional[Decimal] = None

    mfe_pct: Optional[Decimal] = None
    mae_pct: Optional[Decimal] = None

    def is_closed(self) -> bool:
        return self.exit_epoch is not None and self.exit_px is not None and self.realized_usd is not None

    def duration_s(self) -> Optional[int]:
        if self.exit_epoch is None:
            return None
        return max(0, int(self.exit_epoch - self.entry_epoch))

    def notional_entry_usd(self) -> Decimal:
        try:
            n = (self.entry_px or Decimal("0")) * (self.qty or Decimal("0"))
        except Exception:
            n = Decimal("0")
        return n if n > 0 else Decimal("0")

    def realized_return_pct(self) -> Optional[Decimal]:
        if not self.is_closed():
            return None
        denom = self.notional_entry_usd()
        if denom <= 0:
            return None
        return (self.realized_usd or Decimal("0")) / denom * Decimal("100")


@dataclass
class BacktestResults:
    symbol: str
    start_epoch: int
    end_epoch: int

    start_equity: Decimal
    end_equity: Decimal

    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[int, Decimal]] = field(default_factory=list)

    # -------------------------
    # Phase 5 measurability
    # -------------------------
    event_counts: Dict[str, int] = field(default_factory=dict)
    missed_buy_counts: Dict[str, int] = field(default_factory=dict)

    entry_attempts: int = 0
    entry_filled: int = 0
    entry_block_liquidity: int = 0
    entry_block_confluence: int = 0
    entry_block_risk: int = 0
    entry_block_size: int = 0
    entry_block_other: int = 0

    # Optional distributions (collected at "attempt time")
    entry_spread_bps: List[Decimal] = field(default_factory=list)
    entry_vol_1m: List[Decimal] = field(default_factory=list)
    entry_atr_norm: List[Decimal] = field(default_factory=list)
    entry_vol_baseline: List[Decimal] = field(default_factory=list)

    # Computed summary stats (populated by finalize())
    finalized: bool = False
    median_entry_spread_bps: Optional[Decimal] = None
    median_entry_vol_1m: Optional[Decimal] = None
    median_entry_vol_baseline: Optional[Decimal] = None
    median_entry_atr_norm: Optional[Decimal] = None

    p90_entry_spread_bps: Optional[Decimal] = None
    p10_entry_vol_1m: Optional[Decimal] = None
    p10_entry_vol_baseline: Optional[Decimal] = None
    p90_entry_atr_norm: Optional[Decimal] = None

    _saw_entry_attempt_event: bool = False

    # -------------------------
    # Event accounting helpers
    # -------------------------
    def add_event(self, name: str, detail: str = "", snapshot: Any = None) -> None:
        """
        Canonical ingest for engine events.

        Key behavior:
          - event_counts increments for EVERY event (normalized to UPPER for stability)
          - ENTRY_ATTEMPT increments entry_attempts (canonical if present at all)
          - MISSED_BUY_* increments missed_buy_counts + bucket counters
          - WOULD/SHOULD_BUY increments entry_filled (and attempts only if ENTRY_ATTEMPT is not in use)
          - optional: sample liquidity distributions from snapshot if provided, else parse from detail
        """
        k = str(name or "").strip()
        if not k:
            return

        # --------- LINE ABOVE: if not k:
        upper = k.upper()
        self.event_counts[upper] = int(self.event_counts.get(upper, 0)) + 1

        # 0) Canonical attempt boundary (runner-injected)
        if upper == "ENTRY_ATTEMPT":
            self._saw_entry_attempt_event = True
            self.entry_attempts += 1

            # Sampling at attempt boundary (preferred: snapshot)
            if snapshot is not None:
                self.record_entry_attempt(snapshot)
            else:
                self._parse_liq_from_detail(detail)
            return

        # 1) Missed buys: exact reason key counts + bucket counts
        if upper.startswith("MISSED_BUY_"):
            self.missed_buy_counts[upper] = int(self.missed_buy_counts.get(upper, 0)) + 1

            # Buckets (canonical)
            if upper.startswith("MISSED_BUY_LIQ"):
                self.entry_block_liquidity += 1
            elif upper.startswith("MISSED_BUY_CONFL"):
                self.entry_block_confluence += 1
            elif upper.startswith("MISSED_BUY_RISK"):
                self.entry_block_risk += 1
            elif upper.startswith("MISSED_BUY_NO_SIZE") or upper.startswith("MISSED_BUY_MIN_ORDER") or upper.startswith("MISSED_BUY_EXPOSURE"):
                self.entry_block_size += 1
            else:
                self.entry_block_other += 1

            # Attempts:
            # - If ENTRY_ATTEMPT exists anywhere in this run, do NOT count attempts here.
            # - Otherwise (legacy runner), MISSED_BUY_* implies an attempt happened.
            if not self._saw_entry_attempt_event:
                self.entry_attempts += 1

            # Sampling at attempt boundary
            if snapshot is not None:
                self.record_entry_attempt(snapshot)
            else:
                self._parse_liq_from_detail(detail)
            return

        # --------- LINE ABOVE: if upper.startswith("MISSED_BUY_"):
        # 2) Filled attempts: WOULD/SHOULD_BUY = "entry signal produced"
        if upper in ("WOULD_BUY", "SHOULD_BUY"):
            self.entry_filled += 1

            # Attempts only if we are in legacy mode (no ENTRY_ATTEMPT events)
            if not self._saw_entry_attempt_event:
                self.entry_attempts += 1

            # Sampling at attempt boundary
            if snapshot is not None:
                self.record_entry_attempt(snapshot)
            else:
                self._parse_liq_from_detail(detail)
            return

        # 3) Best-effort: if detail contains liq_* tokens, collect them (not an attempt)
        if detail:
            self._parse_liq_from_detail(detail)

    def _parse_liq_from_detail(self, detail: str) -> None:
        """
        Parse liq_* tokens from event message detail if present.

        IMPORTANT: do NOT coerce non-numeric placeholders (e.g., "None") into 0s.
        If we can't parse a real number, we treat it as missing.
        """
        if not detail:
            return

        # --------- LINE ABOVE: if not detail:
        for m in _LIQ_RE.finditer(detail):
            key = (m.group(1) or "").lower()
            val_raw = (m.group(2) or "").strip()

            v = _maybe_decimal(val_raw)
            if v is None:
                continue  # <-- critical: do not inject fake zeros

            if key == "spread_bps":
                self.entry_spread_bps.append(v)
            elif key in ("vol_1m", "vol1m"):
                self.entry_vol_1m.append(v)
            elif key in ("vol_baseline", "volbaseline"):
                self.entry_vol_baseline.append(v)
            elif key in ("atr_norm", "atrnorm"):
                self.entry_atr_norm.append(v)

    def record_entry_attempt(
        self,
        snap_or_none: Any = None,
        *,
        liq_spread_bps: Any = None,
        liq_vol_1m: Any = None,
        liq_vol_baseline: Any = None,
        liq_atr_norm: Any = None,
    ) -> None:
        """
        Runner-side sampling hook.

        Supported call shapes:
          A) record_entry_attempt(snapshot_obj)
          B) record_entry_attempt(liq_spread_bps=..., liq_vol_1m=..., ...)

        IMPORTANT: we only append values that are actually present.
        We do NOT turn None/"None"/non-numeric into 0.
        """
        # --------- LINE ABOVE: def record_entry_attempt(
        if (
            snap_or_none is not None
            and liq_spread_bps is None
            and liq_vol_1m is None
            and liq_vol_baseline is None
            and liq_atr_norm is None
        ):
            liq_spread_bps = getattr(snap_or_none, "liq_spread_bps", None)
            liq_vol_1m = getattr(snap_or_none, "liq_vol_1m", None)
            liq_vol_baseline = getattr(snap_or_none, "liq_vol_baseline", None)
            liq_atr_norm = getattr(snap_or_none, "liq_atr_norm", None)

        v = _maybe_decimal(liq_spread_bps)
        if v is not None:
            self.entry_spread_bps.append(v)

        v = _maybe_decimal(liq_vol_1m)
        if v is not None:
            self.entry_vol_1m.append(v)

        v = _maybe_decimal(liq_vol_baseline)
        if v is not None:
            self.entry_vol_baseline.append(v)

        v = _maybe_decimal(liq_atr_norm)
        if v is not None:
            self.entry_atr_norm.append(v)

    def finalize(self) -> None:
        """
        Compute medians/quantiles for distributions (cheap, deterministic).
        Safe to call multiple times.
        """
        if self.finalized:
            return

        # --------- LINE ABOVE: if self.finalized:
        self.median_entry_spread_bps = _median_dec(self.entry_spread_bps)
        self.median_entry_vol_1m = _median_dec(self.entry_vol_1m)
        self.median_entry_vol_baseline = _median_dec(self.entry_vol_baseline)
        self.median_entry_atr_norm = _median_dec(self.entry_atr_norm)

        self.p90_entry_spread_bps = _quantile_dec(self.entry_spread_bps, 0.90)
        self.p10_entry_vol_1m = _quantile_dec(self.entry_vol_1m, 0.10)
        self.p10_entry_vol_baseline = _quantile_dec(self.entry_vol_baseline, 0.10)
        self.p90_entry_atr_norm = _quantile_dec(self.entry_atr_norm, 0.90)

        self.finalized = True

    # -------------------------
    # Core metrics
    # -------------------------
    def pnl_usd(self) -> Decimal:
        return (self.end_equity or Decimal("0")) - (self.start_equity or Decimal("0"))

    def total_return_pct(self) -> Decimal:
        if self.start_equity <= 0:
            return Decimal("0")
        return (self.end_equity - self.start_equity) / self.start_equity * Decimal("100")

    def max_drawdown_pct(self) -> Decimal:
        peak: Optional[Decimal] = None
        max_dd = Decimal("0")
        for _, eq in self.equity_curve:
            if peak is None or eq > peak:
                peak = eq
            if peak and peak > 0:
                dd = (peak - eq) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd * Decimal("100")

    def max_drawdown_usd(self) -> Decimal:
        peak: Optional[Decimal] = None
        max_dd = Decimal("0")
        for _, eq in self.equity_curve:
            if peak is None or eq > peak:
                peak = eq
            if peak is not None:
                dd = peak - eq
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    def closed_trades(self) -> List[Trade]:
        return [t for t in self.trades if t.is_closed()]

    def win_rate_pct(self) -> Decimal:
        closed = self.closed_trades()
        if not closed:
            return Decimal("0")
        wins = sum(1 for t in closed if (t.realized_usd or Decimal("0")) > 0)
        return Decimal(wins) / Decimal(len(closed)) * Decimal("100")

    def realized_total_usd(self) -> Decimal:
        closed = self.closed_trades()
        return sum((t.realized_usd or Decimal("0")) for t in closed) if closed else Decimal("0")

    def avg_trade_usd(self) -> Decimal:
        closed = self.closed_trades()
        if not closed:
            return Decimal("0")
        return self.realized_total_usd() / Decimal(len(closed))

    def avg_trade_return_pct(self) -> Decimal:
        closed = self.closed_trades()
        rets = [t.realized_return_pct() for t in closed]
        rets = [r for r in rets if r is not None]
        if not rets:
            return Decimal("0")
        return sum(rets) / Decimal(len(rets))

    def avg_win_usd(self) -> Decimal:
        wins = [t for t in self.closed_trades() if (t.realized_usd or Decimal("0")) > 0]
        if not wins:
            return Decimal("0")
        return sum((t.realized_usd or Decimal("0")) for t in wins) / Decimal(len(wins))

    def avg_loss_usd(self) -> Decimal:
        losses = [t for t in self.closed_trades() if (t.realized_usd or Decimal("0")) < 0]
        if not losses:
            return Decimal("0")
        return sum((t.realized_usd or Decimal("0")) for t in losses) / Decimal(len(losses))

    def expectancy_usd(self) -> Decimal:
        return self.avg_trade_usd()

    def profit_factor(self) -> Decimal:
        closed = self.closed_trades()
        if not closed:
            return Decimal("0")
        gross_win = sum((t.realized_usd or Decimal("0")) for t in closed if (t.realized_usd or Decimal("0")) > 0)
        gross_loss = sum((t.realized_usd or Decimal("0")) for t in closed if (t.realized_usd or Decimal("0")) < 0)
        if gross_loss == 0:
            return Decimal("0") if gross_win == 0 else Decimal("999999")
        return gross_win / abs(gross_loss)

    def exposure_pct(self) -> Decimal:
        total_s = max(0, int(self.end_epoch - self.start_epoch))
        if total_s <= 0:
            return Decimal("0")

        closed = self.closed_trades()
        dur_s = sum(int(t.duration_s() or 0) for t in closed)

        pct = Decimal(dur_s) / Decimal(total_s) * Decimal("100")
        if pct < 0:
            return Decimal("0")
        return pct if pct <= 100 else Decimal("100")

    # -------------------------
    # Summary (JSON-friendly)
    # -------------------------
    def summary(self) -> Dict[str, Any]:
        self.finalize()

        closed = self.closed_trades()
        gross = self.realized_total_usd()

        durations = [t.duration_s() for t in closed if t.duration_s() is not None]
        avg_dur = (sum(durations) / len(durations)) if durations else 0

        mfes = [t.mfe_pct for t in closed if t.mfe_pct is not None]
        maes = [t.mae_pct for t in closed if t.mae_pct is not None]
        avg_mfe = (sum(mfes) / Decimal(len(mfes))) if mfes else None
        avg_mae = (sum(maes) / Decimal(len(maes))) if maes else None

        return {
            "symbol": self.symbol,
            "start_epoch": int(self.start_epoch),
            "end_epoch": int(self.end_epoch),
            "start_equity": str(self.start_equity),
            "end_equity": str(self.end_equity),

            "pnl_usd": str(self.pnl_usd()),
            "realized_total_usd": str(gross),
            "total_return_pct": f"{self.total_return_pct():.4f}",
            "max_drawdown_pct": f"{self.max_drawdown_pct():.4f}",
            "max_drawdown_usd": str(self.max_drawdown_usd()),
            "trades_closed": int(len(closed)),
            "win_rate_pct": f"{self.win_rate_pct():.2f}",
            "avg_trade_usd": f"{self.avg_trade_usd():.4f}",
            "avg_trade_return_pct": f"{self.avg_trade_return_pct():.4f}",
            "expectancy_usd": f"{self.expectancy_usd():.4f}",
            "avg_win_usd": f"{self.avg_win_usd():.4f}",
            "avg_loss_usd": f"{self.avg_loss_usd():.4f}",
            "profit_factor": f"{self.profit_factor():.4f}",
            "avg_trade_duration_s": int(avg_dur),
            "exposure_pct": f"{self.exposure_pct():.4f}",

            "avg_mfe_pct_points": (None if avg_mfe is None else f"{avg_mfe:.4f}"),
            "avg_mae_pct_points": (None if avg_mae is None else f"{avg_mae:.4f}"),

            "event_counts": dict(self.event_counts),
            "missed_buy_counts": dict(self.missed_buy_counts),

            "entry_attempts": int(self.entry_attempts),
            "entry_filled": int(self.entry_filled),
            "entry_block_liquidity": int(self.entry_block_liquidity),
            "entry_block_confluence": int(self.entry_block_confluence),
            "entry_block_risk": int(self.entry_block_risk),
            "entry_block_size": int(self.entry_block_size),
            "entry_block_other": int(self.entry_block_other),

            "median_entry_spread_bps": (None if self.median_entry_spread_bps is None else f"{self.median_entry_spread_bps:.4f}"),
            "median_entry_vol_1m": (None if self.median_entry_vol_1m is None else f"{self.median_entry_vol_1m:.4f}"),
            "median_entry_vol_baseline": (None if self.median_entry_vol_baseline is None else f"{self.median_entry_vol_baseline:.4f}"),
            "median_entry_atr_norm": (None if self.median_entry_atr_norm is None else f"{self.median_entry_atr_norm:.6f}"),

            "p90_entry_spread_bps": (None if self.p90_entry_spread_bps is None else f"{self.p90_entry_spread_bps:.4f}"),
            "p10_entry_vol_1m": (None if self.p10_entry_vol_1m is None else f"{self.p10_entry_vol_1m:.4f}"),
            "p10_entry_vol_baseline": (None if self.p10_entry_vol_baseline is None else f"{self.p10_entry_vol_baseline:.4f}"),
            "p90_entry_atr_norm": (None if self.p90_entry_atr_norm is None else f"{self.p90_entry_atr_norm:.6f}"),
        }

    # -------------------------
    # Writers
    # -------------------------
    def write_equity_curve_csv(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["epoch", "equity_usd"])
            for epoch, eq in self.equity_curve:
                w.writerow([int(epoch), str(eq)])

    def write_trades_csv(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "symbol",
                "entry_epoch",
                "exit_epoch",
                "duration_s",
                "entry_px",
                "exit_px",
                "qty",
                "realized_usd",
                "realized_return_pct",
                "mfe_pct_points",
                "mae_pct_points",
            ])
            for t in self.trades:
                w.writerow([
                    self.symbol,
                    int(t.entry_epoch),
                    "" if t.exit_epoch is None else int(t.exit_epoch),
                    "" if t.duration_s() is None else int(t.duration_s()),
                    str(t.entry_px),
                    "" if t.exit_px is None else str(t.exit_px),
                    str(t.qty),
                    "" if t.realized_usd is None else str(t.realized_usd),
                    "" if t.realized_return_pct() is None else f"{t.realized_return_pct():.6f}",
                    "" if t.mfe_pct is None else str(t.mfe_pct),
                    "" if t.mae_pct is None else str(t.mae_pct),
                ])

    def write_summary_json(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.summary(), f, indent=2, sort_keys=True)

    def write_event_counts_csv(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["event", "count"])
            for k in sorted(self.event_counts.keys()):
                w.writerow([k, int(self.event_counts.get(k, 0))])

    def write_entry_attempt_stats_csv(self, path: str):
        self.finalize()

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["metric", "value"])
            w.writerow(["entry_attempts", int(self.entry_attempts)])
            w.writerow(["entry_filled", int(self.entry_filled)])
            w.writerow(["entry_block_liquidity", int(self.entry_block_liquidity)])
            w.writerow(["entry_block_confluence", int(self.entry_block_confluence)])
            w.writerow(["entry_block_risk", int(self.entry_block_risk)])
            w.writerow(["entry_block_size", int(self.entry_block_size)])
            w.writerow(["entry_block_other", int(self.entry_block_other)])

            w.writerow(["median_entry_spread_bps", "" if self.median_entry_spread_bps is None else str(self.median_entry_spread_bps)])
            w.writerow(["median_entry_vol_1m", "" if self.median_entry_vol_1m is None else str(self.median_entry_vol_1m)])
            w.writerow(["median_entry_vol_baseline", "" if self.median_entry_vol_baseline is None else str(self.median_entry_vol_baseline)])
            w.writerow(["median_entry_atr_norm", "" if self.median_entry_atr_norm is None else str(self.median_entry_atr_norm)])

            w.writerow(["p90_entry_spread_bps", "" if self.p90_entry_spread_bps is None else str(self.p90_entry_spread_bps)])
            w.writerow(["p10_entry_vol_1m", "" if self.p10_entry_vol_1m is None else str(self.p10_entry_vol_1m)])
            w.writerow(["p10_entry_vol_baseline", "" if self.p10_entry_vol_baseline is None else str(self.p10_entry_vol_baseline)])
            w.writerow(["p90_entry_atr_norm", "" if self.p90_entry_atr_norm is None else str(self.p90_entry_atr_norm)])


def _d(m: Optional[re.Match], idx: int = 1) -> Optional[Decimal]:
    if not m:
        return None
    try:
        return Decimal(m.group(idx))
    except Exception:
        return None


def parse_buy_event_message(msg: str) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    out["px"] = _d(_PX_RE.search(msg)) or Decimal("0")
    out["fill_px"] = _d(_FILL_RE.search(msg)) or out["px"]
    out["qty"] = _d(_BUY_RE.search(msg)) or Decimal("0")
    return out


def parse_sell_event_message(msg: str) -> Dict[str, Decimal]:
    out: Dict[str, Decimal] = {}
    out["px"] = _d(_PX_RE.search(msg)) or Decimal("0")
    out["eff_sell"] = _d(_EFF_SELL_RE.search(msg))  # optional
    out["proceeds"] = _d(_PROCEEDS_RE.search(msg)) or Decimal("0")
    out["realized_trade"] = _d(_SELL_RE.search(msg)) or Decimal("0")
    return out
