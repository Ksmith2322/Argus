---
name: keep dashboard panels lean — one place per data point
description: User prefers a single authoritative location for each metric on the dashboard. Actively removed duplicated panels during 2026-04-23 session.
type: feedback
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
Keep dashboard panels lean. Every metric should appear in exactly one authoritative place — no standalone banner for a number that's already visible in a table header, no separate panel for a distribution that lives inline in another panel.

**Why:** On 2026-04-23 the user removed (in one sitting) the Exit Reason Distribution panel, Promotion Ladder panel, Risk Exposure banner, SYSTEM HEALTH + OPS VISIBILITY panel, CONFIG / SOURCE OF TRUTH strip, and the entire PROD PAIRS section — all because the data was duplicated elsewhere on the page. Their note: "looks like there was a lot of duplicated info so we are more to the point on what is needed."

**How to apply:**
- Before adding a new dashboard panel, check if its data is already surfaced in an existing table column or banner. If yes, enhance the existing location instead of building a parallel panel.
- When the user asks for a new metric, default to "add a column to an existing table" before "add a new panel."
- If a proposed panel would share >70% of its data with something already on the page, flag the overlap and suggest the consolidated option.
- Paper-only focus — don't rebuild PROD/REAL MONEY sections. They were removed on purpose.
