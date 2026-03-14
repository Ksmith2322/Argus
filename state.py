#!/usr/bin/env python3
# state.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Any, Dict
import json
import os
import tempfile
import time

# line above: from typing import Optional, Any, Dict
from candles import CandleBuilder

# repo-root modules must use TOP-LEVEL imports when executed as top-level modules
from confluence import ConfluenceEngine, ConfluenceResult
from ledger import VirtualLedger
from risk import RiskManager
from strategy_phase2 import Phase2Strategy, StrategyState
from trade_tracker import TradeTracker

# --------- LINE ABOVE: from trade_tracker import TradeTracker
# Phase 4 additions
from regime import RegimeEngine, RegimeResult
from adaptive_confluence import AdaptiveConfluenceEngine

# Phase 5A additions
from structure import StructureEngine, StructureResult

# Phase 5B additions
from liquidity import LiquidityEngine, LiquidityResult

# Phase 18 additions
from trendlines import TrendlineEngine, TrendlineResult

# --------- LINE ABOVE: from liquidity import LiquidityEngine, LiquidityResult
# Phase 8 additions
from execution.adapter import ExecutionAdapter
from execution.paper_adapter import PaperAdapter


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


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_int(x: Any, default: int = 0) -> int:
    if x is None:
        return default
    try:
        return int(x)
    except Exception:
        try:
            return int(float(str(x)))
        except Exception:
            return default


def _as_str(x: Any, default: str = "") -> str:
    if x is None:
        return default
    s = str(x).strip()
    return s if s else default


def _safe_decimal_or_none(x: Any) -> Optional[Decimal]:
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    try:
        return Decimal(s)
    except Exception:
        return None


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    """
    Atomic JSON write:
    - write to temp file in same directory
    - fsync
    - replace target
    """
    # line above: directory = os.path.dirname(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_state_",
        suffix=".json",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _read_json_dict(path: str) -> Dict[str, Any]:
    # line above: if not os.path.exists(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


@dataclass
class BotState:
    # -------------------------
    # Identity
    # -------------------------
    symbol: str

    # -------------------------
    # Multi-timeframe candle builders
    # -------------------------
    candles_1m: CandleBuilder
    candles_5m: CandleBuilder
    candles_1h: CandleBuilder
    candles_4h: CandleBuilder

    # -------------------------
    # Strategies per TF
    # -------------------------
    strat_1m: Phase2Strategy
    strat_5m: Phase2Strategy
    strat_1h: Phase2Strategy

    # -------------------------
    # Confluence
    # -------------------------
    confluence: ConfluenceEngine
    last_conf: Optional[ConfluenceResult] = None

    # -------------------------
    # Phase 4: Regime + Adaptive Confluence
    # -------------------------
    regime_engine: Optional[RegimeEngine] = None
    regime: Optional[RegimeResult] = None
    adaptive_confluence: Optional[AdaptiveConfluenceEngine] = None

    # -------------------------
    # Phase 5A: Structure
    # -------------------------
    structure_engine: Optional[StructureEngine] = None
    last_structure: Optional[StructureResult] = None

    # -------------------------
    # Phase 5B: Liquidity
    # -------------------------
    liquidity_engine: Optional[LiquidityEngine] = None
    last_liquidity: Optional[LiquidityResult] = None

    # -------------------------
    # Phase 18: Trendlines (1h)
    # -------------------------
    trendline_engine: Optional[TrendlineEngine] = None
    last_trendline: Optional[TrendlineResult] = None

    # -------------------------
    # Phase 5C: Session overlay (cached for audit/debug)
    # -------------------------
    # line above: last_liquidity: Optional[LiquidityResult] = None
    last_session: str = ""
    last_session_labels: str = ""
    last_session_bonus: int = 0
    last_session_risk_mult: Optional[Decimal] = None
    last_session_reasons: str = ""

    # -------------------------
    # Ledger + risk + tracker
    # -------------------------
    ledger: Optional[VirtualLedger] = None
    risk: Optional[RiskManager] = None
    tracker: Optional[TradeTracker] = None

    # -------------------------
    # Phase 8: execution adapter + mode
    # -------------------------
    execution_mode: str = "ENGINE"
    execution_adapter: Optional[ExecutionAdapter] = None

    # -------------------------
    # Restart / runtime identity
    # -------------------------
    session_id: str = ""
    run_id: str = ""
    bot_state: str = "FLAT"
    recovery_state: str = "FLAT"

    # -------------------------
    # Execution identity / dedupe
    # -------------------------
    open_trade_id: str = ""
    entry_order_id: str = ""
    exit_order_id: str = ""
    last_processed_fill_id: str = ""
    last_processed_order_id: str = ""
    last_fill_poll_ts: int = 0

    # -------------------------
    # Last closed candles & strategy snapshots
    # -------------------------
    last_candle_close_1m: Optional[Decimal] = None
    last_candle_start_1m: Optional[int] = None

    last_candle_close_5m: Optional[Decimal] = None
    last_candle_start_5m: Optional[int] = None

    last_candle_close_1h: Optional[Decimal] = None
    last_candle_start_1h: Optional[int] = None

    last_candle_close_4h: Optional[Decimal] = None
    last_candle_start_4h: Optional[int] = None
    closed_candles_4h: list = None  # rolling buffer of recent closed 4h Candle objects

    last_st_1m: Optional[StrategyState] = None
    last_st_5m: Optional[StrategyState] = None
    last_st_1h: Optional[StrategyState] = None

    # Phase 5A helper: last closed 1m candle for rejection checks (set by engine.py)
    last_closed_candle_1m: Optional[Any] = None

    # -------------------------
    # Position / exits
    # -------------------------
    peak_price: Optional[Decimal] = None
    entry_epoch: Optional[int] = None
    trend_below_count: int = 0
    cooldown_until_epoch: int = 0
    last_action_ts: int = 0

    # -------------------------
    # MFE / MAE
    # -------------------------
    mfe_pct: Decimal = Decimal("0")
    mae_pct: Decimal = Decimal("0")
    high_since_entry: Optional[Decimal] = None
    low_since_entry: Optional[Decimal] = None

    # -------------------------
    # Feed health
    # -------------------------
    last_good_tick_epoch: Optional[int] = None
    stale_logged: bool = False

    # -------------------------
    # Runner helpers (optional)
    # -------------------------
    last_hold_bump_epoch: int = 0

    # -------------------------
    # Optional runtime reconciliation caches
    # -------------------------
    last_reconcile_epoch: int = 0
    last_execution_heartbeat_ok: bool = True

    # -------------------------
    # Snapshot metadata
    # -------------------------
    snapshot_state_version: int = 1
    snapshot_path: str = ""

    # -------------------------
    # Factory
    # -------------------------
    @classmethod
    def from_config(cls, cfg: dict) -> "BotState":
        # line above: symbol = cfg["PRODUCT_ID"]
        symbol = str(cfg.get("PRODUCT_ID", "")).strip() or "ETH-USD"

        # TF seconds: support both legacy + config.py names
        tf_1m_s = int(cfg.get("CANDLE_SECONDS", 60))
        tf_5m_s = int(cfg.get("CANDLE_SECONDS_5M", cfg.get("TF_5M_SECONDS", 300)))
        tf_1h_s = int(cfg.get("CANDLE_SECONDS_1H", cfg.get("TF_1H_SECONDS", 3600)))

        tf_4h_s = int(cfg.get("CANDLE_SECONDS_4H", cfg.get("TF_4H_SECONDS", 14400)))

        candles_1m = CandleBuilder(tf_1m_s)
        candles_5m = CandleBuilder(tf_5m_s)
        candles_1h = CandleBuilder(tf_1h_s)
        candles_4h = CandleBuilder(tf_4h_s)

        strat_1m = Phase2Strategy(cfg)
        strat_5m = Phase2Strategy(cfg)
        strat_1h = Phase2Strategy(cfg)

        confluence = ConfluenceEngine.from_config(cfg)

        ledger = VirtualLedger.from_config(cfg)
        risk = RiskManager.new()
        tracker = TradeTracker.from_config(cfg)

        # --------- LINE ABOVE: tracker = TradeTracker.from_config(cfg)
        # Phase 4 engines
        regime_engine = RegimeEngine.from_config(cfg)
        adaptive_confluence = AdaptiveConfluenceEngine.from_config(cfg)

        # Phase 5A engine (optional)
        use_structure = _as_bool(cfg.get("USE_STRUCTURE", False), False)
        structure_engine = StructureEngine.from_config(cfg) if use_structure else None

        # --------- LINE ABOVE: structure_engine = StructureEngine.from_config(cfg) if use_structure else None
        # Phase 18 engine (optional)
        use_trendlines = _as_bool(cfg.get("USE_TRENDLINES", False), False)
        trendline_engine = TrendlineEngine.from_config(cfg) if use_trendlines else None

        # Phase 5B engine (optional)
        # Prefer canonical flag name from config.py
        use_liq = _as_bool(
            cfg.get("USE_LIQUIDITY_FILTERS", cfg.get("USE_LIQUIDITY", False)),
            False,
        )
        liquidity_engine = LiquidityEngine.from_config(cfg) if use_liq else None

        # --------- LINE ABOVE: liquidity_engine = LiquidityEngine.from_config(cfg) if use_liq else None
        # Phase 8 execution mode / adapter
        execution_mode = _as_str(cfg.get("EXECUTION_MODE", "ENGINE"), "ENGINE").upper()
        execution_adapter: Optional[ExecutionAdapter] = None

        # line above: ops_log_dir = _as_str(cfg.get("OPS_LOG_DIR", "ops/logs"), "ops/logs")
        ops_log_dir = _as_str(cfg.get("OPS_LOG_DIR", "ops/logs"), "ops/logs")
        os.makedirs(ops_log_dir, exist_ok=True)

        if execution_mode == "ADAPTER":
            adapter_name = _as_str(cfg.get("EXECUTION_ADAPTER", "PAPER"), "PAPER").upper()

            if adapter_name == "PAPER":
                execution_adapter = PaperAdapter(
                    # line above: artifact_dir=ops_log_dir,
                    artifact_dir=ops_log_dir,
                    starting_cash=_as_decimal(
                        cfg.get("START_CASH_USD", cfg.get("STARTING_CASH_USD", "500")),
                        "500",
                    ),
                    currency=_as_str(cfg.get("ACCOUNT_CURRENCY", "USD"), "USD"),
                    fee_bps=_as_decimal(cfg.get("PAPER_FEE_BPS", "0"), "0"),
                    slippage_bps=_as_decimal(cfg.get("PAPER_SLIPPAGE_BPS", "0"), "0"),
                    qty_precision=_as_str(
                        cfg.get("PAPER_QTY_PRECISION", "0.00000001"),
                        "0.00000001",
                    ),
                    px_precision=_as_str(cfg.get("PAPER_PX_PRECISION", "0.01"), "0.01"),
                )
            else:
                raise ValueError(f"Unsupported EXECUTION_ADAPTER={adapter_name!r}")

        # --------- LINE ABOVE: else: raise ValueError(...)
        state_dir = _as_str(cfg.get("STATE_DIR", "./state"), "./state")
        os.makedirs(state_dir, exist_ok=True)

        safe_symbol = symbol.replace("/", "_").replace("-", "_")
        snapshot_path = os.path.join(state_dir, f"runtime_state_{safe_symbol}.json")

        session_id = _as_str(cfg.get("SESSION_ID", ""), "")
        run_id = _as_str(cfg.get("RUN_ID", ""), "")
        if not session_id:
            session_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if not run_id:
            run_id = session_id

        return cls(
            symbol=symbol,
            candles_1m=candles_1m,
            candles_5m=candles_5m,
            candles_1h=candles_1h,
            candles_4h=candles_4h,
            closed_candles_4h=[],
            strat_1m=strat_1m,
            strat_5m=strat_5m,
            strat_1h=strat_1h,
            confluence=confluence,
            ledger=ledger,
            risk=risk,
            tracker=tracker,
            regime_engine=regime_engine,
            adaptive_confluence=adaptive_confluence,
            structure_engine=structure_engine,
            trendline_engine=trendline_engine,
            liquidity_engine=liquidity_engine,
            execution_mode=execution_mode,
            execution_adapter=execution_adapter,
            session_id=session_id,
            run_id=run_id,
            bot_state="FLAT",
            recovery_state="FLAT",
            snapshot_path=snapshot_path,
        )

    # -------------------------
    # Phase 8 helpers
    # -------------------------
    def is_adapter_mode(self) -> bool:
        return str(self.execution_mode).upper() == "ADAPTER" and self.execution_adapter is not None

    def is_engine_mode(self) -> bool:
        return str(self.execution_mode).upper() == "ENGINE"

    def get_execution_adapter(self) -> Optional[ExecutionAdapter]:
        return self.execution_adapter

    def execution_heartbeat(self) -> bool:
        if self.execution_adapter is None:
            self.last_execution_heartbeat_ok = True
            return True

        try:
            ok = bool(self.execution_adapter.heartbeat())
            self.last_execution_heartbeat_ok = ok
            return ok
        except Exception:
            self.last_execution_heartbeat_ok = False
            return False

    def refresh_from_execution(self) -> None:
        """
        Minimal reconciliation helper for Phase 8.
        This is intentionally narrow:
        - pulls account snapshot from adapter
        - updates ledger surface fields only if adapter mode is enabled

        Do not turn this into a second ledger. Fills remain the source of truth.
        """
        # --------- LINE ABOVE: Do not turn this into a second ledger. Fills remain the source of truth.
        if not self.is_adapter_mode() or self.execution_adapter is None or self.ledger is None:
            return

        acct = self.execution_adapter.get_account_state()
        positions = self.execution_adapter.get_positions()

        # Best-effort ledger surface sync. Assumes ledger exposes these attrs.
        if hasattr(self.ledger, "cash_usd"):
            self.ledger.cash_usd = acct.cash
        if hasattr(self.ledger, "equity_usd"):
            self.ledger.equity_usd = acct.equity
        if hasattr(self.ledger, "realized_pnl_usd"):
            self.ledger.realized_pnl_usd = acct.realized_pnl

        if positions:
            pos = positions[0]
            if pos.symbol == self.symbol:
                if hasattr(self.ledger, "position_qty"):
                    self.ledger.position_qty = pos.qty
                if hasattr(self.ledger, "avg_entry_px"):
                    self.ledger.avg_entry_px = pos.avg_entry_px
                if hasattr(self.ledger, "exposure_usd"):
                    mark_px = pos.mark_px if pos.mark_px is not None else pos.avg_entry_px
                    self.ledger.exposure_usd = pos.qty * mark_px
                if hasattr(self.ledger, "unrl_pnl_usd"):
                    self.ledger.unrl_pnl_usd = pos.unrealized_pnl
        else:
            if hasattr(self.ledger, "position_qty"):
                self.ledger.position_qty = Decimal("0")
            if hasattr(self.ledger, "avg_entry_px"):
                self.ledger.avg_entry_px = None
            if hasattr(self.ledger, "exposure_usd"):
                self.ledger.exposure_usd = Decimal("0")
            if hasattr(self.ledger, "unrl_pnl_usd"):
                self.ledger.unrl_pnl_usd = Decimal("0")

    # -------------------------
    # Snapshot helpers
    # -------------------------
    def runtime_snapshot_payload(self) -> Dict[str, Any]:
        """
        Minimum persisted restart state for Phase 8.6.
        """
        # line above: cash = None
        cash = None
        qty = Decimal("0")
        avg_entry = None

        if self.ledger is not None:
            if hasattr(self.ledger, "cash_usd"):
                cash = getattr(self.ledger, "cash_usd", None)
            elif hasattr(self.ledger, "cash"):
                cash = getattr(self.ledger, "cash", None)

            if hasattr(self.ledger, "position_qty"):
                qty = _as_decimal(getattr(self.ledger, "position_qty", None), "0")
            elif hasattr(self.ledger, "qty"):
                qty = _as_decimal(getattr(self.ledger, "qty", None), "0")

            if hasattr(self.ledger, "avg_entry_px"):
                avg_entry = getattr(self.ledger, "avg_entry_px", None)
            elif hasattr(self.ledger, "avg_entry"):
                avg_entry = getattr(self.ledger, "avg_entry", None)
            elif hasattr(self.ledger, "avg_cost"):
                avg_entry = getattr(self.ledger, "avg_cost", None)

        return {
            "state_version": int(self.snapshot_state_version),
            "saved_at": int(time.time()),
            "run_id": str(self.run_id),
            "session_id": str(self.session_id),
            "symbol": str(self.symbol),
            "bot_state": str(self.bot_state or ""),
            "recovery_state": str(self.recovery_state or ""),
            "position_qty": str(qty),
            "avg_entry": "" if avg_entry is None else str(avg_entry),
            "cash": "" if cash is None else str(cash),
            "open_trade_id": str(self.open_trade_id or ""),
            "entry_order_id": str(self.entry_order_id or ""),
            "exit_order_id": str(self.exit_order_id or ""),
            "last_processed_fill_id": str(self.last_processed_fill_id or ""),
            "last_processed_order_id": str(self.last_processed_order_id or ""),
            "cooldown_until": int(self.cooldown_until_epoch or 0),
            "last_action_ts": int(self.last_action_ts or 0),
            "last_fill_poll_ts": int(self.last_fill_poll_ts or 0),
            "entry_epoch": int(self.entry_epoch or 0),
        }

    def save_runtime_snapshot(self, path: Optional[str] = None) -> str:
        """
        Atomic snapshot write.
        Call this after every material execution transition:
        - after entry submit
        - after entry fill
        - after exit submit
        - after exit fill
        - after reconcile completes
        """
        # --------- LINE ABOVE: target = path or self.snapshot_path
        target = path or self.snapshot_path
        if not target:
            raise ValueError("snapshot path is empty")

        payload = self.runtime_snapshot_payload()
        _atomic_write_json(target, payload)
        self.snapshot_path = target
        return target

    def load_runtime_snapshot(self, path: Optional[str] = None) -> Dict[str, Any]:
        # --------- LINE ABOVE: target = path or self.snapshot_path
        target = path or self.snapshot_path
        if not target:
            return {}
        data = _read_json_dict(target)
        if data:
            self.snapshot_path = target
        return data

    def restore_runtime_snapshot(self, payload: Dict[str, Any]) -> None:
        """
        Best-effort restore from persisted runtime snapshot.
        This does not outrank fills/orders during restart reconcile.
        It only restores cached runtime fields and ledger surface fields.
        """
        # --------- LINE ABOVE: if not payload:
        if not payload:
            return

        self.snapshot_state_version = _as_int(payload.get("state_version"), 1)
        self.run_id = _as_str(payload.get("run_id"), self.run_id)
        self.session_id = _as_str(payload.get("session_id"), self.session_id)
        self.symbol = _as_str(payload.get("symbol"), self.symbol)

        self.bot_state = _as_str(payload.get("bot_state"), self.bot_state or "FLAT").upper()
        self.recovery_state = _as_str(payload.get("recovery_state"), self.recovery_state or "FLAT").upper()

        self.open_trade_id = _as_str(payload.get("open_trade_id"), "")
        self.entry_order_id = _as_str(payload.get("entry_order_id"), "")
        self.exit_order_id = _as_str(payload.get("exit_order_id"), "")
        self.last_processed_fill_id = _as_str(payload.get("last_processed_fill_id"), "")
        self.last_processed_order_id = _as_str(payload.get("last_processed_order_id"), "")
        self.cooldown_until_epoch = _as_int(payload.get("cooldown_until"), 0)
        self.last_action_ts = _as_int(payload.get("last_action_ts"), 0)
        self.last_fill_poll_ts = _as_int(payload.get("last_fill_poll_ts"), 0)

        entry_epoch = _as_int(payload.get("entry_epoch"), 0)
        self.entry_epoch = entry_epoch if entry_epoch > 0 else None

        qty = _as_decimal(payload.get("position_qty"), "0")
        avg_entry = _safe_decimal_or_none(payload.get("avg_entry"))
        cash = _safe_decimal_or_none(payload.get("cash"))

        if self.ledger is not None:
            if hasattr(self.ledger, "position_qty"):
                self.ledger.position_qty = qty
            elif hasattr(self.ledger, "qty"):
                self.ledger.qty = qty

            if hasattr(self.ledger, "avg_entry_px"):
                self.ledger.avg_entry_px = avg_entry
            elif hasattr(self.ledger, "avg_entry"):
                self.ledger.avg_entry = avg_entry
            elif hasattr(self.ledger, "avg_cost"):
                self.ledger.avg_cost = avg_entry

            if cash is not None:
                if hasattr(self.ledger, "cash_usd"):
                    self.ledger.cash_usd = cash
                elif hasattr(self.ledger, "cash"):
                    self.ledger.cash = cash

            if hasattr(self.ledger, "exposure_usd"):
                if qty > 0 and avg_entry is not None:
                    self.ledger.exposure_usd = qty * avg_entry
                else:
                    self.ledger.exposure_usd = Decimal("0")

            if hasattr(self.ledger, "unrl_pnl_usd") and qty <= 0:
                self.ledger.unrl_pnl_usd = Decimal("0")

        if qty > 0 and avg_entry is not None:
            self.peak_price = avg_entry if self.peak_price is None else self.peak_price
            self.high_since_entry = avg_entry if self.high_since_entry is None else self.high_since_entry
            self.low_since_entry = avg_entry if self.low_since_entry is None else self.low_since_entry
        else:
            self.peak_price = None
            self.high_since_entry = None
            self.low_since_entry = None

    def save_snapshot_after_reconcile(self) -> str:
        # --------- LINE ABOVE: self.last_reconcile_epoch = int(time.time())
        self.last_reconcile_epoch = int(time.time())
        return self.save_runtime_snapshot()

    def save_snapshot_after_entry_submit(self, order_id: str = "") -> str:
        # --------- LINE ABOVE: self.bot_state = "ENTERING"
        self.bot_state = "ENTERING"
        self.recovery_state = "ENTERING"
        self.entry_order_id = _as_str(order_id, self.entry_order_id)
        self.last_action_ts = int(time.time())
        return self.save_runtime_snapshot()

    def save_snapshot_after_entry_fill(self, trade_id: str = "", fill_id: str = "") -> str:
        # --------- LINE ABOVE: self.bot_state = "OPEN"
        self.bot_state = "OPEN"
        self.recovery_state = "OPEN"
        self.open_trade_id = _as_str(trade_id, self.open_trade_id)
        self.last_processed_fill_id = _as_str(fill_id, self.last_processed_fill_id)
        self.last_action_ts = int(time.time())
        return self.save_runtime_snapshot()

    def save_snapshot_after_exit_submit(self, order_id: str = "") -> str:
        # --------- LINE ABOVE: self.bot_state = "EXITING"
        self.bot_state = "EXITING"
        self.recovery_state = "EXITING"
        self.exit_order_id = _as_str(order_id, self.exit_order_id)
        self.last_action_ts = int(time.time())
        return self.save_runtime_snapshot()

    def save_snapshot_after_exit_fill(self, fill_id: str = "") -> str:
        # --------- LINE ABOVE: self.bot_state = "FLAT"
        self.bot_state = "FLAT"
        self.recovery_state = "FLAT"
        self.open_trade_id = ""
        self.entry_order_id = ""
        self.exit_order_id = ""
        self.last_processed_fill_id = _as_str(fill_id, self.last_processed_fill_id)
        self.last_action_ts = int(time.time())
        self.entry_epoch = None
        self.peak_price = None
        self.high_since_entry = None
        self.low_since_entry = None
        return self.save_runtime_snapshot()