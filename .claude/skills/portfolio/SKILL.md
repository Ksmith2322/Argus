---
name: portfolio
description: Show portfolio-level risk across all Greek family strategies. Open positions, exposure, correlation, directional bias.
allowed-tools: Bash Read
---

Show cross-strategy portfolio risk:

1. **Run portfolio guard scan**:
   ```bash
   cd C:/Argus/repo && python -c "from helio.portfolio_guard import check_current; import json; r=check_current(); print(json.dumps(r.metrics, indent=2, default=str))"
   ```

2. **Show**: total open positions, positions by family, directional bias (net long/short), correlated pairs, concentration

3. **Check limits**: max 6 total, max 3 per family, max 4 directional bias, max 2 correlated

4. **If positions exist**: show each position with family, symbol, direction, entry price

5. **Risk assessment**: are we over-concentrated? too directionally biased? any conflicts?
