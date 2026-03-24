"""Batch 5: Walk-forward validation — 3x30-day non-overlapping windows.

Gold standard robustness test. If the strategy is profitable in all 3
windows, it's robust. If only 1-2, we're curve-fitting.

Windows (from 90-day ETH/BTC data, ~43188 candles each):
  W1: Dec 17 - Jan 16 (BACKTEST_LIMIT=43188, BACKTEST_SKIP_LAST=86376)
  W2: Jan 16 - Feb 15 (BACKTEST_LIMIT=43188, BACKTEST_SKIP_LAST=43188)
  W3: Feb 15 - Mar 17 (BACKTEST_LIMIT=43188, BACKTEST_SKIP_LAST=0)

Tests each window with top configs for both coins.
~24 jobs, ~36 min estimated.
"""
import json

ALL_COINS = {
    "ETH": "data/eth_usd_1m_90d.csv",
    "BTC": "data/btc_usd_1m_90d.csv",
}

BASE = {
    "ARGUS_MODE": "backtest",
    "BT_LITE_MODE": "true",
    "BACKTEST_LIMIT": "43188",
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

BTC_REGIME_TPONLY = {
    "USE_DYNAMIC_REGIME_EXITS": "true",
    "EXIT_BREAKEVEN_TRIGGER_PCT": "0",
    "EXIT_TP_MULT_TREND_UP": "1.5",
    "EXIT_TP_MULT_TREND_DOWN": "0.6",
    "EXIT_TP_MULT_RANGE": "0.8",
    "EXIT_TP_MULT_VOLATILE_RANGE": "1.2",
    "EXIT_SL_MULT_TREND_UP": "1.0",
    "EXIT_SL_MULT_TREND_DOWN": "1.0",
    "EXIT_SL_MULT_RANGE": "1.0",
    "EXIT_SL_MULT_VOLATILE_RANGE": "1.0",
    "EXIT_HOLD_MULT_TREND_UP": "1.0",
    "EXIT_HOLD_MULT_TREND_DOWN": "1.0",
    "EXIT_HOLD_MULT_RANGE": "1.0",
    "EXIT_HOLD_MULT_VOLATILE_RANGE": "1.0",
}

WINDOWS = [
    ("w1", "86376"),  # Dec 17 - Jan 16
    ("w2", "43188"),  # Jan 16 - Feb 15
    ("w3", "0"),      # Feb 15 - Mar 17
]

jobs = []


def job(label, coin, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = ALL_COINS[coin]
    if coin == "BTC":
        env.update(BTC_OVERRIDES)
    env.update(overrides)
    jobs.append({"label": label, "env": env, "single_run": True})


# Top configs to validate across all 3 windows:
# 1. ETH baseline (current live)
# 2. ETH be004
# 3. BTC be008 (current champion on 90d)
# 4. BTC regime_tponly (champion on 45d)
# 5. BTC baseline (no breakeven — strong on 45d)
# 6. ETH tr025+90m (alternative ETH config)

for wname, skip_last in WINDOWS:
    # ETH configs
    job(f"wf_{wname}_eth_baseline", "ETH",
        BACKTEST_SKIP_LAST=skip_last)
    job(f"wf_{wname}_eth_be004", "ETH",
        BACKTEST_SKIP_LAST=skip_last, EXIT_BREAKEVEN_TRIGGER_PCT="0.004")

    # BTC configs
    job(f"wf_{wname}_btc_be008", "BTC",
        BACKTEST_SKIP_LAST=skip_last)
    job(f"wf_{wname}_btc_tponly", "BTC",
        BACKTEST_SKIP_LAST=skip_last, **BTC_REGIME_TPONLY)
    job(f"wf_{wname}_btc_baseline", "BTC",
        BACKTEST_SKIP_LAST=skip_last, EXIT_BREAKEVEN_TRIGGER_PCT="0")

    # ETH alternative
    job(f"wf_{wname}_eth_tr025_h90m", "ETH",
        BACKTEST_SKIP_LAST=skip_last, TRAIL_STOP_PCT="0.025", MAX_HOLD_SECONDS="5400")

    # Governor OFF sanity check (1 per window per coin)
    job(f"wf_{wname}_eth_nogov", "ETH",
        BACKTEST_SKIP_LAST=skip_last, USE_ML_GOVERNOR="false")
    job(f"wf_{wname}_btc_nogov", "BTC",
        BACKTEST_SKIP_LAST=skip_last, USE_ML_GOVERNOR="false")


# Write queue
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Batch 5 (walk-forward): {len(jobs)} jobs (~{len(jobs) * 1.5:.0f} min)")
print(f"3 windows x 8 configs = {len(jobs)} runs")
print(f"  W1: Dec 17 - Jan 16 (SKIP_LAST=86376)")
print(f"  W2: Jan 16 - Feb 15 (SKIP_LAST=43188)")
print(f"  W3: Feb 15 - Mar 17 (SKIP_LAST=0)")