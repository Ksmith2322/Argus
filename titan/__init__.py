"""Titan -- Large swing trade system for stocks, gold, oil.

Catches big moves (5-20%) on 1H/4H/Daily timeframes.
Separate from Argus (FX intraday) -- different edge, different instruments.

Instruments: NVDA, PLTR, SOFI, TSLA, GDX, GLD, USO, MRNA + breakout scanner picks
Timeframes: 1H, 4H, Daily
Hold period: 2-20 days
Strategy: Trendline cascade + breakout + momentum
"""
