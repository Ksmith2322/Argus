---
name: kill-list
description: List all strategies with kill/shelve/scope_down/research_only dispositions. Honest inventory of what's NOT promotable and why.
allowed-tools: Bash
---

Scan every strategy_confidence artifact for a disposition block and report.

```bash
C:/Argus/.venv/Scripts/python.exe -c "
import json, glob
from collections import defaultdict
groups = defaultdict(list)
for p in sorted(glob.glob('C:/Argus/repo/strategy_confidence/*.json')):
    try: a = json.load(open(p))
    except: continue
    d = a.get('disposition')
    if not d: continue
    status = d.get('status','?')
    reason = (d.get('reason','') or '').replace(chr(10), ' ')[:140]
    decided = d.get('decided_at','?')[:10]
    nxt = d.get('next_review_date','')
    groups[status].append({
        'strategy': a.get('strategy', p.split('/')[-1]),
        'file': p.split('/')[-1],
        'reason': reason,
        'decided_at': decided,
        'next_review': nxt,
    })

order = ['kill','shelve','research_only','scope_down','paper_only']
for status in order + [s for s in groups if s not in order]:
    items = groups.get(status, [])
    if not items: continue
    print(f'=== {status.upper()} ({len(items)}) ===')
    for it in items:
        print(f'  {it[\"strategy\"]:25s} decided {it[\"decided_at\"]}')
        print(f'    {it[\"reason\"]}')
        if it['next_review']:
            print(f'    next review: {it[\"next_review\"][:10]}')
    print()

total = sum(len(v) for v in groups.values())
if total == 0:
    print('No dispositions set — all artifacts are disposition-free.')
else:
    print(f'Total: {total} strategies carry formal dispositions.')
"
```

Interpret:
- **kill** — permanent kill. Do NOT revive without re-speccing.
- **shelve** — temporarily parked. May return after a specific blocker resolves.
- **research_only** — code runs but data isn't promotable. Has a `next_review_date`.
- **scope_down** — the union is weak; the validated subset is what should trade. Check `*_validated.json` for the promotable sibling.
- **paper_only** — ready to run in paper, not ready for real capital (when that's relevant).

Anything carrying a disposition should NOT be suggested for promotion unless the disposition has been explicitly lifted (which requires a memory note + artifact rewrite).

Pair with `/stage-check` to see full promotion picture.
