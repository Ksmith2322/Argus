---
name: Canonical dedup + reconciliation keys include symbol + conditional exit_ts
description: Multi-symbol strategies can silently collapse trades in reconciliation. Always include symbol in both the dedup and reconciliation keys; drop exit_ts from the key when the CSV doesn't persist it.
type: feedback
originSessionId: 468782c5-c83c-4fc9-8b1b-60051a2f4e99
---
When adding or modifying a strategy whose CSV writer records trades, make sure `helio/canonical_fills.py:specs` and `helio/reconciliation.py:SPECS` list the strategy with the correct `symbol_hint` and `csv_has_exit_ts` flags.

**Why:** a subtle bug let `forge_jpy_pm_short` (multi-symbol: USDJPY+CADJPY) show $100 PnL drift with row counts matching. The old dedup key `(strategy, entry_ts, exit_ts)` collapsed two simultaneous opens at the same bar into one, silently dropping the CADJPY row from canonical. A parallel bug: some CSV writers (e.g. gld_pm_long) never persist `exit_ts` as a column, so the canonical's real exit_ts made every closed trade look like "extra in canonical".

**How to apply:**
1. New strategy with its own trades.csv — add a spec entry in BOTH `canonical_fills.py` and `reconciliation.py`. Shape: `(label, path, ts_col, valid_only, symbol_hint, csv_has_exit_ts)`.
2. `symbol_hint` — set to the concrete symbol for single-symbol strategies (e.g. `"GLD"`). Set to `None` ONLY if the CSV has a reliable `symbol` column. Without the hint the backfill-written row gets symbol="" but the live-written row gets the real symbol, and they get treated as distinct.
3. `csv_has_exit_ts` — set to `False` if the CSV format doesn't persist exit_ts (or any closing timestamp) as its own column. When False, reconciliation drops exit_ts from the comparison key.
4. When a legacy canonical file has rows written before the symbol key was introduced, run a one-time migration to stamp the inferred symbol via `symbol_hint` so existing rows match the new key shape.

Any new dedup / reconciliation bug of the "row counts match but pnl drifts" shape likely traces to a symbol or exit_ts key mismatch.
