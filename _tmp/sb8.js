
function renderEquityCurve(points, width, height, anchor) {
  if (!points || points.length < 2) {
    return '<div style="color:#7b8ab8;font-size:0.7em;padding:20px;text-align:center;">Not enough trades in window to plot.</div>';
  }
  const pad = {top: 10, right: 10, bottom: 20, left: 44};
  const W = width, H = height;
  const innerW = W - pad.left - pad.right;
  const innerH = H - pad.top - pad.bottom;
  const vals = points.map(p => p.cumulative_pnl_usd);
  let vMin = Math.min(0, ...vals);
  let vMax = Math.max(0, ...vals);
  if (vMax === vMin) vMax = vMin + 1;
  const pad_v = (vMax - vMin) * 0.08;
  vMin -= pad_v; vMax += pad_v;
  const t0 = new Date(points[0].ts).getTime();
  const t1 = new Date(points[points.length-1].ts).getTime();
  const tSpan = Math.max(t1 - t0, 1);
  const x = (ts) => pad.left + ((new Date(ts).getTime() - t0) / tSpan) * innerW;
  const y = (v) => pad.top + (1 - (v - vMin) / (vMax - vMin)) * innerH;

  // Path (smoothed cubic bezier) + area under curve
  // Falls back to straight lines if smoothPath helper isn't defined yet (script load order safety).
  const linePath = (typeof smoothPath === 'function')
    ? smoothPath(points, x, y)
    : points.map((p, i) => (i === 0 ? 'M' : 'L') + x(p.ts).toFixed(1) + ',' + y(p.cumulative_pnl_usd).toFixed(1)).join(' ');
  let areaPath = '';
  if (points.length) {
    const x0 = x(points[0].ts), x1 = x(points[points.length-1].ts), yZero = y(0);
    // Area = smoothed top edge + vertical drop to baseline + close back to start
    areaPath = linePath + ' L' + x1.toFixed(1) + ',' + yZero.toFixed(1) + ' L' + x0.toFixed(1) + ',' + yZero.toFixed(1) + ' Z';
  }

  const finalVal = vals[vals.length-1];
  const stroke = finalVal >= 0 ? '#00ff88' : '#ff4444';
  const fill = finalVal >= 0 ? 'rgba(0,255,136,0.10)' : 'rgba(255,68,68,0.10)';

  // Y ticks (3 values)
  const yTicks = [vMin, (vMin+vMax)/2, vMax];
  let yTickHtml = '';
  for (const v of yTicks) {
    const py = y(v);
    yTickHtml += '<line x1="' + pad.left + '" y1="' + py.toFixed(1) + '" x2="' + (W-pad.right) + '" y2="' + py.toFixed(1) + '" stroke="#1e2a42" stroke-width="1" stroke-dasharray="2,3"/>';
    yTickHtml += '<text x="' + (pad.left - 6) + '" y="' + (py + 3).toFixed(1) + '" fill="#7b8ab8" font-size="10" text-anchor="end">$' + v.toFixed(0) + '</text>';
  }

  // Zero line (if in range)
  let zeroLine = '';
  if (vMin < 0 && vMax > 0) {
    const yZero = y(0);
    zeroLine = '<line x1="' + pad.left + '" y1="' + yZero.toFixed(1) + '" x2="' + (W-pad.right) + '" y2="' + yZero.toFixed(1) + '" stroke="#334" stroke-width="1"/>';
  }

  // X date labels (start, middle, end)
  const fmt = (ts) => { const d = new Date(ts); return (d.getMonth()+1) + '/' + d.getDate(); };
  const xLabels = '<text x="' + pad.left + '" y="' + (H-4) + '" fill="#7b8ab8" font-size="10">' + fmt(points[0].ts) + '</text>'
    + '<text x="' + (W/2) + '" y="' + (H-4) + '" fill="#7b8ab8" font-size="10" text-anchor="middle">' + fmt(points[Math.floor(points.length/2)].ts) + '</text>'
    + '<text x="' + (W-pad.right) + '" y="' + (H-4) + '" fill="#7b8ab8" font-size="10" text-anchor="end">' + fmt(points[points.length-1].ts) + '</text>';

  return '<svg width="' + W + '" height="' + H + '" style="display:block;">'
    + yTickHtml + zeroLine
    + '<path d="' + areaPath + '" fill="' + fill + '" stroke="none"/>'
    + '<path d="' + linePath + '" fill="none" stroke="' + stroke + '" stroke-width="1.5"/>'
    + xLabels
    + '</svg>';
}

function loadFleetEquityCurve() {
  fetch('/api/fleet_equity_curve?window_days=90&include_backfill=false').then(r=>r.json()).then(data=>{
    const el = document.getElementById('fleet-equity-panel');
    if (!el) return;
    const pnl = data.current_cumulative_pnl_usd || 0;
    const pct = data.current_cumulative_pnl_pct || 0;
    const anchor = data.anchor_capital_usd || 10000;
    const trades = data.total_trades || 0;
    const pnlColor = pnl >= 0 ? '#00ff88' : '#ff4444';
    const sign = pnl >= 0 ? '+' : '';

    let contribHtml = '';
    const contrib = data.contribution_by_strategy || {};
    const entries = Object.entries(contrib);
    if (entries.length) {
      contribHtml = '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;font-size:0.68em;">';
      for (const [label, val] of entries) {
        if (Math.abs(val) < 0.005) continue;
        const c = val >= 0 ? '#00ff88' : '#ff4444';
        contribHtml += '<span style="color:#7b8ab8;">' + label + ' <span style="color:' + c + ';">' + (val >= 0 ? '+' : '') + '$' + val.toFixed(2) + '</span></span>';
      }
      contribHtml += '</div>';
    }

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">FLEET EQUITY CURVE</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">live-only last ' + (data.window_days || 90) + 'd | anchor $' + anchor.toLocaleString() + ' | ' + trades + ' trades</div>'
      + '</div>';
    html += '<div style="display:flex;gap:20px;align-items:baseline;margin-bottom:6px;">'
      + '<div style="font-size:1.6em;font-weight:bold;color:' + pnlColor + ';">' + sign + '$' + pnl.toFixed(2) + '</div>'
      + '<div style="font-size:1.0em;color:' + pnlColor + ';">' + sign + pct.toFixed(3) + '%</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">of fleet anchor</div>'
      + '</div>';
    html += '<div>' + renderEquityCurve(data.points || [], Math.max(el.clientWidth - 28, 600), 180, anchor) + '</div>';
    html += contribHtml;
    el.innerHTML = html;
  }).catch((e)=>{
    const el = document.getElementById('fleet-equity-panel');
    if (el) el.innerHTML = '<div style="color:#ff4444;font-size:0.75em;">equity curve error: ' + e + '</div>';
  });
}
loadFleetEquityCurve();
setInterval(loadFleetEquityCurve, 60000);

// Monotone cubic (Fritsch-Carlson) smoother — same algorithm as D3's
// curveMonotoneX. Passes through every data point exactly AND guarantees no
// overshoot/loops between them. Equity curves read as smooth continuous lines
// instead of swirling around trade clusters like Catmull-Rom does.
function smoothPath(pts, xFn, yFn) {
  if (!pts || pts.length === 0) return '';
  const n = pts.length;
  if (n === 1) return 'M' + xFn(pts[0].ts).toFixed(1) + ',' + yFn(pts[0].cumulative_pnl_usd).toFixed(1);
  const xs = pts.map(p => xFn(p.ts));
  const ys = pts.map(p => yFn(p.cumulative_pnl_usd));

  // Secant slopes between consecutive points
  const dx = new Array(n - 1), dy = new Array(n - 1), sec = new Array(n - 1);
  for (let i = 0; i < n - 1; i++) {
    dx[i] = xs[i+1] - xs[i];
    dy[i] = ys[i+1] - ys[i];
    sec[i] = dx[i] === 0 ? 0 : dy[i] / dx[i];
  }

  // Tangent at each point, then Fritsch-Carlson clamp
  const m = new Array(n);
  m[0] = sec[0];
  for (let i = 1; i < n - 1; i++) {
    if (sec[i-1] * sec[i] <= 0) m[i] = 0;
    else m[i] = (sec[i-1] + sec[i]) / 2;
  }
  m[n-1] = sec[n-2];
  for (let i = 0; i < n - 1; i++) {
    if (sec[i] === 0) { m[i] = 0; m[i+1] = 0; continue; }
    const a = m[i] / sec[i], b = m[i+1] / sec[i];
    const s = a*a + b*b;
    if (s > 9) {
      const t = 3 / Math.sqrt(s);
      m[i]   = t * a * sec[i];
      m[i+1] = t * b * sec[i];
    }
  }

  // Convert Hermite tangents to cubic-bezier control points (1/3 of dx out)
  let d = 'M' + xs[0].toFixed(1) + ',' + ys[0].toFixed(1);
  for (let i = 0; i < n - 1; i++) {
    const cp1x = xs[i]   + dx[i] / 3;
    const cp1y = ys[i]   + m[i]   * dx[i] / 3;
    const cp2x = xs[i+1] - dx[i] / 3;
    const cp2y = ys[i+1] - m[i+1] * dx[i] / 3;
    d += ' C' + cp1x.toFixed(1) + ',' + cp1y.toFixed(1)
       + ' ' + cp2x.toFixed(1) + ',' + cp2y.toFixed(1)
       + ' ' + xs[i+1].toFixed(1) + ',' + ys[i+1].toFixed(1);
  }
  return d;
}

// Combined per-strategy overlay — every strategy on one chart, each its own color + legend.
const _stratPalette = ['#00d4ff','#ff9800','#00ff88','#e91e63','#ffc107','#9c27b0','#8bc34a','#ff5722','#03a9f4','#ffeb3b','#009688','#f06292','#cddc39','#ba68c8'];
function loadPerStrategyEquity() {
  fetch('/api/fleet_equity_curve?window_days=90&include_backfill=false').then(r=>r.json()).then(data=>{
    const el = document.getElementById('per-strategy-equity-panel');
    if (!el) return;
    const points = data.points || [];
    const tracked = data.tracked_strategies || [];
    const serverNow = data.server_now_iso ? new Date(data.server_now_iso).getTime() : Date.now();

    // Group raw events by strategy
    const byStrat = new Map();
    for (const p of points) {
      const s = p.strategy || 'unknown';
      if (!byStrat.has(s)) byStrat.set(s, []);
      byStrat.get(s).push(p);
    }

    // t0 = earliest event across all strategies (or now if no trades yet)
    const tsList = points.map(p => new Date(p.ts).getTime()).filter(x => isFinite(x));
    const tEarliest = tsList.length ? Math.min(...tsList) : serverNow;
    const t0ms = tEarliest;
    const t1ms = serverNow;
    const t0iso = new Date(t0ms).toISOString();
    const t1iso = new Date(t1ms).toISOString();

    // Build a series for EVERY tracked strategy (not just ones with trades).
    // Each series starts at (t0, $0) and ends at (t1, final_cum) so all lines
    // share the same left edge and extend to now. Zero-trade strategies render
    // as a flat line at $0 from t0 → t1.
    const series = [];
    const allLabels = new Set([...tracked, ...byStrat.keys()]);
    for (const label of allLabels) {
      const rows = (byStrat.get(label) || []).slice().sort((a,b) => new Date(a.ts) - new Date(b.ts));
      let cum = 0;
      const core = rows.map(r => {
        cum += (r.trade_pnl_usd || 0);
        return { ts: r.ts, cumulative_pnl_usd: Math.round(cum*100)/100 };
      });
      const pts = [{ ts: t0iso, cumulative_pnl_usd: 0 }, ...core, { ts: t1iso, cumulative_pnl_usd: Math.round(cum*100)/100 }];
      series.push({ label, pts, finalPnl: Math.round(cum*100)/100, trades: rows.length });
    }
    // Active (traded) strategies get palette colors + render on top; zero-trade
    // strategies get a muted grey and render first so the active ones overlay.
    const traded = series.filter(s => s.trades > 0).sort((a,b) => Math.abs(b.finalPnl) - Math.abs(a.finalPnl));
    const untraded = series.filter(s => s.trades === 0).sort((a,b) => a.label.localeCompare(b.label));
    traded.forEach((s, i) => { s.color = _stratPalette[i % _stratPalette.length]; s.active = true; });
    untraded.forEach(s => { s.color = '#3a4560'; s.active = false; });
    const drawOrder = [...untraded, ...traded]; // untraded first (underneath), traded on top

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">PER-STRATEGY EQUITY CURVES</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">live-only last ' + (data.window_days || 90) + 'd | ' + traded.length + '/' + series.length + ' strategies have traded</div>'
      + '</div>';

    // Shared axes across all series (use drawOrder so flat-$0 untraded strategies contribute too)
    const allPts = drawOrder.flatMap(s => s.pts);
    let vMin = Math.min(0, ...allPts.map(p => p.cumulative_pnl_usd));
    let vMax = Math.max(0, ...allPts.map(p => p.cumulative_pnl_usd));
    if (vMax === vMin) vMax = vMin + 1;
    const pv = (vMax - vMin) * 0.08; vMin -= pv; vMax += pv;
    const t0 = t0ms;
    const t1 = t1ms;
    const tSpan = Math.max(t1 - t0, 1);
    const W = Math.max(el.clientWidth - 28, 600), H = 280;
    const pad = {top: 10, right: 10, bottom: 22, left: 54};
    const innerW = W - pad.left - pad.right, innerH = H - pad.top - pad.bottom;
    const x = ts => pad.left + ((new Date(ts).getTime() - t0) / tSpan) * innerW;
    const y = v => pad.top + (1 - (v - vMin) / (vMax - vMin)) * innerH;

    // Axes: y ticks, zero line, x date labels
    const yTicks = [vMin, (vMin+vMax)/2, vMax];
    let svg = '';
    for (const v of yTicks) {
      const py = y(v);
      svg += '<line x1="' + pad.left + '" y1="' + py.toFixed(1) + '" x2="' + (W-pad.right) + '" y2="' + py.toFixed(1) + '" stroke="#1e2a42" stroke-width="1" stroke-dasharray="2,3"/>'
           + '<text x="' + (pad.left-6) + '" y="' + (py+3).toFixed(1) + '" fill="#7b8ab8" font-size="10" text-anchor="end">$' + v.toFixed(0) + '</text>';
    }
    if (vMin < 0 && vMax > 0) {
      svg += '<line x1="' + pad.left + '" y1="' + y(0).toFixed(1) + '" x2="' + (W-pad.right) + '" y2="' + y(0).toFixed(1) + '" stroke="#334" stroke-width="1"/>';
    }
    const fmt = ts => { const d = new Date(ts); return (d.getMonth()+1) + '/' + d.getDate(); };
    svg += '<text x="' + pad.left + '" y="' + (H-4) + '" fill="#7b8ab8" font-size="10">' + fmt(t0) + '</text>'
         + '<text x="' + (W-pad.right) + '" y="' + (H-4) + '" fill="#7b8ab8" font-size="10" text-anchor="end">' + fmt(t1) + '</text>';

    // Draw each series (single point = dot, multi point = smooth line)
    for (const s of series) {
      if (s.pts.length === 1) {
        svg += '<circle cx="' + x(s.pts[0].ts).toFixed(1) + '" cy="' + y(s.pts[0].cumulative_pnl_usd).toFixed(1) + '" r="3" fill="' + s.color + '"/>';
      } else {
        svg += '<path d="' + smoothPath(s.pts, x, y) + '" fill="none" stroke="' + s.color + '" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" opacity="0.92"/>';
      }
    }

    html += '<svg width="' + W + '" height="' + H + '" style="display:block;">' + svg + '</svg>';

    // Legend — color swatch + strategy + final PnL + trade count
    html += '<div style="display:flex;flex-wrap:wrap;gap:10px 18px;margin-top:10px;font-size:0.72em;">';
    for (const s of series) {
      const sign = s.finalPnl >= 0 ? '+' : '';
      const pnlColor = s.finalPnl >= 0 ? '#00ff88' : '#ff4444';
      html += '<div style="display:flex;align-items:center;gap:6px;">'
        + '<span style="display:inline-block;width:10px;height:10px;background:' + s.color + ';border-radius:2px;"></span>'
        + '<span style="color:#e0e0e0;">' + s.label + '</span>'
        + '<span style="color:' + pnlColor + ';font-weight:bold;">' + sign + '$' + s.finalPnl.toFixed(2) + '</span>'
        + '<span style="color:#7b8ab8;">(' + s.trades + ')</span>'
        + '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch((e)=>{
    const el = document.getElementById('per-strategy-equity-panel');
    if (el) el.innerHTML = '<div style="color:#ff4444;font-size:0.75em;">per-strategy equity error: ' + e + '</div>';
  });
}
loadPerStrategyEquity();
setInterval(loadPerStrategyEquity, 60000);
window.addEventListener('resize', loadPerStrategyEquity);
