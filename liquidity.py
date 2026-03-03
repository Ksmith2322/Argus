# line above: from __future__ import annotations
# liquidity.py
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Deque, Dict, Optional
from collections import deque


def _d(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _clamp_i(x: Any, lo: int, hi: int) -> int:
    try:
        x = int(x)
    except Exception:
        x = lo
    return max(lo, min(hi, x))


def _round_int(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_HALF_UP))


def _spread_bps_from_bid_ask(bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
    """
    Spread in bps = (ask - bid) / mid * 10_000
    Returns None if bid/ask missing or invalid.
    """
    if bid is None or ask is None:
        return None
    bid = _d(bid, "0")
    ask = _d(ask, "0")
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / Decimal("2")
    if mid <= 0:
        return None
    return (ask - bid) / mid * Decimal("10000")


def _synthetic_spread_bps(
    *,
    px: Optional[Decimal],
    vol_1m: Optional[Decimal],
    atr_norm: Optional[Decimal],
    floor_bps: Decimal,
    atr_mult_bps: Decimal,
    vol_floor: Decimal,
    vol_penalty_bps: Decimal,
) -> Optional[Decimal]:
    """
    Deterministic synthetic spread model (ONLY if you explicitly enable it).

    Intuition:
      - baseline floor always present
      - wider spread when atr_norm is high (more volatility -> worse fills)
      - optional extra bps penalty when vol_1m is very low (thin book proxy)

    This is a proxy, not truth. But it is stable and auditable.
    """
    _ = px  # px reserved if you later want px-dependent scaling; currently unused.

    if floor_bps <= 0 and atr_mult_bps <= 0 and vol_penalty_bps <= 0:
        return None

    out = Decimal("0")
    out += max(Decimal("0"), floor_bps)

    an = _d(atr_norm, "0") if atr_norm is not None else Decimal("0")
    if an > 0 and atr_mult_bps > 0:
        # Example: atr_norm=0.0010 and atr_mult_bps=8000 => +8 bps
        out += (an * atr_mult_bps)

    v1 = _d(vol_1m, "0") if vol_1m is not None else Decimal("0")
    if vol_floor > 0 and v1 > 0 and v1 < vol_floor and vol_penalty_bps > 0:
        out += vol_penalty_bps

    return out if out > 0 else None


@dataclass
class LiquidityResult:
    """
    Keep field names aligned with engine.py + decisions.py expectations.

    engine.py expects:
      - ok
      - spread_bps
      - vol_1m
      - vol_baseline
      - atr_norm
      - mode
      - penalty_points
      - reasons
    """
    ok: bool
    mode: str                       # "BLOCK" or "PENALIZE"
    penalty_points: int             # 0 if ok OR mode=BLOCK
    spread_bps: Optional[Decimal] = None

    vol_1m: Optional[Decimal] = None
    vol_baseline: Optional[Decimal] = None
    atr_norm: Optional[Decimal] = None  # fraction

    reasons: str = ""

    @property
    def liq_ok(self) -> bool:
        return bool(self.ok)


class LiquidityEngine:
    """
    Phase 5B — Liquidity Filters

    Required API:
      - update_on_1m_close(vol_1m)
      - evaluate(bid, ask, vol_1m, atr_norm) -> LiquidityResult

    Convenience:
      - evaluate_tick(tick) pulls bid/ask/vol_1m/atr_norm (+px if needed)

    Policy:
      - LIQ_MODE = BLOCK or PENALIZE
      - BLOCK: any violated condition -> ok=False
      - PENALIZE: violations accumulate penalty_points; ok stays True unless hard-block threshold is enabled

    Backtest override knob (what you asked for):
      - Set env var BT_LIQUIDITY_MODE=OFF  -> disables liquidity entirely in backtest
      - Set env var BT_LIQUIDITY_MODE=RELAX -> keeps liquidity "enabled" but removes the volume/atr gates so fills can happen
      - You can also set cfg["BT_LIQUIDITY_MODE"] to the same values; env wins.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg

        # --------- LINE ABOVE: self.cfg = cfg
        # Backtest override knob (env wins). Only applied when BACKTEST_MODE is true.
        self._is_backtest = _as_bool(cfg.get("BACKTEST_MODE", False), False)
        self._bt_liq_mode = str(os.getenv("BT_LIQUIDITY_MODE", cfg.get("BT_LIQUIDITY_MODE", ""))).strip().upper()

        # Master enable (canonical)
        self.enabled = _as_bool(
            cfg.get("USE_LIQUIDITY_FILTERS", cfg.get("USE_LIQUIDITY", "false")),
            False,
        )

        # Mode: "BLOCK" or "PENALIZE"
        self.mode = str(cfg.get("LIQ_MODE", "BLOCK")).strip().upper()
        if self.mode not in ("BLOCK", "PENALIZE"):
            self.mode = "BLOCK"

        # Penalty size per violation (PENALIZE mode)
        self.penalty_points = _as_int(cfg.get("LIQ_PENALTY_POINTS", 10), 10)
        self.penalty_points = max(0, min(100, self.penalty_points))

        # Optional: in PENALIZE mode, allow a hard block if penalty accumulates too large
        self.penalize_hard_block_at = _as_int(cfg.get("LIQ_PENALIZE_HARD_BLOCK_AT", 0), 0)
        self.penalize_hard_block_at = max(0, min(100, self.penalize_hard_block_at))  # 0 disables

        # Spread threshold (bps)
        self.max_spread_bps = _d(cfg.get("LIQ_MAX_SPREAD_BPS", "25"), "25")

        # Volume thresholds (hard floors; optional)
        # --------- LINE ABOVE: self.max_spread_bps = _d(cfg.get("LIQ_MAX_SPREAD_BPS", "25"), "25")
        # Backward/forward compat: accept multiple keys (your config.py uses LIQ_MIN_VOL_USD_1M)
        self.min_vol_1m = _d(
            cfg.get("LIQ_MIN_VOL_1M", cfg.get("LIQ_MIN_VOL_USD_1M", "0")),
            "0",
        )

        # Rolling baseline threshold: require v1 >= vbase * mult (if both exist)
        self.min_vol_mult = _d(cfg.get("LIQ_MIN_VOL_MULT", "1.2"), "1.2")
        self.vol_window = _clamp_i(cfg.get("LIQ_VOL_BASELINE_WINDOW", 30), 5, 500)

        # ATR normalization thresholds
        self.atr_norm_min = _d(cfg.get("LIQ_ATR_NORM_MIN", "0.0008"), "0.0008")
        self.atr_norm_max = _d(cfg.get("LIQ_ATR_NORM_MAX", "0"), "0")  # 0 => disabled upper bound

        # Missing input policy
        self.block_on_missing_spread = _as_bool(cfg.get("LIQ_BLOCK_ON_MISSING_SPREAD", False), False)
        self.block_on_missing_volume = _as_bool(cfg.get("LIQ_BLOCK_ON_MISSING_VOLUME", False), False)
        self.block_on_missing_atr = _as_bool(cfg.get("LIQ_BLOCK_ON_MISSING_ATR_NORM", False), False)

        # Optional synthetic spread model (explicit)
        self.use_synth_spread = _as_bool(cfg.get("LIQ_USE_SYNTHETIC_SPREAD", False), False)
        self.synth_floor_bps = _d(cfg.get("LIQ_SYNTH_SPREAD_FLOOR_BPS", "8"), "8")
        self.synth_atr_mult_bps = _d(cfg.get("LIQ_SYNTH_SPREAD_ATR_MULT_BPS", "8000"), "8000")
        self.synth_vol_floor = _d(cfg.get("LIQ_SYNTH_SPREAD_VOL_FLOOR", "0"), "0")
        self.synth_vol_penalty_bps = _d(cfg.get("LIQ_SYNTH_SPREAD_VOL_PENALTY_BPS", "0"), "0")

        # Internal state: rolling volumes (store same metric passed as vol_1m)
        self._vol_hist: Deque[Decimal] = deque(maxlen=self.vol_window)

        # ✅ CRITICAL: persist last computed baseline so snapshots can always expose it
        # (Even if vol_hist is empty, this stays None; once populated, it's stable.)
        self._last_vol_baseline: Optional[Decimal] = None

        # --------- LINE ABOVE: self._last_vol_baseline: Optional[Decimal] = None
        # Apply backtest override knob AFTER all defaults are loaded.
        # OFF: disable liquidity so backtests can generate fills (removes MISSED_BUY_LIQUIDITY blocks).
        # RELAX: keep liquidity on, but relax the typical blockers (volume + atr min).
        if self._is_backtest and self._bt_liq_mode:
            if self._bt_liq_mode in ("OFF", "DISABLE", "0", "FALSE"):
                self.enabled = False
            elif self._bt_liq_mode in ("RELAX", "EASY"):
                self.enabled = True
                # Remove the usual "no fill" offenders
                self.min_vol_1m = Decimal("0")
                self.min_vol_mult = Decimal("0")
                self.atr_norm_min = Decimal("0")
                self.atr_norm_max = Decimal("0")
                self.block_on_missing_volume = False
                self.block_on_missing_atr = False
                # Keep spread gate unless you explicitly disable it via config
                # (If you want spread ignored too, set LIQ_MAX_SPREAD_BPS=0 in env/cfg)

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "LiquidityEngine":
        return cls(cfg)

    def update_on_1m_close(self, vol_1m: Any) -> None:
        """
        Call once per 1m candle close with that candle's volume metric.
        """
        # --------- LINE ABOVE: def update_on_1m_close(self, vol_1m: Any) -> None:
        v = _d(vol_1m, "0")
        if v > 0:
            if self._vol_hist.maxlen != self.vol_window:
                self._vol_hist = deque(self._vol_hist, maxlen=self.vol_window)
            self._vol_hist.append(v)

            # ✅ Update cached baseline immediately on each close (auditability)
            self._last_vol_baseline = self.vol_baseline()

    def vol_baseline(self) -> Optional[Decimal]:
        if not self._vol_hist:
            return None
        s = sum(self._vol_hist, Decimal("0"))
        return s / Decimal(len(self._vol_hist))

    def evaluate_tick(self, tick: Any) -> LiquidityResult:
        """
        Convenience wrapper so engine.py can call liquidity_engine.evaluate_tick(tick).
        Pulls:
          - bid / ask
          - vol_1m
          - atr_norm
          - px (only used if synthetic spread enabled)
        """
        # --------- LINE ABOVE: def evaluate_tick(self, tick: Any) -> LiquidityResult:
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        vol_1m = getattr(tick, "vol_1m", None)
        atr_norm = getattr(tick, "atr_norm", None)
        px = getattr(tick, "px", None)
        return self.evaluate(bid=bid, ask=ask, vol_1m=vol_1m, atr_norm=atr_norm, px=px)

    def evaluate(
        self,
        *,
        bid: Optional[Decimal] = None,
        ask: Optional[Decimal] = None,
        vol_1m: Optional[Decimal] = None,
        atr_norm: Optional[Decimal] = None,
        px: Optional[Decimal] = None,
    ) -> LiquidityResult:
        """
        Returns LiquidityResult (never None).

        Guarantees:
          - ok/mode/penalty_points always set
          - reasons always populated with enough info to audit why
          - spread_bps/vol_baseline/atr_norm may be None if missing inputs
        """
        # ✅ Use cached baseline if available (helps when evaluate happens between closes)
        vbase = self._last_vol_baseline
        if vbase is None:
            vbase = self.vol_baseline()
            self._last_vol_baseline = vbase

        v1 = _d(vol_1m, "0") if vol_1m is not None else None
        an = _d(atr_norm, "0") if atr_norm is not None else None

        if not self.enabled:
            return LiquidityResult(
                ok=True,
                mode=self.mode,
                penalty_points=0,
                spread_bps=None,
                vol_1m=v1,
                vol_baseline=vbase,
                atr_norm=an,
                reasons="LIQ_DISABLED",
            )

        reasons: list[str] = []
        violations: list[str] = []
        blocks: list[str] = []
        penalty_total = 0

        def _hit(code: str) -> None:
            nonlocal penalty_total
            violations.append(code)
            if self.mode == "BLOCK":
                blocks.append(code)
            else:
                if self.penalty_points > 0:
                    penalty_total += int(self.penalty_points)

        # -------------------------
        # Spread gate
        # -------------------------
        spread_bps = _spread_bps_from_bid_ask(bid, ask)

        if spread_bps is None and self.use_synth_spread:
            spread_bps = _synthetic_spread_bps(
                px=_d(px, "0") if px is not None else None,
                vol_1m=v1,
                atr_norm=an,
                floor_bps=self.synth_floor_bps,
                atr_mult_bps=self.synth_atr_mult_bps,
                vol_floor=self.synth_vol_floor,
                vol_penalty_bps=self.synth_vol_penalty_bps,
            )
            if spread_bps is not None:
                reasons.append(f"spread=synthetic({spread_bps:.2f}bps)")

        if spread_bps is None:
            reasons.append("spread=NA")
            if self.block_on_missing_spread:
                _hit("SPREAD_MISSING")
        else:
            reasons.append(f"spread_bps={spread_bps:.2f}")
            if self.max_spread_bps > 0 and spread_bps > self.max_spread_bps:
                _hit("SPREAD_TOO_WIDE")

        # -------------------------
        # Volume gates
        # -------------------------
        if v1 is None or v1 <= 0:
            reasons.append("vol_1m=NA")
            if self.block_on_missing_volume:
                _hit("VOLUME_MISSING")
        else:
            reasons.append(f"vol_1m={v1}")
            if self.min_vol_1m > 0:
                reasons.append(f"vol_floor>={self.min_vol_1m}")
                if v1 < self.min_vol_1m:
                    _hit("VOLUME_BELOW_MIN")

            if vbase is None or vbase <= 0:
                reasons.append("vol_base=NA")
            else:
                reasons.append(f"vol_base={vbase}")
                if self.min_vol_mult > 0:
                    need = vbase * self.min_vol_mult
                    reasons.append(f"vol_need>={need:.4f}")
                    if v1 < need:
                        _hit("VOLUME_TOO_THIN")

        # -------------------------
        # ATR normalized gate
        # -------------------------
        if an is None or an <= 0:
            reasons.append("atr_norm=NA")
            if self.block_on_missing_atr:
                _hit("ATR_NORM_MISSING")
        else:
            reasons.append(f"atr_norm={an}")
            if self.atr_norm_min > 0:
                reasons.append(f"atr_min>={self.atr_norm_min}")
                if an < self.atr_norm_min:
                    _hit("ATR_NORM_TOO_LOW")
            if self.atr_norm_max > 0:
                reasons.append(f"atr_max<={self.atr_norm_max}")
                if an > self.atr_norm_max:
                    _hit("ATR_NORM_TOO_HIGH")

        # -------------------------
        # Decide ok / penalties according to mode
        # -------------------------
        if self.mode == "BLOCK":
            ok = (len(blocks) == 0)
            penalty_out = 0
        else:
            penalty_out = max(0, min(100, int(penalty_total)))
            ok = True
            if self.penalize_hard_block_at > 0 and penalty_out >= self.penalize_hard_block_at:
                ok = False
                blocks.append("PENALTY_HARD_BLOCK")

        if violations:
            reasons.append("violations=" + ",".join(violations))
        if blocks:
            reasons.append("BLOCK:" + ",".join(blocks))
        if self.mode == "PENALIZE" and penalty_out > 0:
            reasons.append(f"penalty=-{penalty_out}")
        if not violations and not blocks and (self.mode != "PENALIZE" or penalty_out == 0):
            reasons.append("LIQ_OK")

        return LiquidityResult(
            ok=bool(ok),
            mode=self.mode,
            penalty_points=int(penalty_out),
            spread_bps=spread_bps,
            vol_1m=v1,
            vol_baseline=vbase,
            atr_norm=an,
            reasons=";".join(reasons),
        )