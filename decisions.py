# line above: from __future__ import annotations
# decisions.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional


@dataclass
class EngineEvent:
    name: str
    message: str
    notify_title: Optional[str] = None
    notify_body: Optional[str] = None


@dataclass
class DecisionSnapshot:
    """
    A single engine step output (logged to CSV + printed in console).

    Contracts:
      - live runner calls:     snap.to_row()
      - backtest runner calls: snap.to_signals_row()

    Phase 5 readiness:
      - Structure fields explicit
      - Liquidity fields explicit
      - Session overlay explicit
      - to_row() returns schema-safe aliases to prevent blank-column logging
      - EngineEvent list is always present (events default_factory)
    """

    # -------------------------
    # Core
    # -------------------------
    symbol: str
    ts: str
    epoch: int
    px: Decimal
    paused: bool = False
    stale: bool = False

    # -------------------------
    # Phase 5C: Session overlay
    # NOTE: engine.py currently sets:
    #   snap.session_bonus_points, snap.session_risk_mult, snap.session_reason
    # This class accepts BOTH the canonical names and those legacy variants via aliases in to_row().
    # -------------------------
    session: str = ""
    session_labels: str = ""
    session_bonus: int = 0
    session_risk_mult: Optional[Decimal] = None
    session_reasons: str = ""

    # -------------------------
    # Candle columns (1m)
    # -------------------------
    candle_start_1m: Optional[int] = None
    candle_close_1m: Optional[Decimal] = None
    candle_volume_1m: Optional[Decimal] = None  # ✅ engine.py sets this

    # -------------------------
    # Strategy / indicators (1m)
    # -------------------------
    ma_fast: Optional[Decimal] = None
    ma_slow: Optional[Decimal] = None
    ma50: Optional[Decimal] = None
    ma200: Optional[Decimal] = None
    reasons_1m: str = ""

    # -------------------------
    # Scores / signals
    # -------------------------
    sig_1m: Optional[int] = None
    trend_ok_1m: Optional[bool] = None
    score_1m: Optional[int] = None
    score_5m: Optional[int] = None
    score_1h: Optional[int] = None

    # -------------------------
    # Vol / regime
    # -------------------------
    vol: Optional[Decimal] = None
    regime: str = "UNKNOWN"
    trend_strength: Optional[Decimal] = None

    # -------------------------
    # Adaptive confluence debug
    # -------------------------
    ac_base_score: Optional[int] = None
    ac_base_gate: str = ""
    ac_adjusted_score: Optional[int] = None
    ac_adjusted_gate: str = ""
    ac_delta: int = 0
    ac_reason: str = ""

    # -------------------------
    # Confluence (effective)
    # -------------------------
    confluence_score: Optional[int] = None
    confluence_gate: str = ""
    confluence_reasons: str = ""

    # -------------------------
    # Phase 4 sizing debug
    # -------------------------
    vol_used: Optional[Decimal] = None
    vol_sizing_qty: Optional[Decimal] = None
    vol_reason: str = ""
    sizing_note: str = ""

    # -------------------------
    # Phase 5A structure
    # -------------------------
    nearest_support: Optional[Decimal] = None
    nearest_resistance: Optional[Decimal] = None
    dist_support: Optional[Decimal] = None
    dist_resistance: Optional[Decimal] = None

    near_support: bool = False
    near_resistance: bool = False

    broke_up: bool = False
    broke_down: bool = False
    retest_ok: bool = False
    failed_retest: bool = False

    rejection_at_res: bool = False
    rejection_at_sup: bool = False

    structure_reasons: str = ""

    # -------------------------
    # Phase 5B liquidity
    # -------------------------
    liq_ok: Optional[bool] = None
    liq_spread_bps: Optional[Decimal] = None
    liq_vol_1m: Optional[Decimal] = None
    liq_vol_baseline: Optional[Decimal] = None
    liq_atr_norm: Optional[Decimal] = None
    liq_mode: str = ""
    liq_penalty_points: int = 0
    liq_reasons: str = ""

    # -------------------------
    # Argus (feature sabotage) annotations
    # -------------------------
    argus_profile: str = ""
    argus_forced_regime: str = ""
    argus_forced_liq_block: bool = False

    # -------------------------
    # Portfolio / risk
    # -------------------------
    cash_usd: Decimal = Decimal("0")
    position_qty: Decimal = Decimal("0")
    avg_entry_px: Optional[Decimal] = None
    equity_usd: Decimal = Decimal("0")
    exposure_usd: Decimal = Decimal("0")
    unrl_pnl_usd: Decimal = Decimal("0")
    realized_pnl_usd: Decimal = Decimal("0")

    trades_today: int = 0
    daily_realized_usd: Decimal = Decimal("0")
    lockout_until_epoch: int = 0
    cooldown_remaining_s: int = 0

    # -------------------------
    # Exit levels + distances
    # -------------------------
    hold_s: int = 0
    trend_below_count: int = 0
    peak_price: Optional[Decimal] = None
    take_profit: Optional[Decimal] = None
    stop_loss: Optional[Decimal] = None
    trail_stop: Optional[Decimal] = None

    dist_ma200: Optional[Decimal] = None
    dist_tp: Optional[Decimal] = None
    dist_sl: Optional[Decimal] = None
    dist_trail: Optional[Decimal] = None
    min_dist: Optional[Decimal] = None

    # -------------------------
    # MFE/MAE
    # -------------------------
    mfe_pct: Decimal = Decimal("0")
    mae_pct: Decimal = Decimal("0")

    # -------------------------
    # Decision
    # -------------------------
    action: str = "HOLD"
    action_reason: str = ""
    risk_blocked_reason: str = ""
    next_poll_s: float = 0.0

    # -------------------------
    # Events
    # -------------------------
    events: List[EngineEvent] = field(default_factory=list)

    # -------------------------
    # Output helpers
    # -------------------------
    def to_signals_row(self) -> Dict[str, Any]:
        return self.to_row()

    def to_row(self) -> Dict[str, Any]:
        def _d(x: Any) -> Any:
            if x is None:
                return None
            if isinstance(x, Decimal):
                return str(x)
            return x

        def _b(x: Optional[bool]) -> Any:
            if x is None:
                return None
            return int(bool(x))

        # Pull in legacy attrs that engine.py may have set (without updating dataclass fields).
        # This avoids AttributeError and prevents "blank columns" when names drift.
        legacy_session_bonus = getattr(self, "session_bonus_points", None)
        legacy_session_reason = getattr(self, "session_reason", None)

        row: Dict[str, Any] = {
            # Canonical core
            "ts": self.ts,
            "epoch": int(self.epoch),
            "symbol": self.symbol,
            "px": _d(self.px),
            "paused": int(bool(self.paused)),
            "stale": int(bool(self.stale)),

            # Phase 5C session overlay (canonical)
            "session": self.session,
            "session_labels": self.session_labels,
            "session_bonus": int(self.session_bonus),
            "session_risk_mult": _d(self.session_risk_mult),
            "session_reasons": self.session_reasons,

            # Candle
            "candle_start_1m": self.candle_start_1m,
            "candle_close_1m": _d(self.candle_close_1m),
            "candle_volume_1m": _d(self.candle_volume_1m),

            # Indicators
            "ma_fast": _d(self.ma_fast),
            "ma_slow": _d(self.ma_slow),
            "ma50": _d(self.ma50),
            "ma200": _d(self.ma200),
            "reasons_1m": self.reasons_1m,

            # Signals / scores
            "sig_1m": self.sig_1m,
            "trend_ok_1m": _b(self.trend_ok_1m),
            "score_1m": self.score_1m,
            "score_5m": self.score_5m,
            "score_1h": self.score_1h,

            # Vol / regime
            "vol": _d(self.vol),
            "regime": self.regime,
            "trend_strength": _d(self.trend_strength),

            # Adaptive confluence debug
            "ac_base_score": self.ac_base_score,
            "ac_base_gate": self.ac_base_gate,
            "ac_adjusted_score": self.ac_adjusted_score,
            "ac_adjusted_gate": self.ac_adjusted_gate,
            "ac_delta": int(self.ac_delta),
            "ac_reason": self.ac_reason,

            # Confluence
            "confluence_score": self.confluence_score,
            "confluence_gate": self.confluence_gate,
            "confluence_reasons": self.confluence_reasons,

            # Sizing debug
            "vol_used": _d(self.vol_used),
            "vol_sizing_qty": _d(self.vol_sizing_qty),
            "vol_reason": self.vol_reason,
            "sizing_note": self.sizing_note,

            # Structure
            "nearest_support": _d(self.nearest_support),
            "nearest_resistance": _d(self.nearest_resistance),
            "dist_support": _d(self.dist_support),
            "dist_resistance": _d(self.dist_resistance),
            "near_support": int(bool(self.near_support)),
            "near_resistance": int(bool(self.near_resistance)),
            "broke_up": int(bool(self.broke_up)),
            "broke_down": int(bool(self.broke_down)),
            "retest_ok": int(bool(self.retest_ok)),
            "failed_retest": int(bool(self.failed_retest)),
            "rejection_at_res": int(bool(self.rejection_at_res)),
            "rejection_at_sup": int(bool(self.rejection_at_sup)),
            "structure_reasons": self.structure_reasons,

            # Liquidity
            "liq_ok": _b(self.liq_ok),
            "liq_spread_bps": _d(self.liq_spread_bps),
            "liq_vol_1m": _d(self.liq_vol_1m),
            "liq_vol_baseline": _d(self.liq_vol_baseline),
            "liq_atr_norm": _d(self.liq_atr_norm),
            "liq_mode": self.liq_mode,
            "liq_penalty_points": int(self.liq_penalty_points),
            "liq_reasons": self.liq_reasons,

            # Argus annotations
            "argus_profile": self.argus_profile,
            "argus_forced_regime": self.argus_forced_regime,
            "argus_forced_liq_block": int(bool(self.argus_forced_liq_block)),

            # Portfolio
            "cash_usd": _d(self.cash_usd),
            "position_qty": _d(self.position_qty),
            "avg_entry_px": _d(self.avg_entry_px),
            "equity_usd": _d(self.equity_usd),
            "exposure_usd": _d(self.exposure_usd),
            "unrl_pnl_usd": _d(self.unrl_pnl_usd),
            "realized_pnl_usd": _d(self.realized_pnl_usd),
            "trades_today": int(self.trades_today),
            "daily_realized_usd": _d(self.daily_realized_usd),
            "lockout_until_epoch": int(self.lockout_until_epoch),
            "cooldown_remaining_s": int(self.cooldown_remaining_s),

            # Exits / distances
            "hold_s": int(self.hold_s),
            "trend_below_count": int(self.trend_below_count),
            "peak_price": _d(self.peak_price),
            "take_profit": _d(self.take_profit),
            "stop_loss": _d(self.stop_loss),
            "trail_stop": _d(self.trail_stop),
            "dist_ma200": _d(self.dist_ma200),
            "dist_tp": _d(self.dist_tp),
            "dist_sl": _d(self.dist_sl),
            "dist_trail": _d(self.dist_trail),
            "min_dist": _d(self.min_dist),

            # MFE/MAE
            "mfe_pct": _d(self.mfe_pct),
            "mae_pct": _d(self.mae_pct),

            # Action
            "action": self.action,
            "action_reason": self.action_reason,
            "risk_blocked_reason": self.risk_blocked_reason,
            "next_poll_s": float(self.next_poll_s),
        }

        # ------------------------------------------------------------
        # Session compatibility aliases (engine.py naming drift)
        # ------------------------------------------------------------
        if legacy_session_bonus is not None:
            row["session_bonus"] = int(legacy_session_bonus)
        if legacy_session_reason is not None and str(legacy_session_reason) != "":
            row["session_reasons"] = str(legacy_session_reason)

        # ------------------------------------------------------------
        # Compatibility aliases (prevents blanks if io_logs header uses old names)
        # ------------------------------------------------------------
        row["price"] = row["px"]                          # header: "price"
        row["signal"] = row.get("sig_1m")                 # header: "signal"
        row["trend_ok"] = row.get("trend_ok_1m")          # header: "trend_ok"
        row["score"] = row.get("score_1m")                # header: "score"
        row["reasons"] = row.get("reasons_1m")            # header: "reasons"

        row["candle_start"] = row.get("candle_start_1m")  # header: "candle_start"
        row["candle_close"] = row.get("candle_close_1m")  # header: "candle_close"

        row["equity"] = row.get("equity_usd")             # older prints sometimes use equity
        row["qty"] = row.get("position_qty")              # some code uses qty
        row["unrl"] = row.get("unrl_pnl_usd")
        row["realized"] = row.get("realized_pnl_usd")

        row["stale_data"] = row.get("stale")              # header: "stale_data"

        return row
