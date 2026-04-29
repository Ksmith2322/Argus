
// Stale-data banner: red alert if managed_truth_loop has died and reports have stopped refreshing.
// Checks the age of risk_oversight_report.json + silent_block_alerts.json.
function loadStaleDataBanner() {
  fetch('/api/gateway_status').then(r=>r.json()).then(g=>{
    // We derive staleness from the risk_oversight_report.json age (the daemon writes it every 3min)
    const reportAgeS = g.risk_oversight_age_s || 0;
    const el = document.getElementById('stale-data-banner');
    if (!el) return;
    if (reportAgeS > 900) {  // >15 min stale → daemon likely dead
      el.innerHTML = '<div style="background:#2a0f0f;border:1px solid #ff4444;border-radius:4px;padding:10px 14px;">'
        + '<div style="color:#ff4444;font-weight:bold;font-size:0.85em;letter-spacing:2px;">&#9888; STALE-DATA ALERT</div>'
        + '<div style="font-size:0.75em;margin-top:4px;">risk_oversight_report.json hasn\'t refreshed in ' + Math.round(reportAgeS/60) + ' min. '
        + 'The managed_truth_loop daemon may be dead. Check: <code>Get-Process -Name python | Where-Object { $_.CommandLine -like \'*managed_truth_loop*\' }</code></div>'
        + '</div>';
    } else if (reportAgeS > 400) {  // 6-15 min: warn but not alarm
      el.innerHTML = '<div style="background:#2a2010;border:1px solid #ffaa00;border-radius:4px;padding:6px 12px;font-size:0.72em;color:#ffaa00;">'
        + '&#9432; risk_oversight_report slightly stale (' + Math.round(reportAgeS/60) + ' min old). Daemon cadence is 3 min — this is borderline.'
        + '</div>';
    } else {
      el.innerHTML = '';  // healthy, silent
    }
  }).catch(()=>{});
}
loadStaleDataBanner();
setInterval(loadStaleDataBanner, 30000);
