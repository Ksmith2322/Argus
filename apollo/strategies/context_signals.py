"""apollo/strategies/context_signals.py -- Contextual signals that enhance earnings scoring.

4 signal types that Apollo was missing:
  1. Peer correlation — if a sector peer already reported, use their result
  2. Earnings guidance/reaction — post-ER price action tells us about guidance quality
  3. News feed — scan free RSS/yfinance news for sentiment keywords
  4. Macro context — SPY trend, VIX level, Fed/event risk

All signals return a score [-50, +50] and supporting details.
"""
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

_log = logging.getLogger("apollo.context")

# ── Peer Groups ───────────────────────────────────────────────
# Stocks that report correlated earnings (same sector, same demand drivers)

PEER_GROUPS = {
    "semiconductors": ["NVDA", "AMD", "AVGO", "MU", "MRVL", "INTC", "QCOM", "TXN",
                        "LRCX", "KLAC", "AMAT", "SNPS", "CDNS", "ARM", "SMCI", "TSM"],
    "mega_tech": ["AAPL", "MSFT", "GOOGL", "META", "AMZN"],
    "cloud_saas": ["CRM", "ADBE", "SNOW", "DDOG", "WDAY", "TEAM", "ZS", "CRWD",
                    "FTNT", "PANW", "INTU"],
    "fintech": ["SOFI", "HOOD", "COIN", "UPST", "AFRM", "NU"],
    "banks": ["JPM", "BAC", "GS", "MS"],
    "ev_energy": ["TSLA", "RIVN", "LCID", "PLUG", "FSLR", "ENPH"],
    "biotech": ["MRNA", "BNTX", "CRSP", "EDIT", "REGN", "VRTX", "GILD", "AMGN"],
    "retail_consumer": ["WMT", "COST", "HD", "LOW", "MCD", "SBUX", "KO", "PEP", "PG"],
    "industrials": ["CAT", "GE", "HON", "RTX", "UPS"],
    "china": ["BABA", "PDD", "JD", "NIO"],
    "meme_speculative": ["GME", "AMC", "MARA", "RIOT"],
    "streaming_social": ["NFLX", "SNAP", "PINS", "RBLX", "SHOP"],
}

# Build reverse lookup: symbol -> group name
_SYMBOL_TO_GROUP = {}
for group, symbols in PEER_GROUPS.items():
    for s in symbols:
        _SYMBOL_TO_GROUP[s] = group


# ── 1. Peer Correlation ──────────────────────────────────────

def analyze_peer_results(symbol: str) -> dict:
    """Check if sector peers already reported and what happened.

    If 3 out of 4 semis beat and gapped up, the 4th (unreported) gets a boost.
    """
    result = {"signal": 0, "details": [], "peers_reported": 0, "peers_beat": 0}

    group_name = _SYMBOL_TO_GROUP.get(symbol)
    if not group_name:
        return result

    peers = [s for s in PEER_GROUPS[group_name] if s != symbol]
    if not peers:
        return result

    reported = []
    for peer in peers:
        try:
            t = yf.Ticker(peer)
            hist = t.earnings_history
            if hist is None or hist.empty:
                continue

            latest = hist.iloc[0]
            surprise = latest.get("surprisePercent", 0)
            if surprise is None or np.isnan(surprise):
                continue

            # Check if this is recent (within last 30 days)
            q_date = latest.name
            if hasattr(q_date, 'date'):
                q_date = q_date.date()
            days_ago = (datetime.now().date() - q_date).days if hasattr(q_date, 'day') else 999

            if days_ago <= 45:
                beat = surprise > 0.01
                reported.append({
                    "peer": peer,
                    "beat": beat,
                    "surprise_pct": round(surprise * 100, 1),
                    "days_ago": days_ago,
                })
        except Exception:
            continue

    result["peers_reported"] = len(reported)
    result["peers_beat"] = sum(1 for r in reported if r["beat"])

    if len(reported) >= 2:
        beat_rate = result["peers_beat"] / len(reported)
        if beat_rate >= 0.75:
            result["signal"] += 20
            result["details"].append(
                f"PEER_BULLISH: {result['peers_beat']}/{len(reported)} {group_name} peers beat "
                f"({', '.join(r['peer'] + ':' + str(r['surprise_pct']) + '%' for r in reported[:3])})"
            )
        elif beat_rate <= 0.25:
            result["signal"] -= 15
            result["details"].append(
                f"PEER_BEARISH: only {result['peers_beat']}/{len(reported)} {group_name} peers beat"
            )
        elif len(reported) >= 3:
            result["details"].append(
                f"PEER_MIXED: {result['peers_beat']}/{len(reported)} {group_name} beat"
            )

    return result


# ── 2. Post-Earnings Reaction Analysis ───────────────────────

def analyze_recent_reporters(symbol: str) -> dict:
    """For stocks that JUST reported — analyze the post-ER price action.

    Gap + follow-through = guidance was good → drift play.
    Gap + reversal = guidance was bad despite beat → avoid drift.
    """
    result = {"signal": 0, "details": [], "gap_pct": 0, "follow_through": False}

    try:
        t = yf.Ticker(symbol)
        hist = t.earnings_history
        if hist is None or hist.empty:
            return result

        latest = hist.iloc[0]
        surprise = latest.get("surprisePercent", 0)
        q_date = latest.name
        if hasattr(q_date, 'date'):
            q_date = q_date.date()

        days_since = (datetime.now().date() - q_date).days if hasattr(q_date, 'day') else 999
        if days_since > 10 or days_since < 0:
            return result

        # Get price data around earnings
        df = t.history(period="1mo", interval="1d", auto_adjust=True)
        if df.empty or len(df) < 5:
            return result
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close = df["Close"].values

        # Day 0 = pre-ER close, Day 1 = post-ER open
        if len(close) >= days_since + 2:
            pre_er = close[-(days_since + 1)]
            post_er = close[-days_since] if days_since > 0 else close[-1]
            current = close[-1]

            gap_pct = (post_er - pre_er) / pre_er * 100
            drift_pct = (current - post_er) / post_er * 100

            result["gap_pct"] = round(gap_pct, 1)

            if gap_pct > 3 and drift_pct > 0:
                result["signal"] += 20
                result["follow_through"] = True
                result["details"].append(
                    f"POST_ER_DRIFT: gapped +{gap_pct:.1f}%, continued +{drift_pct:.1f}% — "
                    f"guidance likely strong. DRIFT PLAY ACTIVE."
                )
            elif gap_pct > 3 and drift_pct < -2:
                result["signal"] -= 10
                result["details"].append(
                    f"POST_ER_REVERSAL: gapped +{gap_pct:.1f}% but reversed {drift_pct:+.1f}% — "
                    f"guidance may have been weak despite beat. AVOID."
                )
            elif gap_pct < -3 and drift_pct > 2:
                result["signal"] += 10
                result["details"].append(
                    f"POST_ER_BOUNCE: gapped {gap_pct:+.1f}% then bounced +{drift_pct:.1f}% — "
                    f"overreaction, long entry if above pre-ER close."
                )
            elif gap_pct < -5 and drift_pct < 0:
                result["signal"] -= 15
                result["details"].append(
                    f"POST_ER_CONTINUATION_DOWN: gapped {gap_pct:+.1f}%, still falling {drift_pct:+.1f}% — "
                    f"fundamental problem. AVOID longs."
                )

    except Exception as e:
        _log.debug(f"{symbol} post-ER analysis error: {e}")

    return result


# ── 3. News Sentiment (Keyword-Based) ────────────────────────

# Keywords that historically move stocks
BULLISH_KEYWORDS = [
    "upgrade", "beats", "beat expectations", "raises guidance", "raised outlook",
    "record revenue", "strong demand", "partnership", "contract win", "fda approval",
    "approved", "buyback", "share repurchase", "dividend increase", "price target raised",
    "outperform", "buy rating", "ai demand", "growth accelerat",
]

BEARISH_KEYWORDS = [
    "downgrade", "misses", "missed expectations", "cuts guidance", "lowered outlook",
    "revenue decline", "weak demand", "lawsuit", "sec investigation", "fda rejection",
    "recalled", "layoffs", "restructuring", "price target cut", "sell rating",
    "underperform", "warning", "loss widens", "debt concern", "bankruptcy",
]


def analyze_news_sentiment(symbol: str, use_llm: bool = True) -> dict:
    """Scan yfinance news headlines for sentiment — keyword + optional LLM deep analysis."""
    result = {"signal": 0, "details": [], "headlines": [], "bullish_count": 0, "bearish_count": 0, "llm_analysis": ""}

    try:
        t = yf.Ticker(symbol)
        news = t.news
        if not news:
            return result

        for article in news[:10]:
            title = str(article.get("title", "")).lower()
            result["headlines"].append(title[:80])

            bull_matches = [kw for kw in BULLISH_KEYWORDS if kw in title]
            bear_matches = [kw for kw in BEARISH_KEYWORDS if kw in title]

            if bull_matches:
                result["bullish_count"] += 1
            if bear_matches:
                result["bearish_count"] += 1

        net = result["bullish_count"] - result["bearish_count"]
        if net >= 3:
            result["signal"] += 20
            result["details"].append(
                f"NEWS_BULLISH: {result['bullish_count']} positive vs {result['bearish_count']} negative headlines"
            )
        elif net >= 1:
            result["signal"] += 8
            result["details"].append(f"NEWS_POSITIVE: {result['bullish_count']}B/{result['bearish_count']}N")
        elif net <= -3:
            result["signal"] -= 20
            result["details"].append(
                f"NEWS_BEARISH: {result['bearish_count']} negative vs {result['bullish_count']} positive headlines"
            )
        elif net <= -1:
            result["signal"] -= 8
            result["details"].append(f"NEWS_NEGATIVE: {result['bullish_count']}B/{result['bearish_count']}N")

        # LLM deep analysis (Ollama) — only for stocks with upcoming earnings
        if use_llm and result["headlines"]:
            llm_result = _llm_analyze_headlines(symbol, result["headlines"])
            if llm_result:
                result["llm_analysis"] = llm_result.get("analysis", "")
                llm_signal = llm_result.get("signal", 0)
                if abs(llm_signal) > 5:
                    result["signal"] += llm_signal
                    result["details"].append(f"LLM: {llm_result.get('summary', '')}")

    except Exception as e:
        _log.debug(f"{symbol} news error: {e}")

    return result


def _llm_analyze_headlines(symbol: str, headlines: list[str]) -> dict | None:
    """Use Ollama to analyze headlines more deeply than keyword matching."""
    import json as _json
    from urllib.request import Request, urlopen

    OLLAMA_URL = "http://localhost:11434"

    try:
        # Check Ollama available
        req = Request(f"{OLLAMA_URL}/api/version", method="GET")
        urlopen(req, timeout=2)
    except Exception:
        return None

    try:
        headlines_text = "\n".join(f"  - {h}" for h in headlines[:8])
        prompt = (
            f"You are analyzing news headlines for {symbol} before an upcoming earnings report. "
            f"Rate the sentiment for the stock price on a scale of -10 (very bearish) to +10 (very bullish).\n\n"
            f"Headlines:\n{headlines_text}\n\n"
            f"Reply in exactly this format:\n"
            f"SCORE: [number from -10 to 10]\n"
            f"SUMMARY: [one sentence explaining why]\n"
            f"Nothing else."
        )

        payload = _json.dumps({
            "model": "llama3.2:3b",
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 100},
        }).encode()
        req = Request(f"{OLLAMA_URL}/api/generate", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
        resp = _json.loads(urlopen(req, timeout=15).read())
        text = resp.get("response", "").strip()

        # Parse response
        score = 0
        summary = ""
        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("SCORE:"):
                try:
                    score = int(float(line.split(":", 1)[1].strip().split()[0]))
                    score = max(-10, min(10, score))
                except (ValueError, IndexError):
                    pass
            elif line.upper().startswith("SUMMARY:"):
                summary = line.split(":", 1)[1].strip()

        return {"signal": score, "summary": summary, "analysis": text}
    except Exception:
        return None


# ── 4. Macro Context ─────────────────────────────────────────

def analyze_macro_context() -> dict:
    """Check broad market conditions before taking any trade.

    SPY trend + VIX level + recent market action.
    """
    result = {
        "signal": 0, "details": [],
        "spy_trend": "neutral", "vix_level": 0,
        "spy_5d_ret": 0, "market_risk": "normal",
    }

    try:
        # SPY
        spy = yf.Ticker("SPY")
        spy_df = spy.history(period="6mo", interval="1d", auto_adjust=True)
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_df.columns = spy_df.columns.get_level_values(0)

        if not spy_df.empty and len(spy_df) >= 200:
            close = spy_df["Close"].values
            ema_50 = pd.Series(close).ewm(span=50).mean().values
            ema_200 = pd.Series(close).ewm(span=200).mean().values
            i = len(close) - 1

            ret_5d = (close[i] / close[max(0, i - 5)] - 1) * 100
            result["spy_5d_ret"] = round(ret_5d, 1)

            if close[i] > ema_50[i] > ema_200[i]:
                result["spy_trend"] = "bullish"
                result["signal"] += 10
                result["details"].append(f"MACRO_BULL: SPY above 50 & 200 EMA ({ret_5d:+.1f}% 5d)")
            elif close[i] < ema_50[i] < ema_200[i]:
                result["spy_trend"] = "bearish"
                result["signal"] -= 15
                result["details"].append(f"MACRO_BEAR: SPY below 50 & 200 EMA ({ret_5d:+.1f}% 5d)")
            elif close[i] < ema_200[i]:
                result["spy_trend"] = "weak"
                result["signal"] -= 8
                result["details"].append(f"MACRO_WEAK: SPY below 200 EMA ({ret_5d:+.1f}% 5d)")

            # Crash detection
            if ret_5d < -5:
                result["market_risk"] = "high"
                result["signal"] -= 20
                result["details"].append(f"MARKET_CRASH: SPY {ret_5d:+.1f}% in 5d — AVOID new longs")
            elif ret_5d > 5:
                result["signal"] += 5
                result["details"].append(f"MARKET_RALLY: SPY +{ret_5d:.1f}% in 5d")

        # VIX
        vix = yf.Ticker("^VIX")
        vix_df = vix.history(period="5d", interval="1d")
        if isinstance(vix_df.columns, pd.MultiIndex):
            vix_df.columns = vix_df.columns.get_level_values(0)
        if not vix_df.empty:
            vix_level = float(vix_df["Close"].iloc[-1])
            result["vix_level"] = round(vix_level, 1)

            if vix_level > 30:
                result["market_risk"] = "high"
                result["signal"] -= 15
                result["details"].append(f"HIGH_VIX: {vix_level:.0f} — extreme fear, reduce size")
            elif vix_level > 25:
                result["signal"] -= 5
                result["details"].append(f"ELEVATED_VIX: {vix_level:.0f}")
            elif vix_level < 15:
                result["signal"] += 5
                result["details"].append(f"LOW_VIX: {vix_level:.0f} — complacent market")

    except Exception as e:
        _log.debug(f"Macro analysis error: {e}")

    return result


# ── Combined Context Signal ──────────────────────────────────

def get_full_context(symbol: str) -> dict:
    """Run all context analyses for a symbol."""
    peers = analyze_peer_results(symbol)
    post_er = analyze_recent_reporters(symbol)
    news = analyze_news_sentiment(symbol)
    macro = analyze_macro_context()

    total = peers["signal"] + post_er["signal"] + news["signal"] + macro["signal"]
    all_details = peers["details"] + post_er["details"] + news["details"] + macro["details"]

    return {
        "symbol": symbol,
        "total_signal": max(-100, min(100, total)),
        "details": all_details,
        "peers": peers,
        "post_er": post_er,
        "news": news,
        "macro": macro,
    }
