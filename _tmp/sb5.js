
function loadSilentBlock() {
  fetch('/api/silent_block_alerts').then(r=>r.json()).then(data=>{
    const el = document.getElementById('silent-block-banner');
    if (!el) return;
    const flagged = data.flagged || [];
    if (data.error) {
      el.innerHTML = ''; // silent if file not yet written
      return;
    }
    if (flagged.length === 0) {
      // Subtle green confirmation — tells you the checker is running
      el.innerHTML = '<div style="background:#0d1c11;border:1px solid #143021;border-radius:4px;padding:6px 12px;font-size:0.7em;color:#00e676;">'
        + '<span style="letter-spacing:1px;">SILENT-BLOCK</span>: all ' + (data.ok_count || 0) + ' active strategies emitting signals normally'
        + ' <span style="color:#7b8ab8;">(' + (data.out_of_session_count || 0) + ' out-of-session)</span>'
        + '</div>';
      return;
    }
    // Red alert panel for any flagged
    const strategyList = flagged.map(f =>
      '<div style="margin:4px 0;"><span style="color:#ff4444;font-weight:bold;">' + f.strategy + '</span> '
      + '<span style="color:#9da8c7;">— ' + (f.reason || '?') + '</span></div>'
    ).join('');
    el.innerHTML = '<div style="background:#2a0f0f;border:1px solid #ff4444;border-radius:4px;padding:10px 14px;">'
      + '<div style="color:#ff4444;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">&#9888; SILENT-BLOCK ALERT — ' + flagged.length + ' strategy' + (flagged.length !== 1 ? 'ies' : '') + ' mute during active session</div>'
      + '<div style="font-size:0.75em;">' + strategyList + '</div>'
      + '<div style="font-size:0.65em;color:#7b8ab8;margin-top:6px;">Checked ' + (data.checked_at || '?') + '. Runner alive but emitting no signals — probable data feed issue or stuck eval loop.</div>'
      + '</div>';
  }).catch(()=>{});
}
loadSilentBlock();
setInterval(loadSilentBlock, 30000);
