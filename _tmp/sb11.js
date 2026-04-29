
function loadFleetOverview() {
  fetch('/api/fleet').then(r=>r.json()).then(data=>{
    const el = document.getElementById('fleet-overview');
    if (!el) return;
    const systems = data.systems || [];
    const summary = data.fleet_summary || {};

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">FLEET STATUS</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">'
      + summary.systems_live + '/' + summary.total_systems + ' systems live | '
      + summary.total_trades + ' total trades | '
      + summary.total_open_positions + ' open positions'
      + '</div></div>';

    html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;">';

    for (const sys of systems) {
      const statusColor = sys.status === 'LIVE' || sys.status === 'ACTIVE' ? '#00e676'
        : sys.status === 'DOWN' ? '#ff4444'
        : sys.status === 'SCANNING' ? '#00d4ff' : '#ffc107';

      const pnlColor = sys.total_pnl >= 0 ? '#00ff88' : '#ff4444';
      const wrColor = sys.win_rate >= 50 ? '#00ff88' : (sys.win_rate >= 40 ? '#ffc107' : '#ff4444');

      html += '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:8px;padding:12px;">';

      // Header: system name + status
      html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
        + '<div style="font-weight:bold;color:#00d4ff;font-size:0.9em;">' + sys.name + '</div>'
        + '<div style="font-size:0.6em;font-weight:bold;color:' + statusColor + ';letter-spacing:1px;">' + sys.status + '</div>'
        + '</div>';

      // Strategy name
      html += '<div style="font-size:0.65em;color:#7b8ab8;margin-bottom:8px;">' + sys.strategy + '</div>';

      // Metrics
      if (sys.total_trades > 0) {
        html += '<div style="display:flex;gap:12px;font-size:0.75em;margin-bottom:6px;">'
          + '<div>Trades: <span style="font-weight:bold;color:#fff;">' + sys.total_trades + '</span></div>'
          + '<div>WR: <span style="font-weight:bold;color:' + wrColor + ';">' + sys.win_rate + '%</span></div>'
          + '<div>PnL: <span style="font-weight:bold;color:' + pnlColor + ';">' + (sys.total_pnl >= 0 ? '+' : '') + sys.total_pnl + ' ' + sys.pnl_unit + '</span></div>'
          + '</div>';
      }

      // System-specific content
      if (sys.name === 'Argus' && sys.instruments) {
        html += '<div style="font-size:0.65em;margin-top:4px;">';
        for (const p of sys.instruments) {
          const dot = p.alive ? '<span style="color:#00e676;">&#9679;</span>' : '<span style="color:#ff4444;">&#9679;</span>';
          const pairPnl = p.pnl >= 0 ? '+' + p.pnl : '' + p.pnl;
          html += '<div style="display:flex;justify-content:space-between;padding:2px 0;">'
            + '<span>' + dot + ' ' + p.symbol + '</span>'
            + '<span style="color:' + (p.pnl >= 0 ? '#00ff88' : '#ff4444') + ';">' + pairPnl + 'p (' + p.trades + 't)</span>'
            + '</div>';
        }
        html += '</div>';
      }

      if (sys.name === 'Titan') {
        if (sys.instruments && sys.instruments.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const p of sys.instruments) {
            html += '<div>' + p.symbol + ' ' + p.direction + ' @ $' + p.entry_price.toFixed(2) + ' [' + p.strategy + ']</div>';
          }
          html += '</div>';
        } else {
          html += '<div style="font-size:0.65em;color:#7b8ab8;margin-top:4px;">No open positions</div>';
        }
      }

      if (sys.name === 'Ares') {
        const sig = sys.latest_signal || {};
        if (sig.rankings && sig.rankings.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const r of sig.rankings.slice(0, 4)) {
            const tag = (sig.buy || []).includes(r.symbol) ? ' <span style="color:#00e676;font-weight:bold;">BUY</span>' : '';
            html += '<div>#' + r.rank + ' ' + r.symbol + ' (' + (r.score >= 0 ? '+' : '') + r.score.toFixed(1) + ')' + tag + '</div>';
          }
          if (sig.risk_off) {
            html += '<div style="color:#ff4444;font-weight:bold;">RISK OFF</div>';
          }
          html += '</div>';
        }
      }

      if (sys.name === 'Hermes') {
        if (sys.top_gaps && sys.top_gaps.length > 0) {
          html += '<div style="font-size:0.65em;margin-top:4px;">';
          for (const g of sys.top_gaps.slice(0, 3)) {
            html += '<div>' + g.symbol + ' ' + g.gap_type + ' ' + g.gap_pct + '% (score=' + g.score + ')</div>';
          }
          html += '</div>';
        } else {
          html += '<div style="font-size:0.65em;color:#7b8ab8;margin-top:4px;">No gaps today (' + (sys.todays_gaps || 0) + ' scanned)</div>';
        }
      }

      html += '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
// loadFleetOverview() removed 2026-04-22 — panel deleted as redundant
