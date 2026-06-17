# Halt Truth Reconciliation

- Halted: True
- Sources: FLATTEN_EOD.flag
- Reason: flatten active: auto-flatten: daily loss -4.02% (FLATTEN tier)
- HALT.flag present: False
- FLATTEN_EOD.flag present: True
- Broker drift tripped: False
- Broker drift state timestamp: 2026-05-08T22:50:45.833812+00:00

Runtime execution now reads the reconciled halt state through `helio.halt_state`.
If any source is tripped, new entries fail closed.
