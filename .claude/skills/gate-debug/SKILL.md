---
name: gate-debug
description: Explain why a specific signal was blocked. Given a symbol and optional timestamp, show which gate fired and what threshold would unblock it.
allowed-tools: Bash Read
argument-hint: '[symbol] [timestamp-or-latest]'
---

Debug why a specific signal didn't convert to a trade.

Parse `$ARGUMENTS`:
- First arg: symbol (e.g., `GBPUSD`, `CADJPY`, `USDJPY`)
- Second arg: timestamp (ISO) or `latest` (default)

1. **Find the last SIGNAL line for this symbol**:
   ```bash
   grep -E "inst\.$(echo \"$ARGUMENTS\" | awk '{print tolower($1)}'). *MTF SIGNAL" C:/Argus/repo/argus_flow/logs/runner_unified.log | tail -5
   ```

2. **Find the surrounding context** (5 lines before + 10 lines after the signal):
   Show AI overlay decision, MTF_BLOCK reason, risk check outcome, any NOTIONAL_CAP warning.

3. **Decode the block reason**:
   - `MTF_BLOCK LONG | MTF_LONG_NOT_AT_SUPPORT` → price isn't at the 4H/1H support level required by the trend-continuation pullback gate. By design. Not a bug.
   - `MTF_BLOCK SHORT | MTF_SHORT_NOT_AT_RESISTANCE` → same pattern, opposite direction.
   - `AI OVERLAY SKIP: consensus=...` → AI meta-model voted down. Show the FOR/AGAINST breakdown.
   - `DRAWDOWN_PAUSE` → the risk breaker. Check `portfolio_risk_state.json` for peak/current.
   - `RECON_DRIFT` → local-vs-broker position mismatch. Paper mode produces these harmlessly; real mode is serious.
   - `NOTIONAL_CAP` → warning only; trade still executes at capped size.

4. **Show relevant thresholds** from the config:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import json
   sym = '$ARGUMENTS'.split()[0].lower()
   for suffix in ['mtf_paper_v1', 'range_paper_v1']:
       p = f'C:/Argus/repo/argus_flow/configs/{sym}_{suffix}.json'
       try:
           c = json.load(open(p))
           print(f'Config: {p}')
           print(f'  strategy: {c.get(\"strategy\")}')
           print(f'  stage: {c.get(\"stage\")}')
           print(f'  risk: {json.dumps(c.get(\"risk\",{}), indent=2)}')
           break
       except FileNotFoundError: continue"
   ```

5. **Recommended action** per block reason:
   - MTF_BLOCK: wait or loosen the support-gate band (trade-logic change, cohort reset)
   - AI OVERLAY SKIP: review which weights dominated, retune if needed
   - DRAWDOWN_PAUSE: drop `RESET_DRAWDOWN` flag OR wait for session-reset at 00:00 UTC
   - Pre-trade filter block: check the specific filter in forge/titan_filters.py

6. Final one-liner: "Signal was blocked because X. To unblock: Y."

Use this when you ask "why didn't my strategy trade?" and want a 20-second answer instead of reading 100 log lines.
