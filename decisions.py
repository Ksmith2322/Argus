#!/usr/bin/env python3
# decisions.py
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional
import hashlib


def _safe_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _dec_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, Decimal):
        return str(x)
    try:
        return str(Decimal(str(x)))
    except Exception:
        return str(x)


def _bool_int(x: Optional[bool]) -> Any:
    if x is None:
        return None
    return int(bool(x))


def _hash_key(*parts: Any) -> str:
    raw = "|".join(_safe_str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def make_intent_id(
    *,
    symbol: str,
    action: str,
    epoch: int,
    px: Any,
    score: Any = "",
) -> str:
    """
    Deterministic intent id for a single engine decision moment.
    """
    # line above: return f"intent_{_hash_key(...)}"
    return f"intent_{_hash_key(symbol, action, epoch, _dec_str(px), score)}"


def make_client_order_id(
    *,
    symbol: str,
    action: str,
    intent_id: str,
    epoch: int,
) -> str:
    """
    Deterministic client order id derived from intent identity.
    This makes restart resubmission suppression possible.
    """
    # line above: return f"coid_{_hash_key(...)}"
    return f"coid_{_hash_key(symbol, action, intent_id, epoch)}"


def make_fill_fingerprint(
    *,
    fill_id: str = "",
    order_id: str = "",
    client_order_id: str = "",
    trade_id: str = "",
    symbol: str = "",
    side: str = "",
    ts: Any = "",
    qty: Any = "",
    px: Any = "",
) -> str:
    """
    Stable fallback dedupe key when native fill_id is missing or unreliable.
    """
    # line above: native = _safe_str(fill_id)
    native = _safe_str(fill_id)
    if native:
        return native

    return f"fillfp_{_hash_key(order_id, client_order_id, trade_id, symbol, side, ts, _dec_str(qty), _dec_str(px))}"


@dataclass
class EngineEvent:
    name: str
    message: str
    notify_title: Optional[str] = None
    notify_body: Optional[str] = None
    client_order_id: Optional[str] = None
    order_id: Optional[str] = None
    trade_id: Optional[str] = None

    # -------------------------
    # Phase 8 identity / dedupe
    # -------------------------
    intent_id: Optional[str] = None
    entry_intent_id: Optional[str] = None
    exit_intent_id: Optional[str] = None
    fill_id: Optional[str] = None
    fill_fingerprint: Optional[str] = None
    dedupe_key: Optional[str] = None
    recovery_state: Optional[str] = None
    dedupe_reason: Optional[str] = None

    def to_row(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "message": self.message,
            "notify_title": self.notify_title,
            "notify_body": self.notify_body,
            "intent_id": self.intent_id,
            "entry_intent_id": self.entry_intent_id,
            "exit_intent_id": self.exit_intent_id,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
            "fill_id": self.fill_id,
            "fill_fingerprint": self.fill_fingerprint,
            "dedupe_key": self.dedupe_key,
            "recovery_state": self.recovery_state,
            "dedupe_reason": self.dedupe_reason,
        }


@dataclass
class DecisionSnapshot:
    """
    A single engine step output (logged to CSV + printed in console).

    Contracts:
      - live runner calls:     snap.to_row()
      - backtest runner calls: snap.to_signals_row()

    Phase 5 / Phase 8 readiness:
      - Structure fields explicit
      - Liquidity fields explicit
      - Session overlay explicit
      - Execution identity fields explicit
      - Execution quantity explicit
      - Dedupe / recovery fields explicit
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
    # Phase 8 execution identity
    # -------------------------
    intent_id: str = ""
    entry_intent_id: str = ""
    exit_intent_id: str = ""
    client_order_id: str = ""
    order_id: str = ""
    trade_id: str = ""
    execution_mode: str = ""
    execution_status: str = ""
    execution_reason: str = ""

    # -------------------------
    # Phase 8 dedupe / recovery identity
    # -------------------------
    fill_id: str = ""
    fill_fingerprint: str = ""
    dedupe_key: str = ""
    dedupe_reason: str = ""
    recovery_state: str = ""
    recovery_source: str = ""
    action_suppressed: bool = False
    action_suppressed_reason: str = ""
    submit_allowed: bool = True
    fill_apply_allowed: bool = True

    # -------------------------
    # Phase 8 execution sizing
    # -------------------------
    execution_qty: Optional[Decimal] = None

    # -------------------------
    # Phase 5C: Session overlay
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
    candle_volume_1m: Optional[Decimal] = None  # engine.py sets this

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
    # Phase 18: Trendlines (1h, and multi-TF 5m/1h/4h)
    # -------------------------
    near_support_tl: bool = False
    near_resist_tl: bool = False
    broke_above_resist_tl: bool = False
    broke_below_support_tl: bool = False
    tl_proj_support: Optional[Decimal] = None
    tl_proj_resist: Optional[Decimal] = None
    trendline_reasons: str = ""
    # Multi-TF result object (not serialized to CSV directly — accessed by confluence)
    mtf_trendlines: Optional[Any] = None

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
    # ML Governor
    # -------------------------
    governor_win_prob: Optional[float] = None
    governor_recommendation: str = ""
    governor_score_modifier: int = 0

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
    # Identity helpers
    # -------------------------
    def ensure_intent_identity(self) -> None:
        """
        Stamp deterministic intent / client order identity if missing.
        """
        # line above: action_u = _safe_str(self.action).upper()
        action_u = _safe_str(self.action).upper()
        if action_u not in {"BUY", "SELL"}:
            return

        score = self.confluence_score if self.confluence_score is not None else self.score_1m

        if not self.intent_id:
            self.intent_id = make_intent_id(
                symbol=self.symbol,
                action=action_u,
                epoch=int(self.epoch),
                px=self.px,
                score=score,
            )

        if action_u == "BUY" and not self.entry_intent_id:
            self.entry_intent_id = self.intent_id

        if action_u == "SELL" and not self.exit_intent_id:
            self.exit_intent_id = self.intent_id

        if not self.client_order_id:
            self.client_order_id = make_client_order_id(
                symbol=self.symbol,
                action=action_u,
                intent_id=self.intent_id,
                epoch=int(self.epoch),
            )

        if not self.dedupe_key:
            self.dedupe_key = self.client_order_id or self.intent_id

    def apply_recovery_guard(
        self,
        *,
        recovery_state: str,
        reason: str,
        suppress_action: bool = True,
        allow_submit: bool = False,
    ) -> None:
        """
        Mark a decision as recovery-suppressed so runner logging stays explicit.
        """
        # line above: self.recovery_state = _safe_str(recovery_state).upper()
        self.recovery_state = _safe_str(recovery_state).upper()
        self.action_suppressed = bool(suppress_action)
        self.action_suppressed_reason = _safe_str(reason)
        self.submit_allowed = bool(allow_submit)
        self.dedupe_reason = _safe_str(reason)

        if suppress_action:
            self.execution_status = "RECOVERY_SUPPRESSED"
            self.execution_reason = _safe_str(reason)
            self.risk_blocked_reason = _safe_str(reason)
            self.action = "HOLD"
            self.action_reason = _safe_str(reason)

    def attach_order_identity(
        self,
        *,
        client_order_id: str = "",
        order_id: str = "",
        trade_id: str = "",
        execution_status: str = "",
        execution_reason: str = "",
    ) -> None:
        # line above: if client_order_id:
        if client_order_id:
            self.client_order_id = _safe_str(client_order_id)
        if order_id:
            self.order_id = _safe_str(order_id)
        if trade_id:
            self.trade_id = _safe_str(trade_id)
        if execution_status:
            self.execution_status = _safe_str(execution_status)
        if execution_reason:
            self.execution_reason = _safe_str(execution_reason)

        if not self.dedupe_key:
            self.dedupe_key = (
                self.client_order_id
                or self.order_id
                or self.trade_id
                or self.intent_id
            )

    def attach_fill_identity(
        self,
        *,
        fill_id: str = "",
        order_id: str = "",
        client_order_id: str = "",
        trade_id: str = "",
        side: str = "",
        ts: Any = "",
        qty: Any = "",
        px: Any = "",
    ) -> None:
        """
        Stamp fill identity onto the snapshot for logging / dedupe diagnostics.
        """
        # line above: if order_id:
        if order_id:
            self.order_id = _safe_str(order_id)
        if client_order_id:
            self.client_order_id = _safe_str(client_order_id)
        if trade_id:
            self.trade_id = _safe_str(trade_id)
        if fill_id:
            self.fill_id = _safe_str(fill_id)

        self.fill_fingerprint = make_fill_fingerprint(
            fill_id=fill_id,
            order_id=self.order_id,
            client_order_id=self.client_order_id,
            trade_id=self.trade_id,
            symbol=self.symbol,
            side=side,
            ts=ts,
            qty=qty,
            px=px,
        )

        if not self.dedupe_key:
            self.dedupe_key = self.fill_fingerprint

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

        # Pull in legacy attrs that engine.py may have set without the dataclass
        # knowing about them yet. This prevents drift from causing blank columns
        # or attribute errors during logging.
        legacy_session_bonus = getattr(self, "session_bonus_points", None)
        legacy_session_reason = getattr(self, "session_reason", None)

        legacy_client_order_id = getattr(self, "client_order_id", None)
        legacy_order_id = getattr(self, "order_id", None)
        legacy_trade_id = getattr(self, "trade_id", None)
        legacy_execution_qty = getattr(self, "execution_qty", None)
        legacy_fill_id = getattr(self, "fill_id", None)
        legacy_fill_fingerprint = getattr(self, "fill_fingerprint", None)
        legacy_dedupe_key = getattr(self, "dedupe_key", None)

        row: Dict[str, Any] = {
            # Canonical core
            "ts": self.ts,
            "epoch": int(self.epoch),
            "symbol": self.symbol,
            "px": _d(self.px),
            "paused": int(bool(self.paused)),
            "stale": int(bool(self.stale)),

            # Phase 8 execution identity
            "intent_id": self.intent_id,
            "entry_intent_id": self.entry_intent_id,
            "exit_intent_id": self.exit_intent_id,
            "client_order_id": self.client_order_id,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
            "execution_mode": self.execution_mode,
            "execution_status": self.execution_status,
            "execution_reason": self.execution_reason,

            # Phase 8 dedupe / recovery identity
            "fill_id": self.fill_id,
            "fill_fingerprint": self.fill_fingerprint,
            "dedupe_key": self.dedupe_key,
            "dedupe_reason": self.dedupe_reason,
            "recovery_state": self.recovery_state,
            "recovery_source": self.recovery_source,
            "action_suppressed": int(bool(self.action_suppressed)),
            "action_suppressed_reason": self.action_suppressed_reason,
            "submit_allowed": int(bool(self.submit_allowed)),
            "fill_apply_allowed": int(bool(self.fill_apply_allowed)),

            "execution_qty": _d(self.execution_qty),

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

            # Trendlines (Phase 18)
            "near_support_tl": int(bool(self.near_support_tl)),
            "near_resist_tl": int(bool(self.near_resist_tl)),
            "broke_above_resist_tl": int(bool(self.broke_above_resist_tl)),
            "broke_below_support_tl": int(bool(self.broke_below_support_tl)),
            "tl_proj_support": _d(self.tl_proj_support),
            "tl_proj_resist": _d(self.tl_proj_resist),
            "trendline_reasons": self.trendline_reasons,

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
        # Execution compatibility aliases
        # ------------------------------------------------------------
        if legacy_client_order_id is not None and str(legacy_client_order_id) != "":
            row["client_order_id"] = str(legacy_client_order_id)
        if legacy_order_id is not None and str(legacy_order_id) != "":
            row["order_id"] = str(legacy_order_id)
        if legacy_trade_id is not None and str(legacy_trade_id) != "":
            row["trade_id"] = str(legacy_trade_id)
        if legacy_execution_qty is not None:
            row["execution_qty"] = _d(legacy_execution_qty)
        if legacy_fill_id is not None and str(legacy_fill_id) != "":
            row["fill_id"] = str(legacy_fill_id)
        if legacy_fill_fingerprint is not None and str(legacy_fill_fingerprint) != "":
            row["fill_fingerprint"] = str(legacy_fill_fingerprint)
        if legacy_dedupe_key is not None and str(legacy_dedupe_key) != "":
            row["dedupe_key"] = str(legacy_dedupe_key)

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
        row["qty"] = row.get("position_qty")              # some code uses qty = current position qty
        row["exec_qty"] = row.get("execution_qty")        # execution-facing alias
        row["order_qty"] = row.get("execution_qty")       # alternate execution-facing alias
        row["unrl"] = row.get("unrl_pnl_usd")
        row["realized"] = row.get("realized_pnl_usd")

        row["stale_data"] = row.get("stale")              # header: "stale_data"

        # Older / alternate execution names you may already have in io_logs or analysis.
        row["entry_order_id"] = row.get("order_id")
        row["entry_trade_id"] = row.get("trade_id")
        row["exec_status"] = row.get("execution_status")
        row["exec_reason"] = row.get("execution_reason")

        # Dedupe aliases
        row["fill_key"] = row.get("fill_fingerprint")
        row["submit_key"] = row.get("client_order_id") or row.get("intent_id")
        row["recovery_guard"] = int(bool(self.action_suppressed))

        return row

    def add_event(
        self,
        name: str,
        message: str,
        *,
        notify_title: Optional[str] = None,
        notify_body: Optional[str] = None,
        client_order_id: Optional[str] = None,
        order_id: Optional[str] = None,
        trade_id: Optional[str] = None,
        intent_id: Optional[str] = None,
        entry_intent_id: Optional[str] = None,
        exit_intent_id: Optional[str] = None,
        fill_id: Optional[str] = None,
        fill_fingerprint: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        recovery_state: Optional[str] = None,
        dedupe_reason: Optional[str] = None,
    ) -> None:
        self.events.append(
            EngineEvent(
                name=name,
                message=message,
                notify_title=notify_title,
                notify_body=notify_body,
                client_order_id=client_order_id,
                order_id=order_id,
                trade_id=trade_id,
                intent_id=intent_id,
                entry_intent_id=entry_intent_id,
                exit_intent_id=exit_intent_id,
                fill_id=fill_id,
                fill_fingerprint=fill_fingerprint,
                dedupe_key=dedupe_key,
                recovery_state=recovery_state,
                dedupe_reason=dedupe_reason,
            )
        )