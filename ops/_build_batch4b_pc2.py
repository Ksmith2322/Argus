"""Batch 4B: Complementary tests for PC2 (parallel to batch 4 on PC1).

Covers what batch 4 doesn't:
  1. Full 90-day validation of top configs              — 8 runs
  2. Stop loss sweep (independent from TP)              — 8 runs
  3. Combined best-of-best configs                      — 6 runs
  4. Drawdown pause threshold sweep                     — 4 runs
  5. Volatility sizing params (VOL_STOP_MULT, risk)     — 6 runs
  6. Trendline parameter sensitivity                    — 6 runs
  7. Adaptive confluence tuning                         — 4 runs
  8. Fee sensitivity (what if fees differ?)             — 4 runs
  9. Entry cooldown sweep                               — 4 runs

All on 90-day data (full dataset) unless noted.
~50 jobs, ~75 min estimated.
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
# 1. FULL 90-DAY VALIDATION of best configs (8 runs)
# ============================================================
# Confirm top performers hold up on full dataset with loader fix
# BTC regime_tponly (45d champion PF 1.995)
job("full90d_btc_tponly", "BTC", **BTC_REGIME_TPONLY)
# BTC baseline (45d PF 1.796)
job("full90d_btc_baseline", "BTC", EXIT_BREAKEVEN_TRIGGER_PCT="0")
# BTC be008 (original champion PF 1.72)
job("full90d_btc_be008", "BTC")
# ETH baseline (45d PF 1.640)
job("full90d_eth_baseline", "ETH")
# ETH be004 (45d PF 1.568)
job("full90d_eth_be004", "ETH", EXIT_BREAKEVEN_TRIGGER_PCT="0.004")
# ETH trail 2.5% + 90min (45d PF 1.403)
job("full90d_eth_tr025_h90m", "ETH",
    TRAIL_STOP_PCT="0.025", MAX_HOLD_SECONDS="5400")
# BTC regime_tponly + 20% sizing (for capital scaling data)
job("full90d_btc_tponly_size20", "BTC", **BTC_REGIME_TPONLY,
    COMPOUND_SIZE_PCT="0.20")
# ETH baseline + 20% sizing
job("full90d_eth_baseline_size20", "ETH", COMPOUND_SIZE_PCT="0.20")


# ============================================================
# 2. STOP LOSS SWEEP (8 runs)
# ============================================================
# Batch 4 sweeps TP but not SL independently
for sl in ["0.012", "0.015", "0.025", "0.03"]:
    sl_l = sl.replace("0.", "")
    job(f"eth_sl{sl_l}", "ETH", STOP_LOSS_PCT=sl)
    job(f"btc_sl{sl_l}", "BTC", STOP_LOSS_PCT=sl)


# ============================================================
# 3. COMBINED BEST-OF-BEST CONFIGS (6 runs)
# ============================================================
# Mix the best individual findings into combo configs
# BTC: tponly + best TP + best hold
job("btc_combo_tponly_tp2_h45m", "BTC", **BTC_REGIME_TPONLY,
    TAKE_PROFIT_PCT="0.02", MAX_HOLD_SECONDS="2700")
job("btc_combo_tponly_tp2_h60m", "BTC", **BTC_REGIME_TPONLY,
    TAKE_PROFIT_PCT="0.02", MAX_HOLD_SECONDS="3600")
# ETH: block ASIA + best TP
job("eth_combo_blockasia_tp2", "ETH",
    SESSION_ENTRY_BLOCK_LIST="ASIA", TAKE_PROFIT_PCT="0.02")
# ETH: score 85 + wider trail
job("eth_combo_score85_tr025", "ETH",
    CONFLUENCE_MIN_SCORE="85", TRAIL_STOP_PCT="0.025")
# BTC: tponly + lower score + wider trail
job("btc_combo_tponly_score75_tr025", "BTC", **BTC_REGIME_TPONLY,
    CONFLUENCE_MIN_SCORE="75", TRAIL_STOP_PCT="0.025")
# ETH: be004 + block off-hours
job("eth_combo_be004_blockoff", "ETH",
    EXIT_BREAKEVEN_TRIGGER_PCT="0.004", SESSION_ENTRY_BLOCK_LIST="OFF")


# ============================================================
# 4. DRAWDOWN PAUSE THRESHOLD (4 runs)
# ============================================================
# Currently 3% — is tighter or looser better?
for dd in ["0.02", "0.05"]:
    dd_l = dd.replace("0.", "")
    job(f"eth_dd{dd_l}pct", "ETH", DRAWDOWN_PAUSE_PCT=dd)
    job(f"btc_dd{dd_l}pct", "BTC", DRAWDOWN_PAUSE_PCT=dd)


# ============================================================
# 5. VOLATILITY SIZING PARAMS (6 runs)
# ============================================================
# VOL_STOP_MULT: how many ATRs for vol-based stop (currently 2.0)
for mult in ["1.5", "2.5", "3.0"]:
    m_l = mult.replace(".", "")
    job(f"eth_volmult{m_l}", "ETH", VOL_STOP_MULT=mult)
    job(f"btc_volmult{m_l}", "BTC", VOL_STOP_MULT=mult)


# ============================================================
# 6. TRENDLINE PARAMETER SENSITIVITY (6 runs)
# ============================================================
# Trendline bonuses/penalties — are defaults optimal?
# Higher bonuses = more aggressive trendline scoring
job("eth_tl_high_bonus", "ETH",
    TL_BONUS_NEAR_SUPPORT="8", TL_BONUS_BROKE_ABOVE_RESIST="18",
    TL_PENALTY_NEAR_RESIST="12", TL_PENALTY_BROKE_BELOW_SUPPORT="20")
job("eth_tl_low_bonus", "ETH",
    TL_BONUS_NEAR_SUPPORT="2", TL_BONUS_BROKE_ABOVE_RESIST="6",
    TL_PENALTY_NEAR_RESIST="4", TL_PENALTY_BROKE_BELOW_SUPPORT="8")
job("eth_tl_no_block", "ETH", TL_BLOCK_LONG_BROKE_BELOW_SUPPORT="false")
job("btc_tl_high_bonus", "BTC",
    TL_BONUS_NEAR_SUPPORT="8", TL_BONUS_BROKE_ABOVE_RESIST="18",
    TL_PENALTY_NEAR_RESIST="12", TL_PENALTY_BROKE_BELOW_SUPPORT="20")
job("btc_tl_no_block", "BTC", TL_BLOCK_LONG_BROKE_BELOW_SUPPORT="false")
# Trendlines OFF (confirm they still help on 90d)
job("eth_no_trendlines", "ETH", USE_TRENDLINES="false")


# ============================================================
# 7. ADAPTIVE CONFLUENCE TUNING (4 runs)
# ============================================================
# Heavier trend down penalty
job("eth_ac_heavy_penalty", "ETH",
    AC_BONUS_TREND_DOWN="-25", AC_PENALTY_RANGE="20", AC_PENALTY_VOLATILE_RANGE="25")
# Lighter penalties (let more trades through)
job("eth_ac_light_penalty", "ETH",
    AC_BONUS_TREND_DOWN="-8", AC_PENALTY_RANGE="8", AC_PENALTY_VOLATILE_RANGE="12")
# Same for BTC
job("btc_ac_heavy_penalty", "BTC",
    AC_BONUS_TREND_DOWN="-25", AC_PENALTY_RANGE="20", AC_PENALTY_VOLATILE_RANGE="25")
job("btc_ac_light_penalty", "BTC",
    AC_BONUS_TREND_DOWN="-8", AC_PENALTY_RANGE="8", AC_PENALTY_VOLATILE_RANGE="12")


# ============================================================
# 8. FEE SENSITIVITY (4 runs)
# ============================================================
# What if actual fees + slippage are higher/lower?
job("eth_fee_low", "ETH", FEE_BPS="3", SLIPPAGE_BPS="2", PAPER_FEE_BPS="3", PAPER_SLIPPAGE_BPS="2")
job("eth_fee_high", "ETH", FEE_BPS="10", SLIPPAGE_BPS="8", PAPER_FEE_BPS="10", PAPER_SLIPPAGE_BPS="8")
job("btc_fee_low", "BTC", FEE_BPS="3", SLIPPAGE_BPS="2", PAPER_FEE_BPS="3", PAPER_SLIPPAGE_BPS="2")
job("btc_fee_high", "BTC", FEE_BPS="10", SLIPPAGE_BPS="8", PAPER_FEE_BPS="10", PAPER_SLIPPAGE_BPS="8")


# ============================================================
# 9. ENTRY COOLDOWN SWEEP (4 runs)
# ============================================================
# How long between entries? Currently 300s (5min)
job("eth_cooldown120", "ETH", COOLDOWN_SECONDS="120")
job("eth_cooldown600", "ETH", COOLDOWN_SECONDS="600")
job("btc_cooldown120", "BTC", COOLDOWN_SECONDS="120")
job("btc_cooldown600", "BTC", COOLDOWN_SECONDS="600")


# Write queue
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Batch 4B (PC2 complement): {len(jobs)} jobs")
print(f"Full 90-day data (except where noted)")
print(f"Breakdown:")
print(f"  1. Full 90-day validation:      8 runs")
print(f"  2. Stop loss sweep:             8 runs")
print(f"  3. Combined best-of-best:       6 runs")
print(f"  4. Drawdown pause threshold:    4 runs")
print(f"  5. Volatility sizing params:    6 runs")
print(f"  6. Trendline sensitivity:       6 runs")
print(f"  7. Adaptive confluence tuning:  4 runs")
print(f"  8. Fee sensitivity:             4 runs")
print(f"  9. Entry cooldown sweep:        4 runs")
print(f"  Total: {len(jobs)} jobs (~{len(jobs) * 1.5:.0f} min)")