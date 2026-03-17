# .env.sol — SOL-USD per-coin overrides (layered on base .env)
# Backtests show SOL consistently unprofitable (PF 0.53-0.64 all configs)
# Running at reduced risk to collect data — may disable later

PRODUCT_ID=SOL-USD
START_CASH_USD=500
MAX_POSITION_USD_PER_SYMBOL=75
MAX_PORTFOLIO_EXPOSURE_USD=75

# SOL-specific: reduced risk while we learn
COMPOUND_SIZE_PCT=0.02
DAILY_MAX_LOSS_USD=5
MAX_TRADES_PER_DAY=4