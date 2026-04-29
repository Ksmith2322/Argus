
// ─── OPEN POSITIONS + RISK EXPOSURE ───────────────────────────────
function loadPositionsAndRisk() {
  fetch('/api/positions_open').then(r=>r.json()).then(data=>{
    // Risk banner
    const rb = document.getElementById('risk-exposure-banner');
    if (rb) {
      const usedPct = data.pct_of_budget_used || 0;
      const bg = usedPct >= 80 ? '#2a0f0f' : usedPct >= 50 ? '#2a2010' : '#0d1321';
      const border = usedPct >= 80 ? '#ff4444' : usedPct >= 50 ? '#ffaa00' : '#1e2a42';
      const color = usedPct >= 80 ? '#ff4444' : usedPct >= 50 ? '#ffaa00' : '#00d4ff';
      rb.innerHTML = '<div style="background:' + bg + ';border:1px solid ' + border + ';border-radius:4px;padding:8px 12px;font-size:0.72em;display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;">'
        + '<div><span style="color:' + color + ';font-weight:bold;letter-spacing:2px;">RISK EXPOSURE</span>'
        + ' <span style="color:#7b8ab8;">' + data.count + ' open positions</span></div>'
        + '<div style="color:#9da8c7;">'
        + 'Open risk: <span style="color:#e0e0e0;font-weight:bold;">$' + (data.total_risk_usd||0).toFixed(2) + '</span>'
        + ' / $' + (data.fleet_budget_usd||0).toFixed(0) + ' budget'
        + ' · <span style="color:' + color + ';font-weight:bold;">' + usedPct.toFixed(1) + '% used</span>'
        + ' · anchor $' + (data.anchor_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})
        + '</div></div>';
    }
    // Open positions table
    const pp = document.getElementById('open-positions-panel');
    if (!pp) return;
    if (data.count === 0) {
      pp.innerHTML = '<div style="background:#0d1c11;border:1px solid #143021;border-radius:4px;padding:6px 12px;font-size:0.7em;color:#00e676;">'
        + '<span style="letter-spacing:1px;">OPEN POSITIONS</span>: fleet is FLAT (no live exposure)</div>';
      return;
    }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">OPEN POSITIONS (' + data.count + ')</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.72em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:4px;">Strategy</th>'
      + '<th style="text-align:left;padding:4px;">Entry (local)</th>'
      + '<th style="text-align:left;padding:4px;">Dir</th>'
      + '<th style="text-align:right;padding:4px;">Entry $</th>'
      + '<th style="text-align:right;padding:4px;">Stop</th>'
      + '<th style="text-align:right;padding:4px;">Target</th>'
      + '<th style="text-align:right;padding:4px;">Size</th>'
      + '<th style="text-align:right;padding:4px;">Risk $</th>'
      + '<th style="text-align:right;padding:4px;" title="Risk as % of fleet anchor (broker equity)">Risk %</th>'
      + '<th style="text-align:right;padding:4px;" title="USD-normalized notional, multiplier of fleet anchor">Notional ×</th>'
      + '<th style="text-align:center;padding:4px;" title="Sizing sanity flag: OK / CHECK_MULTIPLIER / OVER_NOTIONAL_CAP / OVER_RISK_CAP">Sizing</th>'
      + '</tr></thead><tbody>';
    for (const p of data.positions) {
      let entryLocal = '—';
      try {
        const d = new Date(p.entry_ts);
        if (!isNaN(d.getTime())) {
          const pad = n => String(n).padStart(2, '0');
          entryLocal = pad(d.getMonth()+1)+'/'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes());
        }
      } catch(e){}
      const dirColor = p.direction === 'long' ? '#00ff88' : p.direction === 'short' ? '#ff4444' : '#9da8c7';
      // Sizing sanity coloring — OK green, CHECK yellow, OVER red. The eye should
      // land on red instantly when something is mis-sized.
      const ss = p.sizing_status || 'OK';
      let sizingColor = '#00ff88';  // OK
      if (ss.startsWith('CHECK_')) sizingColor = '#ffaa00';
      else if (ss.startsWith('OVER_')) sizingColor = '#ff4444';
      const sizingBadge = ss === 'OK'
        ? '<span style="color:#00ff88;font-size:0.92em;">OK</span>'
        : '<span style="background:' + sizingColor + ';color:#000;padding:1px 5px;border-radius:3px;font-weight:bold;font-size:0.85em;letter-spacing:1px;" title="instrument_class=' + (p.instrument_class || '?') + '">' + ss + '</span>';
      const riskPct = p.risk_pct_of_anchor != null ? p.risk_pct_of_anchor.toFixed(2) + '%' : '—';
      const riskPctColor = p.risk_pct_of_anchor != null && p.risk_pct_of_anchor > 1.5 ? '#ffaa00' : '#9da8c7';
      const notX = p.notional_x_anchor != null ? p.notional_x_anchor.toFixed(2) + 'x' : '—';
      const notXColor = p.notional_x_anchor != null && p.notional_x_anchor > 5 ? '#ff4444' : p.notional_x_anchor != null && p.notional_x_anchor > 1.5 ? '#ffaa00' : '#9da8c7';
      html += '<tr style="border-bottom:1px solid #151c2c;">'
        + '<td style="padding:4px;color:#00d4ff;">' + (p.strategy||'—') + (p.instrument ? ' <span style="color:#7b8ab8;">['+p.instrument+']</span>':'') + '</td>'
        + '<td style="padding:4px;color:#9da8c7;">' + entryLocal + '</td>'
        + '<td style="padding:4px;color:' + dirColor + ';">' + (p.direction||'—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:#e0e0e0;">' + (p.entry_px ? Number(p.entry_px).toFixed(4).replace(/\.?0+$/, '') : '—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:#9da8c7;">' + (p.stop_px ? Number(p.stop_px).toFixed(4).replace(/\.?0+$/, '') : '—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:#9da8c7;">' + (p.target_px ? Number(p.target_px).toFixed(4).replace(/\.?0+$/, '') : '—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:#9da8c7;">' + (p.size||'—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:#ffaa00;">' + (p.risk_usd ? '$' + Number(p.risk_usd).toFixed(2) : '—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:' + riskPctColor + ';">' + riskPct + '</td>'
        + '<td style="padding:4px;text-align:right;color:' + notXColor + ';font-weight:bold;">' + notX + '</td>'
        + '<td style="padding:4px;text-align:center;">' + sizingBadge + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    pp.innerHTML = html;
  }).catch(()=>{});
}
loadPositionsAndRisk();
setInterval(loadPositionsAndRisk, 30000);

// Promotion ladder panel removed 2026-04-23 — progress now inline in
// Strategy Performance table. /api/promotion_ladder still powers that column.

// ─── EXIT REASON DISTRIBUTION ─────────────────────────────────────
function loadExitReasons() {
  fetch('/api/exit_reasons').then(r=>r.json()).then(data=>{
    const el = document.getElementById('exit-reasons-panel');
    if (!el) return;
    const strats = data.strategies || {};
    const names = Object.keys(strats);
    if (names.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:8px;">EXIT REASON DISTRIBUTION (post-reset)</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.72em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:4px;">Strategy</th>'
      + '<th style="text-align:right;padding:4px;">Trades</th>'
      + '<th style="text-align:left;padding:4px;">Distribution</th>'
      + '</tr></thead><tbody>';
    const reasonColors = {target:'#00ff88', stop:'#ff4444', time:'#ffc107', timeout:'#ffc107', unknown:'#7b8ab8'};
    for (const name of names.sort()) {
      const s = strats[name];
      html += '<tr style="border-bottom:1px solid #151c2c;">'
        + '<td style="padding:4px;color:#e0e0e0;">' + name + '</td>'
        + '<td style="padding:4px;text-align:right;color:#9da8c7;">' + s.total + '</td>'
        + '<td style="padding:4px;"><div style="display:flex;height:12px;border-radius:2px;overflow:hidden;">';
      for (const [reason, pct] of Object.entries(s.pct_by_reason)) {
        const c = reasonColors[reason] || '#888';
        html += '<div style="background:' + c + ';width:' + pct + '%;" title="' + reason + ': ' + pct + '% (' + s.by_reason[reason] + ')"></div>';
      }
      html += '</div><div style="font-size:0.6em;color:#7b8ab8;margin-top:2px;">';
      html += Object.entries(s.pct_by_reason).map(([r,p]) => r + ' ' + p + '%').join(' · ');
      html += '</div></td></tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadExitReasons();
setInterval(loadExitReasons, 60000);
