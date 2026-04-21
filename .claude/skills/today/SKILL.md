---
name: today
description: One-page digest of the last 24h — trades executed, signals blocked (and why), errors, broker reconnects, alerts. The "what happened overnight?" morning routine.
allowed-tools: Bash
---

Produce a clean 24-hour operator digest.

1. **Broker state right now**:
   ```bash
   curl -s http://localhost:8080/api/gateway_status | python -c "import sys,json; d=json.load(sys.stdin); print(f'equity=\${d[\"broker_equity_usd\"]:,.2f} healthy={d[\"all_healthy\"]} pause={d[\"pause_entries_present\"]}')"
   ```

2. **Trades executed in last 24h** — from canonical_fills.jsonl:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import json
   from datetime import datetime, timezone, timedelta
   cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
   trades = []
   with open('C:/Argus/repo/argus_flow/logs/canonical_fills.jsonl') as f:
       for line in f:
           try: r = json.loads(line)
           except: continue
           if r.get('source') == 'backfill_from_trade_csv': continue
           try:
               ts = datetime.fromisoformat(str(r.get('exit_ts') or r.get('ts') or '').replace('Z','+00:00'))
           except: continue
           if ts >= cutoff: trades.append(r)
   total_pnl = sum(float(t.get('pnl_usd') or 0) for t in trades)
   wins = [t for t in trades if float(t.get('pnl_usd') or 0) > 0]
   print(f'  {len(trades)} trades, {len(wins)} wins, \${total_pnl:+.2f} total')
   for t in sorted(trades, key=lambda x: x.get('exit_ts') or x.get('ts') or ''):
       print(f'  {(t.get(\"exit_ts\") or \"?\")[:16]}  {t.get(\"strategy\",\"?\"):25s} {t.get(\"symbol\",\"?\"):7s} {t.get(\"direction\",\"?\"):5s} \${float(t.get(\"pnl_usd\") or 0):+8.2f} {t.get(\"exit_reason\",\"?\")}')"
   ```

3. **Signal + block histogram** — last 24h across runner_unified.log:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import re
   from datetime import datetime, timezone, timedelta
   cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
   counts = {'SIGNAL':0,'ENTRY':0,'MTF_BLOCK':0,'AI_SKIP':0,'NOTIONAL_CAP':0,'DRAWDOWN_PAUSE':0,'RECON_DRIFT':0,'ERROR':0}
   with open('C:/Argus/repo/argus_flow/logs/runner_unified.log') as f:
       for line in f:
           m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z', line)
           if not m: continue
           try: ts = datetime.fromisoformat(m.group(1)).replace(tzinfo=timezone.utc)
           except: continue
           if ts < cutoff: continue
           if 'MTF SIGNAL' in line: counts['SIGNAL'] += 1
           if re.search(r'\| ENTRY (LONG|SHORT) ', line): counts['ENTRY'] += 1
           if 'MTF_BLOCK' in line: counts['MTF_BLOCK'] += 1
           if 'AI OVERLAY SKIP' in line or 'AI SKIP' in line: counts['AI_SKIP'] += 1
           if 'NOTIONAL_CAP' in line: counts['NOTIONAL_CAP'] += 1
           if 'DRAWDOWN_PAUSE' in line: counts['DRAWDOWN_PAUSE'] += 1
           if 'RECON_DRIFT' in line: counts['RECON_DRIFT'] += 1
           if '[ERROR]' in line: counts['ERROR'] += 1
   for k,v in counts.items(): print(f'  {k:18s} {v}')"
   ```

4. **Drawdown + risk state**:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import json
   s = json.load(open('C:/Argus/repo/argus_flow/logs/_risk/portfolio_risk_state.json'))
   print(f'  drawdown_pause={s[\"drawdown_pause\"]} peak={s[\"peak_pnl\"]:.2f}R current={s[\"current_pnl\"]:.2f}R')"
   ```

5. **Any errors / quarantines in the last 24h?**
   ```bash
   grep "\[ERROR\]\|QUARANTINED\|KILL_SWITCH" C:/Argus/repo/argus_flow/logs/runner_unified.log | tail -10
   ```

6. **Scheduled task results** (did cohort + loop tasks fire and succeed?):
   ```bash
   for t in ArgusCohortReport ArgusWatchdog ArgusGldPmLoop; do
     echo "  $t:"; schtasks /query /TN "$t" /FO LIST /V 2>&1 | grep -E "Last Run|Last Result" | head -2
   done
   ```

7. **One-line verdict** at the end: "N trades (M wins, $+X.XX), K signals blocked by Y, all runners healthy OR WARN/CRITICAL with the specific issue."

Format the whole output as a single readable digest — assume the user skims it in <30 seconds with coffee.
