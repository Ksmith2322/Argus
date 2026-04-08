"""Adaptive AI Overlay — Ensemble of voters with dynamic weight learning.

Each "voter" evaluates a signal from a different angle and casts a score (-1 to +1).
Weights adapt based on which voters recently predicted correctly.

This IS the intelligence layer. It doesn't follow static rules — it learns
which conditions matter RIGHT NOW and adjusts in real-time.

Architecture:
    MTF Signal -> 15 Voters each score [-1, +1] -> Weighted consensus -> GO / NO-GO
    After trade closes: update weights based on which voters were right

Usage:
    overlay = AdaptiveOverlay(symbol="AUDJPY", pip_size=0.01)
    decision = overlay.evaluate(mtf_signal, market_state)
    # Later, after trade closes:
    overlay.learn(trade_pnl)
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

_log = logging.getLogger("argus.ai")


@dataclass
class MarketState:
    """Snapshot of current market conditions for voter evaluation."""
    price: float = 0.0
    atr_14: float = 0.0
    rsi_14: float = 50.0
    spread_pips: float = 1.0
    volume_ratio: float = 1.0     # current vol / avg vol
    dist_from_high_20: float = 0.5
    dist_from_low_20: float = 0.5
    ema_8_slope: float = 0.0      # positive = rising
    ema_21_slope: float = 0.0
    hour: int = 12
    day_of_week: int = 2          # 0=Mon
    bars_since_last_trade: int = 999
    recent_win_rate: float = 0.5  # last N trades WR
    recent_pnl: float = 0.0      # last N trades total PnL
    consecutive_losses: int = 0
    trend_strength_4h: float = 0.0
    rsi_1h: float = 50.0
    confidence_mtf: float = 0.5


@dataclass
class VoterResult:
    """Result from a single voter."""
    name: str
    score: float       # -1 (strong no) to +1 (strong yes)
    reason: str
    weight: float      # current adaptive weight


@dataclass
class OverlayDecision:
    """Final decision from the overlay."""
    action: str             # "TAKE" or "SKIP"
    consensus_score: float  # -1 to +1 weighted average
    confidence: float       # 0-1 how strong the consensus is
    votes: list[VoterResult]
    reason: str
    voters_for: int
    voters_against: int


class AdaptiveOverlay:
    """Adaptive ensemble of voters that learn which conditions predict profitable trades."""

    def __init__(
        self,
        symbol: str,
        pip_size: float = 0.0001,
        *,
        consensus_threshold: float = 0.15,  # minimum weighted score to take trade
        learning_rate: float = 0.1,          # how fast weights adapt
        memory_trades: int = 20,             # how many recent trades to learn from
        state_dir: Optional[Path] = None,
    ):
        self.symbol = symbol
        self.pip_size = pip_size
        self.consensus_threshold = consensus_threshold
        self.learning_rate = learning_rate
        self.memory_trades = memory_trades
        self._log = logging.getLogger(f"ai.{symbol.lower()}")

        # State persistence
        self._state_dir = state_dir or Path(f"argus_flow/logs/{symbol}")
        self._state_file = self._state_dir / "ai_overlay_state.json"

        # Initialize voters with equal weights
        self._voters = self._build_voters()
        self._weights = {name: 1.0 for name in self._voters}

        # Trade memory for learning
        self._trade_history: list[dict] = []  # {pnl, voter_scores, timestamp}
        self._pending_scores: Optional[dict] = None  # scores from current open trade

        # Load persisted state
        self._load_state()

    def _build_voters(self) -> dict:
        """Define all voter functions."""
        return {
            "trend_strength": self._vote_trend_strength,
            "rsi_zone": self._vote_rsi_zone,
            "volume_confirm": self._vote_volume,
            "spread_quality": self._vote_spread,
            "session_quality": self._vote_session,
            "momentum_align": self._vote_momentum,
            "distance_to_level": self._vote_level_proximity,
            "recent_performance": self._vote_recent_perf,
            "loss_streak_guard": self._vote_loss_streak,
            "volatility_regime": self._vote_volatility,
            "time_of_day": self._vote_time_of_day,
            "day_of_week": self._vote_day_of_week,
            "overextension": self._vote_overextension,
            "mtf_confidence": self._vote_mtf_confidence,
            "cooldown": self._vote_cooldown,
        }

    def evaluate(self, direction: str, state: MarketState) -> OverlayDecision:
        """Evaluate all voters and produce a GO/NO-GO decision."""
        votes = []
        weighted_sum = 0.0
        total_weight = 0.0

        for name, voter_fn in self._voters.items():
            try:
                score, reason = voter_fn(direction, state)
                score = max(-1.0, min(1.0, score))  # clamp
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

        # Store scores for learning when trade closes
        self._pending_scores = {v.name: v.score for v in votes}

        decision = OverlayDecision(
            action=action,
            consensus_score=round(consensus, 3),
            confidence=round(confidence, 3),
            votes=votes,
            reason=self._build_reason(votes, consensus, action),
            voters_for=voters_for,
            voters_against=voters_against,
        )

        self._log.info(
            f"AI {action}: consensus={consensus:+.2f} conf={confidence:.0%} "
            f"for={voters_for} against={voters_against} | {decision.reason}"
        )

        return decision

    def learn(self, pnl_pips: float) -> None:
        """Update voter weights based on trade outcome. Call after trade closes."""
        if self._pending_scores is None:
            return

        # Record trade
        self._trade_history.append({
            "pnl": pnl_pips,
            "scores": dict(self._pending_scores),
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # Keep only recent memory
        if len(self._trade_history) > self.memory_trades:
            self._trade_history = self._trade_history[-self.memory_trades:]

        # Update weights: voters whose score aligned with outcome get weight boost
        outcome = 1.0 if pnl_pips > 0 else -1.0
        for name, score in self._pending_scores.items():
            # If voter said YES (+score) and trade won, or voter said NO (-score) and trade lost
            # → voter was RIGHT → increase weight
            alignment = score * outcome  # positive = correct prediction
            old_w = self._weights.get(name, 1.0)
            # Exponential update: w = w * (1 + lr * alignment)
            new_w = old_w * (1.0 + self.learning_rate * alignment)
            # Clamp weights to prevent runaway
            new_w = max(0.1, min(5.0, new_w))
            self._weights[name] = new_w

        self._pending_scores = None

        # Log weight changes
        sorted_w = sorted(self._weights.items(), key=lambda x: x[1], reverse=True)
        top3 = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[:3])
        bot3 = ", ".join(f"{n}={w:.2f}" for n, w in sorted_w[-3:])
        self._log.info(f"AI LEARNED: pnl={pnl_pips:+.1f} | top: {top3} | bottom: {bot3}")

        self._save_state()

    # ══════════════════════════════════════════════════════════
    # VOTER IMPLEMENTATIONS
    # ══════════════════════════════════════════════════════════

    def _vote_trend_strength(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Strong 4H trend = strong yes. Weak trend = skip."""
        strength = s.trend_strength_4h
        if strength > 10:
            return 1.0, f"Strong trend ({strength:.0f} pips separation)"
        elif strength > 5:
            return 0.5, f"Moderate trend ({strength:.0f})"
        elif strength > 2:
            return 0.0, f"Weak trend ({strength:.0f})"
        return -0.5, f"Very weak trend ({strength:.0f})"

    def _vote_rsi_zone(self, direction: str, s: MarketState) -> tuple[float, str]:
        """For longs: RSI 35-55 is ideal (pullback zone). For shorts: 45-65."""
        rsi = s.rsi_1h
        if direction == "long":
            if 35 <= rsi <= 50:
                return 1.0, f"RSI {rsi:.0f} in pullback sweet spot"
            elif 50 < rsi <= 60:
                return 0.3, f"RSI {rsi:.0f} slightly high for long"
            elif rsi > 70:
                return -0.8, f"RSI {rsi:.0f} overbought — DON'T buy here"
            elif rsi < 25:
                return -0.5, f"RSI {rsi:.0f} too oversold — falling knife risk"
            return 0.0, f"RSI {rsi:.0f} neutral"
        else:  # short
            if 50 <= rsi <= 65:
                return 1.0, f"RSI {rsi:.0f} in rally sweet spot for short"
            elif 40 < rsi < 50:
                return 0.3, f"RSI {rsi:.0f} slightly low for short"
            elif rsi < 30:
                return -0.8, f"RSI {rsi:.0f} oversold — DON'T short here"
            elif rsi > 75:
                return -0.5, f"RSI {rsi:.0f} too overbought — blowoff risk"
            return 0.0, f"RSI {rsi:.0f} neutral"

    def _vote_volume(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Above-average volume confirms the move."""
        vr = s.volume_ratio
        if vr > 1.5:
            return 0.8, f"Volume {vr:.1f}x average — strong confirmation"
        elif vr > 1.0:
            return 0.3, f"Volume {vr:.1f}x — slightly above avg"
        elif vr > 0.5:
            return -0.2, f"Volume {vr:.1f}x — below average"
        return -0.6, f"Volume {vr:.1f}x — dead market, avoid"

    def _vote_spread(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Tight spread = good liquidity. Wide spread = danger."""
        sp = s.spread_pips
        if sp < 1.5:
            return 0.5, f"Spread {sp:.1f} pips — tight, good"
        elif sp < 3.0:
            return 0.0, f"Spread {sp:.1f} pips — normal"
        elif sp < 5.0:
            return -0.5, f"Spread {sp:.1f} pips — widening"
        return -1.0, f"Spread {sp:.1f} pips — too wide, SKIP"

    def _vote_session(self, direction: str, s: MarketState) -> tuple[float, str]:
        """London and NY overlap are best. Asian session variable."""
        h = s.hour
        if 7 <= h <= 10:  # London morning
            return 0.7, f"London morning (h={h}) — good liquidity"
        elif 13 <= h <= 16:  # NY overlap
            return 0.8, f"NY overlap (h={h}) — best liquidity"
        elif 0 <= h <= 3:  # Asian
            return 0.2, f"Asian session (h={h}) — JPY pairs active"
        elif 20 <= h <= 23:  # Late NY / early Asian
            return 0.0, f"Session transition (h={h})"
        return -0.3, f"Low activity (h={h})"

    def _vote_momentum(self, direction: str, s: MarketState) -> tuple[float, str]:
        """EMA slopes should agree with direction."""
        if direction == "long":
            if s.ema_8_slope > 0 and s.ema_21_slope > 0:
                return 0.8, "Both EMAs rising — momentum aligned"
            elif s.ema_8_slope > 0:
                return 0.3, "Fast EMA rising, slow flat"
            elif s.ema_8_slope < 0 and s.ema_21_slope < 0:
                return -0.7, "Both EMAs falling — momentum AGAINST long"
            return 0.0, "Mixed momentum"
        else:
            if s.ema_8_slope < 0 and s.ema_21_slope < 0:
                return 0.8, "Both EMAs falling — momentum aligned for short"
            elif s.ema_8_slope < 0:
                return 0.3, "Fast EMA falling, slow flat"
            elif s.ema_8_slope > 0 and s.ema_21_slope > 0:
                return -0.7, "Both EMAs rising — momentum AGAINST short"
            return 0.0, "Mixed momentum"

    def _vote_level_proximity(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Entering near S/R is better than mid-range."""
        if direction == "long":
            # Want to be near support (low in range)
            if s.dist_from_low_20 < 0.3:
                return 0.8, f"Near support (dist={s.dist_from_low_20:.2f})"
            elif s.dist_from_low_20 > 0.8:
                return -0.6, f"Near resistance — bad long entry"
            return 0.0, "Mid-range"
        else:
            if s.dist_from_high_20 < 0.3:
                return 0.8, f"Near resistance (dist={s.dist_from_high_20:.2f})"
            elif s.dist_from_high_20 > 0.8:
                return -0.6, f"Near support — bad short entry"
            return 0.0, "Mid-range"

    def _vote_recent_perf(self, direction: str, s: MarketState) -> tuple[float, str]:
        """If recent trades are winning, keep going. If losing, be cautious."""
        wr = s.recent_win_rate
        if wr > 0.6:
            return 0.5, f"Recent WR {wr:.0%} — strategy working"
        elif wr > 0.4:
            return 0.0, f"Recent WR {wr:.0%} — neutral"
        elif wr > 0.2:
            return -0.3, f"Recent WR {wr:.0%} — struggling"
        return -0.7, f"Recent WR {wr:.0%} — strategy may be broken"

    def _vote_loss_streak(self, direction: str, s: MarketState) -> tuple[float, str]:
        """After consecutive losses, reduce aggression."""
        cl = s.consecutive_losses
        if cl == 0:
            return 0.3, "No recent losses"
        elif cl <= 2:
            return 0.0, f"{cl} consecutive losses — normal"
        elif cl <= 4:
            return -0.5, f"{cl} consecutive losses — cautious"
        return -1.0, f"{cl} consecutive losses — STOP trading, review strategy"

    def _vote_volatility(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Moderate ATR is best. Too low = no move. Too high = stop hunted."""
        atr = s.atr_14
        if atr <= 0:
            return 0.0, "No ATR data"
        # Normalize: typical FX ATR is 5-20 pips for majors
        atr_pips = atr / self.pip_size
        if 5 < atr_pips < 15:
            return 0.5, f"ATR {atr_pips:.0f} pips — moderate, ideal"
        elif 15 <= atr_pips < 30:
            return 0.0, f"ATR {atr_pips:.0f} pips — high but tradable"
        elif atr_pips >= 30:
            return -0.5, f"ATR {atr_pips:.0f} pips — too volatile, widen stops"
        return -0.3, f"ATR {atr_pips:.0f} pips — low volatility"

    def _vote_time_of_day(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Specific hours known to be better/worse from edge discovery."""
        h = s.hour
        # JPY pairs best at Asian open (0-1, 4) and London afternoon (14-15)
        good_hours = {0, 1, 4, 8, 14, 15}
        bad_hours = {5, 6, 9, 16, 17, 21}
        if h in good_hours:
            return 0.6, f"Hour {h} — historically profitable"
        elif h in bad_hours:
            return -0.5, f"Hour {h} — historically unprofitable"
        return 0.0, f"Hour {h} — neutral"

    def _vote_day_of_week(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Tue-Thu typically best. Mon/Fri more erratic."""
        d = s.day_of_week
        if d in (1, 2, 3):  # Tue, Wed, Thu
            return 0.3, f"Day {d} (Tue-Thu) — peak activity"
        elif d == 0:  # Mon
            return -0.2, "Monday — gaps, cautious"
        elif d == 4:  # Fri
            return -0.4, "Friday — reduced liquidity, position squaring"
        return -1.0, "Weekend — should not be trading"

    def _vote_overextension(self, direction: str, s: MarketState) -> tuple[float, str]:
        """If price is too far from mean, risk of snap-back."""
        rsi = s.rsi_14
        if direction == "long" and rsi > 75:
            return -0.8, f"RSI {rsi:.0f} — overextended long, snap-back risk"
        if direction == "short" and rsi < 25:
            return -0.8, f"RSI {rsi:.0f} — overextended short, snap-back risk"
        return 0.1, "Not overextended"

    def _vote_mtf_confidence(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Higher MTF confidence = more aligned timeframes."""
        c = s.confidence_mtf
        if c > 0.8:
            return 1.0, f"MTF conf {c:.0%} — all timeframes strongly agree"
        elif c > 0.6:
            return 0.5, f"MTF conf {c:.0%} — good alignment"
        elif c > 0.4:
            return 0.0, f"MTF conf {c:.0%} — marginal"
        return -0.5, f"MTF conf {c:.0%} — weak alignment"

    def _vote_cooldown(self, direction: str, s: MarketState) -> tuple[float, str]:
        """Don't trade too soon after last trade."""
        bars = s.bars_since_last_trade
        if bars > 60:
            return 0.3, "Well-rested — fresh signal"
        elif bars > 30:
            return 0.1, "Adequate spacing"
        elif bars > 15:
            return -0.3, "Recent trade — may be overtrading"
        return -0.7, "Too soon after last trade"

    # ══════════════════════════════════════════════════════════
    # REASON BUILDER
    # ══════════════════════════════════════════════════════════

    def _build_reason(self, votes: list[VoterResult], consensus: float, action: str) -> str:
        """Build human-readable reason string."""
        # Top positive and negative voters
        sorted_votes = sorted(votes, key=lambda v: v.score * v.weight, reverse=True)
        top_for = [v for v in sorted_votes if v.score > 0.1][:3]
        top_against = [v for v in sorted_votes if v.score < -0.1][-3:]

        parts = []
        if top_for:
            parts.append("FOR: " + ", ".join(f"{v.name}({v.score:+.1f})" for v in top_for))
        if top_against:
            parts.append("AGAINST: " + ", ".join(f"{v.name}({v.score:+.1f})" for v in top_against))
        return " | ".join(parts) if parts else "no strong opinions"

    # ══════════════════════════════════════════════════════════
    # STATE PERSISTENCE
    # ══════════════════════════════════════════════════════════

    def _save_state(self) -> None:
        """Persist weights and trade history to disk."""
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
            self._log.warning(f"Failed to save AI state: {e}")

    def _load_state(self) -> None:
        """Load persisted weights and history."""
        if not self._state_file.exists():
            return
        try:
            state = json.loads(self._state_file.read_text())
            if "weights" in state:
                # Only load weights for voters that still exist
                for name, w in state["weights"].items():
                    if name in self._weights:
                        self._weights[name] = float(w)
            if "trade_history" in state:
                self._trade_history = state["trade_history"]
            self._log.info(f"AI state loaded: {len(self._trade_history)} trades in memory")
        except Exception as e:
            self._log.warning(f"Failed to load AI state: {e}")

    def get_weight_summary(self) -> dict:
        """Return current weights for dashboard/logging."""
        sorted_w = sorted(self._weights.items(), key=lambda x: x[1], reverse=True)
        return {
            "weights": dict(sorted_w),
            "top_3": {n: round(w, 2) for n, w in sorted_w[:3]},
            "bottom_3": {n: round(w, 2) for n, w in sorted_w[-3:]},
            "trades_in_memory": len(self._trade_history),
            "recent_wr": sum(1 for t in self._trade_history[-10:] if t["pnl"] > 0) / max(len(self._trade_history[-10:]), 1),
        }

    def get_weights(self) -> dict[str, float]:
        """Return raw weights dict for pooling."""
        return dict(self._weights)

    def get_trade_history(self) -> list[dict]:
        """Return trade history for pooling."""
        return list(self._trade_history)

    def apply_pooled_weights(self, pooled: dict[str, float], blend: float = 0.3) -> None:
        """Blend pooled fleet weights into this symbol's weights.

        blend=0.3 means 70% symbol-specific + 30% fleet consensus.
        Only applies to voters that exist in both sets.
        """
        for name in self._weights:
            if name in pooled:
                self._weights[name] = (
                    (1.0 - blend) * self._weights[name]
                    + blend * pooled[name]
                )
        self._save_state()


class AIWeightPool:
    """Fleet-level weight aggregation across all symbol overlays.

    Computes a trade-count-weighted average of voter weights across all pairs,
    then lets each pair blend the pooled weights into its own via apply_pooled_weights().

    This accelerates learning: a pair with 1 trade benefits from the 18-trade fleet.
    """

    def __init__(self, state_dir: Path | None = None):
        self._state_dir = state_dir or Path("argus_flow/logs")
        self._pool_file = self._state_dir / "ai_weight_pool.json"
        self._log = logging.getLogger("ai.pool")
        self._pooled_weights: dict[str, float] = {}
        self._contributions: dict[str, int] = {}  # symbol -> trade count
        self._load()

    def update(self, overlays: dict[str, AdaptiveOverlay]) -> dict[str, float]:
        """Recompute pooled weights from all overlays weighted by trade count.

        Returns the new pooled weights dict.
        """
        if not overlays:
            return self._pooled_weights

        # Gather per-symbol weights + trade counts
        all_voter_names: set[str] = set()
        symbol_data: list[tuple[dict[str, float], int]] = []
        self._contributions = {}

        for symbol, overlay in overlays.items():
            weights = overlay.get_weights()
            n_trades = len(overlay.get_trade_history())
            all_voter_names.update(weights.keys())
            symbol_data.append((weights, max(n_trades, 1)))  # min 1 to include all
            self._contributions[symbol] = n_trades

        total_trades = sum(n for _, n in symbol_data)
        if total_trades == 0:
            return self._pooled_weights

        # Weighted average per voter
        pooled: dict[str, float] = {}
        for voter in all_voter_names:
            weighted_sum = 0.0
            weight_total = 0.0
            for weights, n_trades in symbol_data:
                if voter in weights:
                    weighted_sum += weights[voter] * n_trades
                    weight_total += n_trades
            if weight_total > 0:
                pooled[voter] = weighted_sum / weight_total

        self._pooled_weights = pooled
        self._save()

        self._log.info(
            f"Pool updated: {len(overlays)} symbols, {total_trades} total trades. "
            f"Top: {sorted(pooled.items(), key=lambda x: x[1], reverse=True)[:3]}"
        )
        return pooled

    def sync_to_overlays(self, overlays: dict[str, AdaptiveOverlay], blend: float = 0.3) -> None:
        """Push pooled weights to all overlays with blending."""
        if not self._pooled_weights:
            self.update(overlays)
        if not self._pooled_weights:
            return

        for symbol, overlay in overlays.items():
            overlay.apply_pooled_weights(self._pooled_weights, blend=blend)
            self._log.info(f"Synced pool -> {symbol} (blend={blend})")

    def get_summary(self) -> dict:
        """Return pool state for dashboard."""
        return {
            "pooled_weights": {k: round(v, 3) for k, v in self._pooled_weights.items()},
            "contributions": self._contributions,
            "total_trades": sum(self._contributions.values()),
        }

    def _save(self) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            self._pool_file.write_text(json.dumps({
                "pooled_weights": self._pooled_weights,
                "contributions": self._contributions,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        except Exception as e:
            self._log.warning(f"Failed to save pool: {e}")

    def _load(self) -> None:
        if not self._pool_file.exists():
            return
        try:
            data = json.loads(self._pool_file.read_text())
            self._pooled_weights = data.get("pooled_weights", {})
            self._contributions = data.get("contributions", {})
        except Exception as e:
            self._log.warning(f"Failed to load pool: {e}")
