# line above: from __future__ import annotations
from __future__ import annotations

from types import SimpleNamespace
from decimal import Decimal

from config import load_config
from state import BotState
from engine import step
from feed_coinbase import make_http

def main():
    cfg = load_config()
    state = BotState.from_config(cfg)

    tick = SimpleNamespace(
        ts="debug",
        epoch=1730000000,  # seconds epoch
        px=Decimal("3000.00"),
    )

    http = make_http()
    try:
        snap = step(state, tick, cfg, paused=False, http=http)
        print("action:", snap.action)
        print("reason:", snap.action_reason)
        print("conf_gate:", getattr(snap, "confluence_gate", ""))
        if snap.events:
            for e in snap.events:
                print("EV:", e.name, e.message)
    finally:
        try:
            http.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
