"""Batch 3B: Re-run 45-day validation with loader fix.

The original val45d runs in batch 3 tested the FIRST half (Dec-Jan) due to
a bug in loader.py's early-break logic. The fix (removing early-break) means
BACKTEST_LIMIT now correctly returns the LAST N candles.

These 10 runs re-validate top configs on the second half (Feb-Mar).
"""
import json

ALL_COINS = {
    "ETH": "data/eth_usd_1m_90d.csv",
    "BTC": "data/btc_usd_1m_90d.csv",
}

BASE = {
    "ARGUS_MODE": "backtest",
    "BT_LITE_MODE": "true",
    "BACKTEST_LIMIT": "0",
    "USE_STRUCTURE": "false",
    "USE_TRENDLINES": "true",
    "USE_ADAPTIVE_CONFLUENCE": "true",
    "COMPOUND_SIZE_PCT": "0.05",
    "CONFLUENCE_MIN_SCORE": "88",
    "ML_GOVERNOR_THRESHOLD": "0.38",
    "ML_GOVERNOR_MODE": "GATE",
    "USE_ML_GOVERNOR": "true",
    "CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE": "0",
    "MAX_HOLD_SECONDS": "3600",
    "STOP_LOSS_PCT": "0.02",
    "TAKE_PROFIT_PCT": "0.015",
    "TRAIL_STOP_PCT": "0.015",
}

BTC_OVERRIDES = {
    "CONFLUENCE_MIN_SCORE": "80",
    "ML_GOVERNOR_THRESHOLD": "0.30",
    "EXIT_BREAKEVEN_TRIGGER_PCT": "0.008",
}

jobs = []

def job(label, coin, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = ALL_COINS[coin]
    if coin == "BTC":
        env.update(BTC_OVERRIDES)
    env.update(overrides)
    jobs.append({"label": label, "env": env, "single_run": True})


# 45-day validation: BACKTEST_LIMIT=64800 = last 45 days (Feb-Mar)
LIMIT_45D = "64800"

# 1. BTC be008 (the champion — PF 1.72 on full 90d)
job("v2_val45d_btc_be008", "BTC", BACKTEST_LIMIT=LIMIT_45D)

# 2. BTC be002 (runner-up PF 1.69 on 90d)
job("v2_val45d_btc_be002", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0.002")

# 3. BTC trail 2.5% (PF 1.64 on 90d)
job("v2_val45d_btc_tr025", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, TRAIL_STOP_PCT="0.025")

# 4. ETH trail 2.5% + 90min (best ETH: PF 1.045 on 90d)
job("v2_val45d_eth_tr025_h90m", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, TRAIL_STOP_PCT="0.025", MAX_HOLD_SECONDS="5400")

# 5. ETH be004 (new best breakeven from batch 3: PF 1.025)
job("v2_val45d_eth_be004", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0.004")

# 6. ETH baseline (current live config)
job("v2_val45d_eth_baseline", "ETH", BACKTEST_LIMIT=LIMIT_45D)

# 7. BTC baseline (no breakeven)
job("v2_val45d_btc_baseline", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0")

# 8. ETH + BTC with regime exits (default profile)
job("v2_val45d_eth_regime", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true")
job("v2_val45d_btc_regime", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true")

# 10. BTC regime_tponly (new batch 3 finding: PF 1.314)
job("v2_val45d_btc_regime_tponly", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true",
    EXIT_BREAKEVEN_TRIGGER_PCT="0",
    EXIT_TP_MULT_TREND_UP="1.5", EXIT_TP_MULT_TREND_DOWN="0.6",
    EXIT_TP_MULT_RANGE="0.8", EXIT_TP_MULT_VOLATILE_RANGE="1.2",
    EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="1.0",
    EXIT_SL_MULT_RANGE="1.0", EXIT_SL_MULT_VOLATILE_RANGE="1.0",
    EXIT_HOLD_MULT_TREND_UP="1.0", EXIT_HOLD_MULT_TREND_DOWN="1.0",
    EXIT_HOLD_MULT_RANGE="1.0", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")


# Write queue
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Batch 3B (val45d re-run): {len(jobs)} jobs (~{len(jobs) * 1.5:.0f} min)")
print(f"All use BACKTEST_LIMIT=64800 (last 45 days, Feb-Mar)")
print(f"Total: {len(jobs)} jobs")