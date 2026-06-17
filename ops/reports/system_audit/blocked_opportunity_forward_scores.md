# Blocked Opportunity Forward Scoring

This report scores blocked entries only when local forward bars are available.
Missing bar data is a truth blocker for expectancy claims, not a reason to loosen filters.

## Status Counts
- RESOLVED: 117

## Resolved Gate Summary
- MAINTENANCE_BLACKOUT: expectancy -6.6 pips over 1 resolved rows -> KEEP_GATE
- MTF_BLOCKED_MTF_LONG_COUNTER_TREND_NO_RSI: expectancy -4.3 pips over 1 resolved rows -> KEEP_GATE
- MTF_BLOCKED_MTF_LONG_NOT_AT_SUPPORT: expectancy 2.6808 pips over 13 resolved rows -> REVIEW_GATE_WITH_MORE_DATA
- MTF_BLOCKED_MTF_SHORT_COUNTER_TREND_NO_RSI: expectancy -4.1038 pips over 13 resolved rows -> KEEP_GATE
- MTF_BLOCKED_MTF_SHORT_NOT_AT_RESISTANCE: expectancy -2.6989 pips over 45 resolved rows -> KEEP_GATE
- NEWS_BLOCKED: expectancy -1.7929 pips over 7 resolved rows -> KEEP_GATE
- RUNTIME_BLOCKED_RECON_DRIFT: expectancy -0.7909 pips over 22 resolved rows -> KEEP_GATE
- SPREAD_BLOCKED: expectancy -2.7458 pips over 12 resolved rows -> KEEP_GATE
- STALE_TICKER_BLOCKED: expectancy -0.25 pips over 3 resolved rows -> KEEP_GATE
