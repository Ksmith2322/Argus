"""
Atlas — Global Macro Event Intelligence Runner
================================================
Polls RSS feeds, classifies events, tracks cascades, publishes regime state.

Usage:
    python -m forge.atlas.runner              # single poll cycle
    python -m forge.atlas.runner --loop       # continuous monitoring
    python -m forge.atlas.runner --status     # show current regime + recent events
    python -m forge.atlas.runner --backfill   # populate scheduled events
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env for Discord webhook etc.
_env_path = Path(__file__).resolve().parents[2] / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "atlas"
LOG_DIR.mkdir(parents=True, exist_ok=True)

HEARTBEAT_PATH = LOG_DIR / "heartbeat.json"

# Logging setup
log = logging.getLogger("atlas")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
log.addHandler(_sh)
_fh = logging.FileHandler(LOG_DIR / "runner.log", encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)

# Discord webhook (reuse fleet's)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")


def _send_discord(message: str):
    """Send a Discord alert. Fire-and-forget."""
    if not DISCORD_WEBHOOK:
        return
    try:
        import subprocess
        subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-X", "POST",
             DISCORD_WEBHOOK,
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"content": message[:2000]})],
            timeout=10, capture_output=True,
        )
    except Exception as e:
        log.warning("Discord send failed: %s", e)


def run_cycle() -> dict:
    """
    One full Atlas cycle:
    1. Poll RSS feeds
    2. Classify headlines
    3. Store new events in DB
    4. Update regime state
    5. Generate cascade forecasts for high-severity events
    6. Write heartbeat

    Returns summary dict.
    """
    from forge.atlas.sources.rss_poller import poll_all_feeds
    from forge.atlas.sources.gdelt_poller import poll_all_gdelt
    from forge.atlas.classify.keyword_rules import classify_headline
    from forge.atlas.db.schema import init_db, get_connection, DB_PATH
    from forge.atlas.db.queries import insert_event, compute_dedupe_hash, get_recent_events, event_count_by_type
    from forge.atlas.cascade.templates import format_cascade_forecast, get_cascade_template
    from forge.atlas.regime.state import fetch_regime_inputs, compute_regime, update_regime_file, get_position_size_modifier, get_fleet_guidance

    cycle_start = time.time()
    summary = {
        "polled": 0,
        "classified": 0,
        "new_events": 0,
        "high_severity": 0,
        "errors": [],
    }

    # --- 1. Poll feeds (RSS + GDELT) ---
    items = []
    try:
        rss_items = poll_all_feeds()
        items.extend(rss_items)
        log.info("RSS: %d items", len(rss_items))
    except Exception as e:
        log.error("RSS polling failed: %s", e)
        summary["errors"].append(f"RSS: {e}")

    try:
        gdelt_items = poll_all_gdelt()
        items.extend(gdelt_items)
        log.info("GDELT: %d items", len(gdelt_items))
    except Exception as e:
        log.error("GDELT polling failed: %s", e)
        summary["errors"].append(f"GDELT: {e}")

    summary["polled"] = len(items)
    log.info("Total polled: %d items", len(items))

    # --- 2+3. Classify and store ---
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)

    new_high_severity = []

    try:
        for item in items:
            result = classify_headline(item["title"], item.get("summary", ""))
            if result is None:
                continue

            summary["classified"] += 1

            event_dict = {
                "timestamp": item.get("published", datetime.now(timezone.utc).isoformat()),
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "event_type": result["event_type"],
                "event_category": result["event_category"],
                "severity": result["severity"],
                "title": item["title"],
                "description": item.get("summary", ""),
                "source": item.get("source", ""),
                "source_url": item.get("link", ""),
            }

            event_id = insert_event(conn, event_dict)
            if event_id:  # new event (not duplicate)
                summary["new_events"] += 1

                if result["severity"] >= 0.6:
                    summary["high_severity"] += 1
                    new_high_severity.append({
                        "event_id": event_id,
                        "type": result["event_type"],
                        "severity": result["severity"],
                        "title": item["title"],
                    })
    except Exception as e:
        log.error("Classification/storage error: %s", e)
        summary["errors"].append(f"classify: {e}")

    # --- 3b. Create cascade predictions for high-severity events ---
    try:
        from forge.atlas.cascade.tracker import create_predictions, score_due_predictions
        for event in new_high_severity:
            n_preds = create_predictions(
                event["event_id"], event["type"], event["severity"],
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), conn,
            )
            if n_preds:
                log.info("Created %d cascade predictions for %s", n_preds, event["type"])

        # Score any predictions that are past their deadline
        score_result = score_due_predictions(conn)
        if score_result["scored"] > 0:
            log.info("Scored %d predictions: %d/%d correct",
                     score_result["scored"], score_result["correct"], score_result["scored"])
    except Exception as e:
        log.error("Cascade tracking error: %s", e)
        summary["errors"].append(f"cascade: {e}")
    finally:
        conn.close()

    # --- 4. Update regime ---
    regime = None
    try:
        inputs = fetch_regime_inputs()
        regime = compute_regime(
            vix=inputs.get("vix", 20),
            yield_2y_change_60d=inputs.get("yield_2y_change_60d", 0),
            ism=inputs.get("ism", 49),
            hy_spread_change_20d=inputs.get("hy_spread_change_20d", 0),
        )

        # Add fleet guidance with active events
        active_event_types = [e["type"] for e in new_high_severity]
        guidance = get_fleet_guidance(regime, active_event_types)
        regime["fleet_guidance"] = guidance
        regime["position_size_modifier"] = get_position_size_modifier(regime)

        update_regime_file(regime)
        log.info("Regime: %s | vol=%s rates=%s growth=%s liquidity=%s | size_mod=%.2f",
                 regime.get("overall", "?"),
                 regime.get("vol", "?"),
                 regime.get("rates", "?"),
                 regime.get("growth", "?"),
                 regime.get("liquidity", "?"),
                 regime.get("position_size_modifier", 1.0))
    except Exception as e:
        log.error("Regime update failed: %s", e)
        summary["errors"].append(f"regime: {e}")

    # --- 5. Discord alerts for high-severity events ---
    for event in new_high_severity:
        forecast = format_cascade_forecast(event["type"], event["severity"])
        msg = (
            f"**ATLAS EVENT: {event['type']}** (severity {event['severity']:.2f})\n"
            f"> {event['title'][:200]}\n"
        )
        if forecast:
            msg += f"\n**Cascade Forecast:**\n{forecast[:800]}"

        log.info("HIGH SEVERITY: [%s] sev=%.2f | %s", event["type"], event["severity"], event["title"][:100])
        _send_discord(msg)

    # --- 6. Heartbeat ---
    conn = get_connection(DB_PATH)
    try:
        recent = get_recent_events(conn, hours=1)
        events_today_count = len(get_recent_events(conn, hours=24))
    except Exception:
        recent = []
        events_today_count = 0
    finally:
        conn.close()

    heartbeat = {
        "system": "atlas",
        "family": "forge",
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
        "mode": "monitor",
        "events_last_hour": len(recent),
        "events_today": events_today_count,
        "new_this_cycle": summary["new_events"],
        "high_severity_this_cycle": summary["high_severity"],
        "regime": regime.get("overall", "unknown") if regime else "unknown",
        "alert_level": regime.get("alert_level", "unknown") if regime else "unknown",
        "position_size_modifier": regime.get("position_size_modifier", 1.0) if regime else 1.0,
        "cycle_duration_s": round(time.time() - cycle_start, 1),
    }
    HEARTBEAT_PATH.write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")

    log.info("Cycle complete: %d polled, %d classified, %d new, %d high-sev (%.1fs)",
             summary["polled"], summary["classified"], summary["new_events"],
             summary["high_severity"], time.time() - cycle_start)

    return summary


def show_status():
    """Print current Atlas status."""
    from forge.atlas.db.schema import init_db, get_connection, DB_PATH
    from forge.atlas.db.queries import get_recent_events, event_count_by_type

    # Heartbeat
    if HEARTBEAT_PATH.exists():
        hb = json.loads(HEARTBEAT_PATH.read_text())
        print(f"\nAtlas Heartbeat:")
        print(f"  Last run:     {hb.get('ts', '?')}")
        print(f"  Regime:       {hb.get('regime', '?')}")
        print(f"  Alert level:  {hb.get('alert_level', '?')}")
        print(f"  Size modifier:{hb.get('position_size_modifier', '?')}")
        print(f"  Events today: {hb.get('events_today', '?')}")
    else:
        print("\nNo heartbeat found — Atlas hasn't run yet.")

    # Regime file
    regime_path = Path(__file__).resolve().parent / "macro_regime.json"
    if regime_path.exists():
        regime = json.loads(regime_path.read_text())
        print(f"\nMacro Regime:")
        r = regime.get("regime", {})
        print(f"  Overall:    {r.get('overall', '?')}")
        print(f"  Vol:        {r.get('vol', '?')}")
        print(f"  Rates:      {r.get('rates', '?')}")
        print(f"  Growth:     {r.get('growth', '?')}")
        print(f"  Liquidity:  {r.get('liquidity', '?')}")
        g = regime.get("fleet_guidance", {})
        if g.get("sectors_avoid"):
            print(f"  Avoid:      {', '.join(g['sectors_avoid'])}")
        if g.get("sectors_favor"):
            print(f"  Favor:      {', '.join(g['sectors_favor'])}")

    # Recent events
    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    try:
        recent = get_recent_events(conn, hours=24)
        counts = event_count_by_type(conn)
    finally:
        conn.close()

    if recent:
        print(f"\nRecent Events (last 24h): {len(recent)}")
        high_sev = [e for e in recent if e.get("severity", 0) >= 0.6]
        if high_sev:
            print(f"  High severity (>=0.6): {len(high_sev)}")
            for e in high_sev[:10]:
                print(f"    [{e['event_type']}] sev={e['severity']:.2f} | {e['title'][:80]}")

    if counts:
        print(f"\nAll-time event counts by type:")
        for etype, count in sorted(counts.items(), key=lambda x: -x[1])[:15]:
            print(f"  {etype:30s} {count:5d}")


def backfill_scheduled():
    """Populate upcoming scheduled events."""
    from forge.atlas.db.schema import init_db, get_connection, DB_PATH
    from forge.atlas.db.queries import insert_scheduled_event

    # Upcoming known events (manually curated)
    events = [
        # FOMC 2026
        ("2026-04-29", "FOMC_DECISION", "FOMC rate decision + press conference", "high"),
        ("2026-06-17", "FOMC_DECISION", "FOMC rate decision + SEP/dot plot", "high"),
        ("2026-07-29", "FOMC_DECISION", "FOMC rate decision + press conference", "high"),
        ("2026-09-16", "FOMC_DECISION", "FOMC rate decision + SEP/dot plot", "high"),
        ("2026-10-28", "FOMC_DECISION", "FOMC rate decision + press conference", "high"),
        ("2026-12-16", "FOMC_DECISION", "FOMC rate decision + SEP/dot plot", "high"),
        # CPI releases (approximate — usually mid-month)
        ("2026-04-15", "CPI_RELEASE", "March 2026 CPI", "high"),
        ("2026-05-13", "CPI_RELEASE", "April 2026 CPI", "high"),
        ("2026-06-10", "CPI_RELEASE", "May 2026 CPI", "high"),
        # NFP (first Friday of month)
        ("2026-05-01", "NFP_RELEASE", "April 2026 jobs report", "high"),
        ("2026-06-05", "NFP_RELEASE", "May 2026 jobs report", "high"),
        ("2026-07-02", "NFP_RELEASE", "June 2026 jobs report", "high"),
        # Earnings season
        ("2026-04-14", "EARNINGS_SEASON", "Q1 2026 earnings begin (big banks)", "medium"),
        ("2026-04-16", "EARNINGS_SEASON", "TSM + NFLX earnings (Apollo test)", "high"),
        # ECB
        ("2026-04-17", "ECB_DECISION", "ECB rate decision", "medium"),
        ("2026-06-04", "ECB_DECISION", "ECB rate decision", "medium"),
    ]

    init_db(DB_PATH)
    conn = get_connection(DB_PATH)
    try:
        for date, etype, desc, importance in events:
            insert_scheduled_event(conn, date, etype, desc, importance)
        print(f"Backfilled {len(events)} scheduled events.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Atlas — Global Macro Event Intelligence",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--loop", action="store_true", help="Run continuously (poll every --interval-sec)")
    parser.add_argument("--interval-sec", type=int, default=120, help="Poll interval in seconds (default: 120)")
    parser.add_argument("--status", action="store_true", help="Show current regime + recent events")
    parser.add_argument("--backfill", action="store_true", help="Populate scheduled events calendar")
    parser.add_argument("--scorecard", action="store_true", help="Show cascade prediction accuracy")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.backfill:
        backfill_scheduled()
        return

    if args.scorecard:
        from forge.atlas.db.schema import init_db, get_connection, DB_PATH
        from forge.atlas.cascade.tracker import print_scorecard
        init_db(DB_PATH)
        conn = get_connection(DB_PATH)
        try:
            print_scorecard(conn)
        finally:
            conn.close()
        return

    if args.loop:
        log.info("Starting Atlas in LOOP mode (interval=%ds)", args.interval_sec)
        while True:
            try:
                run_cycle()
            except Exception as e:
                log.error("Cycle failed: %s", e)
            log.info("Sleeping %ds until next cycle...", args.interval_sec)
            time.sleep(args.interval_sec)
    else:
        run_cycle()


if __name__ == "__main__":
    main()
