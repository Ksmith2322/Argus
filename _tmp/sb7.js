
function loadRecentTrades() {
  fetch('/api/recent_trades?limit=50&window_days=90&include_backfill=false').then(r=>r.json()).then(data=>{
    const el = document.getElementById('recent-trades-panel');
    if (!el) return;
    const acct = data.account || {};
    const trades = data.trades || [];
    const modeColor = acct.mode === 'PAPER' ? '#00d4ff' : '#ff9800';
    const brokerEq = acct.broker_equity_usd ? '$' + Number(acct.broker_equity_usd).toLocaleString(undefined, {maximumFractionDigits:0}) : '—';
    const anchor = '$' + Number(acct.fleet_sizing_anchor_usd || 0).toLocaleString();

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;flex-wrap:wrap;gap:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">RECENT TRADES</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;">live-only last ' + (data.window_days || 90) + 'd | showing ' + trades.length + ' of ' + (data.total_trades_in_window || 0) + '</div>'
      + '</div>';

    // Account context strip
    html += '<div style="background:#0d1117;border:1px solid #1a1f2e;border-radius:4px;padding:8px 12px;margin-bottom:10px;display:flex;flex-wrap:wrap;gap:18px;font-size:0.72em;">'
      + '<div><span style="color:#7b8ab8;">Account:</span> <span style="color:#e0e0e0;font-weight:bold;">' + (acct.ibkr_account_id || '—') + '</span> <span style="color:' + modeColor + ';font-weight:bold;">[' + (acct.mode || '?') + ']</span></div>'
      + '<div><span style="color:#7b8ab8;">Port:</span> <span style="color:#e0e0e0;">' + (acct.ibkr_port || '?') + '</span></div>'
      + '<div><span style="color:#7b8ab8;">Broker equity:</span> <span style="color:#e0e0e0;">' + brokerEq + '</span></div>'
      + '<div><span style="color:#7b8ab8;">Sizing anchor:</span> <span style="color:#e0e0e0;">' + anchor + '</span></div>'
      + '</div>';

    if (trades.length === 0) {
      html += '<div style="padding:20px;text-align:center;color:#7b8ab8;font-size:0.75em;">No trades in window.</div>';
      el.innerHTML = html;
      return;
    }

    html += '<div style="overflow-x:auto;">';
    html += '<table style="width:100%;border-collapse:collapse;font-size:0.72em;">';
    html += '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:6px 4px;">When <span style="font-weight:normal;color:#555;font-size:0.85em;">(' + Intl.DateTimeFormat().resolvedOptions().timeZone + ')</span></th>'
      + '<th style="text-align:left;padding:6px 4px;">Strategy</th>'
      + '<th style="text-align:left;padding:6px 4px;">Symbol</th>'
      + '<th style="text-align:left;padding:6px 4px;">Dir</th>'
      + '<th style="text-align:right;padding:6px 4px;">Entry</th>'
      + '<th style="text-align:right;padding:6px 4px;">Exit</th>'
      + '<th style="text-align:right;padding:6px 4px;">Size</th>'
      + '<th style="text-align:right;padding:6px 4px;">Buy-in $</th>'
      + '<th style="text-align:right;padding:6px 4px;">Risk $</th>'
      + '<th style="text-align:right;padding:6px 4px;">PnL $</th>'
      + '<th style="text-align:right;padding:6px 4px;">PnL %</th>'
      + '<th style="text-align:left;padding:6px 4px;">Exit reason</th>'
      + '</tr></thead><tbody>';

    for (const t of trades) {
      // Convert UTC ts to browser-local time (so Austin = CDT/CST auto-adjusts).
      // API returns ISO-8601 UTC; Date() handles the conversion.
      let tsShort = '';
      try {
        const d = new Date(t.ts);
        if (!isNaN(d.getTime())) {
          // en-CA gives YYYY-MM-DD; split for readable "YYYY-MM-DD HH:MM:SS" local
          const pad = n => String(n).padStart(2, '0');
          tsShort = d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate())
            + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
        } else {
          tsShort = (t.ts || '').replace('T', ' ').substring(0, 19);
        }
      } catch (e) {
        tsShort = (t.ts || '').replace('T', ' ').substring(0, 19);
      }
      const pnlColor = (t.pnl_usd || 0) >= 0 ? '#00ff88' : '#ff4444';
      const sign = (t.pnl_usd || 0) >= 0 ? '+' : '';
      const risk = t.risk_usd != null ? '$' + Number(t.risk_usd).toFixed(2) : '—';
      const pct = t.pnl_pct_of_fleet !== '' && t.pnl_pct_of_fleet != null ? (Number(t.pnl_pct_of_fleet) >= 0 ? '+' : '') + Number(t.pnl_pct_of_fleet).toFixed(3) + '%' : '—';
      const entry = t.entry_px ? Number(t.entry_px).toFixed(4).replace(/\.?0+$/, '') : '—';
      const exit = t.exit_px ? Number(t.exit_px).toFixed(4).replace(/\.?0+$/, '') : '—';
      const buyIn = t.notional_usd != null ? '$' + Number(t.notional_usd).toLocaleString(undefined, {maximumFractionDigits:0}) : '—';
      html += '<tr style="border-bottom:1px solid #151c2c;">'
        + '<td style="padding:5px 4px;color:#9da8c7;white-space:nowrap;">' + tsShort + '</td>'
        + '<td style="padding:5px 4px;color:#00d4ff;">' + (t.strategy || '—') + '</td>'
        + '<td style="padding:5px 4px;color:#e0e0e0;">' + (t.symbol || '—') + '</td>'
        + '<td style="padding:5px 4px;color:#9da8c7;">' + (t.direction || '—') + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:#9da8c7;">' + entry + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:#9da8c7;">' + exit + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:#9da8c7;">' + (t.size || '—') + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:#e0e0e0;">' + buyIn + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:#9da8c7;">' + risk + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:' + pnlColor + ';font-weight:bold;">' + sign + '$' + (t.pnl_usd || 0).toFixed(2) + '</td>'
        + '<td style="padding:5px 4px;text-align:right;color:' + pnlColor + ';">' + pct + '</td>'
        + '<td style="padding:5px 4px;color:#7b8ab8;">' + (t.exit_reason || '—') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';

    el.innerHTML = html;
  }).catch((e)=>{
    const el = document.getElementById('recent-trades-panel');
    if (el) el.innerHTML = '<div style="color:#ff4444;font-size:0.75em;">recent trades error: ' + e + '</div>';
  });
}
loadRecentTrades();
setInterval(loadRecentTrades, 60000);
