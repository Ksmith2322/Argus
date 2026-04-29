
function loadGatewayStatus() {
  fetch('/api/gateway_status').then(r=>r.json()).then(d=>{
    const el = document.getElementById('gateway-status-banner');
    if (!el) return;
    const healthy = d.all_healthy;
    const pauseFlag = d.pause_entries_present;
    const eq = d.broker_equity_usd;
    const bg = healthy && !pauseFlag ? '#0d2818' : (pauseFlag ? '#3a2a0a' : '#3a0d0d');
    const border = healthy && !pauseFlag ? '#00e676' : (pauseFlag ? '#ff9800' : '#ff4444');
    const label = healthy && !pauseFlag ? 'GATEWAY OK' : (pauseFlag ? 'ENTRIES PAUSED' : 'GATEWAY UNHEALTHY');
    const labelColor = healthy && !pauseFlag ? '#00e676' : (pauseFlag ? '#ff9800' : '#ff4444');
    const pairSummary = Object.entries(d.per_pair || {}).map(([sym, v]) => {
      const ok = v.fresh && v.broker_connected && (v.consecutive_errors || 0) === 0;
      const color = ok ? '#00e676' : '#ff4444';
      return `<span style="color:${color};margin:0 8px;">${sym.toUpperCase()}: ${v.broker_connected ? 'conn' : 'dc'}/${v.position || '?'}</span>`;
    }).join('');
    const eqStr = eq ? '$' + Number(eq).toLocaleString(undefined, {maximumFractionDigits: 0}) : '—';
    const pauseWarn = pauseFlag ? '<span style="color:#ff9800;font-weight:bold;margin-left:12px;">⚠ PAUSE_ENTRIES active</span>' : '';
    el.innerHTML = `<div style="background:${bg};border:1px solid ${border};border-radius:6px;padding:8px 14px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;font-size:0.8em;">`
      + `<span style="color:${labelColor};font-weight:bold;letter-spacing:2px;">${label}</span>`
      + `<span style="color:#7b8ab8;">broker equity: <span style="color:#e0e0e0;">${eqStr}</span></span>`
      + `<span style="color:#7b8ab8;">pairs:</span>${pairSummary}`
      + pauseWarn
      + `</div>`;
  }).catch(()=>{});
}
loadGatewayStatus();
setInterval(loadGatewayStatus, 30000);
