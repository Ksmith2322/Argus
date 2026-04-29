
// Fallback init — runs even if main script has errors
window.addEventListener('load', function() {
  if (typeof loadIBKRFleet === 'function') {
    try { loadIBKRFleet(); } catch(e) { console.error('Fleet load error:', e); }
  } else {
    // loadIBKRFleet not defined — main script failed to parse
    var cards = document.getElementById('ibkr-paper-cards') || document.getElementById('ibkr-watcher-cards') || document.getElementById('ibkr-real-cards');
    if (cards) cards.innerHTML = '<div style="color:#ff4444;padding:20px;">Dashboard script error — check browser console (F12)</div>';
  }
});
