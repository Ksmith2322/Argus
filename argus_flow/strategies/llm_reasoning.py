"""Local LLM Reasoning Layer — optional intelligence via Ollama.

Connects to a local Ollama instance to get a second opinion on trade setups.
Fully optional — system works identically without it. Designed for speed:
5-second timeout, response caching, async-friendly.

Usage:
    reasoner = LocalLLMReasoner(symbol="USDJPY")
    opinion = reasoner.evaluate(direction, market_state, mtf_signal_info, overlay_decision)
    # opinion.available == False if ollama not running
"""
from __future__ import annotations

import csv
import json
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

_log = logging.getLogger("argus.llm")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "5"))  # seconds


@dataclass
class LLMOpinion:
    """Structured response from the LLM."""
    available: bool = False
    action: str = "ABSTAIN"      # TAKE, SKIP, or ABSTAIN (no opinion)
    confidence: int = 50         # 0-100
    reasoning: str = ""
    latency_ms: int = 0
    model: str = ""
    cached: bool = False


class LocalLLMReasoner:
    """Query a local Ollama model for trade evaluation."""

    def __init__(
        self,
        symbol: str,
        log_dir: Optional[Path] = None,
        cache_ttl: int = 300,  # cache responses for 5 min
    ):
        self.symbol = symbol
        self._log = logging.getLogger(f"llm.{symbol.lower()}")
        self._log_dir = log_dir or Path(f"argus_flow/logs/{symbol}")
        self._decisions_file = self._log_dir / "llm_decisions.csv"
        self._cache: dict[str, tuple[float, LLMOpinion]] = {}
        self._cache_ttl = cache_ttl
        self._available: Optional[bool] = None  # lazy check
        self._last_check: float = 0
        self._check_interval = 60  # recheck ollama availability every 60s

    def _check_ollama(self) -> bool:
        """Check if ollama is reachable. Cached for _check_interval seconds."""
        now = time.time()
        if self._available is not None and (now - self._last_check) < self._check_interval:
            return self._available
        try:
            req = Request(f"{OLLAMA_URL}/api/version", method="GET")
            resp = urlopen(req, timeout=2)
            self._available = resp.status == 200
        except Exception:
            self._available = False
        self._last_check = now
        if not self._available:
            self._log.debug("Ollama not available — LLM reasoning disabled")
        return self._available

    def evaluate(
        self,
        direction: str,
        market_state: dict,
        mtf_info: dict,
        overlay_info: dict,
    ) -> LLMOpinion:
        """Get LLM opinion on a trade setup. Returns quickly if ollama unavailable."""

        if not self._check_ollama():
            return LLMOpinion(available=False, reasoning="ollama not running")

        # Check cache
        cache_key = self._make_cache_key(direction, market_state, mtf_info)
        cached = self._cache.get(cache_key)
        if cached:
            ts, opinion = cached
            if time.time() - ts < self._cache_ttl:
                opinion.cached = True
                return opinion

        # Build prompt
        prompt = self._build_prompt(direction, market_state, mtf_info, overlay_info)

        # Query ollama
        t0 = time.time()
        try:
            opinion = self._query_ollama(prompt)
            opinion.latency_ms = int((time.time() - t0) * 1000)
            opinion.available = True
        except Exception as e:
            self._log.warning(f"LLM query failed ({time.time() - t0:.1f}s): {e}")
            return LLMOpinion(available=False, reasoning=f"query failed: {e}")

        # Cache result
        self._cache[cache_key] = (time.time(), opinion)

        # Log decision
        self._log_decision(direction, opinion, market_state)

        self._log.info(
            f"LLM {opinion.action}: conf={opinion.confidence}% "
            f"latency={opinion.latency_ms}ms | {opinion.reasoning}"
        )

        return opinion

    def _build_prompt(
        self,
        direction: str,
        state: dict,
        mtf: dict,
        overlay: dict,
    ) -> str:
        """Build a focused prompt for the LLM. Keep it SHORT for fast inference."""
        return f"""You are a forex trading analyst. Evaluate this {self.symbol} {direction.upper()} trade setup.

MARKET STATE:
- Price: {state.get('price', 'N/A')}
- RSI(14): {state.get('rsi_14', 'N/A')}, RSI(1H): {state.get('rsi_1h', 'N/A')}
- ATR(14): {state.get('atr_14', 'N/A')} pips
- Spread: {state.get('spread_pips', 'N/A')} pips
- Volume ratio: {state.get('volume_ratio', 'N/A')}x avg
- Hour: {state.get('hour', 'N/A')} UTC, Day: {state.get('day_of_week', 'N/A')}

MTF SIGNAL:
- 4H Trend: {mtf.get('trend_4h', 'N/A')}
- 1H Setup: {mtf.get('setup_1h', 'N/A')}
- 5M Trigger: {mtf.get('trigger_5m', 'N/A')}
- Confidence: {mtf.get('confidence', 'N/A')}

AI OVERLAY:
- Consensus: {overlay.get('consensus', 'N/A')}
- Voters for/against: {overlay.get('voters_for', '?')}/{overlay.get('voters_against', '?')}
- Top reasons: {overlay.get('top_reasons', 'N/A')}

RECENT PERFORMANCE:
- Win rate (last 10): {state.get('recent_wr', 'N/A')}
- Consecutive losses: {state.get('consecutive_losses', 0)}

Respond in EXACTLY this format (one line each):
ACTION: TAKE or SKIP
CONFIDENCE: 0-100
REASON: one sentence why"""

    def _query_ollama(self, prompt: str) -> LLMOpinion:
        """Send prompt to ollama and parse response."""
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,  # low temp for consistency
                "num_predict": 100,  # keep response short
            }
        }).encode("utf-8")

        req = Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        resp = urlopen(req, timeout=LLM_TIMEOUT)
        body = json.loads(resp.read())
        text = body.get("response", "")

        return self._parse_response(text, body.get("model", OLLAMA_MODEL))

    def _parse_response(self, text: str, model: str) -> LLMOpinion:
        """Parse the LLM response into structured opinion."""
        action = "ABSTAIN"
        confidence = 50
        reasoning = ""

        for line in text.strip().split("\n"):
            line = line.strip()
            upper = line.upper()
            if upper.startswith("ACTION:"):
                val = line.split(":", 1)[1].strip().upper()
                if "TAKE" in val:
                    action = "TAKE"
                elif "SKIP" in val:
                    action = "SKIP"
            elif upper.startswith("CONFIDENCE:"):
                try:
                    confidence = int("".join(c for c in line.split(":", 1)[1] if c.isdigit())[:3])
                    confidence = max(0, min(100, confidence))
                except (ValueError, IndexError):
                    pass
            elif upper.startswith("REASON:"):
                reasoning = line.split(":", 1)[1].strip()

        if not reasoning:
            reasoning = text.strip()[:100]

        return LLMOpinion(
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            model=model,
        )

    def _make_cache_key(self, direction: str, state: dict, mtf: dict) -> str:
        """Create a cache key from the inputs (quantized to avoid micro-changes)."""
        key_parts = f"{direction}|{state.get('hour')}|{round(state.get('rsi_14', 0), 0)}|{mtf.get('trend_4h')}|{mtf.get('setup_1h')}"
        return hashlib.md5(key_parts.encode()).hexdigest()[:12]

    def _log_decision(self, direction: str, opinion: LLMOpinion, state: dict) -> None:
        """Append LLM decision to CSV log."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            header_needed = not self._decisions_file.exists()
            with open(self._decisions_file, "a", newline="") as f:
                w = csv.writer(f)
                if header_needed:
                    w.writerow(["ts", "direction", "action", "confidence", "reasoning",
                                "latency_ms", "model", "cached", "hour", "rsi"])
                w.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    direction,
                    opinion.action,
                    opinion.confidence,
                    opinion.reasoning[:200],
                    opinion.latency_ms,
                    opinion.model,
                    opinion.cached,
                    state.get("hour", ""),
                    state.get("rsi_14", ""),
                ])
        except Exception as e:
            self._log.warning(f"Failed to log LLM decision: {e}")

    def get_stats(self) -> dict:
        """Return usage statistics."""
        if not self._decisions_file.exists():
            return {"total_queries": 0, "available": bool(self._available)}
        try:
            with open(self._decisions_file) as f:
                rows = list(csv.DictReader(f))
            takes = sum(1 for r in rows if r.get("action") == "TAKE")
            skips = sum(1 for r in rows if r.get("action") == "SKIP")
            avg_lat = sum(int(r.get("latency_ms", 0)) for r in rows) / max(len(rows), 1)
            return {
                "total_queries": len(rows),
                "takes": takes,
                "skips": skips,
                "avg_latency_ms": round(avg_lat),
                "available": bool(self._available),
                "model": OLLAMA_MODEL,
            }
        except Exception:
            return {"total_queries": 0, "available": bool(self._available)}
