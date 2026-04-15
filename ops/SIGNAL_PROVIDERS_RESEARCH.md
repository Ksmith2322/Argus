# Signal Provider / Strategy Research

**Date:** 2026-04-15
**Purpose:** Identify additional strategies to reverse-engineer into the fleet

## Already Built
| Provider | System | Status |
|----------|--------|--------|
| MambaFX | Mamba (NQ/YM breakout scalp) | Built, signal-only, tuning needed |
| Tori Trades | Tori (4H trendline swing) | Built, signal-only, tuning needed |
| Cue Banks | Cue Banks (US30 confluence) | Built, signal-only, tuning needed |

## Candidates to Research

### United Kings — Smart Money Concepts (ICT-Style)
- **Style:** Institutional price action — liquidity sweeps, order blocks, structure shifts
- **Assets:** Gold (XAUUSD) + major forex
- **Key concepts:** Break of Structure (BOS), Change of Character (CHOCH), liquidity zones (equal highs/lows), displacement candles
- **Entry:** After liquidity sweep + confirmation of structure shift
- **Bot-friendly:** Yes — BOS/CHOCH detection + liquidity zone identification is codable
- **Priority:** HIGH — fills a gap (no SMC/institutional flow strategy in the fleet)

### 1000pip Builder — Trend + Fundamental Confluence
- **Style:** High-probability trend following on forex majors + gold
- **Key:** MA trend filter + S/R confluence + fundamental alignment
- **Bot-friendly:** Yes — trend filter + zone-based entries
- **Priority:** MEDIUM — similar to what Argus FX already does

### Learn 2 Trade — AI/ML + Technical Hybrid
- **Style:** Algorithmic scanning + technical analysis
- **Key:** Indicator stacks (RSI, MACD, Fib) + sentiment/news filters
- **Bot-friendly:** Yes — could inform Atlas intelligence layer
- **Priority:** LOW — we already have Atlas + FinBERT

### SureShotFX — Momentum/Trend Indicator Stack
- **Style:** Proprietary "Raven" indicator (trend + momentum)
- **Bot-friendly:** Could replicate with standard indicators
- **Priority:** LOW — standard momentum approach

## Copy-Trading Research
- ZuluTrade: Filter for 1-2yr track records, max DD <30%, positive ROI
- eToro Popular Investors: Transparent stats, analyze for repeatable patterns
- MyFxBook: Browse verified EAs/systems for additional strategy ideas

## Recommendation
**Next build:** United Kings SMC/ICT strategy
- Fills a gap: no institutional flow/liquidity analysis in the fleet
- Complements existing: Mamba (scalp) + Cue Banks (confluence) + United Kings (SMC) = three different reading methods on the same instruments
- Codable: BOS/CHOCH detection, liquidity sweep identification, displacement candle recognition
- Would be system #14: "Kings" or "Sovereign"
