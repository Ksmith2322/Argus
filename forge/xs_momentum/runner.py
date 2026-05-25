"""forge.xs_momentum — runner + backtest for cross-sectional 12-1 momentum.

The runner is monthly-cadence: it evaluates positions on the first
trading day of each month, exits old picks not in this month's top
quintile, enters new picks.

Modes:
  --backtest    Simulate over historical data, report stats.
  --check       Print this-month's ranking + recommended picks.
  --evaluate    One cycle: rebalance to current top quintile.
  --loop        Daemon mode (sleeps until next month-start).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

from forge.logging_setup import setup_logging
from helio.fleet_sizing import (
    get_allocation_factor,
    get_sizing_anchor_usd,
    max_notional_usd,
)
from helio import ibkr_execution as ibkr
from helio.blocker_ledger import record_blocker
from helio.xs_momentum import (
    DEFAULT_UNIVERSE,
    LOOKBACK_LONG_DAYS, LOOKBACK_SHORT_DAYS,
    TOP_QUINTILE_FRACTION,
    rank_universe_by_momentum, select_top_quintile,
)

STRATEGY_LABEL = "forge_xs_momentum"
IBKR_CLIENT_ID = 121
_SIGNAL_ONLY_MODE = False

REPO = Path(__file__).resolve().parents[2]
LOG_DIR = REPO / "forge" / "logs" / "xs_momentum"
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = LOG_DIR / "state.json"
HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
TRADES_PATH = LOG_DIR / "trades.csv"

log = setup_logging("xs_momentum")

PARAMS = {
    "version": "v1_20260519",
    "universe": list(DEFAULT_UNIVERSE),
    "long_lookback": LOOKBACK_LONG_DAYS,
    "short_lookback": LOOKBACK_SHORT_DAYS,
    "top_quintile_fraction": TOP_QUINTILE_FRACTION,
    # Position sizing — each pick gets equal weight from the strategy's
    # allocated capital. 3 picks × 33% each = 100% deployed.
    "per_pick_fraction": 1.0 / 3.0,
    "eval_hour_utc": 14,
    "eval_minute_utc": 30,
}


# ─── Variant configuration (Codex universe-sweep 2026-05-24) ─────────
# Multiple universes survived the 20y disciplined gate. Each gets its
# own paper-trading instance via --variant <name>. The base broad-8
# strategy keeps client_id 121; variants take 122+.

_VARIANT_REGISTRY: dict[str, dict] = {
    "baseline": {
        "label": "forge_xs_momentum",
        "log_dir_name": "xs_momentum",
        "client_id": 121,
        "universe_key": None,  # use DEFAULT_UNIVERSE
        "top_pick_fraction": None,  # use PARAMS default 0.2
        "regime_gate": None,        # no overlay
    },
    "sectors": {
        "label": "forge_xs_momentum_sectors",
        "log_dir_name": "xs_momentum_sectors",
        "client_id": 122,
        "universe_key": "sectors_spdr_11",
        "top_pick_fraction": None,
        "regime_gate": None,
    },
    "style": {
        "label": "forge_xs_momentum_style",
        "log_dir_name": "xs_momentum_style",
        "client_id": 123,
        "universe_key": "style_factors_8",
        "top_pick_fraction": None,
        "regime_gate": None,
    },
    "legacy15": {
        "label": "forge_xs_momentum_legacy15",
        "log_dir_name": "xs_momentum_legacy15",
        "client_id": 124,
        "universe_key": "legacy_sectors_countries_15",
        "top_pick_fraction": None,
        "regime_gate": None,
    },
    # 2026-05-25 amplification: top-3 sweep at 20y showed style_factors_8
    # at top_fraction=0.35 jumps PF 3.78 -> 5.40 with same DD (34%).
    # Ship as a parallel variant so the live evidence decides: does
    # top-3 actually outperform top-2 on the same universe?
    "style_top3": {
        "label": "forge_xs_momentum_style_top3",
        "log_dir_name": "xs_momentum_style_top3",
        "client_id": 125,
        "universe_key": "style_factors_8",
        "top_pick_fraction": 0.35,   # ~3 picks of 8
        "regime_gate": None,
    },
    # 2026-05-25 regime overlay: SPY>200dma gate cuts max DD by 50%+
    # on legacy/sectors/style universes while raising Sharpe on every
    # universe tested. Shipped as a 6th variant on the legacy15
    # universe (worst DD of the bunch at 65% baseline -> 29% gated).
    # Same engine, same universe, same picks — only adds the regime
    # check before each monthly rebalance.
    "legacy15_regime": {
        "label": "forge_xs_momentum_legacy15_regime",
        "log_dir_name": "xs_momentum_legacy15_regime",
        "client_id": 126,
        "universe_key": "legacy_sectors_countries_15",
        "top_pick_fraction": None,
        "regime_gate": "spy_above_200dma",
    },
    # 2026-05-25 universe-broadening sweep: 5 standalone dormant
    # universes (commodities/countries/bonds/real_assets/international)
    # all FAILED their own walk-forward OOS test. But the dispersion
    # hypothesis — that cross-sectional momentum benefits from a wider
    # pool, not 5 narrow ones — was VALIDATED on combined universes.
    # wide_global_47 (broad-8 ∪ sectors ∪ styles ∪ countries ∪
    # international, de-duped to 43 unique ETFs) passes the disciplined
    # gate with STRENGTHENED_OOS: IS CI 1.21 → OOS CI 2.33, Sharpe
    # 0.77 → 1.58 (improves OOS by +0.81). Shipped at 0.25x because
    # the universe overlaps heavily with sectors+style+legacy15 variants
    # already running — live evidence decides whether the broader pool
    # actually outperforms the narrower ones.
    "global47": {
        "label": "forge_xs_momentum_global47",
        "log_dir_name": "xs_momentum_global47",
        "client_id": 128,
        "universe_key": "wide_global_47",
        "top_pick_fraction": None,
        "regime_gate": None,
    },
}


def configure_variant(name: str) -> None:
    """Reconfigure module-level globals for the given variant.

    Must be called BEFORE any other module function (state read,
    backtest, evaluate, loop). Idempotent for the same name.
    """
    global STRATEGY_LABEL, IBKR_CLIENT_ID
    global LOG_DIR, STATE_PATH, HEARTBEAT_PATH, TRADES_PATH

    if name not in _VARIANT_REGISTRY:
        raise ValueError(
            f"unknown xs_momentum variant {name!r}; "
            f"known: {list(_VARIANT_REGISTRY)}"
        )
    cfg = _VARIANT_REGISTRY[name]
    STRATEGY_LABEL = cfg["label"]
    IBKR_CLIENT_ID = cfg["client_id"]
    LOG_DIR = REPO / "forge" / "logs" / cfg["log_dir_name"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH = LOG_DIR / "state.json"
    HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"
    TRADES_PATH = LOG_DIR / "trades.csv"

    # Override universe for non-default variants; for baseline, explicitly
    # reset to DEFAULT_UNIVERSE so re-configuring across variants doesn't
    # leave stale state.
    universe_key = cfg["universe_key"]
    if universe_key is not None:
        from helio.xs_momentum_universes import get_universe
        PARAMS["universe"] = list(get_universe(universe_key))
    else:
        PARAMS["universe"] = list(DEFAULT_UNIVERSE)

    # Override top-quintile fraction for variants that test alternative
    # concentration (e.g. top-3 picks of 8). None = use baseline 0.2 from
    # the helio.xs_momentum.TOP_QUINTILE_FRACTION constant.
    override = cfg.get("top_pick_fraction")
    PARAMS["top_quintile_fraction"] = (override if override is not None
                                       else TOP_QUINTILE_FRACTION)

    # Optional regime-gate overlay (2026-05-25). Variants can opt in by
    # naming a key from helio.regime.REGIME_GATES. When set, the runner's
    # evaluate_once() calls the gate before rebalancing — if FALSE, it
    # exits existing positions and skips new entries. None = no overlay.
    PARAMS["regime_gate"] = cfg.get("regime_gate")


def list_variants() -> list[str]:
    """Return the names of all configured variants."""
    return list(_VARIANT_REGISTRY)

MAX_DAILY_DATA_AGE_DAYS = 5


# ── Data ──────────────────────────────────────────────────────────────────

def _period_start(period: str) -> pd.Timestamp | None:
    """Best-effort cutoff for yfinance-style periods used by this runner."""
    if not period:
        return None
    raw = str(period).strip().lower()
    now = pd.Timestamp.now(tz="UTC").normalize()
    try:
        n = int(raw[:-1])
    except (TypeError, ValueError):
        return None
    suffix = raw[-1:]
    if suffix == "y":
        return now - pd.DateOffset(years=n)
    if suffix == "d":
        return now - pd.Timedelta(days=n)
    return None


def _close_series_from_yf(out: pd.DataFrame, ticker: str) -> pd.Series | None:
    """Extract Close for one ticker across yfinance's MultiIndex shapes."""
    if out is None or out.empty:
        return None
    try:
        if isinstance(out.columns, pd.MultiIndex):
            for key in ((ticker, "Close"), ("Close", ticker)):
                if key in out.columns:
                    return out[key].dropna()
            level0 = out.columns.get_level_values(0)
            level1 = out.columns.get_level_values(1)
            if ticker in set(level0) and "Close" in set(level1):
                return out.xs(ticker, axis=1, level=0)["Close"].dropna()
            if "Close" in set(level0) and ticker in set(level1):
                return out.xs("Close", axis=1, level=0)[ticker].dropna()
        elif "Close" in out.columns:
            return out["Close"].dropna()
    except Exception:
        return None
    return None


def _cached_daily_close(ticker: str, period: str) -> pd.Series | None:
    """Read the repo's CSV cache produced by helio.yfinance_data."""
    safe = ticker.upper().replace("/", "_").replace("=", "_").replace("^", "_")
    path = REPO / "helio" / "data_yfinance" / f"{safe}_daily.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
        if "Close" not in df.columns:
            return None
        idx = pd.to_datetime(df.index, utc=True)
        series = pd.Series(df["Close"].astype(float).to_numpy(), index=idx, name=ticker).dropna()
        start = _period_start(period)
        if start is not None:
            series = series[series.index >= start]
        return series if not series.empty else None
    except Exception as exc:
        log.warning("cached daily close read failed for %s: %s", ticker, exc)
        return None


def _fetch_history(tickers: list[str], period: str = "10y") -> pd.DataFrame:
    """Download daily closes for the universe, with cache fallback.

    auto_adjust=False because the published promotion_gate baseline
    (CI=[1.86, 6.30] from project_2026_05_22_backtest_factory_findings.md)
    was computed at this setting. Switching to True would silently shift
    live PF vs baseline. See the convention block in helio/yfinance_data.py.

    Production rule: do not quietly return an empty ranking when yfinance is
    down or changes column shape. Try the repo CSV cache, then fail loudly.
    """
    cols = {}
    errors: dict[str, str] = {}
    source_by_ticker: dict[str, str] = {}
    try:
        out = yf.download(tickers, period=period, interval="1d",
                          progress=False, auto_adjust=False,
                          group_by="ticker", threads=False)
    except Exception as exc:
        out = pd.DataFrame()
        errors["bulk_yfinance"] = str(exc)

    for ticker in tickers:
        series = _close_series_from_yf(out, ticker)
        source = "yfinance" if series is not None and not series.empty else ""
        if series is None or series.empty:
            series = _cached_daily_close(ticker, period)
            source = "csv_cache" if series is not None and not series.empty else ""
        if series is not None and not series.empty:
            cols[ticker] = series
            source_by_ticker[ticker] = source
        else:
            errors[ticker] = "missing close data"

    if not cols:
        raise RuntimeError(
            "xs_momentum history unavailable for all tickers; "
            f"period={period}; errors={errors}"
        )
    missing = sorted(set(tickers) - set(cols))
    if missing:
        log.warning("xs_momentum history missing %d/%d tickers: %s",
                    len(missing), len(tickers), ",".join(missing))
    df = pd.DataFrame(cols).dropna(how="all")
    df.index = pd.to_datetime(df.index, utc=True)
    df.attrs["source_by_ticker"] = source_by_ticker
    df.attrs["missing_tickers"] = missing
    df.attrs["errors"] = errors
    return df


def data_diagnostics(period: str = "2y") -> dict:
    """Health-check the strategy's market-data path.

    GREEN means the current universe has fresh yfinance data. YELLOW means
    the strategy can still rank, but only by using fallback cache or with a
    partial universe. RED means the usable bars are stale or unavailable.
    """
    universe = list(PARAMS["universe"])
    try:
        closes = _fetch_history(universe, period=period)
    except Exception as exc:
        return {
            "strategy": STRATEGY_LABEL,
            "status": "RED",
            "error": str(exc),
            "period": period,
        }
    if closes.empty:
        return {
            "strategy": STRATEGY_LABEL,
            "status": "RED",
            "error": "empty_history",
            "period": period,
        }

    now = pd.Timestamp.now(tz="UTC")
    latest_by_ticker: dict[str, str] = {}
    age_days_by_ticker: dict[str, float] = {}
    stale_tickers: list[str] = []
    for ticker in closes.columns:
        series = closes[ticker].dropna()
        if series.empty:
            continue
        latest = pd.to_datetime(series.index[-1], utc=True)
        age_days = max(0.0, (now - latest).total_seconds() / 86400.0)
        latest_by_ticker[ticker] = latest.isoformat()
        age_days_by_ticker[ticker] = round(age_days, 2)
        if age_days > MAX_DAILY_DATA_AGE_DAYS:
            stale_tickers.append(ticker)

    source_by_ticker = closes.attrs.get("source_by_ticker", {}) or {}
    missing = list(closes.attrs.get("missing_tickers", []) or [])
    fallback = sorted([
        ticker for ticker, source in source_by_ticker.items()
        if source != "yfinance"
    ])

    if stale_tickers or not latest_by_ticker:
        status = "RED"
    elif missing or fallback:
        status = "YELLOW"
    else:
        status = "GREEN"

    return {
        "strategy": STRATEGY_LABEL,
        "status": status,
        "period": period,
        "max_allowed_age_days": MAX_DAILY_DATA_AGE_DAYS,
        "n_tickers_requested": len(universe),
        "n_tickers_available": len(latest_by_ticker),
        "source_by_ticker": source_by_ticker,
        "latest_by_ticker": latest_by_ticker,
        "age_days_by_ticker": age_days_by_ticker,
        "fallback_tickers": fallback,
        "missing_tickers": missing,
        "stale_tickers": sorted(stale_tickers),
    }


# ── State ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "current_picks": {},  # ticker -> {entry_ts, entry_px, qty}
        "trade_count": 0,
        "session_id": str(uuid.uuid4())[:8],
        "last_rebalance_month": "",
    }


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


TRADE_FIELDS = [
    "ts", "side", "ticker", "px", "qty", "momentum_score",
    "pnl_usd", "pnl_pct", "session_id", "config_version",
]


def _ensure_trade_csv():
    if not TRADES_PATH.exists():
        with open(TRADES_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(TRADE_FIELDS)


def _append_trade(row: dict) -> None:
    _ensure_trade_csv()
    with open(TRADES_PATH, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=TRADE_FIELDS).writerow(
            {k: row.get(k, "") for k in TRADE_FIELDS}
        )


def _write_heartbeat(state: dict, last_rebalance: dict | None) -> None:
    try:
        from helio.strategy_common import git_sha as _git_sha
        git_sha = _git_sha(REPO)
    except Exception:
        git_sha = "unknown"
    HEARTBEAT_PATH.write_text(json.dumps({
        "system": "xs_momentum",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "signal_only" if _SIGNAL_ONLY_MODE else "paper",
        "trade_count": state.get("trade_count", 0),
        "current_picks": state.get("current_picks", {}),
        "last_rebalance": last_rebalance,
        "version": PARAMS["version"],
        "git_sha": git_sha,
    }, indent=2, default=str))


# ── Backtest ──────────────────────────────────────────────────────────────

def backtest(period: str = "10y", return_monthly_series: bool = False,
             universe_override: list[str] | None = None,
             *,
             ranking_mode: str = "single_12_1",
             top_pick_fraction: float | None = None) -> dict:
    """Simulate monthly rebalancing over the historical window.

    At each month-end (resampled from daily closes), rank universe by 12-1
    momentum, compare to last month's picks, simulate sells of removed
    picks and buys of new picks at the next month's first available close.

    When return_monthly_series=True the result includes a `monthly_returns`
    dict (YYYY-MM → equal-weight portfolio return as a fraction, not pct).
    Used by factor-decomposition audits to regress against Fama-French
    proxies.

    universe_override lets callers swap PARAMS['universe'] for a different
    ticker set without mutating PARAMS. Used by alternative-universe
    audits (sector-only, country-only, etc.).

    ranking_mode lets the sweep test alternative rankers:
        "single_12_1"   — production 12-1 momentum (default)
        "multi_horizon" — equal-weight 12-1 + 6-1 + 3-1 ensemble

    top_pick_fraction overrides PARAMS["top_quintile_fraction"] so the
    sweep can test top-1 / top-2 / top-3 concentration variants without
    mutating PARAMS.
    """
    universe = list(universe_override if universe_override is not None
                       else PARAMS["universe"])
    long_lb = PARAMS["long_lookback"]
    short_lb = PARAMS["short_lookback"]
    if top_pick_fraction is None:
        top_pick_fraction = PARAMS["top_quintile_fraction"]

    closes = _fetch_history(universe, period=period)
    if closes.empty:
        return {"error": "no data fetched"}

    # Get month-end indices (last bar of each month)
    closes.index = closes.index.tz_convert("UTC")
    month_ends = closes.groupby([closes.index.year, closes.index.month]).tail(1).index
    if len(month_ends) < 2:
        return {"error": "insufficient monthly history"}

    holdings: dict[str, dict] = {}  # ticker -> {entry_px, entry_idx}
    trades: list[dict] = []

    for month_end in month_ends:
        i = closes.index.get_loc(month_end)
        if i <= long_lb + short_lb:
            continue

        # Rank using data ENDING AT month_end (avoid look-ahead)
        per_asset_closes = {
            t: closes[t].iloc[: i + 1].dropna().tolist()
            for t in universe
            if t in closes.columns
        }
        if ranking_mode == "multi_horizon":
            from helio.xs_momentum_variants import rank_universe_multi_horizon
            ranked = rank_universe_multi_horizon(per_asset_closes)
        else:
            ranked = rank_universe_by_momentum(
                per_asset_closes,
                long_lookback=long_lb, short_lookback=short_lb,
            )
        if not ranked:
            continue
        picks = select_top_quintile(ranked, fraction=top_pick_fraction)
        pick_tickers = {p.ticker for p in picks}

        # Execution happens at the NEXT trading day's close (no look-ahead)
        next_idx = i + 1
        if next_idx >= len(closes):
            continue
        exec_date = closes.index[next_idx]

        # Sell removed
        for ticker in list(holdings.keys()):
            if ticker not in pick_tickers:
                entry = holdings.pop(ticker)
                exit_px = float(closes[ticker].iloc[next_idx])
                pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
                trades.append({
                    "ticker": ticker,
                    "entry_date": entry["entry_date"],
                    "exit_date": exec_date.isoformat(),
                    "entry_px": entry["entry_px"],
                    "exit_px": exit_px,
                    "pnl_pct": pnl_pct,
                    "hold_days": (exec_date - pd.to_datetime(entry["entry_date"])).days,
                })

        # Buy new
        for p in picks:
            if p.ticker in holdings:
                continue
            entry_px = float(closes[p.ticker].iloc[next_idx])
            holdings[p.ticker] = {
                "entry_px": entry_px,
                "entry_date": exec_date.isoformat(),
            }

    # Close any remaining holdings at the final bar
    final_idx = len(closes) - 1
    final_date = closes.index[final_idx]
    for ticker, entry in holdings.items():
        exit_px = float(closes[ticker].iloc[final_idx])
        pnl_pct = (exit_px / entry["entry_px"] - 1.0) * 100.0
        trades.append({
            "ticker": ticker,
            "entry_date": entry["entry_date"],
            "exit_date": final_date.isoformat(),
            "entry_px": entry["entry_px"],
            "exit_px": exit_px,
            "pnl_pct": pnl_pct,
            "hold_days": (final_date - pd.to_datetime(entry["entry_date"])).days,
        })

    if not trades:
        return {"universe_size": len(universe), "trades": 0,
                "note": "no rebalances produced trades"}

    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = abs(sum(wins) / sum(losses)) if losses else float("inf")
    wr = len(wins) / len(trades)

    # Portfolio equity curve: equal-weight across simultaneous picks.
    # Approximation: compound average per-trade returns (since picks are
    # held in parallel, the fleet return per month is the average of the
    # 3 picks' month return).
    eq = 1.0
    peak = 1.0
    max_dd = 0.0
    # Group trades by exit_date to approximate monthly portfolio return
    by_month: dict[str, list[float]] = {}
    for t in trades:
        m = str(t["exit_date"])[:7]
        by_month.setdefault(m, []).append(t["pnl_pct"])
    for m in sorted(by_month):
        ret = sum(by_month[m]) / len(by_month[m]) / 100.0
        eq *= (1.0 + ret)
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    try:
        first_dt = min(pd.to_datetime(t["entry_date"]) for t in trades)
        last_dt = max(pd.to_datetime(t["exit_date"]) for t in trades)
        days = max(1, (last_dt - first_dt).days)
        cagr = (eq ** (365.25 / days) - 1.0) * 100.0
    except Exception:
        cagr = 0.0

    result = {
        "universe_size": len(universe),
        "trades": len(trades),
        "win_rate": round(wr, 3),
        "profit_factor": round(pf, 2),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "portfolio_growth_pct": round((eq - 1.0) * 100, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "first_entry": str(min(t["entry_date"] for t in trades)),
        "last_exit": str(max(t["exit_date"] for t in trades)),
        "trades_per_ticker": _count_per_ticker(trades),
    }
    if return_monthly_series:
        # Equal-weight portfolio return per exit-month, as fraction
        # (matches the equity-curve compounding above).
        result["monthly_returns"] = {
            m: sum(by_month[m]) / len(by_month[m]) / 100.0
            for m in sorted(by_month)
        }
    # Always expose the per-trade ledger so alternative-universe / robustness
    # audits can feed promotion_panel on PER-TRADE pnls (apples-to-apples
    # with the published broad-8 baseline). Each entry has pnl_pct +
    # ticker + entry/exit dates.
    result["trades_detail"] = trades
    return result


def _count_per_ticker(trades: list[dict]) -> dict:
    out: dict[str, int] = {}
    for t in trades:
        out[t["ticker"]] = out.get(t["ticker"], 0) + 1
    return out


def monthly_portfolio_returns(period: str = "10y") -> dict[str, float]:
    """Compute the strategy's true month-over-month equal-weight portfolio
    return over the backtest window. Unlike backtest()'s by_month dict (which
    is keyed by exit_date and skips months where no rotation occurred), this
    walks every calendar month-end and computes the held-portfolio return.

    Returns {"YYYY-MM": fraction, ...}. Used by factor-decomposition audits.
    """
    universe = list(PARAMS["universe"])
    long_lb = PARAMS["long_lookback"]
    short_lb = PARAMS["short_lookback"]
    closes = _fetch_history(universe, period=period)
    if closes.empty:
        return {}
    closes.index = closes.index.tz_convert("UTC")
    month_ends = closes.groupby(
        [closes.index.year, closes.index.month]
    ).tail(1).index

    holdings: set[str] = set()  # current picks held into the next month
    monthly: dict[str, float] = {}
    prev_close: dict[str, float] = {}

    for month_end in month_ends:
        i = closes.index.get_loc(month_end)
        month_key = month_end.strftime("%Y-%m")
        # Step 1: realize this month's return on whatever we were HOLDING
        # at the start of this month (= holdings as set at the previous
        # rebalance) using start-of-month price → end-of-month price.
        if holdings and prev_close:
            rets = []
            for t in holdings:
                if t in prev_close and t in closes.columns:
                    end_px = float(closes[t].iloc[i])
                    start_px = prev_close[t]
                    if start_px > 0:
                        rets.append(end_px / start_px - 1.0)
            if rets:
                monthly[month_key] = sum(rets) / len(rets)
        # Step 2: rebalance — recompute top-2 picks using data ENDING AT
        # this month_end (no look-ahead), then set holdings for next month.
        if i > long_lb + short_lb:
            per_asset = {
                t: closes[t].iloc[: i + 1].dropna().tolist()
                for t in universe
                if t in closes.columns
            }
            ranked = rank_universe_by_momentum(
                per_asset,
                long_lookback=long_lb,
                short_lookback=short_lb,
            )
            picks = select_top_quintile(
                ranked, fraction=PARAMS["top_quintile_fraction"]
            )
            holdings = {p.ticker for p in picks}
        # Step 3: snapshot end-of-month prices for the NEXT month's
        # "start price" reference (we hold from end_of_this_month →
        # end_of_next_month, since rebalance happens at this bar).
        prev_close = {
            t: float(closes[t].iloc[i])
            for t in holdings
            if t in closes.columns
        }
    return monthly


# ── Live execution ────────────────────────────────────────────────────────

def _is_rebalance_due(state: dict, now_utc: datetime) -> bool:
    """First wake-cycle of a new calendar month → rebalance. Constrained
    to the first 7 days of the month so a fresh state on day 20 does not
    accidentally fire a mid-month rebalance — month-end weekends/holidays
    can slide rebalance up to Mon-Tue of the new month, but never past the
    first week. Caller can use --force to bypass."""
    if now_utc.weekday() >= 5:
        return False
    if now_utc.day > 7:
        return False
    current_month_key = now_utc.strftime("%Y-%m")
    last_rebalance_month = state.get("last_rebalance_month", "")
    return current_month_key != last_rebalance_month


def _current_picks(state: dict) -> dict[str, dict]:
    picks = state.get("current_picks") or {}
    if not isinstance(picks, dict):
        return {}
    return picks


def _submit_market_order(
    ib,
    contract,
    action: str,          # "BUY" or "SELL"
    qty: int,
    *,
    est_px: float,
    timeout_s: float = 30.0,
):
    """Plain market-order submit + wait-for-fill. Used by xs_momentum's
    monthly rebalance — no protective brackets (positions hold until next
    rebalance flips them out of the top quintile).

    Routes through helio.ibkr_execution.submit_market_with_boundary so
    all real-money + halt + market-open + kill-registry checks happen
    in one place (static-safety-invariant compliant)."""
    fill = ibkr.submit_market_with_boundary(
        ib, contract, action, qty,
        strategy_label=STRATEGY_LABEL,
        est_px=est_px,
        timeout_s=timeout_s,
    )
    if fill is None or not fill.filled:
        return None
    return fill


def _write_canonical_entry(ticker: str, qty: int, fill_px: float,
                            score: float, anchor_at_fill: float) -> None:
    try:
        from helio.canonical_fills import write_fill
        write_fill(
            strategy=STRATEGY_LABEL,
            symbol=ticker,
            direction="long",
            side="ENTRY",
            entry_ts=datetime.now(timezone.utc).isoformat(),
            entry_px=float(fill_px),
            size=int(qty),
            extra={
                "momentum_score": round(float(score), 6),
                "anchor_at_fill_usd": round(float(anchor_at_fill), 2),
            },
        )
    except Exception as exc:
        log.warning("canonical ENTRY write failed for %s (non-fatal): %s",
                    ticker, exc)


def _write_canonical_exit(ticker: str, qty: int, fill_px: float,
                           entry_px: float, entry_ts: str,
                           pnl_usd: float, reason: str) -> None:
    try:
        from helio.canonical_fills import write_fill
        write_fill(
            strategy=STRATEGY_LABEL,
            symbol=ticker,
            direction="long",
            side="EXIT",
            entry_ts=entry_ts,
            exit_ts=datetime.now(timezone.utc).isoformat(),
            entry_px=float(entry_px),
            exit_px=float(fill_px),
            size=int(qty),
            pnl_usd=round(float(pnl_usd), 2),
            exit_reason=reason,
        )
    except Exception as exc:
        log.warning("canonical EXIT write failed for %s (non-fatal): %s",
                    ticker, exc)


def evaluate_once(force: bool = False) -> dict:
    """Single rebalance cycle. Returns a summary dict (used by --evaluate and
    --loop). When force=False (default), no-ops if it's not the first wake of
    a new calendar month."""
    summary = {"action": "noop", "reason": "",
                "now": datetime.now(timezone.utc).isoformat()}
    state = _load_state()
    state.setdefault("runtime_start", time.time())
    now_utc = datetime.now(timezone.utc)

    if not force and not _is_rebalance_due(state, now_utc):
        summary["reason"] = f"not_due (last_rebalance_month={state.get('last_rebalance_month', '')})"
        record_blocker(
            STRATEGY_LABEL, "NOT_DUE", stage="evaluate",
            reason=summary["reason"],
            context={"last_rebalance_month": state.get("last_rebalance_month", "")},
        )
        _write_heartbeat(state, last_rebalance=state.get("last_rebalance"))
        log.info("xs_momentum eval skipped: %s", summary["reason"])
        return summary

    # ── REGIME GATE — runs BEFORE allocation check ────────────────────
    # H2 fix (2026-05-25): the gate's job is to "exit positions in bad
    # regimes." That has to happen regardless of allocation status —
    # otherwise a deallocated variant trapped in an open position can't
    # exit just because alloc=0. Run the gate FIRST.
    #
    # C1 fix (2026-05-25): fail-CLOSED on missing regime data. Trading
    # without verified regime status is too risky for real money.
    #
    # C3 fix (2026-05-25): require regime data fresh within 2 business
    # days. Yfinance can be stale through long weekends / holidays.
    gate_name = PARAMS.get("regime_gate")
    if gate_name:
        try:
            from helio.regime import REGIME_GATES, fetch_regime_data
            gate_cfg = REGIME_GATES.get(gate_name)
            if gate_cfg is None:
                summary["action"] = "blocked"
                summary["reason"] = f"unknown_regime_gate:{gate_name}"
                record_blocker(STRATEGY_LABEL, "REGIME_UNKNOWN",
                               stage="regime", reason=summary["reason"])
                log.error("UNKNOWN regime_gate=%r — refusing to trade",
                          gate_name)
                return summary
            regime_data = fetch_regime_data(period="2y")
            needed = gate_cfg["needs"][0]
            series = regime_data.get(needed)
            if series is None or series.empty:
                # C1 fix: fail-CLOSED
                summary["action"] = "blocked"
                summary["reason"] = (
                    f"regime_data_missing:{needed} (gate={gate_name})"
                )
                record_blocker(STRATEGY_LABEL, "REGIME_DATA_MISSING",
                               stage="regime",
                               reason=summary["reason"],
                               context={"gate": gate_name, "needed": needed})
                log.error("regime_gate=%s but no %s data — REFUSING to "
                          "trade (fail-CLOSED)", gate_name, needed)
                return summary
            # C3 fix: validate data freshness
            try:
                latest_dt = pd.to_datetime(series.index[-1])
                if latest_dt.tzinfo is not None:
                    latest_dt = latest_dt.tz_localize(None)
                staleness_days = (
                    pd.Timestamp.now().tz_localize(None) - latest_dt
                ).total_seconds() / 86400
            except Exception:
                staleness_days = 999  # treat unparseable as very stale
            if staleness_days > 4.0:   # >4 calendar days = stale (covers long weekends)
                summary["action"] = "blocked"
                summary["reason"] = (
                    f"regime_data_stale:{needed} age={staleness_days:.1f}d"
                )
                record_blocker(STRATEGY_LABEL, "REGIME_DATA_STALE",
                               stage="regime",
                               reason=summary["reason"],
                               context={"gate": gate_name,
                                        "staleness_days": staleness_days})
                log.error("regime_gate=%s data %.1fd stale — REFUSING to "
                          "trade (fail-CLOSED)", gate_name, staleness_days)
                return summary

            is_bullish = bool(gate_cfg["fn"](series))
            if not is_bullish:
                summary["action"] = "regime_skip"
                summary["reason"] = (
                    f"regime_gate {gate_name} = bearish "
                    f"({gate_cfg['description']})"
                )
                record_blocker(
                    STRATEGY_LABEL, "REGIME_BEARISH",
                    stage="regime",
                    reason=summary["reason"],
                    context={"gate": gate_name},
                )
                # M1 fix: always save state, including in early-return paths
                existing_picks = (state.get("current_picks") or {})
                if existing_picks:
                    log.info("regime_skip: exiting %d open picks",
                             len(existing_picks))
                    state["current_picks"] = {}
                _save_state(state)
                _write_heartbeat(state,
                                 last_rebalance=state.get("last_rebalance"))
                log.info("regime_gate FALSE: %s — skipping rebalance",
                         summary["reason"])
                return summary
        except Exception as exc:
            # C1 fix: ANY unexpected error in the gate check = fail-CLOSED.
            # A bug in gate logic should NOT silently allow a trade.
            summary["action"] = "blocked"
            summary["reason"] = f"regime_check_error:{exc}"
            record_blocker(STRATEGY_LABEL, "REGIME_CHECK_ERROR",
                           stage="regime", reason=str(exc),
                           context={"gate": gate_name})
            log.error("regime_gate check raised %s — REFUSING to trade "
                      "(fail-CLOSED)", exc, exc_info=True)
            return summary

    # ── ALLOCATION GATE (now AFTER regime check) ──────────────────────
    try:
        alloc_factor = float(get_allocation_factor(STRATEGY_LABEL))
    except Exception as exc:
        log.error("could not read allocation_factor: %s — refusing to trade", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_read_failed:{exc}"
        record_blocker(STRATEGY_LABEL, "ALLOC_READ_FAILED",
                       stage="allocation", reason=str(exc))
        return summary
    if alloc_factor <= 0.0:
        log.info("[ALLOC-GATE] allocation_factor=%.3f — strategy deallocated, "
                  "no rebalance", alloc_factor)
        summary["action"] = "blocked"
        summary["reason"] = f"alloc_factor={alloc_factor}"
        record_blocker(STRATEGY_LABEL, "ALLOC_ZERO",
                       stage="allocation", reason=summary["reason"])
        _write_heartbeat(state, last_rebalance=state.get("last_rebalance"))
        return summary

    universe = list(PARAMS["universe"])
    try:
        closes = _fetch_history(universe, period="3y")
    except Exception as exc:
        log.error("history fetch failed: %s", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"history_fetch:{exc}"
        record_blocker(STRATEGY_LABEL, "DATA_UNAVAILABLE",
                       stage="history", reason=str(exc))
        return summary
    if closes.empty:
        summary["action"] = "blocked"
        summary["reason"] = "no_history"
        record_blocker(STRATEGY_LABEL, "DATA_UNAVAILABLE",
                       stage="history", reason=summary["reason"])
        return summary

    per_asset = {t: closes[t].dropna().tolist()
                  for t in universe if t in closes.columns}
    ranked = rank_universe_by_momentum(
        per_asset,
        long_lookback=PARAMS["long_lookback"],
        short_lookback=PARAMS["short_lookback"],
    )
    if not ranked:
        summary["action"] = "blocked"
        summary["reason"] = "ranking_empty"
        record_blocker(STRATEGY_LABEL, "RANKING_EMPTY",
                       stage="ranking", reason=summary["reason"])
        return summary

    picks = select_top_quintile(ranked,
                                 fraction=PARAMS["top_quintile_fraction"])
    pick_tickers = {p.ticker for p in picks}
    score_by_ticker = {p.ticker: float(p.score) for p in picks}
    last_px = {t: float(closes[t].dropna().iloc[-1])
                for t in universe if t in closes.columns}

    log.info("xs_momentum picks (effective alloc %.2f×): %s",
             alloc_factor, sorted(pick_tickers))

    current = _current_picks(state)
    to_sell = [t for t in current if t not in pick_tickers]
    to_buy = [t for t in pick_tickers if t not in current]

    if not to_sell and not to_buy:
        log.info("xs_momentum: holdings already match top picks, no orders")
        summary["action"] = "no_change"
        summary["reason"] = "already_optimal"
        state["last_rebalance_month"] = now_utc.strftime("%Y-%m")
        state["last_rebalance"] = {
            "ts": now_utc.isoformat(),
            "picks": sorted(pick_tickers),
            "no_change": True,
        }
        _save_state(state)
        _write_heartbeat(state, last_rebalance=state["last_rebalance"])
        return summary

    if _SIGNAL_ONLY_MODE:
        log.info("SIGNAL-ONLY: would SELL %s, BUY %s", to_sell, to_buy)
        summary["action"] = "signal_only"
        summary["reason"] = "signal_only_mode"
        summary["would_sell"] = to_sell
        summary["would_buy"] = to_buy
        record_blocker(
            STRATEGY_LABEL, "SIGNAL_ONLY", stage="execution",
            reason="signal_only_mode",
            context={"would_sell": to_sell, "would_buy": to_buy},
        )
        return summary

    try:
        ib = ibkr.connect_with_retry(IBKR_CLIENT_ID, max_attempts=3)
    except Exception as exc:
        log.error("IBKR connect failed: %s — aborting rebalance", exc)
        summary["action"] = "blocked"
        summary["reason"] = f"ibkr_connect:{exc}"
        record_blocker(STRATEGY_LABEL, "IBKR_CONNECT_FAILED",
                       stage="connect", reason=str(exc))
        return summary

    try:
        anchor = get_sizing_anchor_usd()
        capital_total = anchor * alloc_factor
        per_pick_usd_cap = max_notional_usd("etf",
                                              strategy_label=STRATEGY_LABEL) or 0.0
        per_pick_usd = capital_total * float(PARAMS["per_pick_fraction"])
        if per_pick_usd_cap > 0:
            per_pick_usd = min(per_pick_usd, per_pick_usd_cap)
        log.info("xs_momentum capital: anchor=%.2f alloc_factor=%.2f "
                  "per_pick_usd=%.2f (cap=%.2f)",
                  anchor, alloc_factor, per_pick_usd, per_pick_usd_cap)

        sells = []
        for ticker in to_sell:
            holding = current.get(ticker) or {}
            qty = int(holding.get("qty") or 0)
            if qty <= 0:
                log.warning("EXIT skipped for %s: state shows qty<=0 (%s)",
                              ticker, qty)
                record_blocker(STRATEGY_LABEL, "SIZE_ZERO",
                               symbol=ticker, stage="exit", action="SELL",
                               qty=qty, reason="state qty <= 0")
                continue
            contract = ibkr.make_contract(ticker, "etf")
            try:
                ib.qualifyContracts(contract)
            except Exception as exc:
                log.error("qualifyContracts(%s) failed: %s", ticker, exc)
                record_blocker(STRATEGY_LABEL, "CONTRACT_QUALIFY_FAILED",
                               symbol=ticker, stage="exit", action="SELL",
                               qty=qty, reason=str(exc))
                continue
            est_px = last_px.get(ticker) or float(holding.get("entry_px") or 0.0)
            fill = _submit_market_order(ib, contract, "SELL", qty,
                                          est_px=est_px)
            if fill is None:
                record_blocker(STRATEGY_LABEL, "ORDER_REJECTED",
                               symbol=ticker, stage="exit", action="SELL",
                               qty=qty, reason="fill_none")
                continue
            entry_px = float(holding.get("entry_px") or 0.0)
            pnl_usd = (fill.fill_price - entry_px) * qty
            _append_trade({
                "ts": datetime.now(timezone.utc).isoformat(),
                "side": "EXIT",
                "ticker": ticker,
                "px": round(fill.fill_price, 4),
                "qty": qty,
                "momentum_score": "",
                "pnl_usd": round(pnl_usd, 2),
                "pnl_pct": round((fill.fill_price / entry_px - 1.0) * 100, 4)
                              if entry_px > 0 else "",
                "session_id": state.get("session_id", ""),
                "config_version": PARAMS["version"],
            })
            _write_canonical_exit(
                ticker=ticker, qty=qty, fill_px=fill.fill_price,
                entry_px=entry_px, entry_ts=str(holding.get("entry_ts") or ""),
                pnl_usd=pnl_usd, reason="rebalance_drop",
            )
            current.pop(ticker, None)
            state["trade_count"] = int(state.get("trade_count", 0)) + 1
            # C2 fix (2026-05-25): persist state IMMEDIATELY after each
            # fill. Previously, state was only saved at end of both
            # loops — a crash mid-loop would leave trades.csv + broker
            # positions out of sync with state (phantom positions).
            state["current_picks"] = current
            _save_state(state)
            sells.append({"ticker": ticker, "qty": qty,
                          "exit_px": fill.fill_price, "pnl_usd": pnl_usd})

        buys = []
        for ticker in to_buy:
            est_px = last_px.get(ticker)
            if not est_px or est_px <= 0:
                log.warning("ENTRY skipped for %s: no estimated price", ticker)
                record_blocker(STRATEGY_LABEL, "DATA_UNAVAILABLE",
                               symbol=ticker, stage="entry", action="BUY",
                               reason="missing estimated price")
                continue
            qty = int(per_pick_usd // est_px)
            if qty < 1:
                record_blocker(STRATEGY_LABEL, "SIZE_ZERO",
                               symbol=ticker, stage="entry", action="BUY",
                               notional_usd=per_pick_usd,
                               reason="computed quantity < 1")
                log.warning("ENTRY skipped for %s: per_pick_usd=%.2f / est_px=%.2f → qty<1",
                              ticker, per_pick_usd, est_px)
                continue
            proposed_notional = float(qty) * float(est_px)
            try:
                from helio.cluster_exposure import (
                    would_breach_cluster_cap,
                    would_breach_strategy_overlap_cap,
                )
                breach = would_breach_strategy_overlap_cap(
                    ticker, "long", proposed_notional,
                    strategy_label=STRATEGY_LABEL,
                )
                if breach is None:
                    breach = would_breach_cluster_cap(
                        ticker, "long", proposed_notional,
                        strategy_label=STRATEGY_LABEL,
                    )
            except Exception as exc:
                breach = f"CAP_CHECK_ERROR:{type(exc).__name__}"
            if breach:
                log.warning("ENTRY blocked for %s: %s notional=%.2f",
                            ticker, breach, proposed_notional)
                record_blocker(
                    STRATEGY_LABEL, "CAP_EXCEEDED", symbol=ticker,
                    stage="entry", action="BUY", qty=qty,
                    notional_usd=proposed_notional, reason=breach,
                )
                continue
            contract = ibkr.make_contract(ticker, "etf")
            try:
                ib.qualifyContracts(contract)
            except Exception as exc:
                log.error("qualifyContracts(%s) failed: %s", ticker, exc)
                record_blocker(STRATEGY_LABEL, "CONTRACT_QUALIFY_FAILED",
                               symbol=ticker, stage="entry", action="BUY",
                               qty=qty, notional_usd=proposed_notional,
                               reason=str(exc))
                continue
            existing = ibkr.query_position(ib, contract)
            if existing != 0:
                log.warning("BROKER_HAS_POSITION %s qty=%.0f — skipping entry",
                              ticker, existing)
                record_blocker(STRATEGY_LABEL, "BROKER_POSITION_EXISTS",
                               symbol=ticker, stage="entry", action="BUY",
                               qty=qty, notional_usd=proposed_notional,
                               reason=f"broker existing qty={existing}")
                continue
            fill = _submit_market_order(ib, contract, "BUY", qty,
                                          est_px=est_px)
            if fill is None:
                record_blocker(STRATEGY_LABEL, "ORDER_REJECTED",
                               symbol=ticker, stage="entry", action="BUY",
                               qty=qty, notional_usd=proposed_notional,
                               reason="fill_none")
                continue
            current[ticker] = {
                "entry_ts": datetime.now(timezone.utc).isoformat(),
                "entry_px": float(fill.fill_price),
                "qty": int(qty),
                "momentum_score": score_by_ticker.get(ticker, 0.0),
            }
            state["trade_count"] = int(state.get("trade_count", 0)) + 1
            _append_trade({
                "ts": datetime.now(timezone.utc).isoformat(),
                "side": "ENTRY",
                "ticker": ticker,
                "px": round(fill.fill_price, 4),
                "qty": qty,
                "momentum_score": round(score_by_ticker.get(ticker, 0.0), 6),
                "pnl_usd": "",
                "pnl_pct": "",
                "session_id": state.get("session_id", ""),
                "config_version": PARAMS["version"],
            })
            _write_canonical_entry(
                ticker=ticker, qty=qty, fill_px=fill.fill_price,
                score=score_by_ticker.get(ticker, 0.0),
                anchor_at_fill=anchor,
            )
            # C2 fix (2026-05-25): persist state immediately after fill.
            state["current_picks"] = current
            _save_state(state)
            buys.append({"ticker": ticker, "qty": qty,
                          "entry_px": fill.fill_price})

        state["current_picks"] = current
        state["last_rebalance_month"] = now_utc.strftime("%Y-%m")
        state["last_rebalance"] = {
            "ts": now_utc.isoformat(),
            "picks": sorted(pick_tickers),
            "sells": sells,
            "buys": buys,
            "alloc_factor": alloc_factor,
            "per_pick_usd": round(per_pick_usd, 2),
        }
        _save_state(state)
        _write_heartbeat(state, last_rebalance=state["last_rebalance"])

        summary["action"] = "rebalanced"
        summary["sells"] = sells
        summary["buys"] = buys
        summary["picks"] = sorted(pick_tickers)
        return summary
    finally:
        try:
            ibkr.disconnect(ib)
        except Exception:
            pass


def loop_mode() -> None:
    """Daily wake at eval_hour_utc:eval_minute_utc. Skips if not first
    wake of the month."""
    log.info("xs_momentum --loop started: daily wake at %02d:%02d UTC",
             PARAMS["eval_hour_utc"], PARAMS["eval_minute_utc"])
    while True:
        try:
            now = datetime.now(timezone.utc)
            fire = now.replace(hour=PARAMS["eval_hour_utc"],
                                minute=PARAMS["eval_minute_utc"],
                                second=15, microsecond=0)
            if fire <= now:
                fire += timedelta(days=1)
            sleep_s = max(5.0, (fire - now).total_seconds())
            log.info("xs_momentum next wake at %s UTC (sleep %.0fs)",
                     fire.isoformat(), sleep_s)
            state = _load_state()
            _write_heartbeat(state, last_rebalance=state.get("last_rebalance"))
            remaining = sleep_s
            while remaining > 0:
                chunk = min(300.0, remaining)
                time.sleep(chunk)
                remaining -= chunk
                state = _load_state()
                _write_heartbeat(state, last_rebalance=state.get("last_rebalance"))
            try:
                evaluate_once()
            except Exception as exc:
                log.error("evaluate_once failed: %s", exc, exc_info=True)
                time.sleep(120)
        except KeyboardInterrupt:
            log.info("xs_momentum loop stopped by user")
            return


# ── Main ──────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument("--period", default="10y")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--evaluate", action="store_true",
                          help="One rebalance cycle (no-op if not month-start)")
    parser.add_argument("--force", action="store_true",
                          help="With --evaluate: rebalance even if not month-start")
    parser.add_argument("--loop", action="store_true",
                          help="Daemon: daily wake, rebalance only on first wake of month")
    parser.add_argument("--signal-only", action="store_true")
    parser.add_argument("--variant", default="baseline",
                          choices=list(_VARIANT_REGISTRY),
                          help="Strategy variant: which universe + log dir + client_id (default: baseline)")
    args = parser.parse_args(argv)

    # Wire variant config BEFORE anything reads state/heartbeat/etc.
    configure_variant(args.variant)
    log.info("variant=%s strategy_label=%s log_dir=%s client_id=%s "
             "universe=%s",
             args.variant, STRATEGY_LABEL, LOG_DIR.name, IBKR_CLIENT_ID,
             PARAMS["universe"])

    global _SIGNAL_ONLY_MODE
    _SIGNAL_ONLY_MODE = bool(args.signal_only)
    if _SIGNAL_ONLY_MODE:
        log.info("SIGNAL-ONLY MODE: no IBKR orders will be submitted")

    if args.backtest:
        stats = backtest(period=args.period)
        print(json.dumps(stats, indent=2, default=str))
        return 0

    if args.check:
        universe = PARAMS["universe"]
        closes = _fetch_history(universe, period="2y")
        per_asset = {
            t: closes[t].dropna().tolist()
            for t in universe if t in closes.columns
        }
        ranked = rank_universe_by_momentum(
            per_asset,
            long_lookback=PARAMS["long_lookback"],
            short_lookback=PARAMS["short_lookback"],
        )
        if not ranked:
            print(json.dumps({
                "error": "ranking_empty",
                "as_of": str(closes.index[-1]) if len(closes) else None,
                "available_columns": sorted(closes.columns.tolist()),
            }, indent=2, default=str))
            return 2
        picks = select_top_quintile(ranked, fraction=PARAMS["top_quintile_fraction"])
        print(json.dumps({
            "as_of": str(closes.index[-1]) if len(closes) else None,
            "data_diagnostics": data_diagnostics(period="2y"),
            "ranking": [{"ticker": s.ticker, "score": round(s.score * 100, 2)} for s in ranked],
            "picks": [{"ticker": p.ticker, "score": round(p.score * 100, 2)} for p in picks],
        }, indent=2, default=str))
        return 0

    # Live/evaluate/loop modes get a single-instance PID lock so a
    # rogue duplicate runner (e.g. system Python launched alongside
    # the venv one — 2026-05-24 incident) refuses to start instead of
    # stomping on shared state.
    if args.evaluate or args.loop:
        from helio.runner_lock import acquire_runner_lock, RunnerAlreadyRunning
        try:
            # Use the variant-aware STRATEGY_LABEL so each variant owns
            # its own lock file under forge/logs/<variant>/runner.lock.
            with acquire_runner_lock(STRATEGY_LABEL):
                if args.evaluate:
                    summary = evaluate_once(force=args.force)
                    print(json.dumps(summary, indent=2, default=str))
                    return 0
                loop_mode()
                return 0
        except RunnerAlreadyRunning as exc:
            log.error("REFUSING_DUPLICATE_RUNNER: %s", exc)
            return 75  # EX_TEMPFAIL

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
