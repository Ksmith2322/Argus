# line above: (new file)

#!/usr/bin/env python3
"""
run_main.py

VS Code / double-click safe launcher for the trade bot.

This ensures the project root is on sys.path and then runs
the real module:

    nova_scripts.trade_bot.main
"""

import os
import sys
import runpy


def main():
    # Path to: .../Nova
    here = os.path.dirname(os.path.abspath(__file__))          # .../nova_scripts/trade_bot
    project_root = os.path.abspath(os.path.join(here, "..", ".."))

    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    runpy.run_module("nova_scripts.trade_bot.main", run_name="__main__")


if __name__ == "__main__":
    main()
