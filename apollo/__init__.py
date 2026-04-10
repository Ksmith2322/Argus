"""Apollo -- Earnings & catalyst event scanner.

God of prophecy. Finds stocks likely to make big moves on upcoming earnings
or news catalysts. Surfaces opportunities 1-5 days before the event.

NOT an execution system — alerts you with scored setups, you decide.
Execute via IBKR manually or through Titan if the setup qualifies.

Strategies:
  1. Pre-earnings compression (BB squeeze before earnings = coiled spring)
  2. Volume buildup (unusual accumulation before announcement)
  3. Post-earnings drift (buy winners after positive surprise, hold 5-20d)
  4. Analyst revision momentum (upgrades/downgrades cluster before moves)
"""
