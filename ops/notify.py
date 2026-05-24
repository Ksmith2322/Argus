#!/usr/bin/env python3
"""ops/notify.py -- Discord webhook notifications for Argus.

Usage:
    python ops/notify.py --test "Hello from Argus"
    python ops/notify.py --backtest-complete <run_id>
    python ops/notify.py --error "Something broke"

Requires DISCORD_WEBHOOK_URL env var (or in .env file). Silently skips if not set.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_webhook_url() -> str:
    # Precedence (highest first): env var → secrets/discord.env → .env
    # The secrets/ path is .gitignored so operators can rotate the webhook
    # without touching tracked files. .env is kept for back-compat during
    # the migration; remove once all 30 consumers also read from secrets/.
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if url:
        return url
    for candidate in (REPO / "secrets" / "discord.env", REPO / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DISCORD_WEBHOOK_URL=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return ""


def send_discord(message: str = "", embed: dict = None, webhook_url: str = None) -> bool:
    url = webhook_url or _load_webhook_url()
    if not url:
        return False
    payload = {}
    if message:
        payload["content"] = message
    if embed:
        payload["embeds"] = [embed]
    if not payload:
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "Argus/1.0",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status in (200, 204)
    except Exception as e:
        print(f"[notify] Discord send failed: {e}", file=sys.stderr)
        return False


def notify_backtest_complete(run_id: str, summary: dict = None):
    if summary is None:
        summary_path = REPO / "ops" / "logs" / f"bt_summary_{run_id}.json"
        if summary_path.exists():
            with open(summary_path) as f:
                summary = json.load(f)
        else:
            send_discord(f"Backtest `{run_id}` completed (no summary found)")
            return

    trades = summary.get("trades_closed", 0)
    wr = summary.get("win_rate_pct", "0")
    pf = summary.get("profit_factor", "0")
    pnl = summary.get("pnl_usd", "0")
    dd = summary.get("max_drawdown_pct", "0")
    exp = summary.get("expectancy_usd", "0")
    fills = summary.get("entry_filled", 0)

    pnl_f = float(pnl) if pnl else 0
    color = 0x00E676 if pnl_f > 0 else 0xFF5252 if pnl_f < 0 else 0xBDBDBD

    embed = {
        "title": f"Backtest Complete: {run_id[-20:]}",
        "color": color,
        "fields": [
            {"name": "Trades", "value": str(trades), "inline": True},
            {"name": "Win Rate", "value": f"{wr}%", "inline": True},
            {"name": "PF", "value": str(pf), "inline": True},
            {"name": "PnL", "value": f"${pnl}", "inline": True},
            {"name": "Max DD", "value": f"{dd}%", "inline": True},
            {"name": "E[$/t]", "value": f"${exp}", "inline": True},
            {"name": "Fills", "value": str(fills), "inline": True},
        ],
        "footer": {"text": "Argus Backtest Engine"},
    }
    send_discord(embed=embed)


def notify_trade(action: str, symbol: str, price: float, qty: float):
    color = 0x00E676 if action.upper() == "BUY" else 0xFF5252
    embed = {
        "title": f"{action.upper()} {symbol}",
        "color": color,
        "fields": [
            {"name": "Price", "value": f"${price:.2f}", "inline": True},
            {"name": "Qty", "value": f"{qty:.8f}", "inline": True},
            {"name": "Notional", "value": f"${price * qty:.2f}", "inline": True},
        ],
        "footer": {"text": "Argus Live Runner"},
    }
    send_discord(embed=embed)


def notify_trade_close(row: dict):
    """Send Discord embed for a closed trade with full details."""
    pnl = float(row.get("pnl", 0) or 0)
    color = 0x00E676 if pnl > 0 else 0xFF5252  # green/red
    symbol = row.get("symbol", "?")
    entry_px = row.get("entry_px", "?")
    exit_px = row.get("exit_px", "?")
    regime = row.get("regime_at_entry", "?")
    exit_reason = row.get("exit_reason", "?")
    dur_s = int(float(row.get("trade_duration_s", 0) or 0))
    if dur_s < 60:
        dur_str = f"{dur_s}s"
    elif dur_s < 3600:
        dur_str = f"{dur_s // 60}m{dur_s % 60:02d}s"
    else:
        dur_str = f"{dur_s // 3600}h{(dur_s % 3600) // 60:02d}m"

    result = "WIN" if pnl > 0 else "LOSS"
    embed = {
        "title": f"Trade Closed: {symbol} ({result})",
        "color": color,
        "fields": [
            {"name": "PnL", "value": f"${pnl:+.4f}", "inline": True},
            {"name": "Entry", "value": f"${entry_px}", "inline": True},
            {"name": "Exit", "value": f"${exit_px}", "inline": True},
            {"name": "Duration", "value": dur_str, "inline": True},
            {"name": "Exit Reason", "value": str(exit_reason), "inline": True},
            {"name": "Regime", "value": str(regime), "inline": True},
        ],
        "footer": {"text": "Argus Live Runner"},
    }
    send_discord(embed=embed)


def notify_daily_digest(coin_stats: list):
    """Send end-of-day P&L digest for all active coins.

    coin_stats: list of dicts with keys: symbol, trades_today, daily_pnl_usd, total_pnl_usd, cash
    """
    if not coin_stats:
        return

    total_daily = sum(float(c.get("daily_pnl_usd", 0) or 0) for c in coin_stats)
    total_pnl = sum(float(c.get("total_pnl_usd", 0) or 0) for c in coin_stats)
    color = 0x00E676 if total_daily >= 0 else 0xFF5252

    fields = []
    for c in coin_stats:
        sym = c.get("symbol", "?")
        dpnl = float(c.get("daily_pnl_usd", 0) or 0)
        trades = int(c.get("trades_today", 0) or 0)
        cash = float(c.get("cash", 0) or 0)
        sign = "+" if dpnl >= 0 else ""
        fields.append({
            "name": sym,
            "value": f"Daily: {sign}${dpnl:.2f} | Trades: {trades} | Cash: ${cash:.2f}",
            "inline": False,
        })

    sign_total = "+" if total_daily >= 0 else ""
    fields.append({"name": "Portfolio Daily", "value": f"{sign_total}${total_daily:.2f}", "inline": True})
    fields.append({"name": "All-Time PnL", "value": f"${total_pnl:.2f}", "inline": True})

    embed = {
        "title": "Daily Digest",
        "color": color,
        "fields": fields,
        "footer": {"text": "Argus Daily Summary"},
    }
    send_discord(embed=embed)


def notify_error(error: str):
    embed = {
        "title": "Argus Error",
        "description": error[:2000],
        "color": 0xFF0000,
        "footer": {"text": "Argus Alert"},
    }
    send_discord(embed=embed)


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python ops/notify.py --test 'msg' | --backtest-complete <run_id> | --error 'msg'")
        return

    if args[0] == "--test":
        msg = args[1] if len(args) > 1 else "Test notification from Argus"
        ok = send_discord(msg)
        print(f"Sent: {ok}")
    elif args[0] == "--backtest-complete":
        if len(args) < 2:
            print("Usage: --backtest-complete <run_id>")
            return
        notify_backtest_complete(args[1])
    elif args[0] == "--error":
        msg = args[1] if len(args) > 1 else "Unknown error"
        notify_error(msg)
    else:
        print(f"Unknown flag: {args[0]}")


if __name__ == "__main__":
    main()