---
name: 5/1 Ceremony prep tools (built 4/28)
description: Three Python scripts that pre-aggregate ceremony inputs and run the operational vetting checklist. Run on 5/1 morning before the verdict pass.
type: reference
originSessionId: 3848919f-b95e-42f4-8fc4-1ed3ac86dd92
---
Three scripts in `ops/` automate the data-independent prep for the 5/1 strategy review ceremony. Built 2026-04-28 to save 60-90 minutes on ceremony day.

## On 5/1 morning, run in order

```bash
cd c:/Argus/repo
C:/Argus/.venv/Scripts/python.exe -m ops.run_5_1_prep            # 1. Snapshot ceremony-day data
C:/Argus/.venv/Scripts/python.exe -m ops.operational_vetting     # 2. Auto-check silent strategies
C:/Argus/.venv/Scripts/python.exe -m ops.generate_verdict_skeleton  # 3. Pre-fill verdict template
```

Outputs land at `argus_flow/logs/ceremony_prep/`:
- `snapshot_<YYYYMMDD_HHMM>.json` + `snapshot_latest.json` — all 5 ceremony data sources in one place. Diff against the 4/28 baseline (`snapshot_20260428_2351.json`) to see what moved during the prep window.
- `operational_vetting_<YYYYMMDD>.json` — per-strategy 15-item checklist results for the WAITING strategies. Auto-completes 9 of 15 items; the remaining 6 are flagged MANUAL for ceremony sign-off.
- `verdict_20260501_skeleton.json` — pre-filled with structural facts (n, PF, operational verdict, eligibility flags); verdict/reasoning/action fields empty for ceremony to fill.

## Important design rule

**These scripts pre-fill facts, not verdicts.** The ceremony file warns against rehearsing decisions pre-ceremony. The verdict skeleton intentionally leaves the `verdict`, `reasoning`, `action`, `next_review_date` fields null — only quantitative `criteria_eligibility` flags are populated (raw fact, not opinion).

## What the 4/28 baseline shows

- 14 of 23 strategies are silent (verdict=WAITING, 0 live trades)
- 5 BLOCKED, 7 OPERATIONAL_VERIFIED_AUTO, 2 INSUFFICIENT_AUTO_CHECKS from the vetting pass
- Only `forge_vix_intraday` meets WINNER-CANDIDATE quant thresholds (n=22, PF=1.34)
- `forge_multi_orb` (n=79, PF=0.81) and `forge_spy_mean_rev` (n=32, PF=0.61) meet the quantitative kill threshold (n≥30 + PF<1.0)

These numbers will change between 4/28 and 5/1. Re-run all three scripts on 5/1 morning before starting the verdict pass.

## Source

- Ceremony spec: [project_5_1_review_ceremony_20260501.md](project_5_1_review_ceremony_20260501.md)
- Scripts committed in `ddd37a1` (2026-04-28)
