---
name: Paper account must mirror real-money behavior
description: Sizing/leverage/exposure on the paper account must reflect what real money would actually do. No "paper has more rope" thinking.
type: feedback
originSessionId: 0256622d-fcf2-4b57-9505-2a4e805eef61
---
**Rule:** The paper account is a real-money rehearsal. Any sizing, leverage, exposure, or risk policy that wouldn't be acceptable on real money is also unacceptable on paper. No "we can be looser on paper because it's just paper" reasoning.

**Why:** User stated explicitly on 2026-04-27 (post PDT cascade + margin cushion warnings): *"I just dont want to over leverage right now as we are getting close to real money. The systems be as accurate as possible to real money behavior."* The whole point of paper is to validate that the live system would survive — if paper hides risks via over-leverage that real money won't allow, the validation is meaningless. Better to undertrade in paper and learn the strategy works at conservative size than to over-trade in paper and blow up in real.

**How to apply:**
- Asset-class caps in `argus_flow/configs/fleet_sizing.json` are expressed as % of account equity (e.g., 0.3× stock = 30% of account max), not as leverage multiples that mask absolute exposure.
- When proposing changes to `notional_caps_by_asset_class`, `risk_pct`, `fleet_max_open_risk_pct`, or any cluster/concentration cap: ask "would this be acceptable on $X of real money?" before "is this what the strategy needs?"
- Reject "but the pip-based risk math wants $400K notional on $30K equity" reasoning — if the math wants more leverage than real money allows, the strategy needs to undertrade, not the cap to loosen.
- Never propose caps that reflect "broker's max margin allowance" — propose what's prudent for the account size.
- This applies through the 5/31 freeze and beyond. Loosen ONLY after 90 days of clean live data prove the edge survives.
