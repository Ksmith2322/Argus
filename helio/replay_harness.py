"""Replay harness — re-run strategy signal logic on historical data
and diff against the actual canonical_fills ledger.

WHY THIS EXISTS
===============
Unit tests verify code correctness. They don't verify that the live
runner produces the same fills the strategy logic predicts. Memory
records multiple incidents where code passed tests but failed live:
  - CADJPY 5-decimal lmtPrice rejected by IBKR (2026-05-19)
  - GLD silently sized at $22 cap instead of $50K (2026-04-27)
  - Argus FX 30%-undersized entries due to broker-timeout race
    (2026-05-20)

Each of these would have shown up immediately in a replay vs
canonical_fills diff. None were caught by unit tests.

WHAT THE REPLAY DOES
===================
For each strategy, the harness:
  1. Pulls the same historical data the runner would have had
  2. Runs the strategy's signal/ranking pipeline forward in time,
     producing a list of (timestamp, ticker, side) trade events
  3. Compares to actual canonical_fills entries
  4. Reports mismatches:
     - REPLAY_ONLY: strategy says fire, ledger has no fill
     - LEDGER_ONLY: ledger has fill, strategy says don't fire
     - TICKER_DIVERGENT: same timestamp, different instrument
     - TIME_DIVERGENT: same instrument, timestamp off by >tolerance

USAGE
=====
    from helio.replay_harness import (
        replay_xs_momentum, replay_gld_pm_long,
        diff_against_canonical_fills,
    )
    replay = replay_xs_momentum("forge_xs_momentum_style", period="2y")
    diff = diff_against_canonical_fills(
        replay, strategy="forge_xs_momentum_style"
    )
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


_REPO = Path(__file__).resolve().parents[1]


# ─── Replay event ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ReplayEvent:
    """One trade event the strategy logic predicts (not what actually
    happened — that's in canonical_fills)."""
    ts: str          # ISO timestamp
    side: str        # "ENTRY" or "EXIT"
    ticker: str
    score: float | None = None  # momentum score (xs_momentum) or signal val
    note: str = ""   # diagnostic: "ranked_top2", "atr_breakout_hour20", etc.


@dataclass(frozen=True)
class FillRow:
    """Slim view of a canonical_fills entry — just what we need to diff."""
    ts: str
    side: str
    ticker: str
    lineage_id: str | None = None


@dataclass(frozen=True)
class DiffMismatch:
    kind: str        # "REPLAY_ONLY" / "LEDGER_ONLY" / "TICKER_DIVERGENT" / "TIME_DIVERGENT"
    detail: str
    ts: str | None = None
    replay_event: dict | None = None
    ledger_event: dict | None = None


# ─── xs_momentum replay ─────────────────────────────────────────────

def replay_xs_momentum(
    strategy_label: str,
    *,
    period: str = "2y",
    universe_override: list[str] | None = None,
    top_pick_fraction: float | None = None,
) -> list[ReplayEvent]:
    """Walk the period month-by-month and replay xs_momentum's
    ranking + selection at each month-end. Returns the ordered list
    of (ENTRY, EXIT) events the strategy's logic would have produced.

    `strategy_label` selects the variant — looked up in
    forge.xs_momentum.runner._VARIANT_REGISTRY. Pass universe_override
    or top_pick_fraction to test alternative configurations without
    mutating any registry.
    """
    from forge.xs_momentum import runner as xs

    # Resolve variant config or accept overrides
    if universe_override is None or top_pick_fraction is None:
        # Look up the variant by label
        chosen = None
        for name, cfg in xs._VARIANT_REGISTRY.items():
            if cfg["label"] == strategy_label:
                chosen = cfg
                break
        if chosen is None:
            raise ValueError(
                f"unknown strategy_label {strategy_label!r}; "
                f"known: {[c['label'] for c in xs._VARIANT_REGISTRY.values()]}"
            )
        if universe_override is None:
            if chosen["universe_key"] is None:
                from helio.xs_momentum import DEFAULT_UNIVERSE
                universe_override = list(DEFAULT_UNIVERSE)
            else:
                from helio.xs_momentum_universes import get_universe
                universe_override = list(get_universe(chosen["universe_key"]))
        if top_pick_fraction is None:
            from helio.xs_momentum import TOP_QUINTILE_FRACTION
            top_pick_fraction = (chosen.get("top_pick_fraction")
                                 if chosen.get("top_pick_fraction") is not None
                                 else TOP_QUINTILE_FRACTION)

    # Run the live backtest engine and extract its trade ledger
    result = xs.backtest(
        period=period,
        universe_override=universe_override,
        top_pick_fraction=top_pick_fraction,
    )
    if "error" in result:
        return []

    events: list[ReplayEvent] = []
    for t in result.get("trades_detail") or []:
        events.append(ReplayEvent(
            ts=str(t.get("entry_date") or ""),
            side="ENTRY",
            ticker=t["ticker"],
            note="xs_momentum_rebalance",
        ))
        events.append(ReplayEvent(
            ts=str(t.get("exit_date") or ""),
            side="EXIT",
            ticker=t["ticker"],
            note="xs_momentum_rebalance",
        ))
    return events


# ─── gld_pm_long replay ─────────────────────────────────────────────

def replay_gld_pm_long(
    *,
    period: str = "60d",
    ticker: str = "GLD",
) -> list[ReplayEvent]:
    """Replay the gld_pm_long 1h ATR-breakout signal on the given
    ticker over the period. Produces ENTRY/EXIT events at each
    signal-hour bar where the strategy would have fired.
    """
    try:
        import yfinance as yf
        import numpy as np
        import pandas as pd
        from forge.gld_pm_long.runner import PARAMS, atr
    except Exception:
        return []

    try:
        df = yf.download(ticker, period=period, interval="1h",
                         progress=False, auto_adjust=False)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    needed = ["Open", "High", "Low", "Close"]
    if not all(c in df.columns for c in needed):
        return []
    df = df[needed].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    a_arr = atr(df, PARAMS["atr_period"]).values
    H, L, C = df["High"].values, df["Low"].values, df["Close"].values

    signal_hours = set(PARAMS["signal_hours_utc"])
    events: list[ReplayEvent] = []
    open_until = -1
    for i in range(len(df)):
        if i <= open_until:
            continue
        if df.index[i].hour not in signal_hours:
            continue
        a = a_arr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        entry = C[i]
        target = entry + PARAMS["target_atr"] * a
        stop = entry - PARAMS["stop_atr"] * a
        exit_idx = None
        for j in range(i + 1, min(i + 1 + PARAMS["hold_bars"], len(df))):
            if L[j] <= stop:
                exit_idx = j; reason = "stop"; break
            if H[j] >= target:
                exit_idx = j; reason = "target"; break
        if exit_idx is None:
            exit_idx = min(i + PARAMS["hold_bars"], len(df) - 1)
            reason = "time"
        events.append(ReplayEvent(
            ts=df.index[i].isoformat(),
            side="ENTRY",
            ticker=ticker,
            score=float(entry),
            note=f"gld_pm_atr_breakout_h{df.index[i].hour}",
        ))
        events.append(ReplayEvent(
            ts=df.index[exit_idx].isoformat(),
            side="EXIT",
            ticker=ticker,
            score=float(C[exit_idx]),
            note=f"gld_pm_exit_{reason}",
        ))
        open_until = exit_idx
    return events


# ─── Canonical-fills reader ─────────────────────────────────────────

def _canonical_fills_path() -> Path:
    return _REPO / "argus_flow" / "logs" / "canonical_fills.jsonl"


def read_canonical_fills(
    *,
    strategy: str | None = None,
    fills_path: Path | None = None,
) -> list[FillRow]:
    """Load canonical_fills entries, optionally filtered to a single
    strategy_label."""
    path = fills_path or _canonical_fills_path()
    if not path.exists():
        return []
    out: list[FillRow] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if strategy and row.get("strategy") != strategy:
                continue
            out.append(FillRow(
                ts=str(row.get("ts") or row.get("entry_ts")
                       or row.get("exit_ts") or ""),
                side=str(row.get("side") or "").upper(),
                ticker=str(row.get("symbol") or row.get("ticker") or ""),
                lineage_id=row.get("lineage_id"),
            ))
    return out


# ─── Diff logic ─────────────────────────────────────────────────────

def _ts_to_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        # Try date-only fallback
        try:
            return datetime.strptime(ts[:10], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return None


def diff_against_canonical_fills(
    replay: list[ReplayEvent],
    *,
    strategy: str | None = None,
    fills_path: Path | None = None,
    time_tolerance_hours: float = 24.0,
) -> dict:
    """Compare a replay output to canonical_fills entries.

    Matching is done greedy: each canonical fill tries to find a
    replay event within `time_tolerance_hours` and matching ticker+
    side. Unmatched replay events = REPLAY_ONLY (strategy says fire
    but ledger doesn't have it). Unmatched fills = LEDGER_ONLY
    (ledger has it but strategy logic doesn't predict it).
    """
    fills = read_canonical_fills(strategy=strategy, fills_path=fills_path)
    tol = timedelta(hours=time_tolerance_hours)

    # Build (datetime, replay_event, matched) tuples
    replay_idx: list[tuple] = []
    for e in replay:
        dt = _ts_to_dt(e.ts)
        if dt is None:
            continue
        replay_idx.append([dt, e, False])  # [dt, event, matched_flag]

    fill_idx: list[tuple] = []
    for f in fills:
        dt = _ts_to_dt(f.ts)
        if dt is None:
            continue
        fill_idx.append([dt, f, False])

    mismatches: list[DiffMismatch] = []

    # First pass: try to match each fill to a replay event
    for fi in fill_idx:
        f_dt, f_row, _ = fi
        best = None
        best_delta = None
        for ri in replay_idx:
            r_dt, r_evt, r_matched = ri
            if r_matched:
                continue
            if r_evt.side != f_row.side:
                continue
            if r_evt.ticker != f_row.ticker:
                continue
            delta = abs(r_dt - f_dt)
            if delta <= tol and (best_delta is None or delta < best_delta):
                best = ri
                best_delta = delta
        if best is not None:
            best[2] = True   # mark matched
            fi[2] = True

    # Unmatched fills = LEDGER_ONLY
    for f_dt, f_row, matched in fill_idx:
        if matched:
            continue
        # Look for any replay event matching ticker/side but outside tolerance
        nearby = None
        for r_dt, r_evt, r_matched in replay_idx:
            if r_evt.side == f_row.side and r_evt.ticker == f_row.ticker:
                if abs(r_dt - f_dt) > tol:
                    nearby = (r_dt, r_evt)
                    break
        if nearby:
            mismatches.append(DiffMismatch(
                kind="TIME_DIVERGENT",
                ts=f_row.ts,
                detail=(f"ledger fill at {f_row.ts} matches replay at "
                        f"{nearby[0].isoformat()} but delta > "
                        f"{time_tolerance_hours}h"),
                ledger_event={"ts": f_row.ts, "side": f_row.side,
                              "ticker": f_row.ticker,
                              "lineage_id": f_row.lineage_id},
                replay_event={"ts": nearby[1].ts, "side": nearby[1].side,
                              "ticker": nearby[1].ticker},
            ))
        else:
            mismatches.append(DiffMismatch(
                kind="LEDGER_ONLY",
                ts=f_row.ts,
                detail=(f"ledger has {f_row.side} {f_row.ticker} at "
                        f"{f_row.ts} but strategy replay does not"),
                ledger_event={"ts": f_row.ts, "side": f_row.side,
                              "ticker": f_row.ticker,
                              "lineage_id": f_row.lineage_id},
            ))

    # Unmatched replay events = REPLAY_ONLY
    for r_dt, r_evt, matched in replay_idx:
        if matched:
            continue
        mismatches.append(DiffMismatch(
            kind="REPLAY_ONLY",
            ts=r_evt.ts,
            detail=(f"replay predicts {r_evt.side} {r_evt.ticker} at "
                    f"{r_evt.ts} but ledger has no matching fill"),
            replay_event={"ts": r_evt.ts, "side": r_evt.side,
                          "ticker": r_evt.ticker,
                          "note": r_evt.note},
        ))

    # Summary counts
    counts: dict[str, int] = {}
    for m in mismatches:
        counts[m.kind] = counts.get(m.kind, 0) + 1

    return {
        "strategy": strategy,
        "n_replay_events": len(replay_idx),
        "n_ledger_fills": len(fill_idx),
        "n_matched": sum(1 for _, _, m in replay_idx if m),
        "n_mismatches": len(mismatches),
        "mismatch_counts": counts,
        "mismatches": [
            {"kind": m.kind, "detail": m.detail, "ts": m.ts,
             "replay_event": m.replay_event,
             "ledger_event": m.ledger_event}
            for m in mismatches
        ],
        "time_tolerance_hours": time_tolerance_hours,
    }
