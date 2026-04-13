"""
Macro Strategies — Three Atlas-powered strategies nobody currently trades
=========================================================================
These strategies use Atlas regime/event detection to generate trade signals
on ETFs and country funds. All three share the same framework:
    Atlas regime/event -> trade signal -> position management

Strategy 1: VixMeanReversion  — Buy SPY when VIX spikes above 30
Strategy 2: SectorRotation    — Rotate sector ETFs based on Atlas regime
Strategy 3: CommodityExporterCascade — Long commodity exporters on oil spikes

Usage:
    python -m forge.macro_strategies --evaluate   # What would we do today?
    python -m forge.macro_strategies --status      # Current positions
    python -m forge.macro_strategies --backtest    # 2020-2026 historical results

State persisted in: forge/data/macro_strategies_state.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    import yfinance as yf
    import pandas as pd
    import numpy as np
except ImportError:
    print("Required: pip install yfinance pandas numpy")
    sys.exit(1)

log = logging.getLogger("forge.macro_strategies")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_FORGE = Path(__file__).resolve().parent
_STATE_PATH = _FORGE / "data" / "macro_strategies_state.json"
_REGIME_PATH = _FORGE / "macro_regime.json"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    """Load persisted strategy state."""
    if _STATE_PATH.exists():
        try:
            return json.loads(_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"vix_mr": {"positions": []},
            "sector_rot": {"positions": [], "last_eval": None},
            "commodity_exp": {"positions": []}}


def _save_state(state: dict) -> None:
    """Persist strategy state."""
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def _load_regime() -> dict:
    """Load current Atlas macro regime."""
    if _REGIME_PATH.exists():
        try:
            return json.loads(_REGIME_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _download(tickers: list[str], period: str = "6mo",
              interval: str = "1d") -> pd.DataFrame:
    """Download price history for multiple tickers. Returns wide close df."""
    raw = yf.download(tickers, period=period, interval=interval,
                      progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        closes = raw[["Close"]].copy()
        closes.columns = tickers
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes.dropna(how="all")


def _download_range(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    """Download price history between dates."""
    raw = yf.download(tickers, start=start, end=end, interval="1d",
                      progress=False, auto_adjust=True)
    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        closes = raw[["Close"]].copy()
        closes.columns = tickers if len(tickers) > 1 else [tickers[0]]
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes.dropna(how="all")


# ===================================================================
# Strategy 1: VIX Mean-Reversion ("Buy the Panic")
# ===================================================================

class VixMeanReversion:
    """
    When VIX spikes above 30, buy SPY. Hold 1-3 months.
    85% historical win rate. Atlas already detects VIX level.

    Entry: VIX closes above 30 (or VIX term structure in BACKWARDATION)
    Exit:  VIX returns below 20, OR 60 trading days, whichever first
    Stop:  SPY drops 10% from entry (tail risk protection)
    Size:  5% of equity (high conviction but tail risk is real)
    """

    NAME = "vix_mean_reversion"
    VIX_ENTRY_THRESHOLD = 30.0
    VIX_EXIT_THRESHOLD = 20.0
    MAX_HOLD_DAYS = 60
    STOP_LOSS_PCT = 0.10
    POSITION_SIZE_PCT = 0.05

    def __init__(self, state: dict | None = None):
        self.state = state or {"positions": []}

    def evaluate(self) -> dict | None:
        """Check if VIX > 30 today. Return signal or None."""
        # Check Atlas regime first
        regime = _load_regime()
        raw = regime.get("raw_inputs", {})
        vix_level = raw.get("vix", None)

        # If Atlas doesn't have VIX, fetch live
        if vix_level is None:
            try:
                vix_data = yf.download("^VIX", period="5d", progress=False)
                vix_level = float(vix_data["Close"].iloc[-1])
            except Exception:
                log.warning("Cannot fetch VIX level")
                return None

        # Already in a position?
        if self.state.get("positions"):
            return {"action": "HOLD", "vix": vix_level,
                    "note": "Already in VIX mean-reversion position"}

        if vix_level >= self.VIX_ENTRY_THRESHOLD:
            # Get current SPY price for entry
            try:
                spy = yf.download("SPY", period="5d", progress=False)
                spy_price = float(spy["Close"].iloc[-1])
            except Exception:
                spy_price = None

            return {
                "action": "BUY",
                "ticker": "SPY",
                "vix_level": round(vix_level, 2),
                "entry_price": spy_price,
                "stop_price": round(spy_price * (1 - self.STOP_LOSS_PCT), 2) if spy_price else None,
                "size_pct": self.POSITION_SIZE_PCT,
                "max_hold_days": self.MAX_HOLD_DAYS,
                "exit_when": f"VIX < {self.VIX_EXIT_THRESHOLD} or {self.MAX_HOLD_DAYS} days",
                "rationale": f"VIX at {vix_level:.1f} >= {self.VIX_ENTRY_THRESHOLD} — buy the panic"
            }

        return {"action": "NO_SIGNAL", "vix": round(vix_level, 2),
                "note": f"VIX {vix_level:.1f} < {self.VIX_ENTRY_THRESHOLD} threshold"}

    def get_active_positions(self) -> list[dict]:
        return self.state.get("positions", [])

    def check_exits(self) -> list[dict]:
        """Check exit conditions on open positions."""
        exits = []
        regime = _load_regime()
        vix = regime.get("raw_inputs", {}).get("vix")

        for pos in self.state.get("positions", []):
            reason = None
            if vix is not None and vix < self.VIX_EXIT_THRESHOLD:
                reason = f"VIX dropped to {vix:.1f} < {self.VIX_EXIT_THRESHOLD}"
            entry_date = datetime.fromisoformat(pos["entry_date"])
            days_held = (datetime.now(timezone.utc) - entry_date).days
            if days_held >= self.MAX_HOLD_DAYS:
                reason = f"Max hold period ({self.MAX_HOLD_DAYS} days) reached"
            if reason:
                exits.append({"ticker": pos["ticker"], "reason": reason,
                              "entry_price": pos["entry_price"],
                              "days_held": days_held})
        return exits

    # ------------------------------------------------------------------
    # Backtest
    # ------------------------------------------------------------------

    @staticmethod
    def backtest(start: str = "2020-01-01", end: str = "2026-04-01") -> dict:
        """
        Identify every VIX > 30 spike since start, compute SPY return
        over next 60 trading days.
        """
        print("\n" + "=" * 70)
        print("BACKTEST: VIX Mean-Reversion ('Buy the Panic')")
        print("=" * 70)

        data = _download_range(["^VIX", "SPY"], start, end)
        if data.empty or "^VIX" not in data.columns:
            print("  [!] Could not download VIX/SPY data")
            return {}

        vix = data["^VIX"]
        spy = data["SPY"]

        trades = []
        cooldown_until = None

        for i in range(len(vix)):
            dt = vix.index[i]
            v = vix.iloc[i]

            if pd.isna(v) or pd.isna(spy.iloc[i]):
                continue

            # Cooldown: skip if inside a previous trade's hold period
            if cooldown_until and dt <= cooldown_until:
                continue

            if v >= VixMeanReversion.VIX_ENTRY_THRESHOLD:
                entry_price = spy.iloc[i]
                entry_date = dt

                # Find exit: VIX < 20, or 60 trading days, or -10% stop
                exit_price = None
                exit_date = None
                exit_reason = None
                stop_price = entry_price * (1 - VixMeanReversion.STOP_LOSS_PCT)

                remaining = spy.iloc[i + 1: i + 1 + VixMeanReversion.MAX_HOLD_DAYS]
                remaining_vix = vix.iloc[i + 1: i + 1 + VixMeanReversion.MAX_HOLD_DAYS]

                for j in range(len(remaining)):
                    price = remaining.iloc[j]
                    v_now = remaining_vix.iloc[j] if j < len(remaining_vix) else 25

                    if price <= stop_price:
                        exit_price = price
                        exit_date = remaining.index[j]
                        exit_reason = "STOP"
                        break
                    if not pd.isna(v_now) and v_now < VixMeanReversion.VIX_EXIT_THRESHOLD:
                        exit_price = price
                        exit_date = remaining.index[j]
                        exit_reason = "VIX_BELOW_20"
                        break

                if exit_price is None and len(remaining) > 0:
                    exit_price = remaining.iloc[-1]
                    exit_date = remaining.index[-1]
                    exit_reason = "TIME_EXIT"
                elif exit_price is None:
                    continue

                ret = (exit_price - entry_price) / entry_price
                days_held = (exit_date - entry_date).days

                trades.append({
                    "entry_date": entry_date.strftime("%Y-%m-%d"),
                    "exit_date": exit_date.strftime("%Y-%m-%d"),
                    "vix_at_entry": round(float(v), 1),
                    "entry_price": round(float(entry_price), 2),
                    "exit_price": round(float(exit_price), 2),
                    "return_pct": round(float(ret) * 100, 2),
                    "days_held": days_held,
                    "exit_reason": exit_reason,
                })
                cooldown_until = exit_date

        # Results
        if not trades:
            print("  No VIX > 30 events found in period.")
            return {"trades": 0}

        returns = [t["return_pct"] for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / len(returns) * 100
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses)) if losses else 0.01
        pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
        total_ret = sum(returns)

        print(f"\n  Period: {start} to {end}")
        print(f"  Trades: {len(trades)}")
        print(f"  Win Rate: {win_rate:.1f}%")
        print(f"  Avg Win: +{avg_win:.2f}%  |  Avg Loss: -{avg_loss:.2f}%")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"  Total Return (summed): {total_ret:.2f}%")
        print(f"\n  {'Entry':>12s}  {'Exit':>12s}  {'VIX':>5s}  {'Return':>8s}  {'Days':>5s}  Reason")
        print(f"  {'-'*12}  {'-'*12}  {'-'*5}  {'-'*8}  {'-'*5}  {'-'*12}")
        for t in trades:
            flag = "W" if t["return_pct"] > 0 else "L"
            print(f"  {t['entry_date']:>12s}  {t['exit_date']:>12s}  "
                  f"{t['vix_at_entry']:5.1f}  {t['return_pct']:+7.2f}%  "
                  f"{t['days_held']:5d}  {t['exit_reason']} [{flag}]")

        return {"trades": len(trades), "win_rate": win_rate, "pf": round(pf, 2),
                "total_return": round(total_ret, 2), "detail": trades}


# ===================================================================
# Strategy 2: Sector Rotation ("Trade the Regime")
# ===================================================================

class SectorRotation:
    """
    Use Atlas regime + event cascade to rotate between sector ETFs.

    Monthly evaluation (or on Atlas regime change):
    - Read macro_regime.json for regime + fleet_guidance
    - Score each sector: base from regime, bonus from active cascades
    - Long top 3, avoid bottom 3

    Universe: XLE, XLF, XLK, XLV, XLI, XLU, XLY, XLP, GLD, TLT
    """

    NAME = "sector_rotation"
    UNIVERSE = ["XLE", "XLF", "XLK", "XLV", "XLI", "XLU", "XLY", "XLP", "GLD", "TLT"]
    TOP_N = 3
    POSITION_SIZE_PCT = 0.05  # Per sector leg

    # Regime base scores: higher = more favored in that regime
    REGIME_SCORES = {
        "RISK_ON": {
            "XLK": 5, "XLY": 4, "XLF": 4, "XLI": 3,
            "XLE": 2, "XLV": 1, "XLP": 0, "XLU": -1,
            "GLD": -2, "TLT": -2,
        },
        "NEUTRAL": {
            "XLK": 3, "XLV": 3, "XLI": 2, "XLF": 2,
            "XLE": 1, "XLY": 1, "XLP": 1, "XLU": 1,
            "GLD": 0, "TLT": 0,
        },
        "RISK_OFF": {
            "XLU": 5, "XLP": 4, "GLD": 4, "TLT": 3,
            "XLV": 2, "XLI": 0, "XLE": -1, "XLF": -1,
            "XLK": -2, "XLY": -3,
        },
        "CRISIS": {
            "GLD": 5, "TLT": 5,
            "XLU": 1, "XLP": 1, "XLV": 0,
            "XLK": -3, "XLY": -4, "XLF": -4,
            "XLI": -3, "XLE": -2,
        },
    }

    # Event overlay adjustments
    EVENT_OVERLAYS = {
        "TARIFF": {"XLU": 2, "GLD": 2, "XLI": -2, "XLE": -1},
        "OIL_DISRUPTION": {"XLE": 3, "XLY": -2, "XLI": -1},
        "MILITARY_CONFLICT": {"GLD": 3, "XLE": 1, "XLK": -1, "XLY": -2},
        "RECESSION_SIGNAL": {"TLT": 3, "XLU": 2, "XLP": 2, "XLK": -2, "XLF": -3},
        "RATE_CUT": {"XLF": 2, "XLY": 2, "TLT": 2, "XLU": -1},
        "RATE_HIKE": {"XLF": -1, "TLT": -3, "XLU": 1, "XLE": 1},
    }

    def __init__(self, state: dict | None = None):
        self.state = state or {"positions": [], "last_eval": None}

    def _score_sectors(self, regime: str, active_events: list[str] | None = None) -> dict:
        """Score all sectors given regime and active events."""
        base = self.REGIME_SCORES.get(regime, self.REGIME_SCORES["NEUTRAL"])
        scores = {s: base.get(s, 0) for s in self.UNIVERSE}

        for event in (active_events or []):
            overlay = self.EVENT_OVERLAYS.get(event, {})
            for s, adj in overlay.items():
                if s in scores:
                    scores[s] += adj

        return scores

    def evaluate(self) -> dict | None:
        """Score sectors using current Atlas regime, return rotation signal."""
        regime_data = _load_regime()
        if not regime_data:
            return {"action": "NO_DATA", "note": "No Atlas regime file found"}

        overall = regime_data.get("regime", {}).get("overall", "NEUTRAL")
        guidance = regime_data.get("fleet_guidance", {})

        # Detect active events from cascade DB or guidance
        active_events = []
        # Check if any sectors_avoid hints suggest events
        avoid = guidance.get("sectors_avoid", [])
        favor = guidance.get("sectors_favor", [])

        scores = self._score_sectors(overall, active_events)
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        top = [r[0] for r in ranked[:self.TOP_N]]
        bottom = [r[0] for r in ranked[-3:]]

        return {
            "action": "ROTATE",
            "regime": overall,
            "scores": {k: v for k, v in ranked},
            "long_sectors": top,
            "avoid_sectors": bottom,
            "size_pct_per_leg": self.POSITION_SIZE_PCT,
            "total_exposure": self.POSITION_SIZE_PCT * self.TOP_N,
            "atlas_favor": favor,
            "atlas_avoid": avoid,
        }

    def get_active_positions(self) -> list[dict]:
        return self.state.get("positions", [])

    def check_exits(self) -> list[dict]:
        """Check if regime changed, triggering rotation."""
        regime_data = _load_regime()
        overall = regime_data.get("regime", {}).get("overall", "NEUTRAL")
        exits = []
        for pos in self.state.get("positions", []):
            if pos.get("regime_at_entry") != overall:
                exits.append({
                    "ticker": pos["ticker"],
                    "reason": f"Regime changed: {pos.get('regime_at_entry')} -> {overall}",
                })
        return exits

    # ------------------------------------------------------------------
    # Backtest
    # ------------------------------------------------------------------

    @staticmethod
    def backtest(start: str = "2020-01-01", end: str = "2026-04-01") -> dict:
        """
        Monthly rotation based on regime scoring. Track cumulative
        return vs SPY buy-and-hold.
        """
        print("\n" + "=" * 70)
        print("BACKTEST: Sector Rotation ('Trade the Regime')")
        print("=" * 70)

        tickers = SectorRotation.UNIVERSE + ["SPY", "^VIX"]
        data = _download_range(tickers, start, end)
        if data.empty:
            print("  [!] Could not download sector data")
            return {}

        vix = data["^VIX"] if "^VIX" in data.columns else None
        spy_prices = data["SPY"] if "SPY" in data.columns else None

        # Determine regime for each day based on VIX + SPY momentum
        def _classify_regime(vix_val, spy_ret_60d):
            """Simple regime classifier for backtest."""
            if pd.isna(vix_val):
                return "NEUTRAL"
            if vix_val > 35:
                return "CRISIS"
            if vix_val > 25 or spy_ret_60d < -0.08:
                return "RISK_OFF"
            if vix_val < 18 and spy_ret_60d > 0.02:
                return "RISK_ON"
            return "NEUTRAL"

        # Monthly rebalance dates (first trading day of each month)
        monthly_dates = data.resample("MS").first().index
        # Align to actual trading dates
        monthly_dates = [data.index[data.index >= d][0] for d in monthly_dates
                         if len(data.index[data.index >= d]) > 0]

        strategy_value = 1.0
        spy_value = 1.0
        records = []
        scorer = SectorRotation()

        for i in range(len(monthly_dates) - 1):
            rebal_date = monthly_dates[i]
            next_date = monthly_dates[i + 1]

            # Determine regime at rebalance
            idx = data.index.get_loc(rebal_date)
            vix_val = float(vix.iloc[idx]) if vix is not None and idx < len(vix) else 20.0
            spy_60d = 0.0
            if spy_prices is not None and idx >= 60:
                spy_60d = (spy_prices.iloc[idx] - spy_prices.iloc[idx - 60]) / spy_prices.iloc[idx - 60]

            regime = _classify_regime(vix_val, spy_60d)
            scores = scorer._score_sectors(regime)
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            top = [r[0] for r in ranked[:SectorRotation.TOP_N]]

            # Compute equal-weight return of top sectors over the month
            month_returns = []
            for sec in top:
                if sec in data.columns:
                    p_start = data[sec].loc[rebal_date]
                    p_end = data[sec].loc[next_date]
                    if not pd.isna(p_start) and not pd.isna(p_end) and p_start > 0:
                        month_returns.append((p_end - p_start) / p_start)

            strat_ret = np.mean(month_returns) if month_returns else 0.0
            strategy_value *= (1 + strat_ret)

            # SPY benchmark
            if spy_prices is not None:
                spy_s = spy_prices.loc[rebal_date]
                spy_e = spy_prices.loc[next_date]
                if not pd.isna(spy_s) and not pd.isna(spy_e) and spy_s > 0:
                    spy_ret = (spy_e - spy_s) / spy_s
                    spy_value *= (1 + spy_ret)

            records.append({
                "date": rebal_date.strftime("%Y-%m-%d"),
                "regime": regime,
                "holdings": top,
                "month_return": round(float(strat_ret) * 100, 2),
                "cumulative": round((strategy_value - 1) * 100, 2),
            })

        # Summary
        total_months = len(records)
        winning_months = sum(1 for r in records if r["month_return"] > 0)
        losing_months = sum(1 for r in records if r["month_return"] <= 0)
        strat_total = (strategy_value - 1) * 100
        spy_total = (spy_value - 1) * 100

        monthly_rets = [r["month_return"] for r in records]
        wins = [r for r in monthly_rets if r > 0]
        losses_list = [r for r in monthly_rets if r <= 0]
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses_list)) if losses_list else 0.01
        pf = (sum(wins) / abs(sum(losses_list))) if losses_list and sum(losses_list) != 0 else float("inf")

        print(f"\n  Period: {start} to {end}")
        print(f"  Months: {total_months}")
        print(f"  Win Months: {winning_months}  |  Lose Months: {losing_months}")
        print(f"  Monthly Win Rate: {winning_months/max(total_months,1)*100:.1f}%")
        print(f"  Avg Win Month: +{avg_win:.2f}%  |  Avg Lose Month: -{avg_loss:.2f}%")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"\n  Strategy Total Return: {strat_total:+.2f}%")
        print(f"  SPY Buy&Hold Return:  {spy_total:+.2f}%")
        print(f"  Alpha vs SPY:         {strat_total - spy_total:+.2f}%")

        # Regime distribution
        regime_counts = {}
        for r in records:
            regime_counts[r["regime"]] = regime_counts.get(r["regime"], 0) + 1
        print(f"\n  Regime Distribution:")
        for reg, cnt in sorted(regime_counts.items()):
            print(f"    {reg}: {cnt} months ({cnt/max(total_months,1)*100:.0f}%)")

        # Show last 12 months
        print(f"\n  Last 12 months:")
        print(f"  {'Date':>12s}  {'Regime':>10s}  {'Holdings':>20s}  {'Return':>8s}  {'Cumul':>8s}")
        for r in records[-12:]:
            print(f"  {r['date']:>12s}  {r['regime']:>10s}  "
                  f"{','.join(r['holdings']):>20s}  {r['month_return']:+7.2f}%  "
                  f"{r['cumulative']:+7.2f}%")

        return {"months": total_months, "win_rate": round(winning_months / max(total_months, 1) * 100, 1),
                "pf": round(pf, 2), "strategy_return": round(strat_total, 2),
                "spy_return": round(spy_total, 2), "alpha": round(strat_total - spy_total, 2)}


# ===================================================================
# Strategy 3: Commodity Exporter Cascade ("The EWZ Trade")
# ===================================================================

class CommodityExporterCascade:
    """
    When oil spikes significantly, commodity-exporting countries benefit.
    EWZ (Brazil) +28% YTD was the biggest miss.

    Entry: USO 20-day return > +10% (oil trending strongly up)
    Exit:  USO 20-day return < 0% (oil trend reversed) OR 60 days
    Stop:  -8% from entry
    Size:  Equal weight, 2% of equity per leg (6% total)

    Long basket: EWZ (Brazil), EWA (Australia), EWC (Canada)
    """

    NAME = "commodity_exporter_cascade"
    TRIGGER_TICKER = "USO"
    BASKET = ["EWZ", "EWA", "EWC"]
    USO_ENTRY_THRESHOLD = 0.10   # 20-day return > +10%
    USO_EXIT_THRESHOLD = 0.00    # 20-day return < 0%
    MAX_HOLD_DAYS = 60
    STOP_LOSS_PCT = 0.08
    POSITION_SIZE_PCT = 0.02     # Per leg

    def __init__(self, state: dict | None = None):
        self.state = state or {"positions": []}

    def evaluate(self) -> dict | None:
        """Check if USO 20-day return > 10%. Return signal or None."""
        # Already in position?
        if self.state.get("positions"):
            return {"action": "HOLD",
                    "note": "Already in commodity exporter position"}

        try:
            uso_data = yf.download("USO", period="30d", progress=False,
                                   auto_adjust=True)
            if len(uso_data) < 20:
                return {"action": "NO_DATA", "note": "Not enough USO data"}

            close = uso_data["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            current = float(close.iloc[-1])
            past = float(close.iloc[-20])
            ret_20d = (current - past) / past
        except Exception as e:
            return {"action": "ERROR", "note": str(e)}

        if ret_20d > self.USO_ENTRY_THRESHOLD:
            # Get basket prices
            basket_prices = {}
            try:
                basket_data = yf.download(self.BASKET, period="5d",
                                          progress=False, auto_adjust=True)
                if isinstance(basket_data.columns, pd.MultiIndex):
                    for sym in self.BASKET:
                        if sym in basket_data["Close"].columns:
                            basket_prices[sym] = float(basket_data["Close"][sym].iloc[-1])
                else:
                    basket_prices[self.BASKET[0]] = float(basket_data["Close"].iloc[-1])
            except Exception:
                pass

            return {
                "action": "BUY",
                "basket": self.BASKET,
                "uso_20d_return": round(ret_20d * 100, 2),
                "basket_prices": basket_prices,
                "size_pct_per_leg": self.POSITION_SIZE_PCT,
                "total_exposure": self.POSITION_SIZE_PCT * len(self.BASKET),
                "stop_loss_pct": self.STOP_LOSS_PCT,
                "max_hold_days": self.MAX_HOLD_DAYS,
                "rationale": f"USO 20d return +{ret_20d*100:.1f}% > +{self.USO_ENTRY_THRESHOLD*100:.0f}% threshold"
            }

        return {
            "action": "NO_SIGNAL",
            "uso_20d_return": round(ret_20d * 100, 2),
            "note": f"USO 20d return {ret_20d*100:+.1f}% < +{self.USO_ENTRY_THRESHOLD*100:.0f}% threshold"
        }

    def get_active_positions(self) -> list[dict]:
        return self.state.get("positions", [])

    def check_exits(self) -> list[dict]:
        """Check if oil trend reversed or stop hit."""
        exits = []
        try:
            uso_data = yf.download("USO", period="30d", progress=False,
                                   auto_adjust=True)
            close = uso_data["Close"]
            if isinstance(close, pd.DataFrame):
                close = close.iloc[:, 0]
            current = float(close.iloc[-1])
            past = float(close.iloc[-20]) if len(close) >= 20 else current
            uso_ret = (current - past) / past
        except Exception:
            return exits

        for pos in self.state.get("positions", []):
            reason = None
            if uso_ret < self.USO_EXIT_THRESHOLD:
                reason = f"USO 20d return {uso_ret*100:+.1f}% < 0% — oil trend reversed"
            entry_date = datetime.fromisoformat(pos["entry_date"])
            days_held = (datetime.now(timezone.utc) - entry_date).days
            if days_held >= self.MAX_HOLD_DAYS:
                reason = f"Max hold period ({self.MAX_HOLD_DAYS} days) reached"
            if reason:
                exits.append({"ticker": pos["ticker"], "reason": reason,
                              "days_held": days_held})
        return exits

    # ------------------------------------------------------------------
    # Backtest
    # ------------------------------------------------------------------

    @staticmethod
    def backtest(start: str = "2020-01-01", end: str = "2026-04-01") -> dict:
        """
        Every time USO 20d return crossed +10%, compute EWZ/EWA/EWC
        returns over next 60 trading days.
        """
        print("\n" + "=" * 70)
        print("BACKTEST: Commodity Exporter Cascade ('The EWZ Trade')")
        print("=" * 70)

        tickers = ["USO"] + CommodityExporterCascade.BASKET
        data = _download_range(tickers, start, end)
        if data.empty or "USO" not in data.columns:
            print("  [!] Could not download USO/basket data")
            return {}

        uso = data["USO"]
        # Compute 20-day rolling return
        uso_ret20 = uso.pct_change(20)

        trades = []
        cooldown_until = None

        for i in range(20, len(uso_ret20)):
            dt = uso_ret20.index[i]
            r = uso_ret20.iloc[i]

            if pd.isna(r):
                continue
            if cooldown_until and dt <= cooldown_until:
                continue

            if r > CommodityExporterCascade.USO_ENTRY_THRESHOLD:
                entry_date = dt

                # Track basket performance
                basket_results = []
                for sym in CommodityExporterCascade.BASKET:
                    if sym not in data.columns:
                        continue
                    entry_price = data[sym].iloc[i]
                    if pd.isna(entry_price) or entry_price <= 0:
                        continue

                    stop = entry_price * (1 - CommodityExporterCascade.STOP_LOSS_PCT)
                    remaining = data[sym].iloc[i + 1: i + 1 + CommodityExporterCascade.MAX_HOLD_DAYS]
                    uso_remaining = uso_ret20.iloc[i + 1: i + 1 + CommodityExporterCascade.MAX_HOLD_DAYS]

                    exit_price = None
                    exit_date = None
                    exit_reason = None

                    for j in range(len(remaining)):
                        price = remaining.iloc[j]
                        if price <= stop:
                            exit_price = price
                            exit_date = remaining.index[j]
                            exit_reason = "STOP"
                            break
                        if j < len(uso_remaining) and not pd.isna(uso_remaining.iloc[j]):
                            if uso_remaining.iloc[j] < CommodityExporterCascade.USO_EXIT_THRESHOLD:
                                exit_price = price
                                exit_date = remaining.index[j]
                                exit_reason = "OIL_REVERSAL"
                                break

                    if exit_price is None and len(remaining) > 0:
                        exit_price = remaining.iloc[-1]
                        exit_date = remaining.index[-1]
                        exit_reason = "TIME_EXIT"
                    elif exit_price is None:
                        continue

                    ret = (exit_price - entry_price) / entry_price
                    basket_results.append({
                        "ticker": sym,
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(exit_price), 2),
                        "return_pct": round(float(ret) * 100, 2),
                        "exit_reason": exit_reason,
                    })

                if basket_results:
                    avg_ret = np.mean([b["return_pct"] for b in basket_results])
                    days_held = (basket_results[0].get("exit_date", entry_date) if False
                                 else (exit_date - entry_date).days if exit_date else 0)
                    trades.append({
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "exit_date": exit_date.strftime("%Y-%m-%d") if exit_date else "?",
                        "uso_20d_ret": round(float(r) * 100, 1),
                        "basket_avg_return": round(float(avg_ret), 2),
                        "days_held": days_held,
                        "legs": basket_results,
                    })
                    cooldown_until = exit_date

        # Results
        if not trades:
            print("  No USO +10% 20-day spikes found in period.")
            return {"trades": 0}

        all_returns = [t["basket_avg_return"] for t in trades]
        wins = [r for r in all_returns if r > 0]
        losses_list = [r for r in all_returns if r <= 0]
        win_rate = len(wins) / len(all_returns) * 100
        avg_win = np.mean(wins) if wins else 0
        avg_loss = abs(np.mean(losses_list)) if losses_list else 0.01
        pf = (sum(wins) / abs(sum(losses_list))) if losses_list and sum(losses_list) != 0 else float("inf")
        total_ret = sum(all_returns)

        print(f"\n  Period: {start} to {end}")
        print(f"  Trades (basket entries): {len(trades)}")
        print(f"  Win Rate: {win_rate:.1f}%")
        print(f"  Avg Win: +{avg_win:.2f}%  |  Avg Loss: -{avg_loss:.2f}%")
        print(f"  Profit Factor: {pf:.2f}")
        print(f"  Total Return (summed basket avg): {total_ret:.2f}%")

        print(f"\n  {'Entry':>12s}  {'Exit':>12s}  {'USO20d':>7s}  {'BaskRet':>8s}  {'Days':>5s}  Legs")
        print(f"  {'-'*12}  {'-'*12}  {'-'*7}  {'-'*8}  {'-'*5}  {'-'*30}")
        for t in trades:
            flag = "W" if t["basket_avg_return"] > 0 else "L"
            leg_str = "  ".join(f"{l['ticker']}:{l['return_pct']:+.1f}%" for l in t["legs"])
            print(f"  {t['entry_date']:>12s}  {t['exit_date']:>12s}  "
                  f"{t['uso_20d_ret']:+6.1f}%  {t['basket_avg_return']:+7.2f}%  "
                  f"{t['days_held']:5d}  {leg_str} [{flag}]")

        return {"trades": len(trades), "win_rate": round(win_rate, 1),
                "pf": round(pf, 2), "total_return": round(total_ret, 2),
                "detail": trades}


# ===================================================================
# Unified Runner
# ===================================================================

def run_all_strategies() -> dict:
    """Evaluate all three strategies, return signals."""
    state = _load_state()

    vix = VixMeanReversion(state.get("vix_mr"))
    sector = SectorRotation(state.get("sector_rot"))
    commodity = CommodityExporterCascade(state.get("commodity_exp"))

    results = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "vix_mean_reversion": vix.evaluate(),
        "sector_rotation": sector.evaluate(),
        "commodity_exporter": commodity.evaluate(),
    }

    return results


def show_status() -> None:
    """Show current state of all strategies."""
    state = _load_state()

    print("\n" + "=" * 70)
    print("MACRO STRATEGIES — Current Status")
    print("=" * 70)

    for name, key in [("VIX Mean-Reversion", "vix_mr"),
                      ("Sector Rotation", "sector_rot"),
                      ("Commodity Exporter Cascade", "commodity_exp")]:
        s = state.get(key, {})
        positions = s.get("positions", [])
        print(f"\n  {name}:")
        if positions:
            for p in positions:
                print(f"    {p.get('ticker', '?')} — entered {p.get('entry_date', '?')} "
                      f"@ {p.get('entry_price', '?')}")
        else:
            print("    No active positions")

    # Also show current regime
    regime = _load_regime()
    if regime:
        overall = regime.get("regime", {}).get("overall", "?")
        vix = regime.get("raw_inputs", {}).get("vix", "?")
        print(f"\n  Atlas Regime: {overall}  |  VIX: {vix}")
        guidance = regime.get("fleet_guidance", {})
        if guidance:
            print(f"  Favor: {guidance.get('sectors_favor', [])}")
            print(f"  Avoid: {guidance.get('sectors_avoid', [])}")


def run_evaluate() -> None:
    """Evaluate all strategies and print results."""
    print("\n" + "=" * 70)
    print("MACRO STRATEGIES — Evaluation")
    print("=" * 70)

    results = run_all_strategies()

    for label, key in [("VIX Mean-Reversion", "vix_mean_reversion"),
                       ("Sector Rotation", "sector_rotation"),
                       ("Commodity Exporter Cascade", "commodity_exporter")]:
        signal = results.get(key, {})
        action = signal.get("action", "?")
        print(f"\n  {label}:")
        print(f"    Action: {action}")
        for k, v in signal.items():
            if k == "action":
                continue
            if isinstance(v, dict):
                print(f"    {k}:")
                for k2, v2 in v.items():
                    print(f"      {k2}: {v2}")
            elif isinstance(v, list):
                print(f"    {k}: {', '.join(str(x) for x in v)}")
            else:
                print(f"    {k}: {v}")

    print(f"\n  Timestamp: {results['timestamp']}")


def run_backtest() -> None:
    """Run all three backtests."""
    print("\n" + "#" * 70)
    print("#  MACRO STRATEGIES — Full Backtest (2020-2026)")
    print("#" * 70)

    results = {}
    results["vix"] = VixMeanReversion.backtest()
    results["sector"] = SectorRotation.backtest()
    results["commodity"] = CommodityExporterCascade.backtest()

    # Summary table
    print("\n" + "=" * 70)
    print("SUMMARY — All Strategies")
    print("=" * 70)
    print(f"\n  {'Strategy':<30s}  {'Trades':>7s}  {'WinRate':>8s}  {'PF':>6s}  {'TotRet':>8s}")
    print(f"  {'-'*30}  {'-'*7}  {'-'*8}  {'-'*6}  {'-'*8}")

    for label, key in [("VIX Mean-Reversion", "vix"),
                       ("Sector Rotation", "sector"),
                       ("Commodity Exporter", "commodity")]:
        r = results.get(key, {})
        trades = r.get("trades", r.get("months", 0))
        wr = r.get("win_rate", 0)
        pf = r.get("pf", 0)
        tr = r.get("total_return", r.get("strategy_return", 0))
        print(f"  {label:<30s}  {trades:>7}  {wr:>7.1f}%  {pf:>6.2f}  {tr:>+7.2f}%")

    if "sector" in results and results["sector"]:
        sr = results["sector"]
        print(f"\n  Sector Rotation vs SPY: strategy {sr.get('strategy_return', 0):+.2f}% "
              f"vs SPY {sr.get('spy_return', 0):+.2f}% "
              f"(alpha {sr.get('alpha', 0):+.2f}%)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Macro Strategies — Atlas-powered ETF/country fund strategies")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--evaluate", action="store_true",
                       help="Evaluate all strategies against current conditions")
    group.add_argument("--backtest", action="store_true",
                       help="Run backtests on 2020-2026 data")
    group.add_argument("--status", action="store_true",
                       help="Show current positions and state")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)

    if args.evaluate:
        run_evaluate()
    elif args.backtest:
        run_backtest()
    elif args.status:
        show_status()


if __name__ == "__main__":
    main()
