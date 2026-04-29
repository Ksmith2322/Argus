
// Maturity verdict banner — one-glance summary of operational maturity state
function loadMaturityBanner() {
  fetch('/api/operational_maturity').then(r=>r.json()).then(data=>{
    const el = document.getElementById('maturity-summary-banner');
    if (!el) return;
    if (data.error) { el.innerHTML = ''; return; }
    const totals = data.totals || {};
    const counts = totals.verdict_counts || {};
    const N = totals.strategies_count || 0;
    const nTrades = totals.total_live_trades || 0;
    const pnl = totals.total_live_pnl_usd || 0;
    const degraded = (counts.DEGRADED || 0);
    const validated = (counts.VALIDATED || 0);
    const emerging = (counts.EMERGING || 0);
    const insuf = (counts.INSUFFICIENT_DATA || 0);
    const waiting = (counts.WAITING || 0);
    const flagged = data.strategies.filter(s => s.verdict === 'DEGRADED').map(s => s.strategy);
    const bgColor = degraded > 0 ? '#2a0f0f' : '#0d1321';
    const borderColor = degraded > 0 ? '#ff4444' : '#1e2a42';
    const pnlColor = pnl >= 0 ? '#00ff88' : '#ff4444';
    const titleColor = degraded > 0 ? '#ff4444' : '#00d4ff';
    let html = '<div style="background:' + bgColor + ';border:1px solid ' + borderColor + ';border-radius:4px;padding:8px 12px;font-size:0.72em;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">'
      + '<div><span style="color:' + titleColor + ';font-weight:bold;letter-spacing:2px;">OPERATIONAL MATURITY</span>'
      + ' <span style="color:#7b8ab8;">(' + N + ' strategies, ' + nTrades + ' post-reset trades)</span></div>'
      + '<div style="color:#9da8c7;">'
      + '<span style="color:#00ff88;">' + validated + ' VALIDATED</span>'
      + ' <span style="color:#ffc107;">· ' + emerging + ' EMERGING</span>'
      + ' <span style="color:' + (degraded > 0 ? '#ff4444' : '#7b8ab8') + ';">· ' + degraded + ' DEGRADED</span>'
      + ' <span style="color:#7b8ab8;">· ' + insuf + ' INSUFFICIENT · ' + waiting + ' WAITING</span>'
      + ' · <span style="color:' + pnlColor + ';font-weight:bold;">' + (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) + '</span>'
      + '</div></div>';
    if (flagged.length > 0) {
      html += '<div style="margin-top:6px;color:#ff9999;">Degraded: ' + flagged.join(', ')
        + ' (live_pf below 60% of backtest)</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadMaturityBanner();
setInterval(loadMaturityBanner, 60000);
