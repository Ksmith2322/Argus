#!/usr/bin/env python3
"""ops/plot_backtest_evolution.py -- Backtest performance timeline + 3D trade visualization.

Reads all bt_summary_*.json files and generates a self-contained HTML page with:
  1. Performance Evolution chart (PF, WR, PnL over time across backtest runs)
  2. 3D Trade Scatter (price x time x PnL per trade using WebGL/Three.js)

Usage:
    python ops/plot_backtest_evolution.py              # all runs
    python ops/plot_backtest_evolution.py --latest 20  # last 20 runs
    python ops/plot_backtest_evolution.py --open        # open in browser
"""
import argparse
import csv
import json
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOGS = REPO / "ops" / "logs"


def load_summaries(latest: int = 0) -> list:
    """Load all bt_summary_*.json files, sorted by timestamp."""
    files = sorted(
        LOGS.glob("bt_summary_bt_*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    # Exclude latest symlink
    files = [f for f in files if "latest" not in f.name]

    if latest > 0:
        files = files[-latest:]

    summaries = []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["_file"] = f.name
            data["_mtime"] = f.stat().st_mtime
            summaries.append(data)
        except Exception:
            pass
    return summaries


def load_trades_for_run(run_id: str) -> list:
    """Load trades CSV for a run."""
    trades_file = LOGS / f"trades_{run_id}.csv"
    if not trades_file.exists():
        return []
    trades = []
    try:
        with open(trades_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(row)
    except Exception:
        pass
    return trades


def build_evolution_data(summaries: list) -> list:
    """Extract time series of key metrics from summaries."""
    points = []
    for s in summaries:
        run_id = s.get("run_id", s.get("_file", ""))
        label = s.get("label", s.get("job_label", run_id))

        # Try to parse timestamp from run_id (bt_YYYYMMDDTHHMMSSZ_hex)
        ts = s.get("_mtime", 0)
        if "run_id" in s:
            try:
                rid = s["run_id"]
                # Format: bt_20260312T024150Z_cd4067b6
                date_part = rid.split("_")[1] if "_" in rid else ""
                if date_part and "T" in date_part:
                    dt = datetime.strptime(date_part, "%Y%m%dT%H%M%SZ")
                    dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
            except Exception:
                pass

        pf = float(s.get("profit_factor", 0) or 0)
        wr = float(s.get("win_rate_pct", 0) or 0)
        pnl = float(s.get("total_pnl_usd", 0) or 0)
        trades = int(s.get("total_closed", s.get("trades_closed", 0)) or 0)
        expectancy = float(s.get("expectancy_usd", 0) or 0)

        # Extract config hints
        config_hints = []
        params = s.get("params", s.get("config", {}))
        if isinstance(params, dict):
            for key in ["CONFLUENCE_MIN_SCORE", "MAX_HOLD_SECONDS", "ML_GOVERNOR_MODE",
                        "STOP_LOSS_PCT", "USE_EXIT_INTEL"]:
                if key in params:
                    config_hints.append(f"{key}={params[key]}")

        points.append({
            "ts": ts * 1000,  # JS expects ms
            "label": str(label)[:40],
            "run_id": str(run_id)[:30],
            "pf": round(pf, 3),
            "wr": round(wr, 1),
            "pnl": round(pnl, 2),
            "trades": trades,
            "expectancy": round(expectancy, 4),
            "config": " | ".join(config_hints) if config_hints else "",
        })

    return sorted(points, key=lambda p: p["ts"])


def build_trade_scatter_data(summaries: list, max_runs: int = 5) -> list:
    """Load trade-level data from the most recent N runs for 3D scatter."""
    recent = summaries[-max_runs:] if len(summaries) > max_runs else summaries
    all_trades = []

    for s in recent:
        run_id = s.get("run_id", "")
        if not run_id:
            continue
        trades = load_trades_for_run(run_id)
        for t in trades:
            try:
                entry_epoch = float(t.get("entry_epoch", t.get("epoch", 0)) or 0)
                entry_px = float(t.get("entry_px", t.get("fill_px", 0)) or 0)
                pnl = float(t.get("realized_pnl", t.get("pnl", 0)) or 0)
                hold_s = float(t.get("hold_seconds", t.get("duration_s", 0)) or 0)

                if entry_epoch > 0 and entry_px > 0:
                    all_trades.append({
                        "x": entry_epoch * 1000,  # time (ms)
                        "y": round(entry_px, 2),   # price
                        "z": round(pnl, 4),         # pnl
                        "hold": round(hold_s, 0),
                        "run": run_id[:20],
                        "win": 1 if pnl > 0 else 0,
                    })
            except Exception:
                pass

    return all_trades


def generate_html(evo_data: list, scatter_data: list) -> str:
    """Generate self-contained HTML with both visualizations."""
    evo_json = json.dumps(evo_data, separators=(",", ":"))
    scatter_json = json.dumps(scatter_data, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Argus — Backtest Evolution & 3D Trades</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #0a0a0f;
    color: #c8c8d4;
    font-family: 'Courier New', monospace;
    font-size: 13px;
    overflow-x: hidden;
}}
h1 {{
    text-align: center;
    color: #4fc3f7;
    padding: 18px 0 8px;
    font-size: 22px;
    letter-spacing: 2px;
}}
h2 {{
    color: #81c784;
    padding: 14px 20px 6px;
    font-size: 16px;
}}
.subtitle {{
    text-align: center;
    color: #666;
    font-size: 11px;
    margin-bottom: 10px;
}}
.chart-wrap {{
    position: relative;
    width: 95%;
    max-width: 1400px;
    margin: 10px auto;
    background: #111118;
    border: 1px solid #222;
    border-radius: 8px;
    overflow: hidden;
}}
canvas.evo {{
    width: 100%;
    height: 350px;
    display: block;
}}
#scatter3d {{
    width: 100%;
    height: 550px;
    cursor: grab;
}}
.legend {{
    display: flex;
    gap: 18px;
    justify-content: center;
    flex-wrap: wrap;
    padding: 6px 10px;
    font-size: 11px;
}}
.legend span {{
    display: flex;
    align-items: center;
    gap: 4px;
}}
.legend .dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}}
.tooltip {{
    position: absolute;
    background: #1a1a2e;
    border: 1px solid #4fc3f7;
    border-radius: 4px;
    padding: 8px 12px;
    font-size: 11px;
    pointer-events: none;
    display: none;
    z-index: 100;
    max-width: 350px;
    line-height: 1.5;
}}
.stats-bar {{
    display: flex;
    gap: 20px;
    justify-content: center;
    padding: 8px;
    flex-wrap: wrap;
}}
.stat {{
    background: #151520;
    border: 1px solid #333;
    border-radius: 6px;
    padding: 8px 16px;
    text-align: center;
}}
.stat .val {{ font-size: 18px; font-weight: bold; }}
.stat .lbl {{ font-size: 10px; color: #888; }}
.pos {{ color: #81c784; }}
.neg {{ color: #e57373; }}
</style>
</head>
<body>

<h1>ARGUS BACKTEST EVOLUTION</h1>
<p class="subtitle">Performance metrics across backtest runs over time</p>

<div class="stats-bar" id="statsBar"></div>

<h2>Profit Factor & Win Rate Over Time</h2>
<div class="chart-wrap">
    <canvas id="evoPfWr" class="evo"></canvas>
    <div class="tooltip" id="tipPfWr"></div>
    <div class="legend">
        <span><span class="dot" style="background:#4fc3f7"></span> Profit Factor</span>
        <span><span class="dot" style="background:#81c784"></span> Win Rate %</span>
        <span style="color:#ffb74d">--- PF=1.0 breakeven</span>
    </div>
</div>

<h2>PnL & Expectancy Over Time</h2>
<div class="chart-wrap">
    <canvas id="evoPnl" class="evo"></canvas>
    <div class="tooltip" id="tipPnl"></div>
    <div class="legend">
        <span><span class="dot" style="background:#ffb74d"></span> Total PnL ($)</span>
        <span><span class="dot" style="background:#ba68c8"></span> Expectancy ($/trade)</span>
    </div>
</div>

<h2>3D Trade Scatter — Price x Time x PnL</h2>
<div class="chart-wrap">
    <div id="scatter3d"></div>
    <div class="legend">
        <span><span class="dot" style="background:#81c784"></span> Winning trade</span>
        <span><span class="dot" style="background:#e57373"></span> Losing trade</span>
        <span style="color:#666">Drag to rotate | Scroll to zoom</span>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/examples/js/controls/OrbitControls.js"></script>

<script>
const EVO = {evo_json};
const SCATTER = {scatter_json};

// --- Stats bar ---
(function() {{
    const bar = document.getElementById('statsBar');
    if (!EVO.length) {{ bar.innerHTML = '<div class="stat"><div class="val">No data</div></div>'; return; }}
    const latest = EVO[EVO.length - 1];
    const best_pf = Math.max(...EVO.map(d => d.pf));
    const best_wr = Math.max(...EVO.map(d => d.wr));
    const stats = [
        ['Runs', EVO.length, ''],
        ['Latest PF', latest.pf.toFixed(2), latest.pf >= 1 ? 'pos' : 'neg'],
        ['Best PF', best_pf.toFixed(2), best_pf >= 1 ? 'pos' : 'neg'],
        ['Latest WR', latest.wr.toFixed(1) + '%', latest.wr >= 40 ? 'pos' : 'neg'],
        ['Best WR', best_wr.toFixed(1) + '%', best_wr >= 40 ? 'pos' : 'neg'],
        ['Latest PnL', '$' + latest.pnl.toFixed(2), latest.pnl >= 0 ? 'pos' : 'neg'],
        ['Total Trades', EVO.reduce((s,d) => s + d.trades, 0), ''],
    ];
    bar.innerHTML = stats.map(([l,v,c]) =>
        `<div class="stat"><div class="val ${{c}}">${{v}}</div><div class="lbl">${{l}}</div></div>`
    ).join('');
}})();

// --- Canvas Evolution Charts ---
function drawEvoChart(canvasId, tipId, series, yConfigs) {{
    const canvas = document.getElementById(canvasId);
    const tip = document.getElementById(tipId);
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    const W = rect.width, H = rect.height;
    const PAD = {{l: 60, r: 60, t: 20, b: 50}};
    const pW = W - PAD.l - PAD.r;
    const pH = H - PAD.t - PAD.b;

    if (!EVO.length) {{
        ctx.fillStyle = '#666';
        ctx.font = '14px Courier New';
        ctx.fillText('No backtest data found', W/2 - 80, H/2);
        return;
    }}

    // X axis: run index
    const xScale = (i) => PAD.l + (i / Math.max(1, EVO.length - 1)) * pW;

    // Draw for each series
    series.forEach((s, si) => {{
        const yc = yConfigs[si];
        const vals = EVO.map(d => d[s.key]);
        const yMin = yc.min !== undefined ? yc.min : Math.min(...vals);
        const yMax = yc.max !== undefined ? yc.max : Math.max(...vals);
        const yRange = yMax - yMin || 1;
        const yScale = (v) => PAD.t + pH - ((v - yMin) / yRange) * pH;

        // Reference line
        if (yc.ref !== undefined) {{
            ctx.strokeStyle = '#ffb74d44';
            ctx.lineWidth = 1;
            ctx.setLineDash([6, 4]);
            ctx.beginPath();
            const ry = yScale(yc.ref);
            ctx.moveTo(PAD.l, ry);
            ctx.lineTo(W - PAD.r, ry);
            ctx.stroke();
            ctx.setLineDash([]);
        }}

        // Line
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        EVO.forEach((d, i) => {{
            const x = xScale(i);
            const y = yScale(d[s.key]);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }});
        ctx.stroke();

        // Points
        EVO.forEach((d, i) => {{
            const x = xScale(i);
            const y = yScale(d[s.key]);
            ctx.fillStyle = s.color;
            ctx.beginPath();
            ctx.arc(x, y, 4, 0, Math.PI * 2);
            ctx.fill();
        }});

        // Y axis label
        ctx.fillStyle = s.color;
        ctx.font = '10px Courier New';
        ctx.save();
        ctx.translate(si === 0 ? 12 : W - 12, PAD.t + pH / 2);
        ctx.rotate(-Math.PI / 2);
        ctx.textAlign = 'center';
        ctx.fillText(s.label, 0, 0);
        ctx.restore();
    }});

    // Grid
    ctx.strokeStyle = '#1e1e1e';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 5; i++) {{
        const y = PAD.t + (i / 5) * pH;
        ctx.beginPath();
        ctx.moveTo(PAD.l, y);
        ctx.lineTo(W - PAD.r, y);
        ctx.stroke();
    }}

    // X labels (show every Nth)
    ctx.fillStyle = '#666';
    ctx.font = '9px Courier New';
    ctx.textAlign = 'center';
    const step = Math.max(1, Math.floor(EVO.length / 10));
    EVO.forEach((d, i) => {{
        if (i % step === 0 || i === EVO.length - 1) {{
            const x = xScale(i);
            const dateStr = d.ts > 0 ? new Date(d.ts).toLocaleDateString('en-US', {{month:'short',day:'numeric'}}) : '#' + i;
            ctx.fillText(dateStr, x, H - PAD.b + 16);
        }}
    }});

    // Tooltip on hover
    canvas.addEventListener('mousemove', (e) => {{
        const br = canvas.getBoundingClientRect();
        const mx = e.clientX - br.left;
        const idx = Math.round(((mx - PAD.l) / pW) * (EVO.length - 1));
        if (idx >= 0 && idx < EVO.length) {{
            const d = EVO[idx];
            tip.innerHTML = `<b>${{d.label || d.run_id}}</b><br>` +
                `PF: ${{d.pf}} | WR: ${{d.wr}}% | PnL: $${{d.pnl}}<br>` +
                `Trades: ${{d.trades}} | Exp: $${{d.expectancy}}<br>` +
                (d.config ? `<span style="color:#888">${{d.config}}</span>` : '');
            tip.style.display = 'block';
            tip.style.left = Math.min(mx + 10, W - 300) + 'px';
            tip.style.top = '30px';
        }}
    }});
    canvas.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});
}}

drawEvoChart('evoPfWr', 'tipPfWr',
    [{{key: 'pf', color: '#4fc3f7', label: 'Profit Factor'}},
     {{key: 'wr', color: '#81c784', label: 'Win Rate %'}}],
    [{{min: 0, max: Math.max(2, ...EVO.map(d=>d.pf)+[1.5]), ref: 1.0}},
     {{min: 0, max: 100}}]
);

drawEvoChart('evoPnl', 'tipPnl',
    [{{key: 'pnl', color: '#ffb74d', label: 'Total PnL ($)'}},
     {{key: 'expectancy', color: '#ba68c8', label: 'Expectancy ($/trade)'}}],
    [{{ref: 0}},
     {{ref: 0}}]
);

// --- 3D Trade Scatter ---
(function() {{
    const container = document.getElementById('scatter3d');
    if (!SCATTER.length) {{
        container.innerHTML = '<div style="padding:40px;text-align:center;color:#666">No trade data for 3D scatter</div>';
        return;
    }}

    const W = container.clientWidth;
    const H = container.clientHeight;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x0a0a0f);

    const camera = new THREE.PerspectiveCamera(55, W / H, 0.1, 5000);
    camera.position.set(250, 200, 350);

    const renderer = new THREE.WebGLRenderer({{ antialias: true, alpha: true }});
    renderer.setSize(W, H);
    renderer.setPixelRatio(window.devicePixelRatio);
    container.appendChild(renderer.domElement);

    const controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = true;
    controls.autoRotateSpeed = 0.5;

    // Normalize data to scene coords
    const xs = SCATTER.map(d => d.x);
    const ys = SCATTER.map(d => d.y);
    const zs = SCATTER.map(d => d.z);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const yMin = Math.min(...ys), yMax = Math.max(...ys);
    const zMin = Math.min(...zs), zMax = Math.max(...zs);
    const xR = xMax - xMin || 1;
    const yR = yMax - yMin || 1;
    const zR = zMax - zMin || 1;
    const SZ = 200; // scene scale

    function norm(v, mn, rng) {{ return ((v - mn) / rng - 0.5) * SZ; }}

    // Grid floor
    const gridHelper = new THREE.GridHelper(SZ, 20, 0x222244, 0x111122);
    gridHelper.position.y = -SZ / 2;
    scene.add(gridHelper);

    // Axes labels using sprites
    function makeLabel(text, pos, color) {{
        const canvas2 = document.createElement('canvas');
        canvas2.width = 256;
        canvas2.height = 64;
        const ctx2 = canvas2.getContext('2d');
        ctx2.font = 'bold 28px Courier New';
        ctx2.fillStyle = color;
        ctx2.fillText(text, 4, 40);
        const tex = new THREE.CanvasTexture(canvas2);
        const mat = new THREE.SpriteMaterial({{ map: tex, transparent: true }});
        const sprite = new THREE.Sprite(mat);
        sprite.position.copy(pos);
        sprite.scale.set(40, 10, 1);
        scene.add(sprite);
    }}

    makeLabel('TIME >>>', new THREE.Vector3(SZ/2 + 20, -SZ/2, 0), '#4fc3f7');
    makeLabel('PRICE', new THREE.Vector3(0, SZ/2 + 10, 0), '#81c784');
    makeLabel('PnL >>>', new THREE.Vector3(0, -SZ/2, SZ/2 + 20), '#ffb74d');

    // Axis lines
    const axMat = new THREE.LineBasicMaterial({{ color: 0x333355 }});
    [
        [[-SZ/2, -SZ/2, -SZ/2], [SZ/2, -SZ/2, -SZ/2]],
        [[-SZ/2, -SZ/2, -SZ/2], [-SZ/2, SZ/2, -SZ/2]],
        [[-SZ/2, -SZ/2, -SZ/2], [-SZ/2, -SZ/2, SZ/2]],
    ].forEach(([a, b]) => {{
        const g = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(...a), new THREE.Vector3(...b)
        ]);
        scene.add(new THREE.Line(g, axMat));
    }});

    // Trade points
    const winGeo = new THREE.SphereGeometry(2.5, 12, 8);
    const loseGeo = new THREE.SphereGeometry(2.5, 12, 8);
    const winMat = new THREE.MeshBasicMaterial({{ color: 0x81c784, transparent: true, opacity: 0.85 }});
    const loseMat = new THREE.MeshBasicMaterial({{ color: 0xe57373, transparent: true, opacity: 0.85 }});

    // PnL=0 plane
    const z0 = norm(0, zMin, zR);
    const planeGeo = new THREE.PlaneGeometry(SZ, SZ);
    const planeMat = new THREE.MeshBasicMaterial({{
        color: 0x444400, transparent: true, opacity: 0.08, side: THREE.DoubleSide
    }});
    const plane = new THREE.Mesh(planeGeo, planeMat);
    plane.rotation.x = Math.PI / 2;
    plane.position.set(0, -SZ/2, z0);
    scene.add(plane);

    SCATTER.forEach(d => {{
        const px = norm(d.x, xMin, xR);
        const py = norm(d.y, yMin, yR);
        const pz = norm(d.z, zMin, zR);
        const geo = d.win ? winGeo : loseGeo;
        const mat = d.win ? winMat : loseMat;
        const mesh = new THREE.Mesh(geo, mat);
        mesh.position.set(px, py, pz);
        scene.add(mesh);

        // Vertical line from point to floor
        const lineGeo = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(px, -SZ/2, pz),
            new THREE.Vector3(px, py, pz),
        ]);
        const lineMat = new THREE.LineBasicMaterial({{
            color: d.win ? 0x81c784 : 0xe57373,
            transparent: true,
            opacity: 0.2,
        }});
        scene.add(new THREE.Line(lineGeo, lineMat));
    }});

    // Ambient + point light
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const pLight = new THREE.PointLight(0x4fc3f7, 0.8, 1000);
    pLight.position.set(100, 200, 100);
    scene.add(pLight);

    // Animate
    function animate() {{
        requestAnimationFrame(animate);
        controls.update();
        renderer.render(scene, camera);
    }}
    animate();

    // Resize
    window.addEventListener('resize', () => {{
        const w = container.clientWidth;
        const h = container.clientHeight;
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        renderer.setSize(w, h);
    }});
}})();
</script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description="Backtest evolution & 3D trade viz")
    parser.add_argument("--latest", type=int, default=0, help="Only show N most recent runs")
    parser.add_argument("--open", action="store_true", help="Open in browser")
    parser.add_argument("--out", type=str, default="", help="Output file path")
    args = parser.parse_args()

    summaries = load_summaries(args.latest)
    if not summaries:
        print("No bt_summary files found in", LOGS)
        sys.exit(1)

    print(f"Found {len(summaries)} backtest summaries")

    evo_data = build_evolution_data(summaries)
    scatter_data = build_trade_scatter_data(summaries, max_runs=8)

    print(f"Evolution points: {len(evo_data)}, 3D trade points: {len(scatter_data)}")

    html = generate_html(evo_data, scatter_data)
    out_path = Path(args.out) if args.out else LOGS / "backtest_evolution.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Written to {out_path}")

    if args.open:
        webbrowser.open(str(out_path))


if __name__ == "__main__":
    main()