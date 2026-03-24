"""Batch 4: Final comprehensive sweep before deployment.

Covers every remaining question:
  1. BTC regime_tponly combos (with/without breakeven)     — 6 runs
  2. Position sizing sweep (10-30% compound)                — 8 runs
  3. Exit parameter sweep (TP/SL/trail combos)              — 8 runs
  4. Confluence score & governor threshold tuning            — 8 runs
  5. Hold time sweep                                        — 6 runs
  6. BTC momentum gate for ETH                              — 4 runs
  7. Session filters (block ASIA/OFF hours)                  — 4 runs
  8. Risk param sweep (max trades, cooldown)                 — 4 runs
  9. No-governor sanity check                                — 2 runs

All on 45-day recent data (Feb-Mar) with loader fix.
~50 jobs, ~75 min estimated.
"""
import json

ALL_COINS = {
    "ETH": "data/eth_usd_1m_90d.csv",
    "BTC": "data/btc_usd_1m_90d.csv",
}

LIMIT_45D = "64800"

BASE = {
    "ARGUS_MODE": "backtest",
    "BT_LITE_MODE": "true",
    "BACKTEST_LIMIT": LIMIT_45D,
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

# BTC regime_tponly overrides (the 45d champion)
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
# 1. BTC REGIME_TPONLY COMBOS (6 runs)
# ============================================================
# Can we combine regime_tponly with breakeven?
_tponly_no_be = {k: v for k, v in BTC_REGIME_TPONLY.items() if k != "EXIT_BREAKEVEN_TRIGGER_PCT"}
job("btc_tponly_be008", "BTC", **_tponly_no_be,
    EXIT_BREAKEVEN_TRIGGER_PCT="0.008")
job("btc_tponly_be004", "BTC", **_tponly_no_be,
    EXIT_BREAKEVEN_TRIGGER_PCT="0.004")
job("btc_tponly_be002", "BTC", **_tponly_no_be,
    EXIT_BREAKEVEN_TRIGGER_PCT="0.002")

# Regime_tponly with wider/tighter TP base
job("btc_tponly_tp2pct", "BTC", **BTC_REGIME_TPONLY,
    TAKE_PROFIT_PCT="0.02")
job("btc_tponly_tp1pct", "BTC", **BTC_REGIME_TPONLY,
    TAKE_PROFIT_PCT="0.01")

# ETH with regime_tponly (does it work for ETH too?)
job("eth_tponly", "ETH",
    USE_DYNAMIC_REGIME_EXITS="true",
    EXIT_TP_MULT_TREND_UP="1.5", EXIT_TP_MULT_TREND_DOWN="0.6",
    EXIT_TP_MULT_RANGE="0.8", EXIT_TP_MULT_VOLATILE_RANGE="1.2",
    EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="1.0",
    EXIT_SL_MULT_RANGE="1.0", EXIT_SL_MULT_VOLATILE_RANGE="1.0",
    EXIT_HOLD_MULT_TREND_UP="1.0", EXIT_HOLD_MULT_TREND_DOWN="1.0",
    EXIT_HOLD_MULT_RANGE="1.0", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")


# ============================================================
# 2. POSITION SIZING SWEEP (8 runs)
# ============================================================
# How does scaling position size affect returns & risk?
for pct in ["0.10", "0.15", "0.20", "0.25"]:
    pct_l = pct.replace("0.", "")
    # ETH baseline at each size
    job(f"eth_size{pct_l}pct", "ETH", COMPOUND_SIZE_PCT=pct)
    # BTC best config at each size (regime_tponly)
    job(f"btc_tponly_size{pct_l}pct", "BTC", **BTC_REGIME_TPONLY,
        COMPOUND_SIZE_PCT=pct)


# ============================================================
# 3. EXIT PARAMETER SWEEP (8 runs)
# ============================================================
# TP sweep (keep SL=2%, trail=TP)
for tp in ["0.008", "0.012", "0.02", "0.025"]:
    tp_l = tp.replace("0.", "")
    job(f"eth_tp{tp_l}", "ETH",
        TAKE_PROFIT_PCT=tp, TRAIL_STOP_PCT=tp)
    job(f"btc_tp{tp_l}", "BTC",
        TAKE_PROFIT_PCT=tp, TRAIL_STOP_PCT=tp)


# ============================================================
# 4. CONFLUENCE SCORE & GOVERNOR THRESHOLD (8 runs)
# ============================================================
# ETH score sweep (currently 88)
for score in ["80", "85", "92", "95"]:
    job(f"eth_score{score}", "ETH", CONFLUENCE_MIN_SCORE=score)

# BTC governor threshold sweep (currently 0.30)
for thresh in ["0.20", "0.35", "0.42", "0.50"]:
    th_l = thresh.replace("0.", "")
    job(f"btc_gov{th_l}", "BTC", ML_GOVERNOR_THRESHOLD=thresh)


# ============================================================
# 5. HOLD TIME SWEEP (6 runs)
# ============================================================
for hold, hl in [("1800", "30m"), ("2700", "45m"), ("5400", "90m")]:
    job(f"eth_hold_{hl}", "ETH", MAX_HOLD_SECONDS=hold)
    job(f"btc_hold_{hl}", "BTC", MAX_HOLD_SECONDS=hold)


# ============================================================
# 6. BTC MOMENTUM GATE for ETH (4 runs)
# ============================================================
# Does gating ETH entries when BTC trends down help?
job("eth_btc_gate_on", "ETH",
    USE_BTC_MOMENTUM_GATE="true",
    BTC_MOMENTUM_BLOCK_REGIMES="TREND_DOWN")
job("eth_btc_gate_wide", "ETH",
    USE_BTC_MOMENTUM_GATE="true",
    BTC_MOMENTUM_BLOCK_REGIMES="TREND_DOWN,VOLATILE_RANGE")
# Same but with best ETH config
job("eth_btc_gate_be004", "ETH",
    USE_BTC_MOMENTUM_GATE="true",
    BTC_MOMENTUM_BLOCK_REGIMES="TREND_DOWN",
    EXIT_BREAKEVEN_TRIGGER_PCT="0.004")
# ETH tponly + momentum gate
job("eth_tponly_btc_gate", "ETH",
    USE_BTC_MOMENTUM_GATE="true",
    BTC_MOMENTUM_BLOCK_REGIMES="TREND_DOWN",
    USE_DYNAMIC_REGIME_EXITS="true",
    EXIT_TP_MULT_TREND_UP="1.5", EXIT_TP_MULT_TREND_DOWN="0.6",
    EXIT_TP_MULT_RANGE="0.8", EXIT_TP_MULT_VOLATILE_RANGE="1.2",
    EXIT_SL_MULT_TREND_UP="1.0", EXIT_SL_MULT_TREND_DOWN="1.0",
    EXIT_SL_MULT_RANGE="1.0", EXIT_SL_MULT_VOLATILE_RANGE="1.0",
    EXIT_HOLD_MULT_TREND_UP="1.0", EXIT_HOLD_MULT_TREND_DOWN="1.0",
    EXIT_HOLD_MULT_RANGE="1.0", EXIT_HOLD_MULT_VOLATILE_RANGE="1.0")


# ============================================================
# 7. SESSION FILTERS (4 runs)
# ============================================================
# Block low-volume sessions
job("eth_block_asia", "ETH", SESSION_ENTRY_BLOCK_LIST="ASIA")
job("eth_block_off", "ETH", SESSION_ENTRY_BLOCK_LIST="OFF")
job("btc_block_asia", "BTC", SESSION_ENTRY_BLOCK_LIST="ASIA")
job("btc_block_off", "BTC", SESSION_ENTRY_BLOCK_LIST="OFF")


# ============================================================
# 8. RISK PARAM SWEEP (4 runs)
# ============================================================
# Max trades per day
job("eth_maxtrades12", "ETH", MAX_TRADES_PER_DAY="12")
job("eth_maxtrades4", "ETH", MAX_TRADES_PER_DAY="4")
# Cooldown after loss
job("eth_cooldown300", "ETH", COOLDOWN_AFTER_LOSS_SECONDS="300")
job("eth_cooldown900", "ETH", COOLDOWN_AFTER_LOSS_SECONDS="900")


# ============================================================
# 9. NO-GOVERNOR SANITY CHECK (2 runs)
# ============================================================
# Is the governor still helping?
job("eth_no_governor", "ETH", USE_ML_GOVERNOR="false")
job("btc_no_governor", "BTC", USE_ML_GOVERNOR="false")


# Write queue
with open("ops/backtest_queue.jsonl", "w") as f:
    for j in jobs:
        f.write(json.dumps(j) + "\n")

print(f"Batch 4 (final sweep): {len(jobs)} jobs")
print(f"All on 45-day recent data (Feb-Mar)")
print(f"Breakdown:")
print(f"  1. BTC regime_tponly combos:  6 runs")
print(f"  2. Position sizing sweep:     8 runs")
print(f"  3. Exit parameter sweep:      8 runs")
print(f"  4. Score & governor tuning:   8 runs")
print(f"  5. Hold time sweep:           6 runs")
print(f"  6. BTC momentum gate:         4 runs")
print(f"  7. Session filters:           4 runs")
print(f"  8. Risk param sweep:          4 runs")
print(f"  9. No-governor sanity check:  2 runs")
print(f"  Total: {len(jobs)} jobs (~{len(jobs) * 1.5:.0f} min)")