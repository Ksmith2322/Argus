# confluence.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

# Phase 5A
from structure import StructureResult

# Phase 18
from trendlines import TrendlineResult


def _as_decimal(x: Any, default: str = "0") -> Decimal:
    if x is None:
        return Decimal(default)
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _as_bool(x: Any, default: bool = False) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _as_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _clamp_i(x: Any, lo: int, hi: int) -> int:
    try:
        x_i = int(x)
    except Exception:
        x_i = lo
    return max(lo, min(hi, x_i))


@dataclass
class TFState:
    """
    Per-timeframe strategy snapshot.
    """
    tf: str                    # "1m", "5m", "1h"
    signal: int                # 1 = bullish bias, 0 = neutral
    trend_ok: bool
    score: int                 # 0â€“100 (per-TF heuristic)
    reasons: str


@dataclass
class ConfluenceResult:
    """
    Aggregated multi-timeframe decision.
    """
    score_1m: int
    score_5m: int
    score_1h: int
    confluence_score: int
    gate: str                  # HOLD / WATCH / TRADE
    reasons: str


# --------- LINE ABOVE: class ConfluenceResult:
# Back-compat: main.py may import ConfluenceSnapshot
ConfluenceSnapshot = ConfluenceResult


class ConfluenceEngine:
    """
    Phase 3.5 â€“ Multi-timeframe confluence engine.
    Phase 5A â€“ Optional structure overlay (support/resistance, break-retest, rejection).

    "Done" condition for Phase 5A here:
      - structure influences score and/or gate deterministically
      - reasons clearly explain the deltas and any hard blocks
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}

        # Weighting by timeframe
        self.weights = {
            "1m": _as_decimal(self.cfg.get("CONFLUENCE_W_1M", "0.2")),
            "5m": _as_decimal(self.cfg.get("CONFLUENCE_W_5M", "0.3")),
            "1h": _as_decimal(self.cfg.get("CONFLUENCE_W_1H", "0.5")),
        }

        # Normalize weights
        wsum = sum(self.weights.values())
        if wsum > 0:
            self.weights = {k: (v / wsum) for k, v in self.weights.items()}
        else:
            # deterministic fallback
            self.weights = {"1m": Decimal("0.2"), "5m": Decimal("0.3"), "1h": Decimal("0.5")}

        # Gating thresholds
        self.watch_threshold = _as_int(self.cfg.get("CONFLUENCE_WATCH_SCORE", 60), 60)
        self.trade_threshold = _as_int(self.cfg.get("CONFLUENCE_TRADE_SCORE", 80), 80)
        self.watch_threshold = _clamp_i(self.watch_threshold, 1, 99)
        self.trade_threshold = _clamp_i(self.trade_threshold, self.watch_threshold + 1, 100)

        # Hard-gate knobs
        self.require_1m_signal = _as_bool(self.cfg.get("CONFLUENCE_REQUIRE_1M_SIGNAL", True), True)
        self.require_1m_trendok = _as_bool(self.cfg.get("CONFLUENCE_REQUIRE_1M_TRENDOK", True), True)

        # HTF confirmation
        self.require_htf_confirm = _as_bool(self.cfg.get("CONFLUENCE_REQUIRE_HTF_CONFIRM", True), True)
        self.htf_min_score = _as_int(self.cfg.get("CONFLUENCE_HTF_MIN_SCORE", 60), 60)
        self.htf_min_score = _clamp_i(self.htf_min_score, 0, 100)

        # Missing TF handling
        self.ignore_missing_tfs = _as_bool(self.cfg.get("CONFLUENCE_IGNORE_MISSING_TFS", False), False)

        # Alignment shaping
        self.alignment_bonus = _as_int(self.cfg.get("CONFLUENCE_ALIGNMENT_BONUS", 5), 5)
        self.conflict_penalty = _as_int(self.cfg.get("CONFLUENCE_CONFLICT_PENALTY", 10), 10)
        self.alignment_bonus = _clamp_i(self.alignment_bonus, 0, 25)
        self.conflict_penalty = _clamp_i(self.conflict_penalty, 0, 50)

        # -------------------------
        # Phase 5A â€” Structure overlay knobs
        # -------------------------
        self.use_structure = _as_bool(self.cfg.get("USE_STRUCTURE", False), False)

        # Soft shaping
        self.struct_bonus_break_retest = _as_int(self.cfg.get("STRUCT_BONUS_BREAK_RETEST", 8), 8)
        self.struct_bonus_near_support = _as_int(self.cfg.get("STRUCT_BONUS_NEAR_SUPPORT", 3), 3)
        self.struct_penalty_reject_res = _as_int(self.cfg.get("STRUCT_PENALTY_REJECT_AT_RES", 8), 8)
        self.struct_penalty_failed_retest = _as_int(self.cfg.get("STRUCT_PENALTY_FAILED_RETEST", 10), 10)

        self.struct_bonus_break_retest = _clamp_i(self.struct_bonus_break_retest, 0, 25)
        self.struct_bonus_near_support = _clamp_i(self.struct_bonus_near_support, 0, 15)
        self.struct_penalty_reject_res = _clamp_i(self.struct_penalty_reject_res, 0, 50)
        self.struct_penalty_failed_retest = _clamp_i(self.struct_penalty_failed_retest, 0, 50)

        # Hard blocks
        self.struct_block_long_on_reject_res = _as_bool(self.cfg.get("STRUCT_BLOCK_LONG_ON_REJECT_RES", False), False)
        self.struct_block_long_below_support = _as_bool(self.cfg.get("STRUCT_BLOCK_LONG_BELOW_SUPPORT", False), False)
        self.struct_block_long_on_failed_retest = _as_bool(self.cfg.get("STRUCT_BLOCK_LONG_ON_FAILED_RETEST", False), False)
        self.struct_block_long_when_breaking_down = _as_bool(self.cfg.get("STRUCT_BLOCK_LONG_WHEN_BREAKING_DOWN", False), False)

        # Optional: allow structure to bump gate even if score is slightly below threshold
        self.struct_gate_bump_enabled = _as_bool(self.cfg.get("STRUCT_GATE_BUMP_ENABLED", True), True)
        self.struct_gate_bump_max_points = _as_int(self.cfg.get("STRUCT_GATE_BUMP_MAX_POINTS", 6), 6)
        self.struct_gate_bump_max_points = _clamp_i(self.struct_gate_bump_max_points, 0, 25)

        # -------------------------
        # Phase 18 — Trendline overlay knobs
        # -------------------------
        self.use_trendlines = _as_bool(self.cfg.get("USE_TRENDLINES", False), False)

        # Soft shaping (score adjustments)
        self.tl_bonus_near_support = _clamp_i(_as_int(self.cfg.get("TL_BONUS_NEAR_SUPPORT", 5), 5), 0, 20)
        self.tl_penalty_near_resist = _clamp_i(_as_int(self.cfg.get("TL_PENALTY_NEAR_RESIST", 8), 8), 0, 30)
        self.tl_bonus_broke_above = _clamp_i(_as_int(self.cfg.get("TL_BONUS_BROKE_ABOVE_RESIST", 12), 12), 0, 30)
        self.tl_penalty_broke_below = _clamp_i(_as_int(self.cfg.get("TL_PENALTY_BROKE_BELOW_SUPPORT", 15), 15), 0, 50)

        # Descending channel penalty (resist_slope_neg=True even when not near the line)
        self.tl_penalty_resist_slope_neg = _clamp_i(_as_int(self.cfg.get("TL_PENALTY_RESIST_SLOPE_NEG", 0), 0), 0, 30)

        # Hard blocks
        self.tl_block_long_near_resist = _as_bool(self.cfg.get("TL_BLOCK_LONG_NEAR_RESIST", False), False)
        self.tl_block_long_broke_below = _as_bool(self.cfg.get("TL_BLOCK_LONG_BROKE_BELOW_SUPPORT", True), True)

    # --------- LINE ABOVE: def __init__(self, cfg: Dict[str, Any]):

    @classmethod
    def from_config(cls, cfg: Dict[str, Any]) -> "ConfluenceEngine":
        return cls(cfg)

    def _weighted_score(self, tf_states: List[TFState]) -> int:
        if not tf_states:
            return 0

        # Missing TF policy: ignore "no_data" if enabled, else treat as zeros (scores already zeroed in norm())
        if self.ignore_missing_tfs:
            present = [s for s in tf_states if (s.reasons or "") != "no_data"]
            if not present:
                return 0

            wsum = sum(self.weights.get(s.tf, Decimal("0")) for s in present)
            if wsum <= 0:
                return 0

            total = Decimal("0")
            for s in present:
                w = self.weights.get(s.tf, Decimal("0")) / wsum
                total += Decimal(int(s.score)) * w
            return int(total.to_integral_value(rounding=ROUND_HALF_UP))

        total = Decimal("0")
        for s in tf_states:
            w = self.weights.get(s.tf, Decimal("0"))
            total += Decimal(int(s.score)) * w
        return int(total.to_integral_value(rounding=ROUND_HALF_UP))

    def _apply_structure_overlay(
        self,
        conf_score: int,
        structure: Optional[StructureResult],
        *,
        s1: TFState,
    ) -> Tuple[int, List[str], List[str], int]:
        """
        Returns:
          (new_score, struct_notes, struct_blocks, gate_bump_points)

        gate_bump_points is used as a *separate* mechanism to upgrade gate when structure is strong,
        without lying about the numeric conf_score.
        """
        if (not self.use_structure) or (structure is None):
            return conf_score, [], [], 0

        notes: List[str] = []
        blocks: List[str] = []
        bump_pts = 0

        # Soft shaping (score deltas)
        if bool(getattr(structure, "retest_ok", False)):
            conf_score = min(100, conf_score + self.struct_bonus_break_retest)
            notes.append(f"+{self.struct_bonus_break_retest}(break_retest)")

            # Gate bump candidate: break+retest is a high-signal structure event
            if self.struct_gate_bump_enabled:
                bump_pts = max(bump_pts, min(self.struct_gate_bump_max_points, self.struct_bonus_break_retest))

        if bool(getattr(structure, "near_support", False)):
            conf_score = min(100, conf_score + self.struct_bonus_near_support)
            notes.append(f"+{self.struct_bonus_near_support}(near_sup)")

        if bool(getattr(structure, "rejection_at_res", False)):
            conf_score = max(0, conf_score - self.struct_penalty_reject_res)
            notes.append(f"-{self.struct_penalty_reject_res}(reject_res)")

        if bool(getattr(structure, "failed_retest", False)):
            conf_score = max(0, conf_score - self.struct_penalty_failed_retest)
            notes.append(f"-{self.struct_penalty_failed_retest}(failed_retest)")

        # Optional hard blocks (only impact TRADE gate)
        if self.struct_block_long_on_reject_res and bool(getattr(structure, "rejection_at_res", False)):
            blocks.append("BLOCK:STRUCT_REJECT_RES")

        if self.struct_block_long_on_failed_retest and bool(getattr(structure, "failed_retest", False)):
            blocks.append("BLOCK:STRUCT_FAILED_RETEST")

        if self.struct_block_long_when_breaking_down and bool(getattr(structure, "broke_down", False)):
            blocks.append("BLOCK:STRUCT_BREAK_DOWN")

        # â€œbelow supportâ€ proxy: if we have a support level and price is below it
        nearest_support = getattr(structure, "nearest_support", None)
        dist_support = getattr(structure, "dist_support", None)
        if self.struct_block_long_below_support and nearest_support is not None:
            if dist_support is not None and dist_support < 0:
                blocks.append("BLOCK:STRUCT_BELOW_SUPPORT")

        # Optional: if 1m signal isn't bullish, structure should not "rescue" the score
        if int(s1.signal) != 1:
            bump_pts = 0

        return conf_score, notes, blocks, bump_pts

    def _apply_trendline_overlay(
        self,
        conf_score: int,
        trendlines,
        mtf_trendlines=None,
    ) -> Tuple[int, List[str], List[str]]:
        """
        Phase 18 (multi-TF): adjust confluence score based on trendlines.

        Uses mtf_trendlines (MultiTFTrendlineResult) when available for
        multi-timeframe confirmation bonuses.  Falls back to single-TF
        TrendlineResult when mtf_trendlines is None.

        Returns: (new_score, tl_notes, tl_blocks)
        """
        if not self.use_trendlines:
            return conf_score, [], []

        notes: List[str] = []
        blocks: List[str] = []

        # ── Multi-TF path (preferred) ─────────────────────────────────────
        if mtf_trendlines is not None:
            mtf = mtf_trendlines
            bk = int(getattr(mtf, "tf_breakout_count", 0))
            sup = int(getattr(mtf, "tf_support_count", 0))
            bkd = int(getattr(mtf, "tf_breakdown_count", 0))

            # Breakout bonuses — tiered by TF confluence
            if bk >= 2:
                bonus = min(100, self.tl_bonus_broke_above + 10)
                conf_score = min(100, conf_score + bonus)
                notes.append(f"+{bonus}(broke_above_{bk}TF)")
            elif bk == 1:
                conf_score = min(100, conf_score + self.tl_bonus_broke_above)
                notes.append(f"+{self.tl_bonus_broke_above}(broke_above_1TF)")

            # Support proximity bonuses — tiered
            if sup >= 2:
                bonus = min(100, self.tl_bonus_near_support + 7)
                conf_score = min(100, conf_score + bonus)
                notes.append(f"+{bonus}(near_support_{sup}TF)")
            elif sup == 1:
                conf_score = min(100, conf_score + self.tl_bonus_near_support)
                notes.append(f"+{self.tl_bonus_near_support}(near_support_1TF)")

            # Resistance proximity penalties
            if bool(getattr(mtf, "near_resist_1h", False)) or bool(getattr(mtf, "near_resist_4h", False)):
                conf_score = max(0, conf_score - self.tl_penalty_near_resist)
                notes.append(f"-{self.tl_penalty_near_resist}(near_resist_tl)")

            # Breakdown penalties — tiered
            if bkd >= 2:
                penalty = self.tl_penalty_broke_below + 10
                conf_score = max(0, conf_score - penalty)
                notes.append(f"-{penalty}(broke_below_{bkd}TF)")
                blocks.append("BLOCK:TL_BROKE_BELOW_SUPPORT")
            elif bkd == 1 and bool(getattr(mtf, "broke_below_1h", False)):
                conf_score = max(0, conf_score - self.tl_penalty_broke_below)
                notes.append(f"-{self.tl_penalty_broke_below}(broke_below_1h)")
                if self.tl_block_long_broke_below:
                    blocks.append("BLOCK:TL_BROKE_BELOW_SUPPORT")

            # Trend change signal — strong bonus
            if bool(getattr(mtf, "trend_change_up", False)):
                conf_score = min(100, conf_score + 5)
                notes.append("+5(trend_change_up)")

            # Bullish TF alignment bonus
            if bool(getattr(mtf, "tf_aligned_bullish", False)):
                conf_score = min(100, conf_score + 3)
                notes.append("+3(tf_aligned_bull)")

            # 4h descending resistance = sell pressure overhead
            if self.tl_penalty_resist_slope_neg > 0 and bool(getattr(mtf, "resist_down_4h", False)):
                conf_score = max(0, conf_score - self.tl_penalty_resist_slope_neg)
                notes.append(f"-{self.tl_penalty_resist_slope_neg}(4h_resist_down)")

            if self.tl_block_long_near_resist and (
                bool(getattr(mtf, "near_resist_1h", False)) or bool(getattr(mtf, "near_resist_4h", False))
            ):
                blocks.append("BLOCK:TL_NEAR_RESIST")

            return conf_score, notes, blocks

        # ── Legacy single-TF path (fallback) ─────────────────────────────
        if trendlines is None:
            return conf_score, [], []

        if bool(getattr(trendlines, "near_support_tl", False)):
            conf_score = min(100, conf_score + self.tl_bonus_near_support)
            notes.append(f"+{self.tl_bonus_near_support}(near_support_tl)")

        if bool(getattr(trendlines, "near_resist_tl", False)):
            conf_score = max(0, conf_score - self.tl_penalty_near_resist)
            notes.append(f"-{self.tl_penalty_near_resist}(near_resist_tl)")

        if bool(getattr(trendlines, "broke_above_resist", False)):
            conf_score = min(100, conf_score + self.tl_bonus_broke_above)
            notes.append(f"+{self.tl_bonus_broke_above}(broke_above_resist_tl)")

        if bool(getattr(trendlines, "broke_below_support", False)):
            conf_score = max(0, conf_score - self.tl_penalty_broke_below)
            notes.append(f"-{self.tl_penalty_broke_below}(broke_below_support_tl)")
            if self.tl_block_long_broke_below:
                blocks.append("BLOCK:TL_BROKE_BELOW_SUPPORT")

        if self.tl_penalty_resist_slope_neg > 0 and bool(getattr(trendlines, "resist_slope_neg", False)):
            conf_score = max(0, conf_score - self.tl_penalty_resist_slope_neg)
            notes.append(f"-{self.tl_penalty_resist_slope_neg}(resist_slope_neg)")

        if self.tl_block_long_near_resist and bool(getattr(trendlines, "near_resist_tl", False)):
            blocks.append("BLOCK:TL_NEAR_RESIST")

        return conf_score, notes, blocks

    def evaluate(
        self,
        st_1m: Optional[TFState],
        st_5m: Optional[TFState],
        st_1h: Optional[TFState],
        structure: Optional[StructureResult] = None,   # Phase 5A optional
        trendlines=None,                               # Phase 18 optional (TrendlineResult, legacy)
        mtf_trendlines=None,                           # Phase 18 multi-TF (MultiTFTrendlineResult)
    ) -> ConfluenceResult:

        def norm(st: Optional[TFState], tf: str) -> TFState:
            if st is None:
                return TFState(tf=tf, signal=0, trend_ok=False, score=0, reasons="no_data")
            if st.tf != tf:
                return TFState(
                    tf=tf,
                    signal=int(st.signal),
                    trend_ok=bool(st.trend_ok),
                    score=int(st.score),
                    reasons=f"{st.reasons}|tf_fix({st.tf}->{tf})",
                )
            return TFState(
                tf=tf,
                signal=int(st.signal),
                trend_ok=bool(st.trend_ok),
                score=int(st.score),
                reasons=str(st.reasons or ""),
            )

        s1 = norm(st_1m, "1m")
        s5 = norm(st_5m, "5m")
        sH = norm(st_1h, "1h")

        tf_states: List[TFState] = [s1, s5, sH]
        base_score = self._weighted_score(tf_states)

        # HTF confirmation snapshot
        htf_support = (
            (s5.trend_ok and s5.signal == 1 and s5.score >= self.htf_min_score) or
            (sH.trend_ok and sH.signal == 1 and sH.score >= self.htf_min_score)
        )
        htf_conflict = (
            (s1.signal == 1) and
            (not ((s5.signal == 1 and s5.trend_ok) or (sH.signal == 1 and sH.trend_ok)))
        )

        # Alignment shaping
        conf_score = int(base_score)
        align_note = ""
        if s1.signal == 1 and htf_support:
            conf_score = min(100, conf_score + self.alignment_bonus)
            align_note = f"bonus=+{self.alignment_bonus}"
        elif htf_conflict:
            conf_score = max(0, conf_score - self.conflict_penalty)
            align_note = f"penalty=-{self.conflict_penalty}"

        # -------------------------
        # Phase 5A structure overlay (score shaping + optional blocks + optional gate bump)
        # -------------------------
        conf_score, struct_notes, struct_blocks, struct_gate_bump = self._apply_structure_overlay(
            conf_score,
            structure,
            s1=s1,
        )

        # -------------------------
        # Phase 18 trendline overlay (score shaping + optional blocks)
        # -------------------------
        conf_score, tl_notes, tl_blocks = self._apply_trendline_overlay(
            conf_score, trendlines, mtf_trendlines=mtf_trendlines
        )

        # Gate from score (raw)
        if conf_score >= self.trade_threshold:
            gate = "TRADE"
        elif conf_score >= self.watch_threshold:
            gate = "WATCH"
        else:
            gate = "HOLD"

        # Optional gate bump: promote WATCH->TRADE if structure is strong and we're close
        bumped = False
        if (
            self.struct_gate_bump_enabled
            and gate == "WATCH"
            and struct_gate_bump > 0
            and (conf_score + struct_gate_bump) >= self.trade_threshold
        ):
            gate = "TRADE"
            bumped = True

        hard_blocks: List[str] = []
        if gate == "TRADE":
            if self.require_1m_signal and s1.signal != 1:
                hard_blocks.append("BLOCK:1M_SIGNAL")
            if self.require_1m_trendok and (not s1.trend_ok):
                hard_blocks.append("BLOCK:1M_TREND_OK")
            if self.require_htf_confirm and (not htf_support):
                hard_blocks.append("BLOCK:HTF_CONFIRM")

            # Append structure blocks (only if enabled)
            hard_blocks.extend(struct_blocks)

            # Append trendline blocks (Phase 18)
            hard_blocks.extend(tl_blocks)

            # If we have blocks, downgrade deterministically
            if hard_blocks:
                gate = "WATCH" if conf_score >= self.watch_threshold else "HOLD"

        # Reasons: make causality explicit (base â†’ align â†’ struct â†’ bump â†’ blocks)
        reasons: List[str] = []
        reasons.append(f"1m:score={s1.score}|signal={s1.signal}|trend_ok={int(s1.trend_ok)}")
        reasons.append(f"5m:score={s5.score}|signal={s5.signal}|trend_ok={int(s5.trend_ok)}")
        reasons.append(f"1h:score={sH.score}|signal={sH.signal}|trend_ok={int(sH.trend_ok)}")

        reasons.append(f"base={base_score}")
        if align_note:
            reasons.append(align_note)

        if struct_notes:
            reasons.append("struct=" + "|".join(struct_notes))
        if bumped:
            reasons.append(f"struct_gate_bump=+{struct_gate_bump}")
        if tl_notes:
            reasons.append("tl=" + "|".join(tl_notes))

        reasons.append(f"confluence={conf_score}")
        reasons.append(f"gate={gate}")

        reasons.append("mode=IGNORE_MISSING_TFS" if self.ignore_missing_tfs else "mode=MISSING_AS_ZERO")

        if structure is not None:
            sraw = str(getattr(structure, "reasons", "") or "")
            if sraw:
                reasons.append("struct_raw=" + sraw)

        if hard_blocks:
            reasons.append("hard=" + ",".join(hard_blocks))

        return ConfluenceResult(
            score_1m=int(s1.score),
            score_5m=int(s5.score),
            score_1h=int(sH.score),
            confluence_score=int(conf_score),
            gate=gate,
            reasons="; ".join(reasons),
        )


# -------------------------
# Minimal self-test (optional)
# -------------------------
if __name__ == "__main__":
    cfg = {
        "CONFLUENCE_W_1M": "0.2",
        "CONFLUENCE_W_5M": "0.3",
        "CONFLUENCE_W_1H": "0.5",
        "CONFLUENCE_WATCH_SCORE": 60,
        "CONFLUENCE_TRADE_SCORE": 80,
        "CONFLUENCE_REQUIRE_1M_SIGNAL": True,
        "CONFLUENCE_REQUIRE_1M_TRENDOK": True,
        "CONFLUENCE_REQUIRE_HTF_CONFIRM": True,
        "CONFLUENCE_HTF_MIN_SCORE": 60,
        "CONFLUENCE_IGNORE_MISSING_TFS": False,
        "USE_STRUCTURE": False,
    }
    eng = ConfluenceEngine(cfg)
    res = eng.evaluate(
        TFState(tf="1m", signal=1, trend_ok=True, score=90, reasons="ok"),
        TFState(tf="5m", signal=1, trend_ok=True, score=70, reasons="ok"),
        TFState(tf="1h", signal=1, trend_ok=True, score=70, reasons="ok"),
        structure=None,
    )
    print(res)

