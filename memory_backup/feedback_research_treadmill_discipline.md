---
name: Research-treadmill discipline — capture-and-defer when "stop" is declared
description: When the user or claude session declares a stopping point, subsequent analyses (however sharp) go to the NEXT session, not extend the current one. Pattern observed 2026-04-26.
type: feedback
originSessionId: 22066483-fb20-4851-b090-d78ddc84252a
---
## The rule

When a stopping point is declared in a session, the NEXT incoming analysis — even if it's the sharpest, most actionable framing yet — gets captured and deferred. It does not extend the current session.

**Why:** Each analysis individually is high-quality. The pattern of perpetually extending is itself a failure mode. The Argus project's own analyses repeatedly identify "permanent research mode" as the dominant risk. The session needs to honor its own stopping signals or it becomes the thing it warns about.

## How to apply

If user says "stop" / "another day" / "call it":
- Capture key NEW concepts in 1-2 sentences in the response
- Do NOT update memory files unless the user explicitly says "add to memory"
- If subsequent analysis arrives, the response is: *"Captured. We'll incorporate next session."*
- The honest read: at this point in the project, the next valuable input is observed real-data behavior (Monday fills, cohort outcomes, drift events), not more frameworks

## What was observed 2026-04-26

Single conversation hit "stopping point" 4+ times. Each time, a sharper analysis followed. Each analysis was genuinely better than the prior. We extended each time.

By the end, memory had ~15 new artifacts and the operating doctrine had been re-edited 3 times. Most edits were valuable. The PATTERN was the failure mode.

The fix is procedural, not analytical: honor stopping signals even when the next thing offered is genuinely better.

## What this memory does NOT mean

- It does not mean refuse all updates after "stop" — small clarifications are fine
- It does not mean dismiss good analysis — capture briefly, defer fully
- It does not mean lecture the user about it more than once

If the pattern recurs in a future session, point at this memory and stop the loop with one sentence, not a paragraph.
