# config.py
import os
from decimal import Decimal, getcontext
from dotenv import load_dotenv

getcontext().prec = 50


def _d(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default))


def _i(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def _f(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def _b(name: str, default: str) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1", "true", "yes", "y", "on")


def _s(name: str, default: str) -> str:
    return str(os.getenv(name, default)).strip()


def _clamp_decimal(x: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _clamp_int(x: int, lo: int, hi: int) -> int:
    try:
        x = int(x)
    except Exception:
        x = lo
    return max(lo, min(hi, x))


def _clamp_float(x: float, lo: float, hi: float) -> float:
    try:
        x = float(x)
    except Exception:
        x = lo
    return max(lo, min(hi, x))


def load_config() -> dict:
    load_dotenv()
    cfg: dict = {}

    # -------------------------
    # Core
    # -------------------------
    cfg["PRODUCT_ID"] = _s("PRODUCT_ID", "ETH-USD")
    cfg["COINBASE_SPOT_URL"] = f"https://api.coinbase.com/v2/prices/{cfg['PRODUCT_ID']}/spot"
    cfg["COINBASE_EXCHANGE_CANDLES_URL"] = "https://api.exchange.coinbase.com/products/{product_id}/candles"

    # -------------------------
    # Candle settings
    # -------------------------
    cfg["CANDLE_SECONDS"] = _i("CANDLE_SECONDS", "60")
    cfg["CANDLE_SECONDS_5M"] = _i("CANDLE_SECONDS_5M", "300")
    cfg["CANDLE_SECONDS_1H"] = _i("CANDLE_SECONDS_1H", "3600")

    cfg["CANDLE_SECONDS"] = _clamp_int(cfg["CANDLE_SECONDS"], 1, 86_400)
    cfg["CANDLE_SECONDS_5M"] = _clamp_int(cfg["CANDLE_SECONDS_5M"], 1, 86_400)
    cfg["CANDLE_SECONDS_1H"] = _clamp_int(cfg["CANDLE_SECONDS_1H"], 1, 86_400)

    # -------------------------
    # Poll tiers
    # -------------------------
    cfg["POLL_SLOW_SECONDS"] = _f("POLL_SLOW_SECONDS", "15")
    cfg["POLL_MED_SECONDS"] = _f("POLL_MED_SECONDS", "5")
    cfg["POLL_FAST_SECONDS"] = _f("POLL_FAST_SECONDS", "2")
    cfg["POLL_TURBO_SECONDS"] = _f("POLL_TURBO_SECONDS", "1")

    cfg["POLL_SLOW_SECONDS"] = _clamp_float(cfg["POLL_SLOW_SECONDS"], 0.25, 600.0)
    cfg["POLL_MED_SECONDS"] = _clamp_float(cfg["POLL_MED_SECONDS"], 0.25, 600.0)
    cfg["POLL_FAST_SECONDS"] = _clamp_float(cfg["POLL_FAST_SECONDS"], 0.25, 600.0)
    cfg["POLL_TURBO_SECONDS"] = _clamp_float(cfg["POLL_TURBO_SECONDS"], 0.10, 600.0)

    # -------------------------
    # HTTP
    # -------------------------
    cfg["HTTP_TIMEOUT"] = _f("HTTP_TIMEOUT", "15")
    cfg["MAX_FAIL_BACKOFF_SECONDS"] = _f("MAX_FAIL_BACKOFF_SECONDS", "60")

    cfg["HTTP_TIMEOUT"] = _clamp_float(cfg["HTTP_TIMEOUT"], 2.0, 120.0)
    cfg["MAX_FAIL_BACKOFF_SECONDS"] = _clamp_float(cfg["MAX_FAIL_BACKOFF_SECONDS"], 5.0, 600.0)

    # -------------------------
    # Indicators
    # -------------------------
    cfg["MA_FAST"] = _i("MA_FAST", "5")
    cfg["MA_SLOW"] = _i("MA_SLOW", "20")
    cfg["MA_TREND_50"] = _i("MA_TREND_50", "50")
    cfg["MA_TREND_200"] = _i("MA_TREND_200", "200")

    cfg["MA_FAST"] = _clamp_int(cfg["MA_FAST"], 2, 5000)
    cfg["MA_SLOW"] = _clamp_int(cfg["MA_SLOW"], 3, 5000)
    cfg["MA_TREND_50"] = _clamp_int(cfg["MA_TREND_50"], 5, 10_000)
    cfg["MA_TREND_200"] = _clamp_int(cfg["MA_TREND_200"], 10, 50_000)

    # -------------------------
    # Exit model
    # -------------------------
    cfg["TAKE_PROFIT_PCT"] = _d("TAKE_PROFIT_PCT", "0.03")
    cfg["TRAIL_STOP_PCT"] = _d("TRAIL_STOP_PCT", "0.015")
    cfg["STOP_LOSS_PCT"] = _d("STOP_LOSS_PCT", "0.02")

    cfg["TAKE_PROFIT_PCT"] = _clamp_decimal(cfg["TAKE_PROFIT_PCT"], Decimal("0"), Decimal("1"))
    cfg["TRAIL_STOP_PCT"] = _clamp_decimal(cfg["TRAIL_STOP_PCT"], Decimal("0"), Decimal("1"))
    cfg["STOP_LOSS_PCT"] = _clamp_decimal(cfg["STOP_LOSS_PCT"], Decimal("0"), Decimal("1"))

    # -------------------------
    # Adaptive distance
    # -------------------------
    cfg["NEAR_ENTRY_PCT"] = _d("NEAR_ENTRY_PCT", "0.005")
    cfg["NEAR_EXIT_PCT"] = _d("NEAR_EXIT_PCT", "0.005")
    cfg["TURBO_PCT"] = _d("TURBO_PCT", "0.002")

    cfg["NEAR_ENTRY_PCT"] = _clamp_decimal(cfg["NEAR_ENTRY_PCT"], Decimal("0"), Decimal("1"))
    cfg["NEAR_EXIT_PCT"] = _clamp_decimal(cfg["NEAR_EXIT_PCT"], Decimal("0"), Decimal("1"))
    cfg["TURBO_PCT"] = _clamp_decimal(cfg["TURBO_PCT"], Decimal("0"), Decimal("1"))

    cfg["COOLDOWN_SECONDS"] = _i("COOLDOWN_SECONDS", "900")
    cfg["MIN_SPREAD_BPS"] = _d("MIN_SPREAD_BPS", "3")

    cfg["COOLDOWN_SECONDS"] = _clamp_int(cfg["COOLDOWN_SECONDS"], 0, 86_400)
    cfg["MIN_SPREAD_BPS"] = _clamp_decimal(cfg["MIN_SPREAD_BPS"], Decimal("0"), Decimal("10_000"))

    # -------------------------
    # Fees / slippage
    # -------------------------
    cfg["FEE_BPS"] = _d("FEE_BPS", "5")
    cfg["SLIPPAGE_BPS"] = _d("SLIPPAGE_BPS", "2")

    cfg["FEE_BPS"] = _clamp_decimal(cfg["FEE_BPS"], Decimal("0"), Decimal("1000"))
    cfg["SLIPPAGE_BPS"] = _clamp_decimal(cfg["SLIPPAGE_BPS"], Decimal("0"), Decimal("1000"))

    # -------------------------
    # Anti-thrash
    # -------------------------
    cfg["MA200_BAND_PCT"] = _d("MA200_BAND_PCT", "0.001")
    cfg["TREND_INVALIDATION_CLOSES"] = _i("TREND_INVALIDATION_CLOSES", "2")
    cfg["MIN_HOLD_SECONDS"] = _i("MIN_HOLD_SECONDS", "60")

    cfg["MA200_BAND_PCT"] = _clamp_decimal(cfg["MA200_BAND_PCT"], Decimal("0"), Decimal("1"))
    cfg["TREND_INVALIDATION_CLOSES"] = _clamp_int(cfg["TREND_INVALIDATION_CLOSES"], 0, 50_000)
    cfg["MIN_HOLD_SECONDS"] = _clamp_int(cfg["MIN_HOLD_SECONDS"], 0, 86_400)

    # --------- LINE ABOVE: cfg["MIN_HOLD_SECONDS"] = _i("MIN_HOLD_SECONDS", "60")
    stale_raw = _i("STALE_TICK_SECONDS", "20")
    cfg["STALE_TICK_SECONDS"] = max(10, int(stale_raw))

    # -------------------------
    # Ops
    # -------------------------
    cfg["KILL_SWITCH_FILE"] = _s("KILL_SWITCH_FILE", "KILL_SWITCH.txt")
    cfg["PAUSE_FILE"] = _s("PAUSE_FILE", "PAUSE.txt")
    cfg["DISCORD_WEBHOOK_URL"] = _s("DISCORD_WEBHOOK_URL", "")

    cfg["BACKTEST_MODE"] = _b("BACKTEST_MODE", "false")
    cfg["TRADE_TRACKER_ENABLED"] = _b("TRADE_TRACKER_ENABLED", "true")
    cfg["DISABLE_TRADE_TRACKER_IN_BACKTEST"] = _b("DISABLE_TRADE_TRACKER_IN_BACKTEST", "true")

    # -------------------------
    # Virtual Ledger
    # -------------------------
    cfg["START_CASH_USD"] = _d("START_CASH_USD", "500")
    cfg["MAX_TRADE_FRACTION"] = _d("MAX_TRADE_FRACTION", "0.25")
    cfg["MAX_TOTAL_EXPOSURE"] = _d("MAX_TOTAL_EXPOSURE", "0.50")
    cfg["MIN_ORDER_USD"] = _d("MIN_ORDER_USD", "10")
    cfg["USD_PER_TRADE"] = _d("USD_PER_TRADE", "0")

    cfg["START_CASH_USD"] = _clamp_decimal(cfg["START_CASH_USD"], Decimal("0"), Decimal("1000000000"))
    cfg["MAX_TRADE_FRACTION"] = _clamp_decimal(cfg["MAX_TRADE_FRACTION"], Decimal("0"), Decimal("1"))
    cfg["MAX_TOTAL_EXPOSURE"] = _clamp_decimal(cfg["MAX_TOTAL_EXPOSURE"], Decimal("0"), Decimal("1"))
    cfg["MIN_ORDER_USD"] = _clamp_decimal(cfg["MIN_ORDER_USD"], Decimal("0"), Decimal("1000000000"))
    cfg["USD_PER_TRADE"] = _clamp_decimal(cfg["USD_PER_TRADE"], Decimal("0"), Decimal("1000000000"))

    # -------------------------
    # Risk rules
    # -------------------------
    cfg["DAILY_MAX_LOSS_USD"] = _d("DAILY_MAX_LOSS_USD", "50")
    cfg["MAX_TRADES_PER_DAY"] = _i("MAX_TRADES_PER_DAY", "10")
    cfg["COOLDOWN_AFTER_LOSS_SECONDS"] = _i("COOLDOWN_AFTER_LOSS_SECONDS", "1800")
    cfg["CLEAR_LOCKOUT_ON_DAY_RESET"] = _b("CLEAR_LOCKOUT_ON_DAY_RESET", "false")

    cfg["DAILY_MAX_LOSS_USD"] = _clamp_decimal(cfg["DAILY_MAX_LOSS_USD"], Decimal("0"), Decimal("1000000000"))
    cfg["MAX_TRADES_PER_DAY"] = _clamp_int(cfg["MAX_TRADES_PER_DAY"], 0, 1_000_000)
    cfg["COOLDOWN_AFTER_LOSS_SECONDS"] = _clamp_int(cfg["COOLDOWN_AFTER_LOSS_SECONDS"], 0, 604_800)

    # -------------------------
    # Helpers
    # -------------------------
    cfg["QUIET_MODE"] = _b("QUIET_MODE", "false")
    cfg["SUMMARY_EVERY_SECONDS"] = _i("SUMMARY_EVERY_SECONDS", "900")
    cfg["TRACK_MFE_MAE"] = _b("TRACK_MFE_MAE", "true")
    cfg["TEST_EASY_ENTRIES"] = _b("TEST_EASY_ENTRIES", "false")
    cfg["TEST_EASY_EXITS"] = _b("TEST_EASY_EXITS", "false")

    cfg["SUMMARY_EVERY_SECONDS"] = _clamp_int(cfg["SUMMARY_EVERY_SECONDS"], 0, 86_400)

    # -------------------------
    # Confluence
    # -------------------------
    # --------- LINE ABOVE: # Confluence
    # Validation-friendly defaults (env can still override):
    # - allow trading with partial TF coverage
    # - remove HTF hard confirmation (temporarily)
    # - lower trade threshold to generate attempts
    cfg["CONFLUENCE_WATCH_SCORE"] = _i("CONFLUENCE_WATCH_SCORE", "50")
    cfg["CONFLUENCE_TRADE_SCORE"] = _i("CONFLUENCE_TRADE_SCORE", "65")

    cfg["REQUIRE_CONFLUENCE"] = _b("REQUIRE_CONFLUENCE", "true")
    # --------- LINE ABOVE: cfg["REQUIRE_CONFLUENCE"] = _b("REQUIRE_CONFLUENCE", "true")
    # Keep MIN_SCORE synced to TRADE_SCORE unless explicitly overridden
    cfg["CONFLUENCE_MIN_SCORE"] = _i("CONFLUENCE_MIN_SCORE", str(cfg["CONFLUENCE_TRADE_SCORE"]))

    cfg["USE_SHOULD_EVENTS"] = _b("USE_SHOULD_EVENTS", "false")

    cfg["CONFLUENCE_REQUIRE_1M_SIGNAL"] = _b("CONFLUENCE_REQUIRE_1M_SIGNAL", "true")
    cfg["CONFLUENCE_REQUIRE_1M_TRENDOK"] = _b("CONFLUENCE_REQUIRE_1M_TRENDOK", "true")
    # --------- LINE ABOVE: cfg["CONFLUENCE_REQUIRE_1M_TRENDOK"] = _b("CONFLUENCE_REQUIRE_1M_TRENDOK", "true")
    cfg["CONFLUENCE_REQUIRE_HTF_CONFIRM"] = _b("CONFLUENCE_REQUIRE_HTF_CONFIRM", "false")
    cfg["CONFLUENCE_HTF_MIN_SCORE"] = _i("CONFLUENCE_HTF_MIN_SCORE", "60")
    # --------- LINE ABOVE: cfg["CONFLUENCE_HTF_MIN_SCORE"] = _i("CONFLUENCE_HTF_MIN_SCORE", "60")
    cfg["CONFLUENCE_IGNORE_MISSING_TFS"] = _b("CONFLUENCE_IGNORE_MISSING_TFS", "true")

    cfg["CONFLUENCE_W_1M"] = _d("CONFLUENCE_W_1M", "0.2")
    cfg["CONFLUENCE_W_5M"] = _d("CONFLUENCE_W_5M", "0.3")
    cfg["CONFLUENCE_W_1H"] = _d("CONFLUENCE_W_1H", "0.5")
    cfg["CONFLUENCE_ALIGNMENT_BONUS"] = _i("CONFLUENCE_ALIGNMENT_BONUS", "5")
    cfg["CONFLUENCE_CONFLICT_PENALTY"] = _i("CONFLUENCE_CONFLICT_PENALTY", "10")

    cfg["CONFLUENCE_WATCH_SCORE"] = _clamp_int(cfg["CONFLUENCE_WATCH_SCORE"], 0, 100)
    cfg["CONFLUENCE_TRADE_SCORE"] = _clamp_int(cfg["CONFLUENCE_TRADE_SCORE"], 0, 100)
    cfg["CONFLUENCE_MIN_SCORE"] = _clamp_int(cfg["CONFLUENCE_MIN_SCORE"], 0, 100)

    cfg["CONFLUENCE_ALIGNMENT_BONUS"] = _clamp_int(cfg["CONFLUENCE_ALIGNMENT_BONUS"], 0, 25)
    cfg["CONFLUENCE_CONFLICT_PENALTY"] = _clamp_int(cfg["CONFLUENCE_CONFLICT_PENALTY"], 0, 50)

    # Normalize weights
    w1 = _clamp_decimal(cfg["CONFLUENCE_W_1M"], Decimal("0"), Decimal("1"))
    w5 = _clamp_decimal(cfg["CONFLUENCE_W_5M"], Decimal("0"), Decimal("1"))
    wH = _clamp_decimal(cfg["CONFLUENCE_W_1H"], Decimal("0"), Decimal("1"))
    wsum = w1 + w5 + wH
    if wsum > 0:
        cfg["CONFLUENCE_W_1M"] = w1 / wsum
        cfg["CONFLUENCE_W_5M"] = w5 / wsum
        cfg["CONFLUENCE_W_1H"] = wH / wsum

    # -------------------------
    # Phase 4A — Volatility sizing
    # -------------------------
    cfg["USE_VOL_SIZING"] = _b("USE_VOL_SIZING", "false")
    cfg["VOL_WINDOW"] = _i("VOL_WINDOW", "60")
    cfg["VOL_LOOKBACK"] = _i("VOL_LOOKBACK", str(cfg["VOL_WINDOW"]))
    cfg["RISK_PER_TRADE_USD"] = _d("RISK_PER_TRADE_USD", "10")
    cfg["VOL_STOP_MULT"] = _d("VOL_STOP_MULT", "2.0")
    cfg["VOL_FALLBACK_TO_CAPS"] = _b("VOL_FALLBACK_TO_CAPS", "true")

    cfg["VOL_WINDOW"] = _clamp_int(cfg["VOL_WINDOW"], 5, 50_000)
    cfg["VOL_LOOKBACK"] = _clamp_int(cfg["VOL_LOOKBACK"], 5, 50_000)
    cfg["RISK_PER_TRADE_USD"] = _clamp_decimal(cfg["RISK_PER_TRADE_USD"], Decimal("0"), Decimal("1000000000"))
    cfg["VOL_STOP_MULT"] = _clamp_decimal(cfg["VOL_STOP_MULT"], Decimal("0.1"), Decimal("50"))

    # -------------------------
    # Phase 4B — Regime detection
    # -------------------------
    cfg["REGIME_LOOKBACK"] = _i("REGIME_LOOKBACK", "50")
    cfg["REGIME_VOL_HIGH"] = _d("REGIME_VOL_HIGH", "0.01")
    cfg["REGIME_VOL_LOW"] = _d("REGIME_VOL_LOW", "0.003")
    cfg["REGIME_TREND_SLOPE_MIN"] = _d("REGIME_TREND_SLOPE_MIN", "0.0003")
    cfg["REGIME_MA_SPREAD_MIN"] = _d("REGIME_MA_SPREAD_MIN", "0.0015")
    cfg["REGIME_MIN_GATE_SCORE"] = _i("REGIME_MIN_GATE_SCORE", "70")

    cfg["REGIME_LOOKBACK"] = _clamp_int(cfg["REGIME_LOOKBACK"], 5, 50_000)
    cfg["REGIME_VOL_HIGH"] = _clamp_decimal(cfg["REGIME_VOL_HIGH"], Decimal("0"), Decimal("1"))
    cfg["REGIME_VOL_LOW"] = _clamp_decimal(cfg["REGIME_VOL_LOW"], Decimal("0"), Decimal("1"))
    cfg["REGIME_TREND_SLOPE_MIN"] = _clamp_decimal(cfg["REGIME_TREND_SLOPE_MIN"], Decimal("0"), Decimal("1"))
    cfg["REGIME_MA_SPREAD_MIN"] = _clamp_decimal(cfg["REGIME_MA_SPREAD_MIN"], Decimal("0"), Decimal("1"))
    cfg["REGIME_MIN_GATE_SCORE"] = _clamp_int(cfg["REGIME_MIN_GATE_SCORE"], 0, 100)

    # -------------------------
    # Phase 4C — Adaptive confluence
    # -------------------------
    cfg["USE_ADAPTIVE_CONFLUENCE"] = _b("USE_ADAPTIVE_CONFLUENCE", "false")

    cfg["AC_BONUS_TREND_UP"] = _i("AC_BONUS_TREND_UP", "5")
    cfg["AC_BONUS_TREND_DOWN"] = _i("AC_BONUS_TREND_DOWN", "0")

    cfg["AC_PENALTY_RANGE"] = _i("AC_PENALTY_RANGE", "7")
    cfg["AC_PENALTY_VOLATILE_RANGE"] = _i("AC_PENALTY_VOLATILE_RANGE", "12")

    cfg["AC_TRADE_SHIFT_RANGE"] = _i("AC_TRADE_SHIFT_RANGE", "5")
    cfg["AC_TRADE_SHIFT_VOLATILE_RANGE"] = _i("AC_TRADE_SHIFT_VOLATILE_RANGE", "10")
    cfg["AC_WATCH_SHIFT_RANGE"] = _i("AC_WATCH_SHIFT_RANGE", "0")
    cfg["AC_WATCH_SHIFT_VOLATILE_RANGE"] = _i("AC_WATCH_SHIFT_VOLATILE_RANGE", "5")

    cfg["AC_MIN_TREND_STRENGTH_FOR_TRADE"] = _d("AC_MIN_TREND_STRENGTH_FOR_TRADE", "0")

    cfg["AC_BONUS_TREND_UP"] = _clamp_int(cfg["AC_BONUS_TREND_UP"], 0, 50)
    cfg["AC_BONUS_TREND_DOWN"] = _clamp_int(cfg["AC_BONUS_TREND_DOWN"], 0, 50)
    cfg["AC_PENALTY_RANGE"] = _clamp_int(cfg["AC_PENALTY_RANGE"], 0, 100)
    cfg["AC_PENALTY_VOLATILE_RANGE"] = _clamp_int(cfg["AC_PENALTY_VOLATILE_RANGE"], 0, 100)
    cfg["AC_TRADE_SHIFT_RANGE"] = _clamp_int(cfg["AC_TRADE_SHIFT_RANGE"], 0, 100)
    cfg["AC_TRADE_SHIFT_VOLATILE_RANGE"] = _clamp_int(cfg["AC_TRADE_SHIFT_VOLATILE_RANGE"], 0, 100)
    cfg["AC_WATCH_SHIFT_RANGE"] = _clamp_int(cfg["AC_WATCH_SHIFT_RANGE"], 0, 100)
    cfg["AC_WATCH_SHIFT_VOLATILE_RANGE"] = _clamp_int(cfg["AC_WATCH_SHIFT_VOLATILE_RANGE"], 0, 100)
    cfg["AC_MIN_TREND_STRENGTH_FOR_TRADE"] = _clamp_decimal(
        cfg["AC_MIN_TREND_STRENGTH_FOR_TRADE"], Decimal("0"), Decimal("1")
    )

    # -------------------------
    # Phase 5A — Market Structure
    # -------------------------
    cfg["USE_STRUCTURE"] = _b("USE_STRUCTURE", "false")

    cfg["STRUCT_SWING_LEFT"] = _i("STRUCT_SWING_LEFT", "3")
    cfg["STRUCT_SWING_RIGHT"] = _i("STRUCT_SWING_RIGHT", "3")

    cfg["STRUCT_NEAR_LEVEL_PCT"] = _d("STRUCT_NEAR_LEVEL_PCT", "0.0015")
    cfg["STRUCT_LEVEL_CLUSTER_PCT"] = _d("STRUCT_LEVEL_CLUSTER_PCT", "0.0010")
    cfg["STRUCT_BREAK_PCT"] = _d("STRUCT_BREAK_PCT", "0.0008")
    cfg["STRUCT_RETEST_MAX_BARS"] = _i("STRUCT_RETEST_MAX_BARS", "12")

    cfg["STRUCT_USE_REJECTION"] = _b("STRUCT_USE_REJECTION", "true")
    cfg["STRUCT_REJECT_WICK_RATIO"] = _d("STRUCT_REJECT_WICK_RATIO", "1.5")
    cfg["STRUCT_REJECT_BODY_MAX_PCT"] = _d("STRUCT_REJECT_BODY_MAX_PCT", "0.0025")

    cfg["STRUCT_MAX_LEVELS"] = _i("STRUCT_MAX_LEVELS", "12")

    cfg["STRUCT_BONUS_BREAK_RETEST"] = _i("STRUCT_BONUS_BREAK_RETEST", "8")
    cfg["STRUCT_BONUS_NEAR_SUPPORT"] = _i("STRUCT_BONUS_NEAR_SUPPORT", "3")
    cfg["STRUCT_PENALTY_REJECT_AT_RES"] = _i("STRUCT_PENALTY_REJECT_AT_RES", "8")
    cfg["STRUCT_PENALTY_FAILED_RETEST"] = _i("STRUCT_PENALTY_FAILED_RETEST", "10")

    cfg["STRUCT_BLOCK_LONG_ON_REJECT_RES"] = _b("STRUCT_BLOCK_LONG_ON_REJECT_RES", "false")
    cfg["STRUCT_BLOCK_LONG_BELOW_SUPPORT"] = _b("STRUCT_BLOCK_LONG_BELOW_SUPPORT", "false")

    cfg["STRUCT_SWING_LEFT"] = _clamp_int(cfg["STRUCT_SWING_LEFT"], 1, 50)
    cfg["STRUCT_SWING_RIGHT"] = _clamp_int(cfg["STRUCT_SWING_RIGHT"], 1, 50)
    cfg["STRUCT_NEAR_LEVEL_PCT"] = _clamp_decimal(cfg["STRUCT_NEAR_LEVEL_PCT"], Decimal("0"), Decimal("0.25"))
    cfg["STRUCT_LEVEL_CLUSTER_PCT"] = _clamp_decimal(cfg["STRUCT_LEVEL_CLUSTER_PCT"], Decimal("0"), Decimal("0.25"))
    cfg["STRUCT_BREAK_PCT"] = _clamp_decimal(cfg["STRUCT_BREAK_PCT"], Decimal("0"), Decimal("0.25"))
    cfg["STRUCT_RETEST_MAX_BARS"] = _clamp_int(cfg["STRUCT_RETEST_MAX_BARS"], 1, 5000)
    cfg["STRUCT_REJECT_WICK_RATIO"] = _clamp_decimal(cfg["STRUCT_REJECT_WICK_RATIO"], Decimal("0"), Decimal("25"))
    cfg["STRUCT_REJECT_BODY_MAX_PCT"] = _clamp_decimal(cfg["STRUCT_REJECT_BODY_MAX_PCT"], Decimal("0"), Decimal("0.25"))
    cfg["STRUCT_MAX_LEVELS"] = _clamp_int(cfg["STRUCT_MAX_LEVELS"], 1, 500)

    cfg["STRUCT_BONUS_BREAK_RETEST"] = _clamp_int(cfg["STRUCT_BONUS_BREAK_RETEST"], 0, 25)
    cfg["STRUCT_BONUS_NEAR_SUPPORT"] = _clamp_int(cfg["STRUCT_BONUS_NEAR_SUPPORT"], 0, 15)
    cfg["STRUCT_PENALTY_REJECT_AT_RES"] = _clamp_int(cfg["STRUCT_PENALTY_REJECT_AT_RES"], 0, 50)
    cfg["STRUCT_PENALTY_FAILED_RETEST"] = _clamp_int(cfg["STRUCT_PENALTY_FAILED_RETEST"], 0, 50)

    # -------------------------
    # Phase 5B — Liquidity Filters
    # -------------------------
    cfg["USE_LIQUIDITY_FILTERS"] = _b("USE_LIQUIDITY_FILTERS", "false")
    cfg["USE_LIQUIDITY"] = cfg["USE_LIQUIDITY_FILTERS"]

    cfg["LIQ_MAX_SPREAD_BPS"] = _d("LIQ_MAX_SPREAD_BPS", "25")
    cfg["LIQ_SPREAD_MAX_BPS"] = cfg["LIQ_MAX_SPREAD_BPS"]  # legacy alias

    cfg["LIQ_MIN_VOL_USD_1M"] = _d("LIQ_MIN_VOL_USD_1M", "0")
    cfg["LIQ_MIN_VOL_UNITS_1M"] = _d("LIQ_MIN_VOL_UNITS_1M", "0")

    cfg["LIQ_MIN_VOL_MULT"] = _d("LIQ_MIN_VOL_MULT", "1.2")
    cfg["LIQ_VOL_BASELINE_WINDOW"] = _i("LIQ_VOL_BASELINE_WINDOW", "30")

    cfg["LIQ_ATR_WINDOW"] = _i("LIQ_ATR_WINDOW", "14")
    cfg["LIQ_ATR_NORM_MIN"] = _d("LIQ_ATR_NORM_MIN", "0.0008")
    cfg["LIQ_ATR_NORM_MAX"] = _d("LIQ_ATR_NORM_MAX", "0")

    cfg["LIQ_MODE"] = _s("LIQ_MODE", "BLOCK").upper()
    if cfg["LIQ_MODE"] not in ("BLOCK", "PENALIZE"):
        cfg["LIQ_MODE"] = "BLOCK"
    cfg["LIQ_PENALTY_POINTS"] = _i("LIQ_PENALTY_POINTS", "10")

    cfg["LIQ_MAX_SPREAD_BPS"] = _clamp_decimal(cfg["LIQ_MAX_SPREAD_BPS"], Decimal("0"), Decimal("500"))
    cfg["LIQ_SPREAD_MAX_BPS"] = cfg["LIQ_MAX_SPREAD_BPS"]

    cfg["LIQ_MIN_VOL_MULT"] = _clamp_decimal(cfg["LIQ_MIN_VOL_MULT"], Decimal("0"), Decimal("20"))
    cfg["LIQ_VOL_BASELINE_WINDOW"] = _clamp_int(int(cfg["LIQ_VOL_BASELINE_WINDOW"]), 5, 500)

    cfg["LIQ_ATR_WINDOW"] = _clamp_int(int(cfg["LIQ_ATR_WINDOW"]), 5, 200)
    cfg["LIQ_ATR_NORM_MIN"] = _clamp_decimal(cfg["LIQ_ATR_NORM_MIN"], Decimal("0"), Decimal("1"))
    try:
        if cfg["LIQ_ATR_NORM_MAX"] < 0:
            cfg["LIQ_ATR_NORM_MAX"] = Decimal("0")
    except Exception:
        cfg["LIQ_ATR_NORM_MAX"] = Decimal("0")

    cfg["LIQ_PENALTY_POINTS"] = _clamp_int(cfg["LIQ_PENALTY_POINTS"], 0, 100)

    # -------------------------
    # Phase 5C — Session Behavior
    # -------------------------
    cfg["USE_SESSION_MODIFIERS"] = _b("USE_SESSION_MODIFIERS", "false")

    cfg["SESSION_TIMEZONE"] = _s("SESSION_TIMEZONE", "UTC").upper()  # informational/logging

    cfg["SESSION_ASIA_START_UTC"] = _i("SESSION_ASIA_START_UTC", "0")
    cfg["SESSION_ASIA_END_UTC"] = _i("SESSION_ASIA_END_UTC", "7")
    cfg["SESSION_LONDON_START_UTC"] = _i("SESSION_LONDON_START_UTC", "8")
    cfg["SESSION_LONDON_END_UTC"] = _i("SESSION_LONDON_END_UTC", "12")
    cfg["SESSION_NY_START_UTC"] = _i("SESSION_NY_START_UTC", "13")
    cfg["SESSION_NY_END_UTC"] = _i("SESSION_NY_END_UTC", "20")
    cfg["SESSION_OVERLAP_START_UTC"] = _i("SESSION_OVERLAP_START_UTC", "13")
    cfg["SESSION_OVERLAP_END_UTC"] = _i("SESSION_OVERLAP_END_UTC", "15")

    cfg["SESSION_OFF_BONUS"] = _i("SESSION_OFF_BONUS", "0")

    cfg["SESSION_ASIA_START_UTC"] = _clamp_int(cfg["SESSION_ASIA_START_UTC"], 0, 23)
    cfg["SESSION_ASIA_END_UTC"] = _clamp_int(cfg["SESSION_ASIA_END_UTC"], 0, 23)
    cfg["SESSION_LONDON_START_UTC"] = _clamp_int(cfg["SESSION_LONDON_START_UTC"], 0, 23)
    cfg["SESSION_LONDON_END_UTC"] = _clamp_int(cfg["SESSION_LONDON_END_UTC"], 0, 23)
    cfg["SESSION_NY_START_UTC"] = _clamp_int(cfg["SESSION_NY_START_UTC"], 0, 23)
    cfg["SESSION_NY_END_UTC"] = _clamp_int(cfg["SESSION_NY_END_UTC"], 0, 23)
    cfg["SESSION_OVERLAP_START_UTC"] = _clamp_int(cfg["SESSION_OVERLAP_START_UTC"], 0, 23)
    cfg["SESSION_OVERLAP_END_UTC"] = _clamp_int(cfg["SESSION_OVERLAP_END_UTC"], 0, 23)

    cfg["SESSION_ASIA_BONUS"] = _i("SESSION_ASIA_BONUS", "0")
    cfg["SESSION_LONDON_BONUS"] = _i("SESSION_LONDON_BONUS", "0")
    cfg["SESSION_NY_BONUS"] = _i("SESSION_NY_BONUS", "0")
    cfg["SESSION_OVERLAP_BONUS"] = _i("SESSION_OVERLAP_BONUS", "2")

    cfg["SESSION_ASIA_BONUS"] = _clamp_int(cfg["SESSION_ASIA_BONUS"], -100, 100)
    cfg["SESSION_LONDON_BONUS"] = _clamp_int(cfg["SESSION_LONDON_BONUS"], -100, 100)
    cfg["SESSION_NY_BONUS"] = _clamp_int(cfg["SESSION_NY_BONUS"], -100, 100)
    cfg["SESSION_OVERLAP_BONUS"] = _clamp_int(cfg["SESSION_OVERLAP_BONUS"], -100, 100)
    cfg["SESSION_OFF_BONUS"] = _clamp_int(cfg["SESSION_OFF_BONUS"], -100, 100)

    cfg["SESSION_ASIA_RISK_MULT"] = _f("SESSION_ASIA_RISK_MULT", "1.0")
    cfg["SESSION_LONDON_RISK_MULT"] = _f("SESSION_LONDON_RISK_MULT", "1.0")
    cfg["SESSION_NY_RISK_MULT"] = _f("SESSION_NY_RISK_MULT", "1.0")
    cfg["SESSION_OVERLAP_RISK_MULT"] = _f("SESSION_OVERLAP_RISK_MULT", "1.0")
    cfg["SESSION_OFF_RISK_MULT"] = _f("SESSION_OFF_RISK_MULT", "1.0")

    cfg["SESSION_ASIA_RISK_MULT"] = float(_clamp_float(cfg["SESSION_ASIA_RISK_MULT"], 0.0, 10.0))
    cfg["SESSION_LONDON_RISK_MULT"] = float(_clamp_float(cfg["SESSION_LONDON_RISK_MULT"], 0.0, 10.0))
    cfg["SESSION_NY_RISK_MULT"] = float(_clamp_float(cfg["SESSION_NY_RISK_MULT"], 0.0, 10.0))
    cfg["SESSION_OVERLAP_RISK_MULT"] = float(_clamp_float(cfg["SESSION_OVERLAP_RISK_MULT"], 0.0, 10.0))
    cfg["SESSION_OFF_RISK_MULT"] = float(_clamp_float(cfg["SESSION_OFF_RISK_MULT"], 0.0, 10.0))

    # -------------------------
    # Phase 5D — Exit Intelligence (placeholders)
    # -------------------------
    cfg["USE_EXIT_INTEL"] = _b("USE_EXIT_INTEL", "false")
    cfg["EXIT_PARTIAL_1R_FRACTION"] = _d("EXIT_PARTIAL_1R_FRACTION", "0.50")
    cfg["EXIT_TIME_STOP_BARS"] = _i("EXIT_TIME_STOP_BARS", "90")
    cfg["EXIT_ATR_TRAIL_MULT"] = _d("EXIT_ATR_TRAIL_MULT", "2.0")

    cfg["EXIT_PARTIAL_1R_FRACTION"] = _clamp_decimal(cfg["EXIT_PARTIAL_1R_FRACTION"], Decimal("0"), Decimal("1"))
    cfg["EXIT_TIME_STOP_BARS"] = _clamp_int(cfg["EXIT_TIME_STOP_BARS"], 0, 1_000_000)
    cfg["EXIT_ATR_TRAIL_MULT"] = _clamp_decimal(cfg["EXIT_ATR_TRAIL_MULT"], Decimal("0.1"), Decimal("50"))

    # -------------------------
    # Trade tracker
    # -------------------------
    cfg["TRADE_TRACKER_FILE"] = _s("TRADE_TRACKER_FILE", "trade_tracker.txt")
    cfg["TRADE_TRACKER_UNIQUE_PER_RUN"] = _b("TRADE_TRACKER_UNIQUE_PER_RUN", "false")
    cfg["TRACK_HOLD_REASONS"] = _b("TRACK_HOLD_REASONS", "true")
    cfg["HOLD_BUMP_EVERY_SECONDS"] = _i("HOLD_BUMP_EVERY_SECONDS", "300")

    cfg["HOLD_BUMP_EVERY_SECONDS"] = _clamp_int(cfg["HOLD_BUMP_EVERY_SECONDS"], 0, 86_400)

    return cfg
