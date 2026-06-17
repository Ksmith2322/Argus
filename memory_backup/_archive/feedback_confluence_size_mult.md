---
name: CONFLUENCE_SIZE_MULT missing from config.py
description: CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE was not a canonical config.py key, so .env value was silently ignored
type: feedback
---

# Bug: CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE not wired through config.py

## What happened
Setting `CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE=0` in `.env` had no effect because `config.py` never loaded it as a canonical key. The engine used `cfg.get("CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE", "0.25")` which always returned the `"0.25"` default.

## Fix applied (2026-03-11)
Added to `config.py` around line 290:
```python
cfg["CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE"] = _d("CONFLUENCE_SIZE_MULT_WHEN_NOT_TRADE", "0.25")
```

## Impact
- All backtests before this fix used MULT=0.25 (25% size for WATCH entries), even when .env said 0
- bt_20260311T232631Z_e6a12e26 and bt_20260312T004551Z_434ef6eb both ran with MULT=0.25
- A fresh backtest is needed to see the true effect of MULT=0 (blocking WATCH entries)

## Lesson
Any new config key added to .env MUST also be registered in config.py. The engine's direct cfg.get() with a default bypasses .env entirely.
