"""Build a comprehensive backtest queue for overnight coin screening + param optimization."""
import json

jobs = []

# ============================================================
# All coins we have data for (or will have shortly)
# ============================================================
ALL_COINS = {
    # Already tested with default config
    "ETH": "data/eth_usd_1m_90d.csv",
    "BTC": "data/btc_usd_1m_90d.csv",
    "SOL": "data/sol_usd_1m_90d.csv",
    "DOGE": "data/doge_usd_1m_90d.csv",
    "LINK": "data/link_usd_1m_90d.csv",
    "AVAX": "data/avax_usd_1m_90d.csv",
    "SUI": "data/sui_usd_1m_90d.csv",
    "ADA": "data/ada_usd_1m_90d.csv",
    # New batch downloading now
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

# Base env for all jobs (winning config)
BASE = {
    "ARGUS_MODE": "backtest",
    "BT_LITE_MODE": "true",
    "BACKTEST_LIMIT": "0",
    "USE_STRUCTURE": "false",
    "USE_TRENDLINES": "true",
    "USE_ADAPTIVE_CONFLUENCE": "true",
    "MAX_HOLD_SECONDS": "3600",
    "COMPOUND_SIZE_PCT": "0.05",
    "CONFLUENCE_MIN_SCORE": "88",
    "ML_GOVERNOR_THRESHOLD": "0.38",
    "ML_GOVERNOR_MODE": "GATE",
    "USE_ML_GOVERNOR": "true",
    "CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE": "0",
    "STOP_LOSS_PCT": "0.02",
    "TAKE_PROFIT_PCT": "0.015",
    "TRAIL_STOP_PCT": "0.015",
}

def job(label, coin, csv_path, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = csv_path
    env.update(overrides)
    jobs.append({"label": label, "env": env, "single_run": True})


# ============================================================
# BATCH 1: COIN DISCOVERY — new coins, winning config (15 runs)
# Question: Which new coins show ANY edge?
# ============================================================
NEW_COINS = ["XRP", "DOT", "UNI", "NEAR", "ATOM", "FIL", "LTC", "XLM",
             "AAVE", "APT", "ARB", "OP", "SHIB", "HBAR", "ICP"]
for coin in NEW_COINS:
    job(f"screen_{coin}_default", coin, ALL_COINS[coin])


# ============================================================
# BATCH 2: NO-GOVERNOR BASELINE — ALL coins (23 runs)
# Question: Is the ETH-trained governor hurting altcoin performance?
# ============================================================
for coin, csv in ALL_COINS.items():
    job(f"no_gov_{coin}", coin, csv,
        USE_ML_GOVERNOR="false", ML_GOVERNOR_MODE="LOG_ONLY")


# ============================================================
# BATCH 3: LOOSE GATING SWEEP — all coins with score 75/80 (46 runs)
# Question: Do altcoins need lower score thresholds?
# ============================================================
for score in ["75", "80"]:
    for coin, csv in ALL_COINS.items():
        job(f"score{score}_{coin}", coin, csv,
            CONFLUENCE_MIN_SCORE=score)


# ============================================================
# BATCH 4: GOVERNOR THRESHOLD SWEEP — ETH + BTC + AVAX (15 runs)
# Question: Is 0.38 really optimal? What about looser/tighter?
# ============================================================
for gate in ["0.25", "0.30", "0.35", "0.40", "0.45"]:
    for coin in ["ETH", "BTC", "AVAX"]:
        job(f"gate{gate.replace('.', '')}_{coin}", coin, ALL_COINS[coin],
            ML_GOVERNOR_THRESHOLD=gate)


# ============================================================
# BATCH 5: HOLD TIME SWEEP — ETH + BTC + top alts (24 runs)
# Question: Optimal hold time per coin?
# ============================================================
for hold in ["1800", "2700", "3600", "5400", "7200", "10800"]:
    hold_label = {
        "1800": "30min", "2700": "45min", "3600": "60min",
        "5400": "90min", "7200": "120min", "10800": "180min"
    }[hold]
    for coin in ["ETH", "BTC", "AVAX", "DOGE"]:
        job(f"hold{hold_label}_{coin}", coin, ALL_COINS[coin],
            MAX_HOLD_SECONDS=hold)


# ============================================================
# BATCH 6: EXIT MODEL SWEEP — ETH + BTC (18 runs)
# Question: Are our TP/SL/trail settings optimal?
# ============================================================
# TP sweep
for tp in ["0.01", "0.015", "0.02", "0.025", "0.03"]:
    tp_label = tp.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"tp{tp_label}_{coin}", coin, ALL_COINS[coin],
            TAKE_PROFIT_PCT=tp)

# SL sweep
for sl in ["0.01", "0.015", "0.02", "0.03", "0.04"]:
    sl_label = sl.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"sl{sl_label}_{coin}", coin, ALL_COINS[coin],
            STOP_LOSS_PCT=sl)

# Trail sweep
for trail in ["0.008", "0.01", "0.015", "0.02", "0.025"]:
    trail_label = trail.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"trail{trail_label}_{coin}", coin, ALL_COINS[coin],
            TRAIL_STOP_PCT=trail)

# Combined: tighter exit (quick scalp model)
for coin in ["ETH", "BTC"]:
    job(f"scalp_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.008", STOP_LOSS_PCT="0.01", TRAIL_STOP_PCT="0.006",
        MAX_HOLD_SECONDS="1800")

# Combined: wider exit (swing model)
for coin in ["ETH", "BTC"]:
    job(f"swing_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.03", STOP_LOSS_PCT="0.04", TRAIL_STOP_PCT="0.025",
        MAX_HOLD_SECONDS="10800")


# ============================================================
# BATCH 7: COMPOUND RATE SWEEP — ETH + BTC (10 runs)
# Question: Optimal compounding aggressiveness?
# ============================================================
for pct in ["0.02", "0.03", "0.05", "0.08", "0.10"]:
    pct_label = pct.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"compound{pct_label}_{coin}", coin, ALL_COINS[coin],
            COMPOUND_SIZE_PCT=pct)


# ============================================================
# BATCH 8: SESSION FILTERS — ETH + BTC (8 runs)
# Question: Should we block certain trading sessions?
# ============================================================
for block_sessions in ["ASIA", "OFF", "ASIA,OFF", "ASIA,OFF,LONDON"]:
    label = block_sessions.replace(",", "_").lower()
    for coin in ["ETH", "BTC"]:
        job(f"block_{label}_{coin}", coin, ALL_COINS[coin],
            SESSION_ENTRY_BLOCK_LIST=block_sessions)


# ============================================================
# BATCH 9: WATCH ENTRY SIZING — ETH + BTC (8 runs)
# Question: Should we allow WATCH entries at reduced size?
# ============================================================
for mult in ["0.10", "0.15", "0.25", "0.50"]:
    mult_label = mult.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"watch{mult_label}_{coin}", coin, ALL_COINS[coin],
            CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=mult,
            CONFLUENCE_MIN_SCORE="80")


# ============================================================
# BATCH 10: REGIME TUNING — ETH + BTC (6 runs)
# Question: Should we allow RANGE regime entries?
# ============================================================
for regime_block in ["TREND_DOWN", "TREND_DOWN,RANGE", ""]:
    label = regime_block.replace(",", "_").lower() if regime_block else "none"
    for coin in ["ETH", "BTC"]:
        job(f"regime_{label}_{coin}", coin, ALL_COINS[coin],
            REGIME_ENTRY_BLOCK_LIST=regime_block)


# ============================================================
# BATCH 11: TRENDLINE TUNING — ETH + BTC (8 runs)
# Question: Trendline param sensitivity
# ============================================================
for near_pct in ["0.003", "0.005", "0.008", "0.01"]:
    label = near_pct.replace("0.", "")
    for coin in ["ETH", "BTC"]:
        job(f"tlnear{label}_{coin}", coin, ALL_COINS[coin],
            TL_NEAR_PCT=near_pct)


# ============================================================
# BATCH 12: ADAPTIVE CONFLUENCE PENALTY SWEEP — ETH + BTC (8 runs)
# Question: Are range/volatile penalties optimal?
# ============================================================
for range_pen in ["10", "15", "20", "25"]:
    for coin in ["ETH", "BTC"]:
        job(f"acpen{range_pen}_{coin}", coin, ALL_COINS[coin],
            AC_PENALTY_RANGE=range_pen)


# ============================================================
# BATCH 13: RISK PARAM SWEEP — ETH + BTC (10 runs)
# Question: Daily loss cap and drawdown pause optimal?
# ============================================================
for daily_loss in ["10", "15", "20", "30", "50"]:
    for coin in ["ETH", "BTC"]:
        job(f"dailyloss{daily_loss}_{coin}", coin, ALL_COINS[coin],
            DAILY_MAX_LOSS_USD=daily_loss)


# ============================================================
# BATCH 14: BEST COMBO for promising alts — AVAX with BTC-style loose params (6 runs)
# Question: Can AVAX be profitable with tuned params?
# ============================================================
for score, gate in [("80", "0.30"), ("75", "0.25"), ("80", "0.35"),
                    ("85", "0.30"), ("75", "0.30"), ("80", "0.25")]:
    job(f"avax_s{score}_g{gate.replace('.', '')}", "AVAX", ALL_COINS["AVAX"],
        CONFLUENCE_MIN_SCORE=score, ML_GOVERNOR_THRESHOLD=gate)


# ============================================================
# Write queue
# ============================================================
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Queue built: {len(jobs)} jobs")
print(f"Estimated runtime: {len(jobs) * 1.5:.0f} min ({len(jobs) * 1.5 / 60:.1f} hours)")
print()

# Batch summary
batches = {}
for j in jobs:
    batch = j["label"].split("_")[0]
    batches[batch] = batches.get(batch, 0) + 1

print("Batch breakdown:")
for b, count in batches.items():
    print(f"  {b}: {count} jobs")