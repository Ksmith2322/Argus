# control_plane.py
import json
import os
import time
import shutil
import hashlib
import csv
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict, List, Optional, Tuple

# line above: from .config import load_config
from config import load_config
from io_logs import logs_dir, ensure_logs, ensure_signals_header_matches_file


def _now_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "+00:00"


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _cfg_hash(cfg: Dict[str, Any]) -> str:
    # Stable hash of config dict (order independent)
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


def _tail_lines(path: str, n: int) -> List[str]:
    if n <= 0:
        return []
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = block if size >= block else size
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
            lines = data.splitlines()[-n:]
            return [ln.decode("utf-8", errors="replace") for ln in lines]
    except Exception:
        return []


def _read_last_signal_row(signal_csv: str) -> Optional[Dict[str, Any]]:
    """
    Returns last CSV row as dict using header row.
    Uses csv.reader (NOT split(',')) to handle quoted commas correctly.
    """
    if not os.path.exists(signal_csv):
        return None
    try:
        # Read a tail chunk for speed, but parse with csv.
        lines = _tail_lines(signal_csv, 400)
        if not lines:
            return None

        # Header comes from the true file start (source of truth)
        with open(signal_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            hdr = next(reader, None)
            if not hdr:
                return None

        # Parse tail as CSV safely (handles quoted commas)
        tail_text = "\n".join(lines)
        tail_reader = csv.reader(tail_text.splitlines())

        last_row: Optional[List[str]] = None
        for row in tail_reader:
            # skip header if it appears in the tail
            if row == hdr:
                continue
            if row:
                last_row = row

        if not last_row:
            return None

        if len(last_row) != len(hdr):
            # schema drift / partial row
            return {
                "_error": "row_len_mismatch",
                "_expected": len(hdr),
                "_got": len(last_row),
                "_raw": ",".join(last_row[:10]),  # don't dump entire row
            }

        return dict(zip(hdr, last_row))
    except Exception:
        return None


def _events_path() -> str:
    return os.path.join(logs_dir(), "live_events.csv")


def _signals_path() -> str:
    return os.path.join(logs_dir(), "live_signals.csv")


def _audit_path() -> str:
    return os.path.join(logs_dir(), "control_plane_audit.jsonl")


def _pause_file(cfg: Dict[str, Any]) -> str:
    # never CWD-relative; always rooted at logs_dir
    return os.path.join(logs_dir(), cfg.get("PAUSE_FILE", "PAUSE.txt"))


def _kill_file(cfg: Dict[str, Any]) -> str:
    # never CWD-relative; always rooted at logs_dir
    return os.path.join(logs_dir(), cfg.get("KILL_SWITCH_FILE", "KILL_SWITCH.txt"))


def _append_audit(event: str, detail: Dict[str, Any]) -> None:
    rec = {"ts": _now_ts(), "event": event, **detail}
    try:
        with open(_audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    except Exception:
        pass


def _is_paused(cfg: Dict[str, Any]) -> bool:
    return os.path.exists(_pause_file(cfg))


def _set_paused(cfg: Dict[str, Any], paused: bool) -> None:
    p = _pause_file(cfg)
    if paused:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("paused\n")
    else:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def _recent_critical_errors(events_csv: str, window_s: int) -> List[str]:
    """
    Scan events tail and find critical event names inside last window_s seconds.
    CSV-safe: uses csv.reader so commas/quotes in detail fields don't break parsing.

    IMPORTANT: if we see malformed rows (shorter than header), we treat that as a
    blocking condition by emitting "MALFORMED_EVENT_ROW" (conservative).
    """
    crit = {"RUNNER_CRASH", "PRICE_FETCH_FAIL", "PRELOAD_FAIL"}
    out: List[str] = []

    if not os.path.exists(events_csv):
        return out

    lines = _tail_lines(events_csv, 800)
    if not lines:
        return out

    try:
        # Header from the real file start (source of truth)
        with open(events_csv, "r", encoding="utf-8", newline="") as f:
            r = csv.reader(f)
            hdr = next(r, None)
        if not hdr:
            return out

        def _idx(*names: str) -> int:
            for n in names:
                if n in hdr:
                    return hdr.index(n)
            return -1

        # Your file shows: ['ts','symbol','epoch','price','event',...]
        name_i = _idx("event", "name")
        ts_i = _idx("ts", "timestamp")
        if name_i < 0:
            return out  # can't evaluate

        now = time.time()
        cutoff = now - max(0, int(window_s))

        tail_text = "\n".join(lines)
        tail_r = csv.reader(tail_text.splitlines())

        rows: List[List[str]] = []
        for row in tail_r:
            if not row or row == hdr:
                continue
            rows.append(row)

        # Walk backwards; stop after enough signal.
        for row in reversed(rows):
            # If row is too short to even contain the event column, that's schema corruption.
            if len(row) <= name_i:
                out.append("MALFORMED_EVENT_ROW")
                break

            name = (row[name_i] or "").strip()
            if name not in crit:
                continue

            recent = True
            if ts_i >= 0:
                if len(row) <= ts_i:
                    # missing ts column in row -> conservative
                    recent = True
                else:
                    ts = (row[ts_i] or "").strip()
                    try:
                        from datetime import datetime, timezone

                        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if t.tzinfo is None:
                            t = t.replace(tzinfo=timezone.utc)
                        recent = (t.timestamp() >= cutoff)
                    except Exception:
                        # can't parse -> conservative
                        recent = True

            if recent:
                out.append(name)

            if len(out) >= 25:
                break

    except Exception:
        return out

    return out


def _resume_gates(cfg: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    Minimal non-negotiable gates, conservative (false negatives > false positives):
      - warmup complete (no WARMUP_* in last signal row)
      - schema ok (header exists + last row length matches)
      - last tick reasonably recent (based on signals file mtime)
      - no critical errors in tail window
      - kill switch not present
    """
    flags: Dict[str, Any] = {
        "kill_switch_present": os.path.exists(_kill_file(cfg)),
        "schema_ok": True,
        "warmup_complete": True,
        "last_tick_recent": True,
        "recent_crit_errors": [],
    }

    sigp = _signals_path()
    last = _read_last_signal_row(sigp)
    if last is None:
        flags["schema_ok"] = False
        flags["schema_error"] = "no_signals_file_or_empty"
    elif "_error" in last:
        flags["schema_ok"] = False
        flags["schema_error"] = last
    else:
        ar = (last.get("action_reason") or "") + " " + (last.get("risk_blocked_reason") or "")
        if "WARMUP" in ar:
            flags["warmup_complete"] = False
            flags["warmup_reason"] = ar.strip()

        ts = (last.get("ts") or "").strip()
        if not ts:
            flags["last_tick_recent"] = False
            flags["last_tick_reason"] = "missing_ts"
        else:
            try:
                m = os.path.getmtime(sigp)
                if (time.time() - m) > float(cfg.get("RESUME_MAX_SIGNAL_STALENESS_S", 120)):
                    flags["last_tick_recent"] = False
                    flags["last_tick_reason"] = f"signals_mtime_stale_s={int(time.time()-m)}"
            except Exception:
                flags["last_tick_recent"] = False
                flags["last_tick_reason"] = "mtime_check_failed"

    evp = _events_path()
    window_s = _safe_int(cfg.get("RESUME_CRIT_WINDOW_S", 300), 300)
    crits = _recent_critical_errors(evp, window_s=window_s)
    flags["recent_crit_errors"] = crits

    ok = (
        (not flags["kill_switch_present"])
        and flags["schema_ok"]
        and flags["warmup_complete"]
        and flags["last_tick_recent"]
        and (len(crits) == 0)
    )
    return ok, flags


class ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "ArgusControlPlane/0.1"

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _unauth(self) -> None:
        self._json(401, {"error": "unauthorized"})

    def _auth_ok(self) -> bool:
        secret = (getattr(self.server, "auth_secret", "") or "").strip()
        if not secret:
            return False
        got = (self.headers.get("X-ARGUS-AUTH", "") or "").strip()
        return bool(got) and got == secret

    def do_GET(self) -> None:
        if not self._auth_ok():
            return self._unauth()

        cfg: Dict[str, Any] = getattr(self.server, "cfg", {})
        p = urlparse(self.path)
        qs = parse_qs(p.query or "")

        if p.path == "/status":
            sigp = _signals_path()
            last = _read_last_signal_row(sigp)
            payload = {
                "ts": _now_ts(),
                "env": cfg.get("ARGUS_ENV") or os.environ.get("ARGUS_ENV") or "",
                "log_dir": logs_dir(),
                "paused": _is_paused(cfg),
                "kill_switch_present": os.path.exists(_kill_file(cfg)),
                "config_hash": _cfg_hash(cfg),
                "signals_path": sigp,
                "events_path": _events_path(),
                "last_signal": last,
            }
            return self._json(200, payload)

        if p.path == "/signals":
            tail = _safe_int(qs.get("tail", ["200"])[0], 200)
            return self._json(
                200,
                {"path": _signals_path(), "tail": tail, "lines": _tail_lines(_signals_path(), tail)},
            )

        if p.path == "/events":
            tail = _safe_int(qs.get("tail", ["200"])[0], 200)
            return self._json(
                200,
                {"path": _events_path(), "tail": tail, "lines": _tail_lines(_events_path(), tail)},
            )

        if p.path == "/health":
            sigp = _signals_path()
            evp = _events_path()
            du = shutil.disk_usage(logs_dir())
            now = time.time()

            def _age(path: str) -> Optional[int]:
                try:
                    return int(now - os.path.getmtime(path))
                except Exception:
                    return None

            payload = {
                "ts": _now_ts(),
                "uptime_s": int(now - getattr(self.server, "start_time", now)),
                "log_dir": logs_dir(),
                "disk_free_bytes": du.free,
                "disk_total_bytes": du.total,
                "signals_mtime_age_s": _age(sigp),
                "events_mtime_age_s": _age(evp),
                "paused": _is_paused(cfg),
                "kill_switch_present": os.path.exists(_kill_file(cfg)),
                # non-sensitive server fingerprint
                "auth_sha12": getattr(self.server, "auth_sha12", ""),
            }
            return self._json(200, payload)

        return self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if not self._auth_ok():
            return self._unauth()

        cfg: Dict[str, Any] = getattr(self.server, "cfg", {})
        p = urlparse(self.path)

        if p.path == "/pause":
            _set_paused(cfg, True)
            _append_audit(
                "pause",
                {"by": "http", "ip": self.client_address[0] if self.client_address else ""},
            )
            return self._json(200, {"ok": True, "paused": True})

        if p.path == "/resume":
            ok, gates = _resume_gates(cfg)
            if not ok:
                _append_audit(
                    "resume_blocked",
                    {
                        "by": "http",
                        "ip": self.client_address[0] if self.client_address else "",
                        "gates": gates,
                    },
                )
                return self._json(403, {"ok": False, "blocked": True, "gates": gates})

            _set_paused(cfg, False)
            _append_audit(
                "resume",
                {"by": "http", "ip": self.client_address[0] if self.client_address else ""},
            )
            return self._json(200, {"ok": True, "paused": False, "gates": gates})

        return self._json(404, {"error": "not_found"})


def run_control_plane(host: str = "127.0.0.1", port: int = 8787) -> int:
    """
    Starts local-only control plane.
    Auth: X-ARGUS-AUTH header must match ARGUS_CP_SECRET env var.
    """
    ensure_logs()
    ensure_signals_header_matches_file()

    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("Refusing to bind non-local host")

    secret = os.environ.get("ARGUS_CP_SECRET", "").strip()
    if not secret:
        raise SystemExit("Missing required env var: ARGUS_CP_SECRET")

    # line above: cfg = load_config()
    cfg = load_config()
    cfg["ARGUS_ENV"] = os.environ.get("ARGUS_ENV", cfg.get("ARGUS_ENV", ""))

    auth_sha12 = hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]

    srv = HTTPServer((host, int(port)), ControlPlaneHandler)
    srv.auth_secret = secret  # type: ignore[attr-defined]
    srv.cfg = cfg             # type: ignore[attr-defined]
    srv.start_time = time.time()  # type: ignore[attr-defined]
    srv.auth_sha12 = auth_sha12   # type: ignore[attr-defined]

    _append_audit(
        "control_plane_start",
        {
            "host": host,
            "port": int(port),
            "env": cfg.get("ARGUS_ENV", ""),
            "log_dir": logs_dir(),
            "auth_sha12": auth_sha12,
        },
    )
    print(f"[CONTROL] listening http://{host}:{port} (local-only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _append_audit("control_plane_stop", {"host": host, "port": int(port)})
        try:
            srv.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    # line above: raise SystemExit(run_control_plane(...))
    raise SystemExit(run_control_plane())

