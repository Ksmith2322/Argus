#!/usr/bin/env python3
# config.py

import os
from decimal import Decimal, getcontext
from dotenv import load_dotenv

getcontext().prec = 50


def _d(name: str, default: str) -> Decimal:
    try:
        return Decimal(str(os.getenv(name, default)).strip())
    except Exception:
        return Decimal(str(default))


def _i(name: str, default: str) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return int(default)


def _f(name: str, default: str) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except Exception:
        return float(default)


def _b(name: str, default: str) -> bool:
    v = str(os.getenv(name, str(default))).strip().lower()
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
    dotenv_path = os.getenv("DOTENV_PATH", "").strip()
    if dotenv_path:
        load_dotenv(dotenv_path=dotenv_path)
    else:
        load_dotenv()

    cfg: dict = {}

    # -------------------------
    # Core
    # -------------------------
    cfg["PRODUCT_ID"] = _s("PRODUCT_ID", "ETH-USD")
    cfg["COINBASE_SPOT_URL"] = f"https://api.coinbase.com/v2/prices/{cfg['PRODUCT_ID']}/spot"
    cfg["COINBASE_EXCHANGE_CANDLES_URL"] = "https://api.exchange.coinbase.com/products/{product_id}/candles"

    # -------------------------
    # Runtime / mode flags
    # -------------------------
    cfg["DRY_RUN"] = _b("DRY_RUN", "false")
    cfg["BACKTEST_MODE"] = _b("BACKTEST_MODE", "false")

    # Deterministic smoke-test flags
    cfg["FORCE_TEST_BUY"] = _b("FORCE_TEST_BUY", "false")
    cfg["FORCE_TEST_SELL"] = _b("FORCE_TEST_SELL", "false")
    cfg["FORCE_TEST_ONLY_ONCE"] = _b("FORCE_TEST_ONLY_ONCE", "true")
    cfg["FORCE_TEST_TAG"] = _s("FORCE_TEST_TAG", "phase8_smoke")

    # New: explicit forced-test containment behavior
    cfg["FORCE_TEST_EXIT_ON_SUCCESS"] = _b("FORCE_TEST_EXIT_ON_SUCCESS", "true")
    cfg["FORCE_TEST_EXIT_ON_FAILURE"] = _b("FORCE_TEST_EXIT_ON_FAILURE", "false")
    cfg["FORCE_TEST_BLOCK_NORMAL_TRADING"] = _b("FORCE_TEST_BLOCK_NORMAL_TRADING", "true")
    cfg["FORCE_TEST_REQUIRE_FLAT_START"] = _b("FORCE_TEST_REQUIRE_FLAT_START", "false")
    cfg["FORCE_TEST_REQUIRE_FINAL_FLAT"] = _b("FORCE_TEST_REQUIRE_FINAL_FLAT", "false")
    cfg["FORCE_TEST_CLOSE_OPEN_POSITION_ON_EXIT"] = _b("FORCE_TEST_CLOSE_OPEN_POSITION_ON_EXIT", "false")

    # Safety: don't allow both forced directions at once
    if cfg["FORCE_TEST_BUY"] and cfg["FORCE_TEST_SELL"]:
        cfg["FORCE_TEST_SELL"] = False

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
    cfg["MIN_SPREAD_BPS"] = _clamp_decimal(cfg["MIN_SPREAD_BPS"], Decimal("0"), Decimal("10000"))

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
    cfg["MAX_HOLD_SECONDS"] = _i("MAX_HOLD_SECONDS", "5400")

    cfg["MA200_BAND_PCT"] = _clamp_decimal(cfg["MA200_BAND_PCT"], Decimal("0"), Decimal("1"))
    cfg["TREND_INVALIDATION_CLOSES"] = _clamp_int(cfg["TREND_INVALIDATION_CLOSES"], 0, 50_000)
    cfg["MIN_HOLD_SECONDS"] = _clamp_int(cfg["MIN_HOLD_SECONDS"], 0, 86_400)
    cfg["MAX_HOLD_SECONDS"] = _clamp_int(cfg["MAX_HOLD_SECONDS"], 0, 86_400)

    stale_raw = _i("STALE_TICK_SECONDS", "20")
    cfg["STALE_TICK_SECONDS"] = max(10, int(stale_raw))

    # -------------------------
    # Ops
    # -------------------------
    cfg["KILL_SWITCH_FILE"] = _s("KILL_SWITCH_FILE", "KILL_SWITCH.txt")
    cfg["PAUSE_FILE"] = _s("PAUSE_FILE", "PAUSE.txt")
    cfg["DISCORD_WEBHOOK_URL"] = _s("DISCORD_WEBHOOK_URL", "")

    # -------------------------
    # Phase 16: Runtime mode & health checks
    # -------------------------
    cfg["RUNTIME_MODE_FILE"] = _s("RUNTIME_MODE_FILE", "")  # empty = default (ops/logs/runtime_mode.json)
    cfg["HEALTH_FEED_STALE_THRESHOLD_S"] = _f("HEALTH_FEED_STALE_THRESHOLD_S", "120")
    cfg["HEALTH_FILL_LATENCY_THRESHOLD_S"] = _f("HEALTH_FILL_LATENCY_THRESHOLD_S", "30")
    cfg["HEALTH_CONSECUTIVE_LOSS_THRESHOLD"] = _i("HEALTH_CONSECUTIVE_LOSS_THRESHOLD", "5")
    cfg["HEALTH_SLIPPAGE_P99_BPS"] = _f("HEALTH_SLIPPAGE_P99_BPS", "50")
    cfg["HEALTH_CHECK_ENABLED"] = _b("HEALTH_CHECK_ENABLED", "true")
    cfg["RUNTIME_MODE_GATING_ENABLED"] = _b("RUNTIME_MODE_GATING_ENABLED", "true")

    cfg["TRADE_TRACKER_ENABLED"] = _b("TRADE_TRACKER_ENABLED", "true")
    cfg["DISABLE_TRADE_TRACKER_IN_BACKTEST"] = _b("DISABLE_TRADE_TRACKER_IN_BACKTEST", "true")

    cfg["BACKTEST_LIMIT"] = _i("BACKTEST_LIMIT", "0")
    cfg["BT_LIQUIDITY_MODE"] = _s("BT_LIQUIDITY_MODE", "")

    # -------------------------
    # Artifact roots / path doctrine
    # -------------------------
    cfg["OPS_LOG_DIR"] = _s("OPS_LOG_DIR", "ops/logs")
    cfg["RUN_LOG_DIR"] = _s("RUN_LOG_DIR", "logs")
    cfg["ARTIFACT_LOG_DIR"] = _s("ARTIFACT_LOG_DIR", cfg["RUN_LOG_DIR"])
    cfg["CANONICAL_TRUTH_DIR"] = _s("CANONICAL_TRUTH_DIR", cfg["OPS_LOG_DIR"])

    # Lifecycle artifact filenames/prefixes
    cfg["ORDERS_FILE_PREFIX"] = _s("ORDERS_FILE_PREFIX", "orders")
    cfg["FILLS_FILE_PREFIX"] = _s("FILLS_FILE_PREFIX", "fills")
    cfg["ACCOUNT_FILE_PREFIX"] = _s("ACCOUNT_FILE_PREFIX", "account")
    cfg["POSITIONS_FILE_PREFIX"] = _s("POSITIONS_FILE_PREFIX", "positions")
    cfg["TRADE_JOURNAL_FILE_PREFIX"] = _s("TRADE_JOURNAL_FILE_PREFIX", "trade_journal")
    cfg["DAILY_SUMMARY_FILE_PREFIX"] = _s("DAILY_SUMMARY_FILE_PREFIX", "daily_summary")
    cfg["RUN_SUMMARY_FILE_PREFIX"] = _s("RUN_SUMMARY_FILE_PREFIX", "run_summary")
    cfg["RECONCILIATION_FILE_PREFIX"] = _s("RECONCILIATION_FILE_PREFIX", "reconciliation")

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
    cfg["CONFLUENCE_WATCH_SCORE"] = _i("CONFLUENCE_WATCH_SCORE", "50")
    cfg["CONFLUENCE_TRADE_SCORE"] = _i("CONFLUENCE_TRADE_SCORE", "65")

    cfg["REQUIRE_CONFLUENCE"] = _b("REQUIRE_CONFLUENCE", "true")
    cfg["CONFLUENCE_MIN_SCORE"] = _i("CONFLUENCE_MIN_SCORE", str(cfg["CONFLUENCE_TRADE_SCORE"]))
    # Multiplier for WATCH-gate entries (0 = block, 0.25 = 25% size, 1.0 = full size)
    cfg["CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE"] = _d("CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE", "0.25")

    cfg["USE_SHOULD_EVENTS"] = _b("USE_SHOULD_EVENTS", "false")

    cfg["CONFLUENCE_REQUIRE_1M_SIGNAL"] = _b("CONFLUENCE_REQUIRE_1M_SIGNAL", "true")
    cfg["CONFLUENCE_REQUIRE_1M_TRENDOK"] = _b("CONFLUENCE_REQUIRE_1M_TRENDOK", "true")
    cfg["CONFLUENCE_REQUIRE_HTF_CONFIRM"] = _b("CONFLUENCE_REQUIRE_HTF_CONFIRM", "false")
    cfg["CONFLUENCE_HTF_MIN_SCORE"] = _i("CONFLUENCE_HTF_MIN_SCORE", "60")
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
    cfg["REGIME_ENTRY_BLOCK_LIST"] = os.environ.get("REGIME_ENTRY_BLOCK_LIST", "TREND_DOWN,RANGE")

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
    cfg["AC_BONUS_TREND_DOWN"] = _clamp_int(cfg["AC_BONUS_TREND_DOWN"], -50, 50)
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
    cfg["STRUCT_REJECT_BODY_MAX_PCT"] = _clamp_decimal(
        cfg["STRUCT_REJECT_BODY_MAX_PCT"], Decimal("0"), Decimal("0.25")
    )
    cfg["STRUCT_MAX_LEVELS"] = _clamp_int(cfg["STRUCT_MAX_LEVELS"], 1, 500)

    cfg["STRUCT_BONUS_BREAK_RETEST"] = _clamp_int(cfg["STRUCT_BONUS_BREAK_RETEST"], 0, 25)
    cfg["STRUCT_BONUS_NEAR_SUPPORT"] = _clamp_int(cfg["STRUCT_BONUS_NEAR_SUPPORT"], 0, 15)
    cfg["STRUCT_PENALTY_REJECT_AT_RES"] = _clamp_int(cfg["STRUCT_PENALTY_REJECT_AT_RES"], 0, 50)
    cfg["STRUCT_PENALTY_FAILED_RETEST"] = _clamp_int(cfg["STRUCT_PENALTY_FAILED_RETEST"], 0, 50)

    # -------------------------
    # Phase 18 — Dynamic Trendlines (1h)
    # -------------------------
    cfg["USE_TRENDLINES"] = _b("USE_TRENDLINES", "false")

    cfg["TL_SWING_LEFT"] = _clamp_int(_i("TL_SWING_LEFT", "3"), 1, 20)
    cfg["TL_SWING_RIGHT"] = _clamp_int(_i("TL_SWING_RIGHT", "3"), 1, 20)
    cfg["TL_NEAR_PCT"] = _clamp_decimal(_d("TL_NEAR_PCT", "0.005"), Decimal("0"), Decimal("0.10"))
    cfg["TL_BREAK_PCT"] = _clamp_decimal(_d("TL_BREAK_PCT", "0.003"), Decimal("0"), Decimal("0.10"))
    cfg["TL_MAX_PIVOT_HISTORY"] = _clamp_int(_i("TL_MAX_PIVOT_HISTORY", "20"), 2, 100)
    cfg["TL_MIN_PIVOTS"] = _clamp_int(_i("TL_MIN_PIVOTS", "2"), 2, 10)
    cfg["TL_MAX_SLOPE_AGE_BARS"] = _clamp_int(_i("TL_MAX_SLOPE_AGE_BARS", "60"), 5, 500)

    cfg["TL_BONUS_NEAR_SUPPORT"] = _clamp_int(_i("TL_BONUS_NEAR_SUPPORT", "5"), 0, 20)
    cfg["TL_PENALTY_NEAR_RESIST"] = _clamp_int(_i("TL_PENALTY_NEAR_RESIST", "8"), 0, 30)
    cfg["TL_BONUS_BROKE_ABOVE_RESIST"] = _clamp_int(_i("TL_BONUS_BROKE_ABOVE_RESIST", "12"), 0, 30)
    cfg["TL_PENALTY_BROKE_BELOW_SUPPORT"] = _clamp_int(_i("TL_PENALTY_BROKE_BELOW_SUPPORT", "15"), 0, 50)
    cfg["TL_BLOCK_LONG_NEAR_RESIST"] = _b("TL_BLOCK_LONG_NEAR_RESIST", "false")
    cfg["TL_BLOCK_LONG_BROKE_BELOW_SUPPORT"] = _b("TL_BLOCK_LONG_BROKE_BELOW_SUPPORT", "true")
    cfg["TL_PENALTY_RESIST_SLOPE_NEG"] = _clamp_int(_i("TL_PENALTY_RESIST_SLOPE_NEG", "0"), 0, 30)

    # -------------------------
    # Phase 5B — Liquidity Filters
    # -------------------------
    cfg["USE_LIQUIDITY_FILTERS"] = _b("USE_LIQUIDITY_FILTERS", "false")
    cfg["USE_LIQUIDITY"] = cfg["USE_LIQUIDITY_FILTERS"]

    cfg["LIQ_MAX_SPREAD_BPS"] = _d("LIQ_MAX_SPREAD_BPS", "25")
    cfg["LIQ_SPREAD_MAX_BPS"] = cfg["LIQ_MAX_SPREAD_BPS"]

    v_usd = _d("LIQ_MIN_VOL_USD_1M", "0")
    v_units = _d("LIQ_MIN_VOL_UNITS_1M", "0")
    cfg["LIQ_MIN_VOL_USD_1M"] = v_usd
    cfg["LIQ_MIN_VOL_UNITS_1M"] = v_units
    cfg["LIQ_MIN_VOL_1M"] = _d("LIQ_MIN_VOL_1M", str(max(v_usd, v_units)))

    cfg["LIQ_MIN_VOL_MULT"] = _d("LIQ_MIN_VOL_MULT", "1.2")
    cfg["LIQ_VOL_BASELINE_WINDOW"] = _i("LIQ_VOL_BASELINE_WINDOW", "30")

    cfg["LIQ_ATR_WINDOW"] = _i("LIQ_ATR_WINDOW", "14")
    cfg["LIQ_ATR_NORM_MIN"] = _d("LIQ_ATR_NORM_MIN", "0.0008")
    cfg["LIQ_ATR_NORM_MAX"] = _d("LIQ_ATR_NORM_MAX", "0")

    cfg["LIQ_MODE"] = _s("LIQ_MODE", "BLOCK").upper()
    if cfg["LIQ_MODE"] not in ("BLOCK", "PENALIZE"):
        cfg["LIQ_MODE"] = "BLOCK"

    cfg["LIQ_PENALTY_POINTS"] = _i("LIQ_PENALTY_POINTS", "10")
    cfg["LIQ_PENALIZE_HARD_BLOCK_AT"] = _i("LIQ_PENALIZE_HARD_BLOCK_AT", "0")

    cfg["LIQ_BLOCK_ON_MISSING_SPREAD"] = _b("LIQ_BLOCK_ON_MISSING_SPREAD", "false")
    cfg["LIQ_BLOCK_ON_MISSING_VOLUME"] = _b("LIQ_BLOCK_ON_MISSING_VOLUME", "false")
    cfg["LIQ_BLOCK_ON_MISSING_ATR_NORM"] = _b("LIQ_BLOCK_ON_MISSING_ATR_NORM", "false")

    cfg["LIQ_USE_SYNTHETIC_SPREAD"] = _b("LIQ_USE_SYNTHETIC_SPREAD", "false")
    cfg["LIQ_SYNTH_SPREAD_FLOOR_BPS"] = _d("LIQ_SYNTH_SPREAD_FLOOR_BPS", "8")
    cfg["LIQ_SYNTH_SPREAD_ATR_MULT_BPS"] = _d("LIQ_SYNTH_SPREAD_ATR_MULT_BPS", "8000")
    cfg["LIQ_SYNTH_SPREAD_VOL_FLOOR"] = _d("LIQ_SYNTH_SPREAD_VOL_FLOOR", "0")
    cfg["LIQ_SYNTH_SPREAD_VOL_PENALTY_BPS"] = _d("LIQ_SYNTH_SPREAD_VOL_PENALTY_BPS", "0")

    cfg["BT_DISABLE_LIQUIDITY"] = _b("BT_DISABLE_LIQUIDITY", "false")

    cfg["LIQ_MAX_SPREAD_BPS"] = _clamp_decimal(cfg["LIQ_MAX_SPREAD_BPS"], Decimal("0"), Decimal("500"))
    cfg["LIQ_SPREAD_MAX_BPS"] = cfg["LIQ_MAX_SPREAD_BPS"]

    cfg["LIQ_MIN_VOL_1M"] = _clamp_decimal(cfg["LIQ_MIN_VOL_1M"], Decimal("0"), Decimal("1000000000000"))
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
    cfg["LIQ_PENALIZE_HARD_BLOCK_AT"] = _clamp_int(cfg["LIQ_PENALIZE_HARD_BLOCK_AT"], 0, 100)

    # -------------------------
    # Phase 5C — Session Behavior
    # -------------------------
    cfg["USE_SESSION_MODIFIERS"] = _b("USE_SESSION_MODIFIERS", "false")

    cfg["SESSION_TIMEZONE"] = _s("SESSION_TIMEZONE", "UTC").upper()

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
    # Phase 5D — Exit Intelligence
    # -------------------------
    cfg["USE_EXIT_INTEL"] = _b("USE_EXIT_INTEL", "false")
    cfg["EXIT_PARTIAL_1R_FRACTION"] = _d("EXIT_PARTIAL_1R_FRACTION", "0.50")
    cfg["EXIT_TIME_STOP_BARS"] = _i("EXIT_TIME_STOP_BARS", "90")
    cfg["EXIT_ATR_TRAIL_MULT"] = _d("EXIT_ATR_TRAIL_MULT", "2.0")

    cfg["EXIT_PARTIAL_1R_FRACTION"] = _clamp_decimal(cfg["EXIT_PARTIAL_1R_FRACTION"], Decimal("0"), Decimal("1"))
    cfg["EXIT_TIME_STOP_BARS"] = _clamp_int(cfg["EXIT_TIME_STOP_BARS"], 0, 1_000_000)

    exit_atr_key = "EXIT_ATR_TRAIL_MULT"
    if "EXIT_ATr_TRAIL_MULT" in cfg:
        cfg[exit_atr_key] = cfg.get("EXIT_ATr_TRAIL_MULT")
    cfg[exit_atr_key] = _clamp_decimal(
        _d(exit_atr_key, str(cfg.get(exit_atr_key, "2.0"))),
        Decimal("0.1"),
        Decimal("50"),
    )
    cfg["EXIT_ATR_TRAIL_MULT"] = cfg[exit_atr_key]

    # -------------------------
    # Trade tracker
    # -------------------------
    cfg["TRADE_TRACKER_FILE"] = _s("TRADE_TRACKER_FILE", "trade_tracker.txt")
    cfg["TRADE_TRACKER_UNIQUE_PER_RUN"] = _b("TRADE_TRACKER_UNIQUE_PER_RUN", "false")
    cfg["TRACK_HOLD_REASONS"] = _b("TRACK_HOLD_REASONS", "true")
    cfg["HOLD_BUMP_EVERY_SECONDS"] = _i("HOLD_BUMP_EVERY_SECONDS", "300")

    cfg["HOLD_BUMP_EVERY_SECONDS"] = _clamp_int(cfg["HOLD_BUMP_EVERY_SECONDS"], 0, 86_400)

    # -------------------------
    # Phase 8 — Execution runtime
    # -------------------------
    cfg["EXECUTION_MODE"] = _s("EXECUTION_MODE", "ENGINE").upper()
    if cfg["EXECUTION_MODE"] not in ("ENGINE", "ADAPTER"):
        cfg["EXECUTION_MODE"] = "ENGINE"

    cfg["EXECUTION_ADAPTER"] = _s("EXECUTION_ADAPTER", "PAPER").upper()
    if cfg["EXECUTION_ADAPTER"] not in ("PAPER",):
        cfg["EXECUTION_ADAPTER"] = "PAPER"

    cfg["ACCOUNT_CURRENCY"] = _s("ACCOUNT_CURRENCY", "USD") or "USD"

    cfg["PAPER_FEE_BPS"] = _d("PAPER_FEE_BPS", str(cfg["FEE_BPS"]))
    cfg["PAPER_SLIPPAGE_BPS"] = _d("PAPER_SLIPPAGE_BPS", str(cfg["SLIPPAGE_BPS"]))
    cfg["PAPER_QTY_PRECISION"] = _s("PAPER_QTY_PRECISION", "0.00000001")
    cfg["PAPER_PX_PRECISION"] = _s("PAPER_PX_PRECISION", "0.01")

    cfg["PAPER_FEE_BPS"] = _clamp_decimal(cfg["PAPER_FEE_BPS"], Decimal("0"), Decimal("1000"))
    cfg["PAPER_SLIPPAGE_BPS"] = _clamp_decimal(cfg["PAPER_SLIPPAGE_BPS"], Decimal("0"), Decimal("1000"))

    cfg["MAX_ENTRY_SPREAD_BPS"] = _d("MAX_ENTRY_SPREAD_BPS", "0")
    cfg["MAX_POSITION_QTY_PER_SYMBOL"] = _d("MAX_POSITION_QTY_PER_SYMBOL", "0")
    cfg["MAX_POSITION_USD_PER_SYMBOL"] = _d("MAX_POSITION_USD_PER_SYMBOL", "0")
    cfg["MAX_PORTFOLIO_EXPOSURE_USD"] = _d("MAX_PORTFOLIO_EXPOSURE_USD", "0")
    cfg["MAX_MARKET_DATA_AGE_SECONDS"] = _i("MAX_MARKET_DATA_AGE_SECONDS", "0")

    cfg["MAX_ENTRY_SPREAD_BPS"] = _clamp_decimal(cfg["MAX_ENTRY_SPREAD_BPS"], Decimal("0"), Decimal("10000"))
    cfg["MAX_POSITION_QTY_PER_SYMBOL"] = _clamp_decimal(
        cfg["MAX_POSITION_QTY_PER_SYMBOL"], Decimal("0"), Decimal("1000000000")
    )
    cfg["MAX_POSITION_USD_PER_SYMBOL"] = _clamp_decimal(
        cfg["MAX_POSITION_USD_PER_SYMBOL"], Decimal("0"), Decimal("1000000000")
    )
    cfg["MAX_PORTFOLIO_EXPOSURE_USD"] = _clamp_decimal(
        cfg["MAX_PORTFOLIO_EXPOSURE_USD"], Decimal("0"), Decimal("1000000000")
    )
    cfg["MAX_MARKET_DATA_AGE_SECONDS"] = _clamp_int(cfg["MAX_MARKET_DATA_AGE_SECONDS"], 0, 604_800)

    cfg["EXECUTION_RECONCILE_ON_STARTUP"] = _b("EXECUTION_RECONCILE_ON_STARTUP", "true")
    cfg["EXECUTION_HEARTBEAT_REQUIRED"] = _b("EXECUTION_HEARTBEAT_REQUIRED", "false")
    cfg["EXECUTION_POLL_FILLS_EVERY_SECONDS"] = _f("EXECUTION_POLL_FILLS_EVERY_SECONDS", "1")
    cfg["EXECUTION_POLL_FILLS_EVERY_SECONDS"] = _clamp_float(
        cfg["EXECUTION_POLL_FILLS_EVERY_SECONDS"], 0.10, 600.0
    )

    # -------------------------
    # Phase 8 — Closure / lifecycle / reconcile
    # -------------------------
    cfg["TRADE_JOURNAL_ENABLED"] = _b("TRADE_JOURNAL_ENABLED", "true")
    cfg["DAILY_SUMMARY_ENABLED"] = _b("DAILY_SUMMARY_ENABLED", "true")
    cfg["RUN_SUMMARY_ENABLED"] = _b("RUN_SUMMARY_ENABLED", "true")
    cfg["RECONCILIATION_ENABLED"] = _b("RECONCILIATION_ENABLED", "true")
    cfg["PHASE8_RECONCILE_REPORT_DIR"] = _s("PHASE8_RECONCILE_REPORT_DIR", cfg["OPS_LOG_DIR"])

    # New: journal write policy / closure strictness
    cfg["TRADE_JOURNAL_APPEND_ON_CLOSE_ONLY"] = _b("TRADE_JOURNAL_APPEND_ON_CLOSE_ONLY", "true")
    cfg["TRADE_JOURNAL_REQUIRE_FLAT_AFTER_CLOSE"] = _b("TRADE_JOURNAL_REQUIRE_FLAT_AFTER_CLOSE", "true")
    cfg["RECONCILIATION_USE_TRADE_JOURNAL_AS_PRIMARY"] = _b("RECONCILIATION_USE_TRADE_JOURNAL_AS_PRIMARY", "true")
    cfg["RECONCILIATION_REQUIRE_TRUE_STARTING_FLAT_BASELINE"] = _b(
        "RECONCILIATION_REQUIRE_TRUE_STARTING_FLAT_BASELINE", "true"
    )

    # Paper fill realism / failure-path testing
    cfg["PAPER_FILL_MODE"] = _s("PAPER_FILL_MODE", "IMMEDIATE").upper()
    if cfg["PAPER_FILL_MODE"] not in ("IMMEDIATE", "DELAYED", "PARTIAL", "MANUAL", "NO_FILL"):
        cfg["PAPER_FILL_MODE"] = "IMMEDIATE"

    cfg["PAPER_FILL_DELAY_MS"] = _i("PAPER_FILL_DELAY_MS", "0")
    cfg["PAPER_PARTIAL_FILL_RATIO"] = _d("PAPER_PARTIAL_FILL_RATIO", "0.50")
    cfg["PAPER_ALLOW_MANUAL_FILL_TRIGGER"] = _b("PAPER_ALLOW_MANUAL_FILL_TRIGGER", "true")

    cfg["PAPER_FILL_DELAY_MS"] = _clamp_int(cfg["PAPER_FILL_DELAY_MS"], 0, 3_600_000)
    cfg["PAPER_PARTIAL_FILL_RATIO"] = _clamp_decimal(
        cfg["PAPER_PARTIAL_FILL_RATIO"], Decimal("0"), Decimal("1")
    )

    # Runtime failure-path timing
    cfg["ORDER_ACK_TIMEOUT_SECONDS"] = _f("ORDER_ACK_TIMEOUT_SECONDS", "30")
    cfg["PARTIAL_FILL_TIMEOUT_SECONDS"] = _f("PARTIAL_FILL_TIMEOUT_SECONDS", "120")

    cfg["ORDER_ACK_TIMEOUT_SECONDS"] = _clamp_float(cfg["ORDER_ACK_TIMEOUT_SECONDS"], 0.10, 86_400.0)
    cfg["PARTIAL_FILL_TIMEOUT_SECONDS"] = _clamp_float(
        cfg["PARTIAL_FILL_TIMEOUT_SECONDS"], 0.10, 86_400.0
    )

    # -------------------------
    # Phase 8 — Failure controls / runtime vetoes
    # -------------------------
    cfg["FAILURE_LOCKOUT_ENABLED"] = _b("FAILURE_LOCKOUT_ENABLED", "true")
    cfg["LOCKOUT_ON_HEARTBEAT_FAILURE"] = _b("LOCKOUT_ON_HEARTBEAT_FAILURE", "true")
    cfg["LOCKOUT_ON_STALE_MARKET_DATA"] = _b("LOCKOUT_ON_STALE_MARKET_DATA", "true")
    cfg["LOCKOUT_ON_ORDER_ACK_TIMEOUT"] = _b("LOCKOUT_ON_ORDER_ACK_TIMEOUT", "true")
    cfg["LOCKOUT_ON_PARTIAL_FILL_TIMEOUT"] = _b("LOCKOUT_ON_PARTIAL_FILL_TIMEOUT", "false")
    cfg["LOCKOUT_ON_DUPLICATE_FILL_REPLAY"] = _b("LOCKOUT_ON_DUPLICATE_FILL_REPLAY", "true")

    # -------------------------
    # Coherence rules
    # -------------------------
    if cfg["PAPER_FILL_MODE"] == "IMMEDIATE":
        cfg["PAPER_FILL_DELAY_MS"] = 0

    if cfg["PAPER_FILL_MODE"] == "NO_FILL":
        cfg["PAPER_PARTIAL_FILL_RATIO"] = Decimal("0")

    if cfg["PAPER_FILL_MODE"] == "PARTIAL" and cfg["PAPER_PARTIAL_FILL_RATIO"] == Decimal("0"):
        cfg["PAPER_PARTIAL_FILL_RATIO"] = Decimal("0.50")

    if cfg["PAPER_FILL_MODE"] != "MANUAL":
        cfg["PAPER_ALLOW_MANUAL_FILL_TRIGGER"] = False

    # Forced-test containment coherence
    if not cfg["FORCE_TEST_ONLY_ONCE"]:
        # If test loop is allowed to continue, containment flags become advisory only.
        cfg["FORCE_TEST_EXIT_ON_SUCCESS"] = False

    if not (cfg["FORCE_TEST_BUY"] or cfg["FORCE_TEST_SELL"]):
        cfg["FORCE_TEST_EXIT_ON_SUCCESS"] = False
        cfg["FORCE_TEST_EXIT_ON_FAILURE"] = False

    # Journal primary truth coherence
    if not cfg["TRADE_JOURNAL_ENABLED"]:
        cfg["RECONCILIATION_USE_TRADE_JOURNAL_AS_PRIMARY"] = False
        cfg["TRADE_JOURNAL_APPEND_ON_CLOSE_ONLY"] = False
        cfg["TRADE_JOURNAL_REQUIRE_FLAT_AFTER_CLOSE"] = False

    return cfg