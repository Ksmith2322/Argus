# state.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Any

# line above: from typing import Optional, Any
from .candles import CandleBuilder

# ✅ FIX: all internal modules must be imported relatively
from .confluence import ConfluenceEngine, ConfluenceResult
from .ledger import VirtualLedger
from .risk import RiskManager
from .strategy_phase2 import Phase2Strategy, StrategyState
from .trade_tracker import TradeTracker

# --------- LINE ABOVE: from .trade_tracker import TradeTracker
# Phase 4 additions
from .regime import RegimeEngine, RegimeResult
from .adaptive_confluence import AdaptiveConfluenceEngine

# Phase 5A additions
from .structure import StructureEngine, StructureResult

# Phase 5B additions
from .liquidity import LiquidityEngine, LiquidityResult


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


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
    # Last closed candles & strategy snapshots
    # -------------------------
    last_candle_close_1m: Optional[Decimal] = None
    last_candle_start_1m: Optional[int] = None

    last_candle_close_5m: Optional[Decimal] = None
    last_candle_start_5m: Optional[int] = None

    last_candle_close_1h: Optional[Decimal] = None
    last_candle_start_1h: Optional[int] = None

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

        candles_1m = CandleBuilder(tf_1m_s)
        candles_5m = CandleBuilder(tf_5m_s)
        candles_1h = CandleBuilder(tf_1h_s)

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
        # Phase 5B engine (optional)
        # Prefer canonical flag name from config.py
        use_liq = _as_bool(
            cfg.get("USE_LIQUIDITY_FILTERS", cfg.get("USE_LIQUIDITY", False)),
            False,
        )
        liquidity_engine = LiquidityEngine.from_config(cfg) if use_liq else None

        return cls(
            symbol=symbol,
            candles_1m=candles_1m,
            candles_5m=candles_5m,
            candles_1h=candles_1h,
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
            liquidity_engine=liquidity_engine,
        )
