# (top of file)
import os
import sys
import runpy


def _find_project_root(start_dir: str) -> str:
    """
    Walk upwards until we find a directory that looks like the repo root.
    Preference order:
      1) directory containing 'nova_scripts'
      2) directory containing 'pyproject.toml'
      3) directory containing 'requirements.txt'
    Fallback: original computed path (3 levels up).
    """
    cur = os.path.abspath(start_dir)

    for _ in range(10):  # don't walk forever
        if os.path.isdir(os.path.join(cur, "nova_scripts")):
            return cur
        if os.path.isfile(os.path.join(cur, "pyproject.toml")):
            return cur
        if os.path.isfile(os.path.join(cur, "requirements.txt")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent

    # fallback (previous behavior): .../Nova (assumes scripts live under nova_scripts/trade_bot/backtest/)
    return os.path.abspath(os.path.join(start_dir, "..", "..", ".."))


def main() -> None:
    # Add project root (Nova) to sys.path so package imports resolve
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = _find_project_root(here)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # --------- LINE ABOVE: if project_root not in sys.path:
    # Run the package module (keeps relative imports working inside the package)
    runpy.run_module(
        "nova_scripts.trade_bot.backtest.runner",
        run_name="__main__",
    )


if __name__ == "__main__":
    main()
