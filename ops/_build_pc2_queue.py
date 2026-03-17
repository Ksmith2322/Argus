"""Build PC2 queue (~400 jobs) — complementary to PC1's queue.
Focus: confluence weights, vol sizing, per-coin deep dives, alt coin tuning."""
import json

jobs = []

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

def job(label, coin, csv_path, **overrides):
    env = dict(BASE)
    env["PRODUCT_ID"] = f"{coin}-USD"
    env["BACKTEST_CSV"] = csv_path
    env.update(overrides)
    jobs.append({"label": label, "env": env, "single_run": True})


# ============================================================
# BATCH P1: CONFLUENCE WEIGHT GRID (ETH + BTC) — 36 jobs
# Current: 1m=0.2, 5m=0.3, 1h=0.5
# Question: Are these weights optimal?
# ============================================================
weight_combos = [
    # (w1m, w5m, w1h) — must sum to ~1.0
    ("0.1", "0.3", "0.6"),   # heavy 1h
    ("0.1", "0.2", "0.7"),   # very heavy 1h
    ("0.15", "0.35", "0.5"), # balanced-ish
    ("0.2", "0.3", "0.5"),   # current (baseline)
    ("0.2", "0.4", "0.4"),   # equal 5m/1h
    ("0.3", "0.3", "0.4"),   # more 1m weight
    ("0.3", "0.4", "0.3"),   # heavy 5m
    ("0.33", "0.33", "0.34"),# equal weights
    ("0.4", "0.3", "0.3"),   # heavy 1m
    ("0.1", "0.4", "0.5"),   # low 1m, high 5m
    ("0.2", "0.5", "0.3"),   # dominant 5m
    ("0.15", "0.25", "0.6"), # dominant 1h
]
for w1m, w5m, w1h in weight_combos:
    label = f"cw_{w1m.replace('0.','')}-{w5m.replace('0.','')}-{w1h.replace('0.','')}"
    for coin in ["ETH", "BTC", "AVAX"]:
        job(f"{label}_{coin}", coin, ALL_COINS[coin],
            CONFLUENCE_W_1M=w1m, CONFLUENCE_W_5M=w5m, CONFLUENCE_W_1H=w1h)


# ============================================================
# BATCH P2: VOL SIZING GRID (ETH + BTC) — 30 jobs
# Question: Risk per trade × vol stop multiplier interaction
# ============================================================
RISK_VALUES = ["3", "5", "8", "10", "15"]
VOL_MULT_VALUES = ["1.5", "2.0", "2.5", "3.0"]
for risk in RISK_VALUES:
    for mult in VOL_MULT_VALUES:
        mult_l = mult.replace(".", "")
        for coin in ["ETH"]:
            job(f"vol_r{risk}_m{mult_l}_{coin}", coin, ALL_COINS[coin],
                RISK_PER_TRADE_USD=risk, VOL_STOP_MULT=mult)
# Subset for BTC
for risk in ["3", "5", "10"]:
    for mult in ["1.5", "2.0", "3.0"]:
        mult_l = mult.replace(".", "")
        job(f"vol_r{risk}_m{mult_l}_BTC", "BTC", ALL_COINS["BTC"],
            RISK_PER_TRADE_USD=risk, VOL_STOP_MULT=mult)


# ============================================================
# BATCH P3: ALT COIN DEEP TUNING — all alts with loose+no-gov combos (69 jobs)
# Question: Best params for each alt
# ============================================================
ALT_COINS = ["DOGE", "LINK", "AVAX", "SUI", "ADA", "XRP", "DOT", "UNI",
             "NEAR", "ATOM", "FIL", "LTC", "XLM", "AAVE", "APT", "ARB",
             "OP", "SHIB", "HBAR", "ICP", "SOL"]

# No-gov + loose gating (best chance for alts)
for coin in ALT_COINS:
    if coin not in ALL_COINS:
        continue
    job(f"alt_nogov_s75_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="75")
    job(f"alt_nogov_s80_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80")
    job(f"alt_nogov_s85_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="85")


# ============================================================
# BATCH P4: ALT COIN EXIT TUNING — wider stops for volatile alts (42 jobs)
# Question: Do alts need different exit params?
# ============================================================
for coin in ALT_COINS:
    if coin not in ALL_COINS:
        continue
    # Wider exits for volatile alts
    job(f"alt_wide_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        TAKE_PROFIT_PCT="0.02", STOP_LOSS_PCT="0.03", TRAIL_STOP_PCT="0.02",
        MAX_HOLD_SECONDS="5400")
    # Tight scalp for alts
    job(f"alt_scalp_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        TAKE_PROFIT_PCT="0.008", STOP_LOSS_PCT="0.012", TRAIL_STOP_PCT="0.006",
        MAX_HOLD_SECONDS="1800")


# ============================================================
# BATCH P5: SCORE × GOVERNOR GRID — ETH + BTC (30 jobs)
# Question: Score threshold × governor threshold interaction
# ============================================================
for score in ["80", "85", "88", "90", "92"]:
    for gate in ["0.30", "0.35", "0.38", "0.42", "0.45", "0.50"]:
        gate_l = gate.replace(".", "")
        for coin in ["ETH"]:
            job(f"sg_s{score}_g{gate_l}_{coin}", coin, ALL_COINS[coin],
                CONFLUENCE_MIN_SCORE=score, ML_GOVERNOR_THRESHOLD=gate)
# Subset for BTC
for score in ["75", "80", "85"]:
    for gate in ["0.25", "0.30", "0.35", "0.40"]:
        gate_l = gate.replace(".", "")
        job(f"sg_s{score}_g{gate_l}_BTC", "BTC", ALL_COINS["BTC"],
            CONFLUENCE_MIN_SCORE=score, ML_GOVERNOR_THRESHOLD=gate)


# ============================================================
# BATCH P6: ADAPTIVE CONFLUENCE DEEP SWEEP — ETH + BTC (24 jobs)
# Question: Optimal bonus/penalty combo
# ============================================================
for trend_bonus in ["3", "5", "8", "10"]:
    for range_penalty in ["10", "15", "20"]:
        for coin in ["ETH", "BTC"]:
            job(f"ac_tb{trend_bonus}_rp{range_penalty}_{coin}", coin, ALL_COINS[coin],
                AC_BONUS_TREND_UP=trend_bonus, AC_PENALTY_RANGE=range_penalty)


# ============================================================
# BATCH P7: TRENDLINE ON/OFF × SCORE for all alts (42 jobs)
# Question: Do trendlines help or hurt alts?
# ============================================================
for coin in ALT_COINS:
    if coin not in ALL_COINS:
        continue
    # Trendlines OFF
    job(f"alt_notl_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        USE_TRENDLINES="false")
    # Trendlines ON (already covered in other batches, but with consistent params)
    job(f"alt_tl_{coin}", coin, ALL_COINS[coin],
        USE_ML_GOVERNOR="false", CONFLUENCE_MIN_SCORE="80",
        USE_TRENDLINES="true")


# ============================================================
# BATCH P8: COOLDOWN SWEEP — ETH + BTC (10 jobs)
# Question: Optimal time between trades
# ============================================================
for cd in ["120", "300", "600", "900", "1800"]:
    cd_l = {"120": "2m", "300": "5m", "600": "10m", "900": "15m", "1800": "30m"}[cd]
    for coin in ["ETH", "BTC"]:
        job(f"cd_{cd_l}_{coin}", coin, ALL_COINS[coin],
            COOLDOWN_SECONDS=cd)


# ============================================================
# BATCH P9: MAX TRADES PER DAY — ETH + BTC (10 jobs)
# Question: Should we trade more or less frequently?
# ============================================================
for mtpd in ["4", "6", "8", "12", "16"]:
    for coin in ["ETH", "BTC"]:
        job(f"mtpd{mtpd}_{coin}", coin, ALL_COINS[coin],
            MAX_TRADES_PER_DAY=mtpd)


# ============================================================
# BATCH P10: BEST COMBO CANDIDATES — hand-picked promising combos (20 jobs)
# Based on what we know: structure OFF, trendlines ON is best
# ============================================================
# Aggressive scalper: tight everything, fast
for coin in ["ETH", "BTC"]:
    job(f"combo_aggscalp_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.008", STOP_LOSS_PCT="0.01", TRAIL_STOP_PCT="0.005",
        MAX_HOLD_SECONDS="1200", COOLDOWN_SECONDS="120", MAX_TRADES_PER_DAY="16",
        CONFLUENCE_MIN_SCORE="80")

# Conservative swing: wide targets, patient
for coin in ["ETH", "BTC"]:
    job(f"combo_consswing_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.03", STOP_LOSS_PCT="0.04", TRAIL_STOP_PCT="0.025",
        MAX_HOLD_SECONDS="14400", COOLDOWN_SECONDS="900", MAX_TRADES_PER_DAY="4",
        CONFLUENCE_MIN_SCORE="92")

# Balanced optimal: tweaked from current
for coin in ["ETH", "BTC"]:
    job(f"combo_balanced_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.012", STOP_LOSS_PCT="0.018", TRAIL_STOP_PCT="0.01",
        MAX_HOLD_SECONDS="3600", COOLDOWN_SECONDS="300")

# High-frequency: many small trades
for coin in ["ETH", "BTC"]:
    job(f"combo_highfreq_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.006", STOP_LOSS_PCT="0.008", TRAIL_STOP_PCT="0.004",
        MAX_HOLD_SECONDS="900", COOLDOWN_SECONDS="60", MAX_TRADES_PER_DAY="20",
        CONFLUENCE_MIN_SCORE="75", USE_ML_GOVERNOR="false")

# Risk-averse: big edge or no trade
for coin in ["ETH", "BTC"]:
    job(f"combo_riskaverse_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.02", STOP_LOSS_PCT="0.015", TRAIL_STOP_PCT="0.015",
        MAX_HOLD_SECONDS="5400", CONFLUENCE_MIN_SCORE="92",
        ML_GOVERNOR_THRESHOLD="0.50")

# Asymmetric R:R — tight stop, wide target
for coin in ["ETH", "BTC"]:
    job(f"combo_asymm_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.025", STOP_LOSS_PCT="0.01", TRAIL_STOP_PCT="0.012",
        MAX_HOLD_SECONDS="5400")

# Trailing-dominant — let winners run
for coin in ["ETH", "BTC"]:
    job(f"combo_traildom_{coin}", coin, ALL_COINS[coin],
        TAKE_PROFIT_PCT="0.05", STOP_LOSS_PCT="0.015", TRAIL_STOP_PCT="0.01",
        MAX_HOLD_SECONDS="7200")

# Breakeven-focused — lock in early, then trail
for coin in ["ETH", "BTC"]:
    job(f"combo_befocus_{coin}", coin, ALL_COINS[coin],
        EXIT_BREAKEVEN_TRIGGER_PCT="0.002", TRAIL_STOP_PCT="0.008",
        TAKE_PROFIT_PCT="0.02", STOP_LOSS_PCT="0.015",
        MAX_HOLD_SECONDS="3600")

# Session-filtered + optimal
for coin in ["ETH", "BTC"]:
    job(f"combo_nyonly_{coin}", coin, ALL_COINS[coin],
        SESSION_ENTRY_BLOCK_LIST="ASIA,OFF")

# No regime block (allow RANGE entries)
for coin in ["ETH", "BTC"]:
    job(f"combo_allregime_{coin}", coin, ALL_COINS[coin],
        REGIME_ENTRY_BLOCK_LIST="TREND_DOWN")


# ============================================================
# Write queue
# ============================================================
with open("ops/backtest_queue_pc2.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"PC2 queue built: {len(jobs)} jobs")
print(f"Estimated runtime: {len(jobs) * 1.5:.0f} min ({len(jobs) * 1.5 / 60:.1f} hours)")