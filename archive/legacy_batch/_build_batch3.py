"""Batch 3: Validate new features + fill remaining gaps.

Tests:
  1. Dynamic regime exits (ETH + BTC) — 12 multiplier combos
  2. Breakeven trigger fine-tuning on ETH — 8 combos
  3. BTC winning config + regime exits — 6 combos
  4. 45-day validation of top 5 configs (ETH + BTC) — 10 runs
  5. Regime exit + breakeven combined — 8 runs
  6. Trail stop sweep (ETH was best at 2.5%) — 6 runs
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

# BTC base overrides (looser gating)
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


# ============================================================
# 1. DYNAMIC REGIME EXITS — test multiplier profiles (12 runs)
# ============================================================
# Profile A: Conservative (mild adjustments)
for coin in ["ETH", "BTC"]:
    job(f"regime_conservative_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.3", EXIT_TP_MULT_TREND_DOWN="0.7",
        EXIT_TP_MULT_RANGE="0.9", EXIT_TP_MULT_VOLATILE_RANGE="1.1",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="0.8",
        EXIT_SL_MULT_RANGE="0.9", EXIT_SL_MULT_VOLATILE_RANGE="1.1",
        EXIT_HOLD_MULT_TREND_UP="1.3", EXIT_HOLD_MULT_TREND_DOWN="0.7",
        EXIT_HOLD_MULT_RANGE="0.8", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")

# Profile B: Aggressive (big adjustments — let uptrends run, cut downtrends fast)
for coin in ["ETH", "BTC"]:
    job(f"regime_aggressive_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="2.0", EXIT_TP_MULT_TREND_DOWN="0.5",
        EXIT_TP_MULT_RANGE="0.7", EXIT_TP_MULT_VOLATILE_RANGE="1.3",
        EXIT_SL_MULT_TREND_UP="1.2", EXIT_SL_MULT_TREND_DOWN="0.6",
        EXIT_SL_MULT_RANGE="0.7", EXIT_SL_MULT_VOLATILE_RANGE="1.3",
        EXIT_HOLD_MULT_TREND_UP="2.0", EXIT_HOLD_MULT_TREND_DOWN="0.5",
        EXIT_HOLD_MULT_RANGE="0.6", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")

# Profile C: Default multipliers (from config.py)
for coin in ["ETH", "BTC"]:
    job(f"regime_default_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true")

# Profile D: TP-only regime (keep SL/hold static, only adjust take profit)
for coin in ["ETH", "BTC"]:
    job(f"regime_tponly_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.5", EXIT_TP_MULT_TREND_DOWN="0.6",
        EXIT_TP_MULT_RANGE="0.8", EXIT_TP_MULT_VOLATILE_RANGE="1.2",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="1.0",
        EXIT_SL_MULT_RANGE="1.0", EXIT_SL_MULT_VOLATILE_RANGE="1.0",
        EXIT_HOLD_MULT_TREND_UP="1.0", EXIT_HOLD_MULT_TREND_DOWN="1.0",
        EXIT_HOLD_MULT_RANGE="1.0", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")

# Profile E: Hold-only regime (keep TP/SL static, only adjust hold time)
for coin in ["ETH", "BTC"]:
    job(f"regime_holdonly_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.0", EXIT_TP_MULT_TREND_DOWN="1.0",
        EXIT_TP_MULT_RANGE="1.0", EXIT_TP_MULT_VOLATILE_RANGE="1.0",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="1.0",
        EXIT_SL_MULT_RANGE="1.0", EXIT_SL_MULT_VOLATILE_RANGE="1.0",
        EXIT_HOLD_MULT_TREND_UP="1.5", EXIT_HOLD_MULT_TREND_DOWN="0.5",
        EXIT_HOLD_MULT_RANGE="0.75", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")

# Profile F: SL-only regime
for coin in ["ETH", "BTC"]:
    job(f"regime_slonly_{coin}", coin,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.0", EXIT_TP_MULT_TREND_DOWN="1.0",
        EXIT_TP_MULT_RANGE="1.0", EXIT_TP_MULT_VOLATILE_RANGE="1.0",
        EXIT_SL_MULT_TREND_UP="1.2", EXIT_SL_MULT_TREND_DOWN="0.6",
        EXIT_SL_MULT_RANGE="0.8", EXIT_SL_MULT_VOLATILE_RANGE="1.2",
        EXIT_HOLD_MULT_TREND_UP="1.0", EXIT_HOLD_MULT_TREND_DOWN="1.0",
        EXIT_HOLD_MULT_RANGE="1.0", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")


# ============================================================
# 2. ETH BREAKEVEN TRIGGER FINE-TUNING (8 runs)
# ============================================================
# Batch 2 showed ETH be003 PF 1.014. Let's find the sweet spot.
for be in ["0.002", "0.003", "0.004", "0.005", "0.006", "0.007", "0.008", "0.010"]:
    be_l = be.replace("0.", "")
    job(f"eth_be_fine_{be_l}", "ETH",
        EXIT_BREAKEVEN_TRIGGER_PCT=be)


# ============================================================
# 3. BTC WINNING CONFIG + REGIME EXITS (6 runs)
# ============================================================
# BTC be008 is PF 1.72 — does adding regime exits help further?
for profile, tp_m, sl_m, h_m_up, h_m_down in [
    ("cons", "1.3", "1.0", "1.3", "0.7"),
    ("aggr", "2.0", "0.6", "2.0", "0.5"),
    ("tponly", "1.5", "1.0", "1.0", "1.0"),
]:
    job(f"btc_be8_regime_{profile}", "BTC",
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP=tp_m, EXIT_TP_MULT_TREND_DOWN="0.6",
        EXIT_SL_MULT_TREND_UP=sl_m, EXIT_SL_MULT_TREND_DOWN="0.7",
        EXIT_HOLD_MULT_TREND_UP=h_m_up, EXIT_HOLD_MULT_TREND_DOWN=h_m_down)

# Also test BTC without breakeven but WITH regime exits
for profile, tp_m in [("cons", "1.3"), ("aggr", "2.0"), ("tponly", "1.5")]:
    job(f"btc_nobe_regime_{profile}", "BTC",
        EXIT_BREAKEVEN_TRIGGER_PCT="0",
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP=tp_m, EXIT_TP_MULT_TREND_DOWN="0.6",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="0.7",
        EXIT_HOLD_MULT_TREND_UP="1.3", EXIT_HOLD_MULT_TREND_DOWN="0.7")


# ============================================================
# 4. 45-DAY VALIDATION of top configs (10 runs)
# ============================================================
# Use second half of 90-day data (approx 64800 candles = 45 days)
LIMIT_45D = "64800"

# BTC be008 (the champion)
job("val45d_btc_be008", "BTC", BACKTEST_LIMIT=LIMIT_45D)

# BTC be002 (runner-up PF 1.69)
job("val45d_btc_be002", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0.002")

# BTC trail 2.5% + 60min (PF 1.64)
job("val45d_btc_tr025_h60m", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, TRAIL_STOP_PCT="0.025")

# ETH trail 2.5% + 90min (best ETH: PF 1.045)
job("val45d_eth_tr025_h90m", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, TRAIL_STOP_PCT="0.025", MAX_HOLD_SECONDS="5400")

# ETH be003 (PF 1.014)
job("val45d_eth_be003", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0.003")

# ETH baseline (current live config)
job("val45d_eth_baseline", "ETH", BACKTEST_LIMIT=LIMIT_45D)

# BTC baseline (no breakeven)
job("val45d_btc_baseline", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, EXIT_BREAKEVEN_TRIGGER_PCT="0")

# ETH + BTC with regime exits (default profile)
job("val45d_eth_regime", "ETH",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true")
job("val45d_btc_regime", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true")

# BTC be008 + regime aggressive
job("val45d_btc_be8_regime_aggr", "BTC",
    BACKTEST_LIMIT=LIMIT_45D, USE_DYNAMIC_REGIME_EXITS="true",
    EXIT_TP_MULT_TREND_UP="2.0", EXIT_TP_MULT_TREND_DOWN="0.5",
    EXIT_SL_MULT_TREND_UP="1.2", EXIT_SL_MULT_TREND_DOWN="0.6",
    EXIT_HOLD_MULT_TREND_UP="2.0", EXIT_HOLD_MULT_TREND_DOWN="0.5")


# ============================================================
# 5. REGIME EXITS + BREAKEVEN COMBINED (8 runs)
# ============================================================
for be in ["0.003", "0.005", "0.008", "0.010"]:
    be_l = be.replace("0.", "")
    # ETH: breakeven + regime conservative
    job(f"eth_be{be_l}_regime_cons", "ETH",
        EXIT_BREAKEVEN_TRIGGER_PCT=be,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.3", EXIT_TP_MULT_TREND_DOWN="0.7",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="0.8",
        EXIT_HOLD_MULT_TREND_UP="1.3", EXIT_HOLD_MULT_TREND_DOWN="0.7")
    # BTC: breakeven + regime conservative
    job(f"btc_be{be_l}_regime_cons", "BTC",
        EXIT_BREAKEVEN_TRIGGER_PCT=be,
        USE_DYNAMIC_REGIME_EXITS="true",
        EXIT_TP_MULT_TREND_UP="1.3", EXIT_TP_MULT_TREND_DOWN="0.7",
        EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="0.8",
        EXIT_HOLD_MULT_TREND_UP="1.3", EXIT_HOLD_MULT_TREND_DOWN="0.7")


# ============================================================
# 6. TRAIL STOP SWEEP (6 runs)
# ============================================================
# ETH best was trail 2.5% — test around that
for trail in ["0.02", "0.025", "0.03", "0.035", "0.04", "0.05"]:
    tr_l = trail.replace("0.", "")
    job(f"eth_trail_{tr_l}", "ETH", TRAIL_STOP_PCT=trail)


# Write queue
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Batch 3 queue: {len(jobs)} jobs (~{len(jobs) * 1.5:.0f} min)")
print(f"Breakdown:")
print(f"  Regime exit profiles: 12 runs")
print(f"  ETH breakeven fine-tuning: 8 runs")
print(f"  BTC winning + regime combos: 6 runs")
print(f"  45-day validation: 10 runs")
print(f"  Regime + breakeven combined: 8 runs")
print(f"  Trail stop sweep: 6 runs")
print(f"  Total: {len(jobs)} jobs")