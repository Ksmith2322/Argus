"""Build extended queue jobs to append to PC1's queue (target: ~400 total on PC1)."""
import json

jobs = []

# All coins with data
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
}

def job(label, coin, csv_path, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = csv_path
    env.update(overrides)
    jobs.append({"label": label, "env": env, "single_run": True})


# ============================================================
# COMBO GRID: TP × SL × Hold for ETH + BTC
# This is the money grid — interactions between exit params
# 5 TP × 5 SL × 4 hold × 2 coins = 200 jobs
# Too many — reduce to smart combos
# ============================================================

# TP × SL grid (keep hold at 60min, trail at 1.5%)
TP_VALUES = ["0.008", "0.01", "0.015", "0.02", "0.025"]
SL_VALUES = ["0.01", "0.015", "0.02", "0.03", "0.04"]

for tp in TP_VALUES:
    for sl in SL_VALUES:
        # Skip combos where SL < TP (nonsensical tight stop with wide target)
        tp_f, sl_f = float(tp), float(sl)
        if sl_f < tp_f * 0.5:
            continue
        tp_l = tp.replace("0.", "")
        sl_l = sl.replace("0.", "")
        for coin in ["ETH", "BTC"]:
            job(f"grid_tp{tp_l}_sl{sl_l}_{coin}", coin, ALL_COINS[coin],
                TAKE_PROFIT_PCT=tp, STOP_LOSS_PCT=sl, MAX_HOLD_SECONDS="3600")

# TP × Hold grid (keep SL at 2%, trail at 1.5%)
HOLD_VALUES = ["1800", "2700", "3600", "5400", "7200"]
for tp in TP_VALUES:
    for hold in HOLD_VALUES:
        tp_l = tp.replace("0.", "")
        hold_l = {"1800": "30m", "2700": "45m", "3600": "60m", "5400": "90m", "7200": "120m"}[hold]
        for coin in ["ETH", "BTC"]:
            job(f"grid_tp{tp_l}_h{hold_l}_{coin}", coin, ALL_COINS[coin],
                TAKE_PROFIT_PCT=tp, MAX_HOLD_SECONDS=hold)

# Trail × Hold grid (keep TP at 1.5%, SL at 2%)
TRAIL_VALUES = ["0.006", "0.01", "0.015", "0.02", "0.025"]
for trail in TRAIL_VALUES:
    for hold in HOLD_VALUES:
        tr_l = trail.replace("0.", "")
        hold_l = {"1800": "30m", "2700": "45m", "3600": "60m", "5400": "90m", "7200": "120m"}[hold]
        for coin in ["ETH", "BTC"]:
            job(f"grid_tr{tr_l}_h{hold_l}_{coin}", coin, ALL_COINS[coin],
                TRAIL_STOP_PCT=trail, MAX_HOLD_SECONDS=hold)

# ============================================================
# BREAKEVEN TRIGGER sweep (ETH + BTC)
# ============================================================
for be_trigger in ["0.002", "0.003", "0.005", "0.008", "0.01"]:
    be_l = be_trigger.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"be_trig{be_l}_{coin}", coin, ALL_COINS[coin],
            EXIT_BREAKEVEN_TRIGGER_PCT=be_trigger)

# ============================================================
# Write — append to existing queue
# ============================================================
with open("ops/backtest_queue_pc1_extend.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"PC1 extension built: {len(jobs)} additional jobs")
print(f"Estimated additional runtime: {len(jobs) * 1.5:.0f} min ({len(jobs) * 1.5 / 60:.1f} hours)")