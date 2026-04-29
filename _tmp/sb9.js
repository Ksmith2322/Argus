
function loadFleetHealth() {
  fetch('/api/fleet_health').then(r=>r.json()).then(data=>{
    const el = document.getElementById('fleet-health');
    if (!el) return;
    const systems = data.systems || {};
    if (Object.keys(systems).length === 0) {
      el.innerHTML = '<div style="font-size:0.7em;color:#7b8ab8;">Fleet monitor not running</div>';
      return;
    }

    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">FLEET HEALTH</div>'
      + '<div style="font-size:0.65em;color:#7b8ab8;">Auto-refresh 30s | Watchdog active</div>'
      + '</div>';
    html += '<div style="display:flex;gap:8px;flex-wrap:wrap;">';

    for (const [name, sys] of Object.entries(systems)) {
      const status = sys.status || '?';
      const color = status === 'OK' ? '#00ff88' : (status === 'STALE' ? '#ffc107' : '#ff4444');
      const proc = sys.process_alive ? 'PROC OK' : 'PROC DOWN';
      const ageStr = sys.max_age_s !== undefined ? Math.floor(sys.max_age_s) + 's' : '';
      html += '<div style="background:#141b2d;border:1px solid #1e2a42;border-left:3px solid ' + color + ';border-radius:4px;padding:6px 12px;font-size:0.7em;">'
        + '<div style="font-weight:bold;color:' + color + ';">&#9679; ' + name.toUpperCase() + ' ' + status + '</div>'
        + '<div style="color:#7b8ab8;font-size:0.85em;">' + proc + ' | ' + ageStr + '</div>'
        + '</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadFleetHealth();
setInterval(loadFleetHealth, 30000);
