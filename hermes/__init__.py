"""Hermes -- Gap fill strategy. Daily execution.

Scans for stocks that gap >2% at open, trades the fill back to prior close.
~70% of gaps fill within 1-3 days. Mechanical entry/exit rules.

Universe: Dynamic — scans top movers each morning
Timeframe: Daily, 1-3 day holds
Entry: Gap detected at open
Exit: Fill to prior close (target), 2x gap size (stop), or 3-day timeout
"""
