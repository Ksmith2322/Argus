---
name: future dashboard feature ideas (defer until current monitoring proves out)
description: Nice-to-have dashboard enhancements user mentioned in passing. Not urgent. Build when fleet has real-fill data worth visualizing better.
type: project
originSessionId: 81afc97b-10bf-49fd-ba79-e5a89a7f400b
---
## Idea #1 — Fleet vs S&P 500 % comparison chart (2026-04-25)

User: *"have a percent tracker line chart and have S&P500 bar on it as well and see how they match up side by side"*

**What it'd show:** Two lines on one chart over the same time window:
- Fleet equity curve as % return from start (already have this — `/api/fleet_equity_curve` returns `cumulative_pnl_pct` per point)
- SPY benchmark as % return from same start

The visual answer to "are we beating SPY?" — which is the underlying question for the whole fleet.

**Implementation sketch (when we build it):**
- New endpoint `/api/spy_benchmark` that pulls SPY 1d bars (or whatever resolution matches) for the same window the fleet equity curve uses
- yfinance is fine; or use the existing IBKR connection (gdx_gld already pulls SPY-adjacent data)
- Compute SPY % return: `(spy_close[t] / spy_close[t0] - 1) × 100`
- In the existing `loadFleetEquityCurve` JS, fetch both series in parallel + render as overlaid lines (cyan = fleet, orange = SPY) with shared y-axis (%)
- Could also show numeric "Fleet vs SPY: +X.X% / -Y.Y%" delta in the panel header

**Effort estimate:** ~2 hours including endpoint + JS + testing.

**Why deferred:**
- We literally just connected real execution Friday. The current "fleet equity curve" is mostly simulated data. Comparing simulated math to real SPY isn't useful.
- Per `feedback_master_before_build.md`: master what we have first.
- Best timing: 2-4 weeks of real fills accumulated, then build this for the 5/1 (or later) review.

**Tag:** "more for fun" per user's framing — this is *visualization/dashboarding*, not load-bearing.

## Idea #2 — Hover tooltip on Fleet Equity Curve (2026-04-29)

User: *"would there be a way we can see price on the chart when you hover over the line with the mouse?"*

**What it'd show:** Standard Chart.js hover tooltip — when cursor is anywhere on the equity curve, a small popup shows the date + cumulative PnL ($ + %) at that point.

**Implementation sketch (when we build it):**
- The chart is already Chart.js (chart.js@4 + chartjs-adapter-date-fns@3 loaded in DASHBOARD_HTML head). Tooltips are a built-in plugin — likely just disabled or set to point-only intersect.
- In the chart options where Fleet Equity Curve is rendered (`loadFleetEquityCurve` JS function), add:
  ```js
  options: {
    interaction: { mode: 'index', intersect: false },  // line-hover, not point-hover
    plugins: {
      tooltip: {
        enabled: true,
        mode: 'index',
        intersect: false,
        callbacks: {
          label: (ctx) => '$' + ctx.parsed.y.toFixed(2) + ' (' + ((ctx.parsed.y / ctx.chart.data.anchor) * 100).toFixed(2) + '% of anchor)'
        }
      }
    }
  }
  ```
- Likely adjust `loadFleetEquityCurve` to pass `anchor` into the chart's data object so the callback can compute %.

**Effort:** ~10-15 min once we sit down. Single-function change in dashboard.py.

**Why deferred:** trivial UX polish, not load-bearing. Bundle with other small chart improvements next time we touch that area.

## How to apply this memory

If user says any of:
- "let's add the SPY comparison"
- "I want to see how the fleet stacks up against SPY/the market"
- "build that dashboard idea I mentioned"
- (or anything along those lines)

Pull this memory, confirm the spec hasn't drifted, then build. ~2hr task.

If new dashboard ideas come up, append to this file rather than scattering across multiple memos.
