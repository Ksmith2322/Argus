"""Oracle -- Polymarket prediction market trading system.

Scans Polymarket for mispriced events, volume anomalies, and edge opportunities.
Surfaces the best bets via Discord. Semi-automated — scanner finds opportunities,
human decides.

APIs:
  - Gamma API: market discovery (https://gamma-api.polymarket.com)
  - Data API: analytics (https://data-api.polymarket.com)
  - CLOB API: execution (https://clob.polymarket.com) — future, needs auth
"""
