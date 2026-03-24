"""Batch 2: 50 jobs per machine, focused on lost high-value tests.
PC1: coin screening + breakeven trigger combos
PC2: alt coin tuning + confluence weight validation
"""
import json

pc1_jobs = []
pc2_jobs = []

ALL_COINS = {
    "ETH": "data/eth_usd_1m_90d.csv",
    "BTC": "data/btc_usd_1m_90d.csv",
    "SOL": "data/sol_usd_1m_90d.csv",
    "DOGE": "data/doge_usd_1m_90d.csv",
    "LINK": "data/link_usd_1m_90d.csv",
    "AVAX": "data/avax_usd_1m_90d.csv",
    "SUI": "data/sui_usd_1m_90d.csv",
    "ADA": "data/ada_usd_1m_90d.csv",
    "XRP": "data/xrp_usd_1m_90d.csv",
    "DOT": "data/dot_usd_1m_90d.csv",
    "UNI": "data/uni_usd_1m_90d.csv",
    "NEAR": "data/near_usd_1m_90d.csv",
    "ATOM": "data/atom_usd_1m_90d.csv",
    "FIL": "data/fil_usd_1m_90d.csv",
    "LTC": "data/ltc_usd_1m_90d.csv",
    "XLM": "data/xlm_usd_1m_90d.csv",
    "AAVE": "data/aave_usd_1m_90d.csv",
    "APT": "data/apt_usd_1m_90d.csv",
    "ARB": "data/arb_usd_1m_90d.csv",
    "OP": "data/op_usd_1m_90d.csv",
    "SHIB": "data/shib_usd_1m_90d.csv",
    "HBAR": "data/hbar_usd_1m_90d.csv",
    "ICP": "data/icp_usd_1m_90d.csv",
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

def job(target, label, coin, csv_path, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = csv_path
    env.update(overrides)
    target.append({"label": label, "env": env, "single_run": True})


# ============================================================
# PC1: COIN SCREENING (default config) — 15 new coins
# These were lost in the cleanup
# ============================================================
NEW_COINS = ["XRP", "DOT", "UNI", "NEAR", "ATOM", "FIL", "LTC", "XLM",
             "AAVE", "APT", "ARB", "OP", "SHIB", "HBAR", "ICP"]
for coin in NEW_COINS:
    job(pc1_jobs, f"screen_{coin}_default", coin, ALL_COINS[coin])

# PC1: NO-GOVERNOR baseline for new coins (15 runs) — governor may be hurting them
for coin in NEW_COINS:
    job(pc1_jobs, f"nogov_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", ML_GOVERNOR_MODE="LOG_ONLY")

# PC1: BREAKEVEN TRIGGER on ETH (big finding for BTC, test on ETH) — 5 runs
for be in ["0.003", "0.005", "0.008", "0.010", "0.015"]:
    be_l = be.replace("0.", "")
    job(pc1_jobs, f"eth_be{be_l}", "ETH", ALL_COINS["ETH"],
        EXIT_BREAKEVEN_TRIGGER_PCT=be)

# PC1: BTC with new breakeven + exit combos — 10 runs
for tp, sl in [("0.015", "0.02"), ("0.02", "0.025"), ("0.01", "0.015"),
               ("0.02", "0.03"), ("0.025", "0.03")]:
    tp_l = tp.replace("0.", "")
    sl_l = sl.replace("0.", "")
    job(pc1_jobs, f"btc_be8_tp{tp_l}_sl{sl_l}", "BTC", ALL_COINS["BTC"],
        EXIT_BREAKEVEN_TRIGGER_PCT="0.008", TAKE_PROFIT_PCT=tp, STOP_LOSS_PCT=sl)
    job(pc1_jobs, f"eth_be8_tp{tp_l}_sl{sl_l}", "ETH", ALL_COINS["ETH"],
        EXIT_BREAKEVEN_TRIGGER_PCT="0.008", TAKE_PROFIT_PCT=tp, STOP_LOSS_PCT=sl)


# ============================================================
# PC2: ALT COIN TUNING (loose + no-gov + wider exits)
# ============================================================
ALT_TOP = ["AVAX", "XRP", "LTC", "DOGE", "LINK", "DOT", "UNI", "HBAR"]
for coin in ALT_TOP:
    # No-gov + loose score + wider exits
    job(pc2_jobs, f"alt_loose_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="75",
        TAKE_PROFIT_PCT="0.02", STOP_LOSS_PCT="0.03", MAX_HOLD_SECONDS="5400")
    # No-gov + tight scalp
    job(pc2_jobs, f"alt_scalp_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        TAKE_PROFIT_PCT="0.008", STOP_LOSS_PCT="0.012", MAX_HOLD_SECONDS="1800")
    # No-gov + breakeven trigger (the BTC winner)
    job(pc2_jobs, f"alt_be8_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        EXIT_BREAKEVEN_TRIGGER_PCT="0.008")

# PC2: CONFLUENCE WEIGHT VALIDATION (ETH + BTC) — 10 runs
top_weights = [
    ("0.1", "0.3", "0.6"), ("0.1", "0.2", "0.7"),
    ("0.3", "0.3", "0.4"), ("0.2", "0.5", "0.3"),
    ("0.15", "0.25", "0.6"),
]
for w1m, w5m, w1h in top_weights:
    wl = f"{w1m.replace('0.','')}-{w5m.replace('0.','')}-{w1h.replace('0.','')}"
    job(pc2_jobs, f"cw_{wl}_ETH", "ETH", ALL_COINS["ETH"],
        CONFLUENCE_W_1M=w1m, CONFLUENCE_W_5M=w5m, CONFLUENCE_W_1H=w1h)
    job(pc2_jobs, f"cw_{wl}_BTC", "BTC", ALL_COINS["BTC"],
        CONFLUENCE_W_1M=w1m, CONFLUENCE_W_5M=w5m, CONFLUENCE_W_1H=w1h)

# PC2: SESSION FILTERS (ETH + BTC) — 6 runs
for block in ["ASIA", "OFF", "ASIA,OFF"]:
    bl = block.replace(",", "_").lower()
    job(pc2_jobs, f"session_{bl}_ETH", "ETH", ALL_COINS["ETH"],
        SESSION_ENTRY_BLOCK_LIST=block)
    job(pc2_jobs, f"session_{bl}_BTC", "BTC", ALL_COINS["BTC"],
        SESSION_ENTRY_BLOCK_LIST=block)


# Write queues
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in pc1_jobs:
        f.write(json.dumps(j) + "\n")

with open("ops/backtest_queue_pc2.jsonl", "w") as f:
    for j in pc2_jobs:
        f.write(json.dumps(j) + "\n")

print(f"PC1 queue: {len(pc1_jobs)} jobs (~{len(pc1_jobs) * 1.5:.0f} min)")
print(f"PC2 queue: {len(pc2_jobs)} jobs (~{len(pc2_jobs) * 1.0:.0f} min)")
print(f"Total: {len(pc1_jobs) + len(pc2_jobs)} jobs")