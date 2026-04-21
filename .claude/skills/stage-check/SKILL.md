---
name: stage-check
description: Show promotion gate status for a pair (or all pairs). Reads the 13-check promotion gate + disposition without a manual walk. Prevents mistaken promotions.
allowed-tools: Bash Read
argument-hint: '[symbol-or-strategy-name]'
---

Check promotion readiness for `$ARGUMENTS` (or all strategies if empty).

1. **Canonical promotion-readiness data**:
   ```bash
   curl -s "http://localhost:8080/api/promotion_readiness" 2>&1 | python -c "
   import sys, json
   target = '$ARGUMENTS'.strip().lower()
   d = json.load(sys.stdin)
   for s in d.get('strategies', []):
       name = s.get('strategy','?').lower()
       if target and target not in name: continue
       st = s.get('stats', {})
       print(f'  {s[\"strategy\"]:25s}')
       print(f'    trades={st.get(\"trades\",0):3d}  pf={st.get(\"profit_factor\")}  wr={st.get(\"win_rate\")}  pnl=\${st.get(\"pnl_usd\")}')
       print(f'    review={s.get(\"review_gate_passed\")}  canonical={s.get(\"canonical_gate_passed\")}  next={s.get(\"next_action\")}')
       blockers = s.get('review_gate_blockers') or s.get('canonical_gate_blockers') or []
       if blockers: print(f'    blockers: {blockers[:3]}')
       dispo = s.get('blocking_disposition')
       if dispo: print(f'    DISPOSITION: {dispo}')
   "
   ```

2. **Per-strategy confidence artifact disposition**:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import json, glob
   target = '$ARGUMENTS'.strip().lower()
   for p in sorted(glob.glob('C:/Argus/repo/strategy_confidence/*.json')):
       try: a = json.load(open(p))
       except: continue
       name = a.get('strategy','').lower()
       if target and target not in name and target not in p.lower(): continue
       d = a.get('disposition')
       if d: print(f'  {a.get(\"strategy\",p):25s} status={d.get(\"status\")}  reason={(d.get(\"reason\",\"\") or \"\")[:80]}')"
   ```

3. **Config stage per pair**:
   ```bash
   C:/Argus/.venv/Scripts/python.exe -c "
   import json, glob
   target = '$ARGUMENTS'.strip().lower()
   for p in sorted(glob.glob('C:/Argus/repo/argus_flow/configs/*.json')):
       if 'discovery' in p or 'hashes' in p or 'fleet_sizing' in p: continue
       try: c = json.load(open(p))
       except: continue
       sym = (c.get('symbol') or '').lower()
       if target and target not in sym and target not in p.lower(): continue
       stage = c.get('stage') or (c.get('deployment') or {}).get('stage') or '?'
       print(f'  {c.get(\"symbol\",p):10s} stage={stage:8s} strategy={c.get(\"strategy\",\"?\")}')"
   ```

4. **Verdict per strategy/pair**:
   - **ELIGIBLE for promotion** (review_gate_passed=True, no kill disposition)
   - **OBSERVE_MORE** (review gate partially open, accumulating)
   - **BLOCKED by disposition** (kill/shelve/research_only — do NOT promote until lifted)
   - **NO_LIVE_EVIDENCE** (0 live trades)

5. One-line action per strategy: "promote to paper", "wait N more valid trades", "kill candidate", or "do not promote, disposition=X".

Context: memory note `project_kill_or_rework_20260420.md` — dispositions are the persistent do-not-promote signal. Always check `disposition.status in {kill, shelve, scope_down, research_only}` before suggesting advancement.
