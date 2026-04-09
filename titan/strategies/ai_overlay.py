"""Titan AI Overlay — Adaptive ensemble for swing trade signal filtering.

Ported from Argus FX overlay, adapted for daily/4H timeframes and stock-specific conditions.
15 voters score each signal [-1, +1]. Weights learn from trade outcomes.

Architecture:
    SwingEngine signal -> 15 Voters each score [-1, +1] -> Weighted consensus -> GO / NO-GO
    After trade closes: update weights based on which voters were right
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

_log = logging.getLogger("titan.ai")


@dataclass
class SwingMarketState:
    """Snapshot of current market conditions for voter evaluation."""
    price: float = 0.0
    atr_pct: float = 0.0            # ATR as % of price
    rsi_daily: float = 50.0
    rsi_4h: float = 50.0
    volume_ratio: float = 1.0       # current vol / 20d avg
    bb_pctile: float = 50.0         # BB width percentile (0=tight, 100=wide)
    dist_from_ema50_pct: float = 0.0  # distance from EMA50 as %
    dist_from_52w_high_pct: float = 0.0
    ema_aligned: bool = False        # EMA8 > EMA21 > EMA50
    above_ema200: bool = False
    adx: float = 0.0
    day_of_week: int = 2            # 0=Mon
    signal_strength: int = 0        # swing engine score 0-100
    strategy_type: str = ""         # TREND_FOLLOW, BREAKOUT, etc.
    sector: str = ""                # gold, semi, fintech, etc.
    recent_win_rate: float = 0.5
    consecutive_losses: int = 0
    return_5d: float = 0.0          # 5-day price return %
    return_20d: float = 0.0         # 20-day price return %
    narrow_range_days: int = 0


@dataclass
class VoterResult:
    name: str
    score: float
    reason: str
    weight: float


@dataclass
class OverlayDecision:
    action: str              # "TAKE" or "SKIP"
    consensus_score: float   # -1 to +1
    confidence: float        # 0-1
    votes: list[VoterResult]
    reason: str
    voters_for: int
    voters_against: int


class SwingOverlay:
    """Adaptive ensemble for swing trade filtering."""

    def __init__(
        self,
        symbol: str = "FLEET",
        *,
        consensus_threshold: float = 0.10,
        learning_rate: float = 0.08,
        memory_trades: int = 50,
        state_dir: Optional[Path] = None,
    ):
        self.symbol = symbol
        self.consensus_threshold = consensus_threshold
        self.learning_rate = learning_rate
        self.memory_trades = memory_trades
        self._log = logging.getLogger(f"titan.ai.{symbol.lower()}")

        self._state_dir = state_dir or Path("titan/logs")
        self._state_file = self._state_dir / f"ai_overlay_{symbol.lower()}.json"

        self._voters = self._build_voters()
        self._weights = {name: 1.0 for name in self._voters}
        self._trade_history: list[dict] = []
        self._pending_scores: Optional[dict] = None

        self._load_state()

    def _build_voters(self) -> dict:
        return {
            "trend_strength": self._vote_trend_strength,
            "adx_confirmation": self._vote_adx,
            "rsi_zone": self._vote_rsi_zone,
            "volume_confirm": self._vote_volume,
            "bb_squeeze": self._vote_bb_squeeze,
            "ema_structure": self._vote_ema_structure,
            "above_200": self._vote_above_200,
            "momentum_5d": self._vote_momentum_5d,
            "momentum_20d": self._vote_momentum_20d,
            "overextension": self._vote_overextension,
            "signal_quality": self._vote_signal_quality,
            "day_of_week": self._vote_day_of_week,
            "recent_performance": self._vote_recent_perf,
            "loss_streak_guard": self._vote_loss_streak,
            "compression": self._vote_compression,
        }

    def evaluate(self, direction: str, state: SwingMarketState) -> OverlayDecision:
        votes = []
        weighted_sum = 0.0
        total_weight = 0.0

        for name, voter_fn in self._voters.items():
            try:
                score, reason = voter_fn(direction, state)
                score = max(-1.0, min(1.0, score))
            except Exception as e:
                score, reason = 0.0, f"error: {e}"

            w = self._weights.get(name, 1.0)
            votes.append(VoterResult(name=name, score=score, reason=reason, weight=w))
            weighted_sum += score * w
            total_weight += abs(w)

        consensus = weighted_sum / total_weight if total_weight > 0 else 0.0
        confidence = abs(consensus)
        voters_for = sum(1 for v in votes if v.score > 0.1)
        voters_against = sum(1 for v in votes if v.score < -0.1)

        action = "TAKE" if consensus >= self.consensus_threshold else "SKIP"
        self._pending_scores = {v.name: v.score for v in votes}

        decision = OverlayDecision(
            action=action,
            consensus_score=round(consensus, 3),
            confidence=round(confidence, 3),
            votes=votes,
            reason=self._build_reason(votes),
            voters_for=voters_for,
            voters_against=voters_against,
        )

        self._log.info(
            f"AI {action}: consensus={consensus:+.2f} "
            f"for={voters_for} against={voters_against} | {decision.reason}"
        )
        return decision

    def learn(self, pnl_pct: float) -> None:
        if self._pending_scores is None:
            return

        self._trade_history.append({
            "pnl": pnl_pct,
            "scores": dict(self._pending_scores),
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        if len(self._trade_history) > self.memory_trades:
            self._trade_history = self._trade_history[-self.memory_trades:]

        outcome = 1.0 if pnl_pct > 0 else -1.0
        for name, score in self._pending_scores.items():
            alignment = score * outcome
            old_w = self._weights.get(name, 1.0)
            new_w = old_w * (1.0 + self.learning_rate * alignment)
            self._weights[name] = max(0.1, min(5.0, new_w))

        self._pending_scores = None
        self._save_state()

    # ══════════════════════════════════════════════════════════
    # VOTER IMPLEMENTATIONS (tuned for swing/daily timeframe)
    # ══════════════════════════════════════════════════════════

    def _vote_trend_strength(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """EMA alignment = strong trend confirmation."""
        if s.ema_aligned:
            if direction == "long":
                return 0.8, "EMAs bullish aligned (8>21>50)"
            return -0.5, "EMAs bullish — against short"
        return 0.0, "EMAs mixed"

    def _vote_adx(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """ADX > 25 = trending market, good for trend following."""
        if s.adx > 30:
            return 0.8, f"Strong trend ADX={s.adx:.0f}"
        elif s.adx > 20:
            return 0.3, f"Moderate trend ADX={s.adx:.0f}"
        elif s.adx < 15:
            return -0.4, f"No trend ADX={s.adx:.0f}"
        return 0.0, f"ADX={s.adx:.0f}"

    def _vote_rsi_zone(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """For longs: RSI 35-55 ideal (pullback). For shorts: 45-65."""
        rsi = s.rsi_daily
        if direction == "long":
            if 35 <= rsi <= 55:
                return 0.8, f"RSI {rsi:.0f} pullback zone"
            elif rsi > 75:
                return -0.8, f"RSI {rsi:.0f} overbought"
            elif rsi < 25:
                return -0.3, f"RSI {rsi:.0f} falling knife risk"
            return 0.0, f"RSI {rsi:.0f}"
        else:
            if 45 <= rsi <= 65:
                return 0.8, f"RSI {rsi:.0f} rally zone for short"
            elif rsi < 25:
                return -0.8, f"RSI {rsi:.0f} oversold"
            return 0.0, f"RSI {rsi:.0f}"

    def _vote_volume(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Volume confirms the move."""
        vr = s.volume_ratio
        if vr > 2.0:
            return 0.8, f"Volume spike {vr:.1f}x — strong confirmation"
        elif vr > 1.3:
            return 0.4, f"Above-avg volume {vr:.1f}x"
        elif vr < 0.5:
            return -0.5, f"Dead volume {vr:.1f}x — no conviction"
        return 0.0, f"Volume {vr:.1f}x"

    def _vote_bb_squeeze(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Low BB percentile = coiled for breakout."""
        if s.bb_pctile < 10:
            return 0.8, f"Extreme squeeze (BB {s.bb_pctile:.0f}%ile)"
        elif s.bb_pctile < 25:
            return 0.4, f"Squeeze (BB {s.bb_pctile:.0f}%ile)"
        elif s.bb_pctile > 85:
            return -0.3, f"Already expanded (BB {s.bb_pctile:.0f}%ile)"
        return 0.0, f"BB {s.bb_pctile:.0f}%ile"

    def _vote_ema_structure(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Price near EMA50 = good entry zone for trend trades."""
        dist = abs(s.dist_from_ema50_pct)
        if dist < 2:
            return 0.7, f"Near EMA50 ({s.dist_from_ema50_pct:+.1f}%)"
        elif dist < 5:
            return 0.3, f"Close to EMA50 ({s.dist_from_ema50_pct:+.1f}%)"
        elif dist > 10:
            return -0.5, f"Far from EMA50 ({s.dist_from_ema50_pct:+.1f}%)"
        return 0.0, f"EMA50 dist {s.dist_from_ema50_pct:+.1f}%"

    def _vote_above_200(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Above 200 EMA = structural uptrend. Critical for longs."""
        if direction == "long":
            if s.above_ema200:
                return 0.6, "Above EMA200 — structural bull"
            return -0.7, "Below EMA200 — risky long"
        else:
            if not s.above_ema200:
                return 0.6, "Below EMA200 — structural bear"
            return -0.3, "Above EMA200 — fighting the trend"

    def _vote_momentum_5d(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Short-term momentum alignment."""
        if direction == "long":
            if -3 < s.return_5d < 3:
                return 0.3, f"5d flat ({s.return_5d:+.1f}%) — coiling"
            elif s.return_5d > 8:
                return -0.4, f"5d +{s.return_5d:.1f}% — chasing"
            elif s.return_5d < -8:
                return -0.3, f"5d {s.return_5d:+.1f}% — falling"
        else:
            if -3 < s.return_5d < 3:
                return 0.3, f"5d flat ({s.return_5d:+.1f}%)"
            elif s.return_5d < -8:
                return -0.4, f"5d {s.return_5d:+.1f}% — chasing down"
        return 0.0, f"5d {s.return_5d:+.1f}%"

    def _vote_momentum_20d(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Medium-term momentum — confirms the big picture."""
        if direction == "long" and s.return_20d > 5:
            return 0.4, f"20d momentum +{s.return_20d:.1f}%"
        elif direction == "short" and s.return_20d < -5:
            return 0.4, f"20d momentum {s.return_20d:+.1f}%"
        elif direction == "long" and s.return_20d < -10:
            return -0.5, f"20d down {s.return_20d:+.1f}% — counter-trend long"
        return 0.0, f"20d {s.return_20d:+.1f}%"

    def _vote_overextension(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Too far from mean = snap-back risk."""
        if direction == "long" and s.rsi_daily > 78:
            return -0.8, f"RSI {s.rsi_daily:.0f} — overextended"
        if direction == "short" and s.rsi_daily < 22:
            return -0.8, f"RSI {s.rsi_daily:.0f} — oversold extreme"
        return 0.1, "Not overextended"

    def _vote_signal_quality(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Higher swing engine score = higher quality setup."""
        if s.signal_strength >= 80:
            return 0.8, f"Strong signal (score={s.signal_strength})"
        elif s.signal_strength >= 70:
            return 0.4, f"Good signal (score={s.signal_strength})"
        elif s.signal_strength >= 60:
            return 0.0, f"Marginal signal (score={s.signal_strength})"
        return -0.5, f"Weak signal (score={s.signal_strength})"

    def _vote_day_of_week(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Mon-Wed best for entries. Thu-Fri more risky (weekend gap)."""
        d = s.day_of_week
        if d in (0, 1, 2):  # Mon, Tue, Wed
            return 0.3, f"Day {d} — good entry window"
        elif d == 3:  # Thu
            return 0.0, "Thursday — neutral"
        elif d == 4:  # Fri
            return -0.4, "Friday — weekend gap risk"
        return -1.0, "Weekend"

    def _vote_recent_perf(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        wr = s.recent_win_rate
        if wr > 0.6:
            return 0.4, f"Recent WR {wr:.0%} — strategy working"
        elif wr > 0.4:
            return 0.0, f"Recent WR {wr:.0%}"
        return -0.5, f"Recent WR {wr:.0%} — struggling"

    def _vote_loss_streak(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        cl = s.consecutive_losses
        if cl == 0:
            return 0.2, "No losses"
        elif cl <= 2:
            return 0.0, f"{cl} consecutive losses"
        elif cl <= 4:
            return -0.5, f"{cl} losses — cautious"
        return -1.0, f"{cl} losses — stop trading"

    def _vote_compression(self, direction: str, s: SwingMarketState) -> tuple[float, str]:
        """Narrow range days = coiling for a move."""
        if s.narrow_range_days >= 4:
            return 0.7, f"{s.narrow_range_days} narrow days — breakout imminent"
        elif s.narrow_range_days >= 2:
            return 0.3, f"{s.narrow_range_days} narrow days"
        return 0.0, "No compression"

    # ══════════════════════════════════════════════════════════
    # HELPERS
    # ══════════════════════════════════════════════════════════

    def _build_reason(self, votes: list[VoterResult]) -> str:
        sorted_votes = sorted(votes, key=lambda v: v.score * v.weight, reverse=True)
        top_for = [v for v in sorted_votes if v.score > 0.1][:3]
        top_against = [v for v in sorted_votes if v.score < -0.1][-3:]
        parts = []
        if top_for:
            parts.append("FOR: " + ", ".join(f"{v.name}({v.score:+.1f})" for v in top_for))
        if top_against:
            parts.append("AGAINST: " + ", ".join(f"{v.name}({v.score:+.1f})" for v in top_against))
        return " | ".join(parts) if parts else "no strong opinions"

    def get_weights(self) -> dict[str, float]:
        return dict(self._weights)

    def get_trade_history(self) -> list[dict]:
        return list(self._trade_history)

    def get_weight_summary(self) -> dict:
        sorted_w = sorted(self._weights.items(), key=lambda x: x[1], reverse=True)
        return {
            "weights": dict(sorted_w),
            "top_3": {n: round(w, 2) for n, w in sorted_w[:3]},
            "bottom_3": {n: round(w, 2) for n, w in sorted_w[-3:]},
            "trades_in_memory": len(self._trade_history),
        }

    def _save_state(self) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            state = {
                "symbol": self.symbol,
                "weights": self._weights,
                "trade_history": self._trade_history[-self.memory_trades:],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._state_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            self._log.warning(f"Failed to save state: {e}")

    def _load_state(self) -> None:
        if not self._state_file.exists():
            return
        try:
            state = json.loads(self._state_file.read_text())
            if "weights" in state:
                for name, w in state["weights"].items():
                    if name in self._weights:
                        self._weights[name] = float(w)
            if "trade_history" in state:
                self._trade_history = state["trade_history"]
            self._log.info(f"State loaded: {len(self._trade_history)} trades in memory")
        except Exception as e:
            self._log.warning(f"Failed to load state: {e}")
