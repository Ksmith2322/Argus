
// ─── MARKET CLOCK ───────────────────────────────────────────────────
// Refreshes from server every 60s; ticks the countdown locally every 1s.
let _marketClockData = null;
let _marketClockFetchedAt = 0;
const COLOR_MAP = {green:'#00e676', yellow:'#ffaa00', red:'#ff4444'};
function fmtCountdown(secs) {
  if (secs == null || secs < 0) return '—';
  if (secs < 60) return secs + 's';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h >= 24) return Math.floor(h/24) + 'd ' + (h%24) + 'h';
  if (h > 0) return h + 'h ' + String(m).padStart(2,'0') + 'm';
  return m + 'm ' + String(secs % 60).padStart(2,'0') + 's';
}
function renderMarketClock() {
  const el = document.getElementById('market-clock-bar');
  if (!el || !_marketClockData) return;
  const elapsed = Math.floor((Date.now() - _marketClockFetchedAt) / 1000);
  const m = _marketClockData;
  const blocks = [];
  for (const key of ['us_stocks','fx','futures']) {
    const mk = m[key]; if (!mk) continue;
    const dot = '<span style="color:'+COLOR_MAP[mk.status_color]+';font-weight:bold;">●</span>';
    const remaining = mk.next_event ? Math.max(0, mk.next_event.seconds_until - elapsed) : null;
    const event = mk.next_event ? '<span style="color:#7b8ab8;">→ '+mk.next_event.label+' (in '+fmtCountdown(remaining)+')</span>' : '';
    blocks.push('<span style="white-space:nowrap;">'+dot+' <span style="color:#e0e0e0;font-weight:bold;">'+mk.label+':</span> <span style="color:'+COLOR_MAP[mk.status_color]+';">'+mk.status_label+'</span> '+event+'</span>');
  }
  el.innerHTML = blocks.join('<span style="color:#1e2a42;">|</span>')
    + '<span style="color:#7b8ab8;font-size:0.85em;margin-left:auto;">'+m.now_et_display+'</span>';
}
function loadMarketClock() {
  fetch('/api/market_clock').then(r=>r.json()).then(data=>{
    _marketClockData = data;
    _marketClockFetchedAt = Date.now();
    renderMarketClock();
  }).catch(()=>{});
}
loadMarketClock();
setInterval(loadMarketClock, 60000);   // re-fetch every 60s
setInterval(renderMarketClock, 1000);  // tick countdown every 1s (no network)

// ─── DATA EPOCH BANNER ─────────────────────────────────────────────
// Shows the live-paper reset cutoff so panels showing "n=N trades" are
// understood in context. Without this, "30d window" misleadingly suggests
// the data spans pre + post reset epochs. Per the dashboard upgrades memo:
// "Data provenance labels are mandatory on every panel."
// Hidden when in_sync=true and days_since_reset <= 7 (mostly noisy then);
// shown otherwise so the operator never forgets the boundary.
let _DATA_EPOCH = null;  // module-level cache for use by other panels
function loadDataEpoch() {
  fetch('/api/data_epoch').then(r=>r.json()).then(data=>{
    _DATA_EPOCH = data;
    const el = document.getElementById('data-epoch-bar');
    if (!el) return;
    const cur = data.current_epoch || {};
    const days = cur.days_since_reset;
    const inSync = data.in_sync;
    // Always show — provenance is the kind of thing that gets forgotten if hidden
    const syncBadge = inSync === false
      ? '<span style="background:#3a0a0a;color:#ff4444;padding:1px 6px;border-radius:3px;font-weight:bold;font-size:0.85em;letter-spacing:1px;">⚠ DRIFT</span>'
      : (inSync === true
         ? '<span style="background:#0d1c11;color:#00ff88;padding:1px 6px;border-radius:3px;font-size:0.85em;letter-spacing:1px;">in sync</span>'
         : '<span style="color:#7b8ab8;font-size:0.85em;">unknown</span>');
    el.style.display = 'block';
    el.innerHTML = '<div style="background:#0a1224;border:1px solid #1e2a42;border-radius:6px;padding:6px 14px;font-size:0.74em;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">'
      + '<span style="color:#7b8ab8;letter-spacing:2px;">DATA EPOCH</span>'
      + '<span style="color:#9da8c7;">Live epoch: <b style="color:#fff;">' + (cur.label || '—') + '</b></span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Cutoff: <b style="color:#fff;">' + (cur.cutoff_utc || '—').replace('T', ' ').replace('+00:00', ' UTC') + '</b></span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Days since reset: <b style="color:#fff;">' + (days != null ? days.toFixed(1) + 'd' : '—') + '</b></span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Generators ' + syncBadge + '</span>'
      + (data.prior_epochs && data.prior_epochs.length ? '<span style="color:#1e2a42;">|</span><span style="color:#7b8ab8;font-size:0.85em;" title="prior epochs archived for forensics">' + data.prior_epochs.length + ' prior epoch(s) archived</span>' : '')
      + '</div>';
  }).catch(()=>{});
}
loadDataEpoch();
setInterval(loadDataEpoch, 600000);  // 10 min — epoch rarely changes, just verifying sync

// ─── HALT BANNER ───────────────────────────────────────────────────
function loadHaltStatus() {
  fetch('/api/halt_status').then(r=>r.json()).then(d=>{
    const el = document.getElementById('halt-banner');
    if (!el) return;
    if (!d.halted) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const setAt = d.set_at ? new Date(d.set_at).toLocaleString() : 'unknown';
    el.innerHTML = '<div style="background:#3a0a0a;border:2px solid #ff4444;border-radius:6px;padding:12px 18px;color:#ffe;font-size:0.9em;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">'
      + '<div><span style="color:#ff4444;font-weight:bold;letter-spacing:2px;font-size:1.05em;">⛔ FLEET HALTED</span>'
      + ' <span style="color:#ffaaaa;margin-left:12px;">no new entries will be submitted</span></div>'
      + '<button onclick="resumeFleet()" style="background:#1a3a1a;border:1px solid #00ff88;color:#00ff88;padding:6px 14px;border-radius:4px;cursor:pointer;font-weight:bold;letter-spacing:1px;font-size:0.85em;">RESUME FLEET</button>'
      + '</div>'
      + '<div style="margin-top:6px;color:#ffe;"><b>Reason:</b> ' + (d.reason || '(none)') + '</div>'
      + '<div style="margin-top:2px;color:#ffaaaa;font-size:0.85em;">Engaged at: ' + setAt + '</div>'
      + '</div>';
  }).catch(()=>{});
}
function resumeFleet() {
  if (!confirm('Resume fleet? Runners will be allowed to submit new entries on next eval cycle.')) return;
  fetch('/api/resume_fleet', {method:'POST'}).then(r=>r.json()).then(d=>{
    alert(d.message || 'Fleet resumed.');
    loadHaltStatus();
  });
}
loadHaltStatus();
setInterval(loadHaltStatus, 10000);  // re-check every 10s

// ─── TWS HEALTH BANNER ─────────────────────────────────────────────
// Surfaces the overnight-reset failure mode (TCP connected but data farms dead).
// Only renders when status != "healthy" (no clutter when normal).
function loadTwsHealth() {
  fetch('/api/tws_health').then(r=>r.json()).then(d=>{
    const el = document.getElementById('tws-health-banner');
    if (!el) return;
    if (d.status === 'healthy' || d.status === 'unknown') { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const config = {
      degraded:    {bg:'#3a0a0a', border:'#ff4444', icon:'⚠', title:'TWS DEGRADED', subtitle:'connected but data farms dead — re-login to TWS'},
      unreachable: {bg:'#3a0a0a', border:'#ff4444', icon:'✕', title:'TWS UNREACHABLE', subtitle:'TWS not responding on the API port'},
      error:       {bg:'#2a2010', border:'#ffaa00', icon:'?', title:'TWS PROBE ERROR', subtitle:'probe failed unexpectedly'},
    }[d.status] || {bg:'#2a2010', border:'#ffaa00', icon:'?', title:'TWS UNKNOWN', subtitle:''};
    const ageStr = d.age_seconds != null ? Math.floor(d.age_seconds/60) + 'm ago' : 'unknown';
    el.innerHTML = '<div style="background:' + config.bg + ';border:2px solid ' + config.border + ';border-radius:6px;padding:12px 18px;color:#ffe;font-size:0.9em;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">'
      + '<div><span style="color:' + config.border + ';font-weight:bold;letter-spacing:2px;font-size:1.05em;">' + config.icon + ' ' + config.title + '</span>'
      + ' <span style="color:#ffaaaa;margin-left:12px;">' + config.subtitle + '</span></div>'
      + '<div style="color:#ffaaaa;font-size:0.85em;">last probe: ' + ageStr + '</div>'
      + '</div>'
      + '<div style="margin-top:6px;color:#ffe;"><b>Reason:</b> ' + (d.reason || '(none)') + '</div>'
      + '<div style="margin-top:4px;color:#ffaaaa;font-size:0.85em;">Fix: re-login to TWS (paper account DUP472829), then run <code>start_all_runners.ps1 -RestartAll</code> to re-establish runner sessions. See failure mode #10 in reference_failure_modes.md.</div>'
      + '</div>';
  }).catch(()=>{});
}
loadTwsHealth();
setInterval(loadTwsHealth, 30000);  // re-check every 30s

// ─── CIRCUIT BREAKER BANNER ────────────────────────────────────────
// Daily loss tiers: OK / WARN (-1%) / PAUSE (-2%) / FLATTEN (-4%)
function loadCircuitBreaker() {
  fetch('/api/circuit_breaker_status').then(r=>r.json()).then(d=>{
    const el = document.getElementById('circuit-breaker-banner');
    if (!el) return;
    const tier = d.current_tier || 'OK';
    if (tier === 'OK') { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const config = {
      WARN:    {bg:'#2a2010', border:'#ffaa00', icon:'⚠', title:'CIRCUIT BREAKER: WARN', subtitle:'daily loss at -1% (alert only, no action taken)'},
      PAUSE:   {bg:'#3a2010', border:'#ff8800', icon:'⏸', title:'CIRCUIT BREAKER: PAUSE', subtitle:'daily loss at -2% — HALT.flag set automatically; existing positions exit normally'},
      FLATTEN: {bg:'#3a0a0a', border:'#ff4444', icon:'✕', title:'CIRCUIT BREAKER: FLATTEN', subtitle:'daily loss at -4% — HALT + FLATTEN_EOD set; positions force-closed'},
    }[tier] || {bg:'#0d1321', border:'#7b8ab8', icon:'?', title:'CIRCUIT BREAKER: ' + tier, subtitle:''};
    const pnl = (d.latest_pnl_pct ?? 0).toFixed(2);
    const eq = (d.latest_equity_usd ?? 0).toLocaleString(undefined,{maximumFractionDigits:0});
    const open = (d.day_open_equity_usd ?? 0).toLocaleString(undefined,{maximumFractionDigits:0});
    el.innerHTML = '<div style="background:' + config.bg + ';border:2px solid ' + config.border + ';border-radius:6px;padding:12px 18px;color:#ffe;font-size:0.9em;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;">'
      + '<div><span style="color:' + config.border + ';font-weight:bold;letter-spacing:2px;font-size:1.05em;">' + config.icon + ' ' + config.title + '</span>'
      + ' <span style="color:#ffaaaa;margin-left:12px;">' + config.subtitle + '</span></div>'
      + '<div style="color:#ffaaaa;font-size:0.85em;">day open $' + open + ' → now $' + eq + ' (' + pnl + '%)</div>'
      + '</div></div>';
  }).catch(()=>{});
}
loadCircuitBreaker();
setInterval(loadCircuitBreaker, 30000);

// ─── MFE CAPTURE PANEL ─────────────────────────────────────────────
// True MFE-based exit-quality with bar lookback (replaces target_capture
// which was a proxy from existing data).
function loadMfeCapture() {
  fetch('/api/mfe_capture').then(r=>r.json()).then(data=>{
    const el = document.getElementById('mfe-capture-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">MFE CAPTURE (' + (data.window_days||30) + 'd, true bar-lookback)</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">capture = realized / MFE. Low = exits leave money on table. mfe_to_mae shows risk shape.</span></div>'
      + '<div style="color:#7b8ab8;font-size:0.75em;">' + (data.n_trades_processed||0) + '/' + (data.n_trades_total||0) + ' trades · skipped ' + (data.n_skipped_invalid||0) + ' invalid + ' + (data.n_skipped_no_bars||0) + ' no-bars</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">Mean cap</th>'
      + '<th style="text-align:right;padding:5px 6px;">Median</th>'
      + '<th style="text-align:right;padding:5px 6px;">Excellent</th>'
      + '<th style="text-align:right;padding:5px 6px;">Good</th>'
      + '<th style="text-align:right;padding:5px 6px;">Partial</th>'
      + '<th style="text-align:right;padding:5px 6px;">Adverse</th>'
      + '<th style="text-align:right;padding:5px 6px;">MFE/MAE</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const meanColor = s.mean_capture > 0.5 ? '#00ff88' : (s.mean_capture > 0 ? '#ffc107' : '#ff4444');
      const mfeMaeUndef = (s.mean_mfe_to_mae === null || s.mean_mfe_to_mae === undefined);
      const mfeMaeColor = mfeMaeUndef ? '#7b8ab8' : (s.mean_mfe_to_mae > 2.0 ? '#00ff88' : (s.mean_mfe_to_mae > 1.0 ? '#ffc107' : '#ff4444'));
      const undefShare = (s.n_mae_undefined && s.n) ? Math.round(100 * s.n_mae_undefined / s.n) : 0;
      const mfeMaeText = mfeMaeUndef
        ? '<span title="MAE undefined for all ' + s.n + ' trades (never went adverse)">—</span>'
        : (s.mean_mfe_to_mae.toFixed(2) + (undefShare > 0 ? '<span style="color:#ffaa00;font-size:0.85em;margin-left:3px;" title="' + s.n_mae_undefined + ' of ' + s.n + ' trades had no adverse excursion — excluded from average. Treat ratio as best-effort, not raw."> ⚠</span>' : ''));
      const safeId = s.strategy.replace(/[^a-z0-9]/gi, '_');
      html += '<tr style="border-top:1px solid #1e2a42;cursor:pointer;" data-mfe-id="' + safeId + '" onclick="toggleMfeDrill(this.dataset.mfeId)" title="Click to expand last 20 trades">'
        + '<td style="padding:5px 6px;color:#e0e0e0;"><span id="mfe-arrow-' + safeId + '" style="color:#7b8ab8;font-size:0.85em;">▶</span> ' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.n + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + meanColor + ';font-weight:bold;">' + s.mean_capture.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.median_capture.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#00ff88;">' + s.excellent_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.good_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ffc107;">' + s.partial_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ff4444;">' + s.adverse_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + mfeMaeColor + ';">' + mfeMaeText + '</td>'
        + '</tr>';
      // Hidden drill-down row with per-trade detail
      const trades = s.trades || [];
      if (trades.length > 0) {
        html += '<tr id="mfe-drill-' + safeId + '" style="display:none;">'
          + '<td colspan="9" style="padding:6px 14px;background:#0a1224;">'
          + '<div style="font-size:0.72em;color:#7b8ab8;margin-bottom:4px;">Last ' + Math.min(trades.length, 20) + ' trades for ' + s.strategy + ':</div>'
          + '<table style="width:100%;border-collapse:collapse;font-size:0.72em;">'
          + '<thead><tr style="color:#7b8ab8;"><th style="text-align:left;padding:3px 6px;">Entry</th><th style="text-align:left;padding:3px 6px;">Symbol</th><th style="text-align:center;padding:3px 6px;">Dir</th><th style="text-align:right;padding:3px 6px;">MFE</th><th style="text-align:right;padding:3px 6px;">MAE</th><th style="text-align:right;padding:3px 6px;">Realized</th><th style="text-align:right;padding:3px 6px;">Capture</th><th style="text-align:center;padding:3px 6px;">Bar</th></tr></thead><tbody>';
        for (const t of trades.slice(-20).reverse()) {
          const cc = t.capture_ratio > 0.5 ? '#00ff88' : (t.capture_ratio > 0 ? '#ffc107' : '#ff4444');
          // Mini bar showing capture as percentage of MFE
          const barW = Math.max(2, Math.min(40, Math.abs(t.capture_ratio) * 40));
          const barColor = t.capture_ratio >= 0 ? '#00ff88' : '#ff4444';
          html += '<tr style="border-top:1px solid #1e2a42;">'
            + '<td style="padding:3px 6px;color:#9da8c7;">' + t.entry_ts.slice(5,16) + '</td>'
            + '<td style="padding:3px 6px;color:#e0e0e0;">' + t.symbol + '</td>'
            + '<td style="padding:3px 6px;text-align:center;color:#9da8c7;">' + t.direction.slice(0,1).toUpperCase() + '</td>'
            + '<td style="padding:3px 6px;text-align:right;color:#00ff88;">+' + t.mfe_distance.toFixed(4) + '</td>'
            + '<td style="padding:3px 6px;text-align:right;color:#ff4444;">-' + t.mae_distance.toFixed(4) + '</td>'
            + '<td style="padding:3px 6px;text-align:right;color:' + cc + ';">' + (t.realized_distance >= 0 ? '+' : '') + t.realized_distance.toFixed(4) + '</td>'
            + '<td style="padding:3px 6px;text-align:right;color:' + cc + ';font-weight:bold;">' + t.capture_ratio.toFixed(2) + '</td>'
            + '<td style="padding:3px 6px;text-align:center;"><div style="display:inline-block;width:40px;height:8px;background:#1e2a42;border-radius:1px;position:relative;"><div style="position:absolute;left:0;top:0;height:100%;width:' + barW + 'px;background:' + barColor + ';"></div></div></td>'
            + '</tr>';
        }
        html += '</tbody></table></td></tr>';
      }
    }
    html += '</tbody></table>'
      + '<div style="font-size:0.7em;color:#7b8ab8;margin-top:6px;">Excellent: capture &ge; 80% of MFE. Adverse: stopped past entry. MFE/MAE &gt; 2 = trade goes favorable before going adverse (good signal). Click strategy row to drill in.</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
function toggleMfeDrill(safeId) {
  const row = document.getElementById('mfe-drill-' + safeId);
  const arrow = document.getElementById('mfe-arrow-' + safeId);
  if (!row) return;
  const expanded = row.style.display !== 'none';
  row.style.display = expanded ? 'none' : 'table-row';
  if (arrow) arrow.textContent = expanded ? '▶' : '▼';
}
loadMfeCapture();
setInterval(loadMfeCapture, 300000);  // refresh every 5min (data computed by managed_truth_loop)

// ─── CLUSTER EXPOSURE PANEL ────────────────────────────────────────
// Per-cluster horizontal bar chart (used vs cap). Auto-hides when fleet flat.
function loadClusterExposurePanel() {
  fetch('/api/cluster_exposure').then(r=>r.json()).then(data=>{
    const el = document.getElementById('cluster-exposure-panel');
    if (!el) return;
    const clusters = (data.clusters || []).filter(c => c.pct_used > 0);
    if (clusters.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">'
      + '<span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">CLUSTER EXPOSURE</span>'
      + '<span style="color:#7b8ab8;font-size:0.78em;">total $' + (data.total_notional_usd||0).toLocaleString(undefined,{maximumFractionDigits:0}) + ' / $' + (data.total_cap_usd||0).toLocaleString(undefined,{maximumFractionDigits:0}) + ' (' + (data.total_pct_used||0).toFixed(1) + '%)</span></div>'
      + '<div style="display:flex;flex-direction:column;gap:6px;">';
    for (const c of clusters) {
      const barColor = c.pct_used >= 90 ? '#ff4444' : (c.pct_used >= 70 ? '#ff8800' : (c.pct_used >= 40 ? '#ffaa00' : '#00d4ff'));
      const barW = Math.min(100, c.pct_used).toFixed(1);
      html += '<div style="display:flex;align-items:center;gap:10px;font-size:0.78em;">'
        + '<div style="min-width:160px;color:#e0e0e0;">' + c.cluster + '</div>'
        + '<div style="flex:1;background:#0a1224;border:1px solid #1e2a42;border-radius:3px;height:14px;position:relative;overflow:hidden;">'
        + '<div style="background:' + barColor + ';height:100%;width:' + barW + '%;"></div>'
        + '<div style="position:absolute;top:0;left:0;right:0;text-align:center;line-height:14px;color:#fff;font-size:0.78em;font-weight:bold;text-shadow:0 0 2px #000;">' + c.pct_used.toFixed(1) + '%</div>'
        + '</div>'
        + '<div style="min-width:160px;text-align:right;color:#9da8c7;">$' + c.used_usd.toLocaleString(undefined,{maximumFractionDigits:0}) + ' / $' + c.cap_usd.toLocaleString(undefined,{maximumFractionDigits:0}) + '</div>'
        + '</div>';
    }
    html += '</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;margin-top:6px;">Per-instrument cap: ' + (data.single_instrument_cap_x||0.6) + 'x equity. Cluster cap pre-trade check rejects entries with cluster_cap_breach.</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadClusterExposurePanel();
setInterval(loadClusterExposurePanel, 30000);

// ─── THREE-STATE DIMENSION DISPLAY ─────────────────────────────────
// Runtime / Trading / Decision per strategy. Catches the degradation that
// a single 'OK' tile hides (process up, decision=KILL_CANDIDATE).
const RUNTIME_COLORS = {OK:'#00e676', STALE:'#ffaa00', DOWN:'#ff4444', DEGRADED:'#ff8800', BLOCKED:'#ff4444', UNKNOWN:'#7b8ab8'};
const TRADING_COLORS = {FLAT:'#7b8ab8', IN_TRADE:'#00d4ff', WAITING:'#9da8c7'};
const DECISION_COLORS = {SCALE_UP:'#00ff88', HOLD:'#9da8c7', REDUCE:'#ffaa00', KILL:'#ff4444', OBSERVE:'#7b8ab8', UNKNOWN:'#7b8ab8'};
function loadThreeState() {
  fetch('/api/strategy_states').then(r=>r.json()).then(data=>{
    const el = document.getElementById('three-state-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">THREE-STATE DIMENSIONS'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">runtime, trading, decision - one OK tile hides too much</span></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:center;padding:5px 6px;">Runtime</th>'
      + '<th style="text-align:center;padding:5px 6px;">Trading</th>'
      + '<th style="text-align:center;padding:5px 6px;">Decision</th>'
      + '<th style="text-align:right;padding:5px 6px;">Conf</th>'
      + '<th style="text-align:left;padding:5px 6px;">Reason</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const rc = RUNTIME_COLORS[s.runtime] || '#7b8ab8';
      const tc = TRADING_COLORS[s.trading] || '#7b8ab8';
      const dc = DECISION_COLORS[s.decision] || '#7b8ab8';
      const flagDanger = (s.runtime === 'OK' && (s.decision === 'KILL' || s.decision === 'REDUCE'));
      const rowBg = flagDanger ? 'background:#2a1010;' : '';
      html += '<tr style="border-top:1px solid #1e2a42;' + rowBg + '">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + (flagDanger ? ' <span title="Runtime OK but decision recommends action" style="color:#ffaa00;">!</span>' : '') + '</td>'
        + '<td style="padding:5px 6px;text-align:center;color:' + rc + ';font-weight:bold;">' + s.runtime + '</td>'
        + '<td style="padding:5px 6px;text-align:center;color:' + tc + ';">' + s.trading + '</td>'
        + '<td style="padding:5px 6px;text-align:center;color:' + dc + ';font-weight:bold;">' + s.decision + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + (s.decision_confidence_pct||0) + '%</td>'
        + '<td style="padding:5px 6px;color:#7b8ab8;font-size:0.92em;">' + (s.decision_reason || '-') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="font-size:0.7em;color:#7b8ab8;margin-top:4px;">Red row = runtime OK but decision recommends action. The hidden-degradation case the memo flagged.</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadThreeState();
setInterval(loadThreeState, 60000);

// ─── FLEET DIMENSIONS PANEL (#6 + #8) ──────────────────────────────
// Top: 5 fleet sub-scores (operational / evidence / execution / risk / attribution)
// Bottom: per-strategy 5-OK status as colored dots (PROC/EDGE/EXEC/RISK/PROMOTION)
function loadDimensions() {
  fetch('/api/strategy_dimensions').then(r=>r.json()).then(data=>{
    const el = document.getElementById('dimensions-panel');
    if (!el) return;
    const sub = data.subscores || {};
    const overall = data.overall || 0;
    const naive = data.overall_naive_avg;
    const cap = data.binding_cap_reason || 'naive_average';
    const overallColor = overall >= 80 ? '#00ff88' : (overall >= 60 ? '#ffc107' : '#ff4444');
    // Show binding cap when it's pinning the overall score below the naive mean.
    // Honest reading: a 78% naive average masking a 37% evidence layer is dishonest;
    // the cap surfaces *what is limiting readiness* instead of averaging it away.
    const capBadge = (cap !== 'naive_average')
      ? '<span style="font-size:0.7em;color:#7b8ab8;margin-left:8px;" title="Naive avg would be ' + (naive!=null?naive+'%':'—') + '. Cap binds: ' + cap + '">⚠ capped</span>'
      : '';
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">'
      + '<span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">FLEET DIMENSIONS</span>'
      + '<span style="font-size:0.78em;color:' + overallColor + ';font-weight:bold;">overall ' + overall + '%' + capBadge + '</span>'
      + '</div>'
      + (cap !== 'naive_average' ? '<div style="font-size:0.72em;color:#9da8c7;margin-bottom:8px;padding:4px 8px;background:#0a1224;border-left:3px solid #ffaa00;border-radius:2px;">Capped at ' + overall + '%: ' + cap + '. Naive average would be ' + (naive!=null?naive+'%':'—') + '.</div>' : '');
    // Sub-score bars
    const subDefs = [
      ['operational', 'Operational', '% of strategies with healthy process'],
      ['evidence',    'Evidence',    '% of strategies with n>=10 valid trades'],
      ['execution',   'Execution',   '100 minus failure rate of entry attempts'],
      ['risk',        'Risk',        'penalized when any cluster cap > 50% used'],
      ['attribution', 'Attribution', '100 minus top-1 alpha share (more diversified = higher)'],
    ];
    html += '<div style="display:flex;flex-direction:column;gap:5px;margin-bottom:10px;">';
    for (const [key, label, tip] of subDefs) {
      const v = sub[key] || 0;
      const c = v >= 80 ? '#00ff88' : (v >= 60 ? '#ffc107' : '#ff4444');
      html += '<div style="display:flex;align-items:center;gap:10px;font-size:0.78em;" title="' + tip + '">'
        + '<div style="min-width:120px;color:#9da8c7;">' + label + '</div>'
        + '<div style="flex:1;background:#0a1224;border:1px solid #1e2a42;border-radius:3px;height:12px;position:relative;overflow:hidden;">'
        + '<div style="background:' + c + ';height:100%;width:' + Math.min(100,v) + '%;"></div>'
        + '</div>'
        + '<div style="min-width:50px;text-align:right;color:' + c + ';font-weight:bold;">' + v + '%</div>'
        + '</div>';
    }
    html += '</div>';

    // Per-strategy 5-OK dots
    const rows = data.strategies || [];
    if (rows.length > 0) {
      html += '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
        + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
        + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
        + '<th style="text-align:center;padding:5px 6px;" title="process up + heartbeat fresh">Proc</th>'
        + '<th style="text-align:center;padding:5px 6px;" title="not in DECLINING drift, not bleeding heavily">Edge</th>'
        + '<th style="text-align:center;padding:5px 6px;" title="no chronic execution failures">Exec</th>'
        + '<th style="text-align:center;padding:5px 6px;" title="no cluster cap > 80%">Risk</th>'
        + '<th style="text-align:center;padding:5px 6px;" title="action allows scaling (not KILL)">Promo</th>'
        + '<th style="text-align:right;padding:5px 6px;">Decision</th>'
        + '</tr></thead><tbody>';
      const dot = (ok, key) => {
        const c = ok ? '#00ff88' : '#ff4444';
        const sym = ok ? '✓' : '✗';
        return '<span style="color:' + c + ';font-weight:bold;font-size:1.1em;" title="' + key + '=' + (ok?'OK':'FAIL') + '">' + sym + '</span>';
      };
      const decisionColors = {SCALE_UP:'#00ff88', HOLD:'#9da8c7', REDUCE:'#ffaa00', KILL:'#ff4444', OBSERVE:'#7b8ab8', UNKNOWN:'#7b8ab8'};
      for (const s of rows) {
        const dc = decisionColors[s.decision] || '#7b8ab8';
        html += '<tr style="border-top:1px solid #1e2a42;">'
          + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
          + '<td style="padding:5px 6px;text-align:center;">' + dot(s.proc_ok, 'PROC_OK') + '</td>'
          + '<td style="padding:5px 6px;text-align:center;">' + dot(s.edge_ok, 'EDGE_OK') + '</td>'
          + '<td style="padding:5px 6px;text-align:center;">' + dot(s.exec_ok, 'EXEC_OK') + '</td>'
          + '<td style="padding:5px 6px;text-align:center;">' + dot(s.risk_ok, 'RISK_OK') + '</td>'
          + '<td style="padding:5px 6px;text-align:center;">' + dot(s.promotion_ok, 'PROMOTION_OK') + '</td>'
          + '<td style="padding:5px 6px;text-align:right;color:' + dc + ';font-weight:bold;">' + s.decision + '</td>'
          + '</tr>';
      }
      html += '</tbody></table>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(()=>{});
}
loadDimensions();
setInterval(loadDimensions, 60000);

// ─── CAPITAL SAFETY BAR ────────────────────────────────────────────
// Single-line top-of-page summary. Combines positions_open (risk $) +
// cluster_exposure (gross notional + top cluster). Color-codes each
// segment so degraded states pop visually before you scroll.
function loadCapitalSafetyBar() {
  Promise.all([
    fetch('/api/positions_open').then(r=>r.json()),
    fetch('/api/cluster_exposure').then(r=>r.json()),
    fetch('/api/margin_status').then(r=>r.json()).catch(()=>({status:'error'})),
    fetch('/api/broker_drift_status').then(r=>r.json()).catch(()=>({status:'error'})),
    fetch('/api/metric_integrity').then(r=>r.json()).catch(()=>({headline_status:'unknown'})),
  ]).then(([pos, clu, mar, drift, integ])=>{
    const el = document.getElementById('capital-safety-bar');
    if (!el) return;
    const anchor = pos.anchor_usd || 0;
    const openCount = pos.count || 0;
    const openRisk = pos.total_risk_usd || 0;
    const riskPct = pos.pct_of_budget_used || 0;          // % of risk budget (e.g. $1,899)
    const riskPctEquity = anchor ? (openRisk / anchor * 100) : 0;  // % of total equity
    const grossUsd = clu.total_notional_usd || 0;
    const grossPct = anchor ? (grossUsd / anchor * 100) : 0;
    const totalCapPct = clu.total_pct_used || 0;
    // Top-cluster from clu.clusters
    const clusters = clu.clusters || [];
    const topCluster = clusters.find(c => c.pct_used > 0);
    // Color thresholds (consistent with risk-banner pattern)
    const riskColor = riskPct >= 80 ? '#ff4444' : riskPct >= 50 ? '#ffaa00' : '#00d4ff';
    const grossColor = grossPct >= 250 ? '#ff4444' : grossPct >= 150 ? '#ffaa00' : '#00d4ff';
    const totalColor = totalCapPct >= 80 ? '#ff4444' : totalCapPct >= 50 ? '#ffaa00' : '#00d4ff';
    const fleetState = openCount === 0 ? 'FLAT' : (riskPct >= 80 ? 'HOT' : 'ACTIVE');
    const stateColor = fleetState === 'FLAT' ? '#00e676' : (fleetState === 'HOT' ? '#ff4444' : '#00d4ff');
    // Margin segment (only show when status=ok and not stale)
    let marginSeg = '';
    if (mar && mar.status === 'ok' && !mar.stale) {
      const marColor = mar.band === 'HOT' ? '#ff4444' : mar.band === 'WARN' ? '#ffaa00' : '#00d4ff';
      marginSeg = '<span style="color:#1e2a42;">|</span>'
        + '<span style="color:#9da8c7;">Margin: <b style="color:' + marColor + ';">' + mar.margin_used_pct.toFixed(1) + '%</b>'
        + ' used · headroom <b style="color:#fff;">$' + mar.available_funds_usd.toLocaleString(undefined,{maximumFractionDigits:0}) + '</b></span>';
    } else if (mar && mar.status === 'ok' && mar.stale) {
      marginSeg = '<span style="color:#1e2a42;">|</span>'
        + '<span style="color:#7b8ab8;">Margin: <i>stale (' + mar.age_s + 's)</i></span>';
    }
    // Broker drift segment — only show when state is known + non-zero or breached.
    // 'unknown'/'error' or pristine zero-divergence stay hidden to avoid noise.
    let driftSeg = '';
    if (drift && drift.divergence_pct !== undefined) {
      const dpct = drift.divergence_pct;
      const sustained = drift.sustained_minutes || 0;
      const tripped = drift.tripped === true;
      const breach = Math.abs(dpct) > (drift.tolerance_pct || 1.0);
      // Render only if interesting (breach, tripped, or non-trivial divergence)
      if (tripped || breach || Math.abs(dpct) >= 0.25) {
        const dColor = tripped ? '#ff4444' : breach ? '#ffaa00' : '#7b8ab8';
        const label = tripped ? 'TRIPPED' : breach ? 'BREACH' : 'OK';
        driftSeg = '<span style="color:#1e2a42;">|</span>'
          + '<span style="color:#9da8c7;">Drift: <b style="color:' + dColor + ';">'
          + (dpct >= 0 ? '+' : '') + dpct.toFixed(2) + '%</b>'
          + (sustained > 0 ? ' (' + sustained.toFixed(0) + 'min ' + label + ')' : '')
          + '</span>';
      }
    }
    // Metric Integrity segment — single-line status of "is the data trustable?"
    // Lean implementation: hide when status=ok (no need to clutter the bar with
    // a green checkmark); show ⚠ + count when issues exist. Tooltip enumerates.
    let integritySeg = '';
    if (integ && integ.headline_status && integ.headline_status !== 'ok' && integ.headline_status !== 'unknown') {
      const iColor = integ.headline_status === 'fail' ? '#ff4444' : '#ffaa00';
      const issueList = (integ.issues || []).map(i => '• ' + i.message).join(' | ');
      integritySeg = '<span style="color:#1e2a42;">|</span>'
        + '<span style="color:#9da8c7;" title="' + issueList.replace(/"/g, '&quot;') + '">Integrity: '
        + '<b style="color:' + iColor + ';">⚠ ' + integ.checks_failed + ' issue' + (integ.checks_failed > 1 ? 's' : '') + '</b>'
        + ' <span style="color:#7b8ab8;font-size:0.92em;">(' + integ.summary + ')</span>'
        + '</span>';
    }
    el.innerHTML = '<div style="background:#0a1224;border:1px solid #1e2a42;border-radius:6px;padding:8px 14px;font-size:0.78em;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">'
      + '<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center;">'
      + '<span style="color:#7b8ab8;letter-spacing:2px;">CAPITAL SAFETY</span>'
      + '<span style="color:' + stateColor + ';font-weight:bold;letter-spacing:1px;">' + fleetState + '</span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Open: <b style="color:#fff;">' + openCount + ' pos</b> · risk <b style="color:' + riskColor + ';">$' + openRisk.toFixed(2) + '</b> / $' + (pos.fleet_budget_usd||0).toFixed(0) + ' budget (<b style="color:' + riskColor + ';">' + riskPct.toFixed(1) + '%</b>) · <span style="color:#7b8ab8;" title="Open risk as % of total broker equity (anchor)">' + riskPctEquity.toFixed(2) + '% of equity</span></span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Gross notional: <b style="color:' + grossColor + ';">$' + grossUsd.toLocaleString(undefined,{maximumFractionDigits:0}) + '</b> (' + grossPct.toFixed(0) + '% of equity)</span>'
      + '<span style="color:#1e2a42;">|</span>'
      + '<span style="color:#9da8c7;">Total cap usage: <b style="color:' + totalColor + ';">' + totalCapPct.toFixed(1) + '%</b></span>'
      // Today's PnL — single-number snapshot. Different from cumulative
      // equity curve; shows just-today's session impact at a glance.
      + (function(){
          const pnlT = pos.pnl_today_usd;
          if (pnlT == null) return '';
          const pnlTPct = pos.pnl_today_pct_of_anchor || 0;
          const tcount = pos.pnl_today_trade_count || 0;
          const pColor = pnlT > 0 ? '#00ff88' : pnlT < 0 ? '#ff4444' : '#9da8c7';
          const sign = pnlT >= 0 ? '+' : '';
          return '<span style="color:#1e2a42;">|</span>'
            + '<span style="color:#9da8c7;" title="Sum of pnl_usd from canonical_fills with exit_ts on today UTC, across ' + tcount + ' fill(s)">Today: <b style="color:' + pColor + ';">' + sign + '$' + pnlT.toFixed(2) + '</b> (' + sign + pnlTPct.toFixed(2) + '% · ' + tcount + ' fills)</span>';
        })()
      + marginSeg
      + driftSeg
      + integritySeg
      + (topCluster ? '<span style="color:#1e2a42;">|</span><span style="color:#9da8c7;">Top cluster: <b style="color:#fff;">' + topCluster.cluster + '</b> ' + topCluster.pct_used.toFixed(1) + '%</span>' : '')
      + '</div>'
      + '<span style="color:#7b8ab8;font-size:0.92em;">anchor $' + anchor.toLocaleString(undefined,{maximumFractionDigits:0}) + '</span>'
      + '</div>';
  }).catch(()=>{});
}
loadCapitalSafetyBar();
setInterval(loadCapitalSafetyBar, 15000);  // every 15s during market hours

// ─── REAL-MONEY READINESS BAR ──────────────────────────────────────
// Thin progress strip with click-to-expand for the full 20-item view.
// Single source of truth is the markdown checklist in the user's memory
// dir. Renders nothing when status=missing or after freeze + complete.
let _READINESS_EXPANDED = false;
function toggleReadinessExpand() {
  _READINESS_EXPANDED = !_READINESS_EXPANDED;
  loadReadinessBar();
}
function loadReadinessBar() {
  fetch('/api/readiness_check').then(r=>r.json()).then(d=>{
    const el = document.getElementById('readiness-bar');
    if (!el) return;
    if (d.status !== 'ok' || d.total === 0) { el.style.display = 'none'; return; }
    if (d.passed >= d.total && d.days_until_freeze < 0) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const pct = d.pct;
    const barColor = pct >= 80 ? '#00ff88' : pct >= 50 ? '#ffc107' : '#7b8ab8';
    const daysColor = d.days_until_freeze < 0 ? '#ff4444' : d.days_until_freeze <= 14 ? '#ffaa00' : '#9da8c7';
    const daysLabel = d.days_until_freeze < 0
      ? Math.abs(d.days_until_freeze) + 'd past 5/31 freeze'
      : d.days_until_freeze + 'd to 5/31 freeze';
    const arrow = _READINESS_EXPANDED ? '▼' : '▶';
    // Compact bar (always shown)
    let html = '<div onclick="toggleReadinessExpand()" style="cursor:pointer;background:#0a1224;border:1px solid #1e2a42;border-radius:6px;padding:6px 14px;font-size:0.74em;display:flex;align-items:center;gap:12px;" title="Click to ' + (_READINESS_EXPANDED ? 'collapse' : 'expand') + ' the 20-item checklist">'
      + '<span style="color:#7b8ab8;letter-spacing:1px;font-weight:bold;">' + arrow + ' REAL-MONEY READINESS</span>'
      + '<div style="flex:1;background:#0a1224;border:1px solid #1e2a42;border-radius:3px;height:10px;position:relative;overflow:hidden;">'
      + '<div style="background:' + barColor + ';height:100%;width:' + Math.min(100, pct) + '%;"></div>'
      + '</div>'
      + '<span style="color:' + barColor + ';font-weight:bold;min-width:90px;text-align:right;">' + d.passed + '/' + d.total + ' (' + pct + '%)</span>'
      + '<span style="color:' + daysColor + ';font-size:0.92em;min-width:130px;text-align:right;">' + daysLabel + '</span>'
      + '</div>';
    // Expanded section list (only when toggled open)
    if (_READINESS_EXPANDED) {
      html += '<div style="background:#0a1224;border:1px solid #1e2a42;border-top:none;border-radius:0 0 6px 6px;padding:10px 14px;font-size:0.78em;">';
      html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:14px;">';
      for (const sec of (d.sections || [])) {
        const secColor = sec.pct >= 80 ? '#00ff88' : sec.pct >= 50 ? '#ffc107' : '#7b8ab8';
        html += '<div style="padding:6px 0;">'
          + '<div style="display:flex;justify-content:space-between;align-items:baseline;border-bottom:1px solid #1e2a42;padding-bottom:3px;margin-bottom:4px;">'
          + '<span style="color:#00d4ff;font-weight:bold;letter-spacing:1px;font-size:0.92em;">' + sec.name + '</span>'
          + '<span style="color:' + secColor + ';font-size:0.85em;font-weight:bold;">' + sec.passed + '/' + sec.total + '</span>'
          + '</div>';
        for (const item of (sec.items || [])) {
          const checkColor = item.passed ? '#00ff88' : '#7b8ab8';
          const checkBox = item.passed ? '☑' : '☐';
          const titleColor = item.passed ? '#9da8c7' : '#e0e0e0';
          const titleStyle = item.passed ? 'text-decoration:line-through;' : '';
          html += '<div style="padding:3px 0;display:flex;gap:6px;align-items:flex-start;">'
            + '<span style="color:' + checkColor + ';font-size:1.1em;font-weight:bold;flex-shrink:0;">' + checkBox + '</span>'
            + '<div style="flex:1;">'
            + '<span style="color:' + titleColor + ';' + titleStyle + 'font-weight:' + (item.passed ? 'normal' : 'bold') + ';">#' + item.n + ' ' + item.title + '</span>'
            + (item.detail ? '<div style="color:#7b8ab8;font-size:0.85em;margin-top:1px;">' + item.detail + '</div>' : '')
            + '</div>'
            + '</div>';
        }
        html += '</div>';
      }
      html += '</div>';
      html += '<div style="margin-top:8px;padding-top:6px;border-top:1px solid #1e2a42;font-size:0.85em;color:#7b8ab8;">'
        + 'Source: <span style="color:#9da8c7;">' + (d.source_path || '—').replace(/\\/g, '/') + '</span>. '
        + 'Edit checkboxes in the markdown file (`- [ ]` → `- [x]`); the dashboard will pick up changes within 10 min.'
        + '</div>';
      html += '</div>';
    }
    el.innerHTML = html;
  }).catch(()=>{
    const el = document.getElementById('readiness-bar');
    if (el) el.style.display = 'none';
  });
}
loadReadinessBar();
setInterval(loadReadinessBar, 600000);  // 10 min — checklist changes are rare

// ─── BLOCKED ENTRIES (LAST 24h) ────────────────────────────────────
// Compact one-line counter of guard fires. Hides when totals are all zero.
// Buckets, in priority order: cluster_cap_breach (interesting), fx_below_idealpro_min
// (sizing too small), fleet_halted (kill-switch), market_closed (RTH guard working),
// real_entry_failed / broker_has_position / notional_cap_clamp / size_zero_skip.
const BUCKET_LABELS = {
  cluster_cap_breach:    {label:'Cluster cap',    color:'#ffaa00'},
  fx_below_idealpro_min: {label:'FX < $25K min',  color:'#ffaa00'},
  fleet_halted:          {label:'Fleet halted',   color:'#ff4444'},
  market_closed:         {label:'Market closed',  color:'#7b8ab8'},
  real_entry_failed:     {label:'Entry failed',   color:'#ff4444'},
  broker_has_position:   {label:'Broker has pos', color:'#7b8ab8'},
  notional_cap_clamp:    {label:'Notional clamp', color:'#9da8c7'},
  size_zero_skip:        {label:'Size=0 skip',    color:'#7b8ab8'},
};
function loadBlockedEntries() {
  fetch('/api/blocked_entries_today').then(r=>r.json()).then(data=>{
    const el = document.getElementById('blocked-entries-bar');
    if (!el) return;
    const totals = data.totals || {};
    const total = Object.values(totals).reduce((a,b)=>a+b, 0);
    if (total === 0) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    const blocks = [];
    for (const [bucket, meta] of Object.entries(BUCKET_LABELS)) {
      const n = totals[bucket] || 0;
      if (n === 0) continue;
      blocks.push('<span style="white-space:nowrap;color:'+meta.color+';">'+meta.label+': <b>'+n+'</b></span>');
    }
    el.innerHTML = '<div style="background:#0d1321;border:1px solid #1e2a42;border-radius:6px;padding:6px 14px;font-size:0.74em;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">'
      + '<div style="display:flex;gap:14px;flex-wrap:wrap;">'
      + '<span style="color:#7b8ab8;letter-spacing:1px;">GUARDS (24h)</span>'
      + blocks.join('<span style="color:#1e2a42;">|</span>')
      + '</div>'
      + '<span style="color:#7b8ab8;font-size:0.92em;" title="Click for per-strategy breakdown">total ' + total + '</span>'
      + '</div>';
  }).catch(()=>{});
}
loadBlockedEntries();
setInterval(loadBlockedEntries, 60000);  // refresh every 60s

// Apply Decision-Engine recommendation to allocation_factors via POST.
// One-click closes the loop on the Layer 3 -> Layer 4 path.
function applyAllocFactor(strategy, factor) {
  const verbose = factor === 0 ? 'KILL (set 0x)' : (factor > 1 ? 'SCALE UP (set ' + factor + 'x)' : (factor < 1 ? 'REDUCE (set ' + factor + 'x)' : 'set ' + factor + 'x'));
  if (!confirm('Apply ' + verbose + ' for ' + strategy + '? Effective on next eval cycle (' + (strategy.includes('argus') ? 'minutes' : '5-15 min depending on strategy') + '). Existing positions unaffected.')) return;
  fetch('/api/allocation_factors', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({strategy: strategy, factor: factor}),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      // Refresh the panel
      loadDecisionEngine();
    } else {
      alert('Failed: ' + (d.error || 'unknown'));
    }
  }).catch(e => alert('Network error: ' + e));
}

// ─── DECISION ENGINE PANEL ─────────────────────────────────────────
// Layer 3 of the metrics->scoring->decision->allocation stack.
// Primary "what do I do?" view. Sorted by action priority (SCALE_UP first).
//
// Tiny inline SVG sparkline: 2 vertical bars side-by-side. Left = prior 30
// trades' expectancy, right = recent 30. Bar height proportional to absolute
// expectancy. Fill color: positive=green, negative=red. Provides at-a-glance
// "is the edge growing or shrinking?" without needing to read delta_pct.
function renderExpectancySpark(priorExp, recentExp) {
  if ((priorExp == null || priorExp === 0) && (recentExp == null || recentExp === 0)) {
    return '<span style="color:#555;font-size:0.85em;">—</span>';
  }
  const p = priorExp || 0;
  const r = recentExp || 0;
  const maxAbs = Math.max(Math.abs(p), Math.abs(r), 1);  // floor at 1 to avoid div-by-zero
  const w = 8, gap = 3, maxH = 16;
  const pH = Math.abs(p) / maxAbs * maxH;
  const rH = Math.abs(r) / maxAbs * maxH;
  const pColor = p > 0 ? '#00ff88' : (p < 0 ? '#ff4444' : '#555');
  const rColor = r > 0 ? '#00ff88' : (r < 0 ? '#ff4444' : '#555');
  // Bars hang from a midline (positive = up, negative = down)
  const mid = maxH;  // total svg height = 2*maxH
  const pY = p >= 0 ? (mid - pH) : mid;
  const rY = r >= 0 ? (mid - rH) : mid;
  const svgH = 2 * maxH + 2;
  const svgW = w + gap + w;
  const tooltip = 'prior 30: $' + p.toFixed(2) + ' / recent 30: $' + r.toFixed(2);
  return '<svg width="' + svgW + '" height="' + svgH + '" style="vertical-align:middle;" title="' + tooltip + '">'
    + '<line x1="0" y1="' + mid + '" x2="' + svgW + '" y2="' + mid + '" stroke="#1e2a42" stroke-width="1"/>'
    + '<rect x="0" y="' + pY + '" width="' + w + '" height="' + Math.max(1, pH) + '" fill="' + pColor + '" opacity="0.5"/>'
    + '<rect x="' + (w + gap) + '" y="' + rY + '" width="' + w + '" height="' + Math.max(1, rH) + '" fill="' + rColor + '"/>'
    + '</svg>';
}

const ACTION_COLORS = {
  SCALE_UP:   {bg:'#0d1c11', border:'#00ff88', fg:'#00ff88'},
  HOLD:       {bg:'#0d1321', border:'#1e2a42', fg:'#9da8c7'},
  REDUCE:     {bg:'#2a2010', border:'#ffaa00', fg:'#ffaa00'},
  QUARANTINE: {bg:'#1f0a2a', border:'#a855f7', fg:'#c084fc'},
  KILL:       {bg:'#3a0a0a', border:'#ff4444', fg:'#ff4444'},
  OBSERVE:    {bg:'#0d1321', border:'#1e2a42', fg:'#7b8ab8'},
};

// ─── RECOMMENDED ACTIONS ──────────────────────────────────────────
// Cross-panel synthesis: pulls findings from decision_engine + drilldown
// + benchmark_alpha + trade_validity into a single priority-ranked do-list.
// Hides itself when there are no actions (clean fleet = clean dashboard).
const ACTION_VISUAL = {
  KILL:           {color: '#ff4444', label: 'KILL'},
  KILL_CANDIDATE: {color: '#ff4444', label: 'KILL CANDIDATE'},
  QUARANTINE:     {color: '#a855f7', label: 'QUARANTINE'},
  SCOPE_DOWN:     {color: '#ffaa00', label: 'SCOPE DOWN'},
  PROMOTE_REVIEW: {color: '#00ff88', label: 'PROMOTE REVIEW'},
  ALPHA_NEGATIVE: {color: '#ffc107', label: 'ALPHA NEGATIVE'},
  REVIEW:         {color: '#9da8c7', label: 'REVIEW'},
};
function loadRecommendedActions() {
  fetch('/api/recommended_actions?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('recommended-actions-panel');
    if (!el) return;
    const actions = data.actions || [];
    if (actions.length === 0) {
      el.innerHTML = '<div style="background:#0d1c11;border:1px solid #00ff88;border-radius:6px;padding:8px 14px;font-size:0.78em;color:#00ff88;">'
        + '<span style="font-weight:bold;letter-spacing:2px;">RECOMMENDED ACTIONS</span> · <span style="color:#9da8c7;">no actions queued — fleet is in steady state</span>'
        + '</div>';
      return;
    }
    const sum = data.summary || {};
    let html = '<div style="background:#141b2d;border:1px solid #00d4ff;border-radius:6px;padding:12px 16px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:1.0em;letter-spacing:2px;">RECOMMENDED ACTIONS</span>'
      + ' <span style="color:#7b8ab8;font-size:0.78em;margin-left:8px;">cross-panel synthesis · ' + actions.length + ' action' + (actions.length===1?'':'s') + ' queued</span></div>'
      + '<div style="font-size:0.78em;color:#7b8ab8;">'
      + (sum.kill_candidates ? '<span style="color:#ff4444;">' + sum.kill_candidates + ' kill</span> · ' : '')
      + (sum.scope_down      ? '<span style="color:#ffaa00;">' + sum.scope_down + ' scope-down</span> · ' : '')
      + (sum.quarantine      ? '<span style="color:#c084fc;">' + sum.quarantine + ' quarantine</span> · ' : '')
      + (sum.promote_review  ? '<span style="color:#00ff88;">' + sum.promote_review + ' promote</span> · ' : '')
      + (sum.alpha_negative  ? '<span style="color:#ffc107;">' + sum.alpha_negative + ' illusory PnL</span> · ' : '')
      + (sum.review          ? '<span style="color:#9da8c7;">' + sum.review + ' review</span>' : '')
      + '</div></div>';
    // Action list — priority groups visually separated
    let lastPriority = null;
    for (const a of actions) {
      const v = ACTION_VISUAL[a.action] || {color: '#9da8c7', label: a.action};
      if (lastPriority !== null && a.priority !== lastPriority) {
        html += '<div style="border-top:1px dashed #1e2a42;margin:6px 0;"></div>';
      }
      lastPriority = a.priority;
      const priorityBadge = a.priority === 1 ? 'P1' : a.priority === 2 ? 'P2' : 'P3';
      const priorityColor = a.priority === 1 ? '#ff4444' : a.priority === 2 ? '#ffaa00' : '#9da8c7';
      html += '<div style="display:flex;gap:10px;padding:6px 0;align-items:flex-start;">'
        + '<span style="background:transparent;color:' + priorityColor + ';border:1px solid ' + priorityColor + ';padding:2px 6px;border-radius:3px;font-weight:bold;font-size:0.7em;letter-spacing:1px;flex-shrink:0;align-self:flex-start;">' + priorityBadge + '</span>'
        + '<span style="background:' + v.color + ';color:#000;padding:2px 8px;border-radius:3px;font-weight:bold;font-size:0.78em;letter-spacing:1px;flex-shrink:0;align-self:flex-start;">' + v.label + '</span>'
        + '<div style="flex:1;font-size:0.85em;">'
        + '<div style="color:#e0e0e0;font-weight:bold;margin-bottom:2px;">' + a.strategy + '</div>'
        + '<div style="color:#9da8c7;font-size:0.92em;">' + a.reason + '</div>'
        + '<div style="color:#7b8ab8;font-size:0.85em;margin-top:3px;">'
        + (a.data_citations || []).map(c => '↳ ' + c).join(' &nbsp; · &nbsp; ')
        + '</div>'
        + '</div>'
        + '</div>';
    }
    html += '<div style="margin-top:10px;font-size:0.7em;color:#7b8ab8;border-top:1px solid #1e2a42;padding-top:6px;">'
      + 'Synthesizes decision_engine + strategy_drilldown + benchmark_alpha + trade_validity. '
      + 'KILL_CANDIDATE fires only when all three independent signals agree.'
      + '</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('recommended actions error:', e);});
}
loadRecommendedActions();
setInterval(loadRecommendedActions, 60000);

// ─── 24H CHANGES PANEL ─────────────────────────────────────────────
// Diffs the two most-recent operational_maturity snapshots and shows
// which strategies actually moved in the last 24h. Hides itself when
// nothing material changed (clean fleet = clean dashboard).
function loadChanges24h() {
  fetch('/api/changes_24h').then(r=>r.json()).then(data=>{
    const el = document.getElementById('changes-24h-panel');
    if (!el) return;
    if (data.status !== 'ok' || !data.changes || data.changes.length === 0) {
      el.innerHTML = '';
      return;
    }
    // Header with snapshot dates
    const todayDate = (data.today_snapshot || '').replace('operational_maturity_','').replace('.json','');
    const yestDate = (data.yesterday_snapshot || '').replace('operational_maturity_','').replace('.json','');
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">24H CHANGES</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">what moved between '
      + yestDate + ' → ' + todayDate + ' snapshots</span></div>'
      + '<div style="font-size:0.78em;color:#7b8ab8;">' + data.n_changes + ' material change' + (data.n_changes === 1 ? '' : 's')
      + (data.n_verdict_transitions > 0 ? ' · <b style="color:#ffaa00;">' + data.n_verdict_transitions + ' verdict transition' + (data.n_verdict_transitions === 1 ? '' : 's') + '</b>' : '')
      + '</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.78em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N (Δ)</th>'
      + '<th style="text-align:right;padding:5px 6px;">PF yest → today (Δ)</th>'
      + '<th style="text-align:right;padding:5px 6px;">PnL yest → today (Δ)</th>'
      + '<th style="text-align:left;padding:5px 6px;">Verdict</th>'
      + '</tr></thead><tbody>';
    for (const c of data.changes) {
      const pnlDelta = c.pnl_delta || 0;
      const pnlDeltaColor = pnlDelta > 0 ? '#00ff88' : pnlDelta < 0 ? '#ff4444' : '#9da8c7';
      const pfDelta = c.pf_delta;
      const pfDeltaColor = pfDelta == null ? '#7b8ab8' : pfDelta > 0 ? '#00ff88' : pfDelta < 0 ? '#ff4444' : '#9da8c7';
      const nDeltaColor = c.n_delta > 0 ? '#9da8c7' : c.n_delta < 0 ? '#ffaa00' : '#7b8ab8';
      const pfYest = c.pf_yest != null ? c.pf_yest.toFixed(2) : '—';
      const pfToday = c.pf_today != null ? c.pf_today.toFixed(2) : '—';
      const pfDeltaStr = pfDelta != null ? (pfDelta >= 0 ? '+' : '') + pfDelta.toFixed(2) : '—';
      const verdictCell = c.verdict_changed
        ? '<span style="color:#ffaa00;font-weight:bold;">' + (c.verdict_yest || '?') + ' → ' + (c.verdict_today || '?') + '</span>'
        : '<span style="color:#7b8ab8;">' + (c.verdict_today || '—') + '</span>';
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + c.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + c.n_yest + ' → ' + c.n_today + ' <span style="color:' + nDeltaColor + ';font-weight:bold;">(' + (c.n_delta >= 0 ? '+' : '') + c.n_delta + ')</span></td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + pfYest + ' → ' + pfToday + ' <span style="color:' + pfDeltaColor + ';font-weight:bold;">(' + pfDeltaStr + ')</span></td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">$' + (c.pnl_yest >= 0 ? '+' : '') + c.pnl_yest.toFixed(2) + ' → $' + (c.pnl_today >= 0 ? '+' : '') + c.pnl_today.toFixed(2) + ' <span style="color:' + pnlDeltaColor + ';font-weight:bold;">($' + (pnlDelta >= 0 ? '+' : '') + pnlDelta.toFixed(2) + ')</span></td>'
        + '<td style="padding:5px 6px;">' + verdictCell + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="margin-top:6px;font-size:0.7em;color:#7b8ab8;">'
      + 'Filter: shown only when |PnL Δ| ≥ $5, |PF Δ| ≥ 0.10, n changed, or verdict transitioned. Sorted by |PnL Δ| desc.'
      + '</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{
    const el = document.getElementById('changes-24h-panel');
    if (el) el.innerHTML = '';
  });
}
loadChanges24h();
setInterval(loadChanges24h, 600000);  // 10 min — daily snapshot only changes overnight

// ─── ACTIVE BLEEDERS PANEL ─────────────────────────────────────────
// Cumulative-bleed leaderboard over last 7d. Different angle from 24h
// changes (delta-focused) and recommended_actions (verdict-focused) —
// this is "who is killing my capital RIGHT NOW." Hides when no bleeders.
function loadActiveBleeders() {
  fetch('/api/active_bleeders?window_days=7&top_n=3').then(r=>r.json()).then(data=>{
    const el = document.getElementById('active-bleeders-panel');
    if (!el) return;
    const bleeders = data.bleeders || [];
    if (bleeders.length === 0) {
      el.innerHTML = '<div style="background:#0d1c11;border:1px solid #143021;border-radius:6px;padding:6px 14px;font-size:0.78em;color:#00ff88;">'
        + '<span style="letter-spacing:1px;font-weight:bold;">ACTIVE BLEEDERS (7d)</span> · <span style="color:#9da8c7;">none — no strategy net-negative on n>=3 fills</span></div>';
      return;
    }
    const totalBleed = data.total_bleed_usd || 0;
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">ACTIVE BLEEDERS</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">top ' + (data.top_n || 3) + ' by cumulative bleed · last ' + (data.window_days || 7) + 'd</span></div>'
      + '<div style="font-size:0.78em;color:#ff4444;font-weight:bold;">total bleed: $' + totalBleed.toFixed(2) + '</div>'
      + '</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.78em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">Win rate</th>'
      + '<th style="text-align:right;padding:5px 6px;">Avg / trade</th>'
      + '<th style="text-align:right;padding:5px 6px;">Worst single</th>'
      + '<th style="text-align:right;padding:5px 6px;">Cumulative</th>'
      + '</tr></thead><tbody>';
    for (let i = 0; i < bleeders.length; i++) {
      const b = bleeders[i];
      const rankBadge = i === 0
        ? '<span style="background:#3a0a0a;color:#ff4444;padding:1px 6px;border-radius:3px;font-weight:bold;letter-spacing:1px;font-size:0.78em;margin-right:6px;">#1</span>'
        : '<span style="color:#7b8ab8;margin-right:6px;font-size:0.85em;">#' + (i+1) + '</span>';
      const wrColor = b.win_rate_pct < 35 ? '#ff4444' : b.win_rate_pct < 50 ? '#ffaa00' : '#9da8c7';
      const worstColor = b.worst_trade_usd < -50 ? '#ff4444' : '#ffaa00';
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + rankBadge + b.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + b.n_total + ' (' + b.n_wins + 'W/' + b.n_losses + 'L)</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + wrColor + ';">' + b.win_rate_pct.toFixed(1) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ff4444;">$' + b.avg_pnl_per_trade.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + worstColor + ';">$' + b.worst_trade_usd.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ff4444;font-weight:bold;">$' + b.pnl_total_usd.toFixed(2) + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="margin-top:6px;font-size:0.7em;color:#7b8ab8;">'
      + 'Net-negative + n>=3 over ' + (data.window_days || 7) + 'd. Different from 24h-changes (deltas) and recommended_actions (verdicts).'
      + '</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{
    const el = document.getElementById('active-bleeders-panel');
    if (el) el.innerHTML = '';
  });
}
loadActiveBleeders();
setInterval(loadActiveBleeders, 300000);  // 5 min refresh

// ─── TOM TRADE OUTCOME PANEL ───────────────────────────────────────
// Surfaces the 6-ETF basket P&L when tom_international holds an open
// position. Hides itself when FLAT. Tactical view — different from the
// generic Open Positions table (which doesn't aggregate by strategy).
function loadTomOutcome() {
  fetch('/api/tom_outcome').then(r=>r.json()).then(d=>{
    const el = document.getElementById('tom-outcome-panel');
    if (!el) return;
    if (d.status !== 'open') {
      el.innerHTML = '';  // hide when flat or missing
      return;
    }
    const totalUnr = d.total_unrealized_usd || 0;
    const totalUnrPct = d.total_unrealized_pct || 0;
    const totColor = totalUnr > 0 ? '#00ff88' : totalUnr < 0 ? '#ff4444' : '#9da8c7';
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">TOM BASKET</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">forge_tom_international turn-of-month event</span></div>'
      + '<div style="font-size:0.78em;color:#7b8ab8;">'
      + d.instruments_count + ' positions · entry ' + (d.entry_ts || '?').substring(0, 16).replace('T', ' ')
      + ' · next entry ' + (d.next_entry_day || '?')
      + '</div>'
      + '</div>'
      + '<div style="display:flex;gap:20px;align-items:baseline;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #1e2a42;">'
      + '<div><span style="color:#9da8c7;">Basis: </span><b style="color:#fff;">$' + (d.total_basis_usd || 0).toLocaleString(undefined,{maximumFractionDigits:2}) + '</b></div>'
      + '<div><span style="color:#9da8c7;">Mark: </span><b style="color:#fff;">$' + ((d.total_basis_usd || 0) + totalUnr).toLocaleString(undefined,{maximumFractionDigits:2}) + '</b></div>'
      + '<div><span style="color:#9da8c7;">Unrealized: </span><b style="color:' + totColor + ';">' + (totalUnr >= 0 ? '+' : '') + '$' + totalUnr.toFixed(2) + '</b> '
      + '<span style="color:' + totColor + ';">(' + (totalUnrPct >= 0 ? '+' : '') + totalUnrPct.toFixed(2) + '%)</span></div>'
      + '</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.78em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Instrument</th>'
      + '<th style="text-align:right;padding:5px 6px;">Qty</th>'
      + '<th style="text-align:right;padding:5px 6px;">Avg cost</th>'
      + '<th style="text-align:right;padding:5px 6px;">Last</th>'
      + '<th style="text-align:right;padding:5px 6px;">Basis</th>'
      + '<th style="text-align:right;padding:5px 6px;">Mark</th>'
      + '<th style="text-align:right;padding:5px 6px;">Unrealized</th>'
      + '<th style="text-align:right;padding:5px 6px;">% from entry</th>'
      + '</tr></thead><tbody>';
    for (const r of (d.positions || [])) {
      const unr = r.unrealized_usd;
      const unrPct = r.unrealized_pct;
      const c = unr == null ? '#7b8ab8' : unr > 0 ? '#00ff88' : unr < 0 ? '#ff4444' : '#9da8c7';
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;font-weight:bold;">' + r.instrument + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + r.qty + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">$' + r.avg_cost.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + (r.last_price != null ? '$' + r.last_price.toFixed(2) : '—') + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">$' + r.basis_usd.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + (r.mark_usd != null ? '$' + r.mark_usd.toFixed(2) : '—') + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + c + ';font-weight:bold;">' + (unr != null ? (unr >= 0 ? '+' : '') + '$' + unr.toFixed(2) : '—') + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + c + ';">' + (unrPct != null ? (unrPct >= 0 ? '+' : '') + unrPct.toFixed(2) + '%' : '—') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="margin-top:6px;font-size:0.7em;color:#7b8ab8;">Prices: yfinance daily-bar tail. Position basis: TWS broker_snapshot.json.</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{
    const el = document.getElementById('tom-outcome-panel');
    if (el) el.innerHTML = '';
  });
}
loadTomOutcome();
setInterval(loadTomOutcome, 300000);  // 5 min refresh

// ─── DECISION HISTORY PANEL ────────────────────────────────────────
// Governance audit trail — every allocation factor change with timestamp +
// source + reason. Renders the 10 most-recent. Hides itself when no
// history exists (clean panel for clean fleet). Per project_capital_allocator_policy
// "Manual approval until 90 days real evidence" — having this surfaced
// on the dashboard makes the governance discipline visible.
function loadDecisionHistory() {
  fetch('/api/decision_history?limit=10').then(r=>r.json()).then(data=>{
    const el = document.getElementById('decision-history-panel');
    if (!el) return;
    const records = data.records || [];
    if (records.length === 0) {
      el.innerHTML = '';  // hide empty
      return;
    }
    const currentFactors = data.current_factors || {};
    const factorRows = Object.entries(currentFactors).map(([s, f]) =>
      '<span style="color:#9da8c7;">' + s + ': <b style="color:' + (f === 0 ? '#ff4444' : f < 1 ? '#ffaa00' : f > 1 ? '#00ff88' : '#fff') + ';">' + f + 'x</b></span>'
    ).join(' · ');
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">DECISION HISTORY</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">capital governance audit trail · ' + records.length + ' recent</span></div>'
      + '<div style="font-size:0.78em;color:#7b8ab8;">Current factors: ' + (factorRows || '<i>none set</i>') + '</div>'
      + '</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.78em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;width:130px;">When (UTC)</th>'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">Before → After</th>'
      + '<th style="text-align:left;padding:5px 6px;width:90px;">Source</th>'
      + '<th style="text-align:left;padding:5px 6px;">Reason</th>'
      + '</tr></thead><tbody>';
    for (const r of records) {
      const when = (r.ts || '').substring(0, 19).replace('T', ' ');
      const before = r.before == null ? '<i>unset</i>' : r.before + 'x';
      const after = r.after == null ? '?' : r.after + 'x';
      // Color the after-value by direction: down = orange/red, up = green
      let arrowColor = '#9da8c7';
      if (r.before != null && r.after != null) {
        if (r.after < r.before) arrowColor = r.after === 0 ? '#ff4444' : '#ffaa00';
        else if (r.after > r.before) arrowColor = '#00ff88';
      }
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#7b8ab8;">' + when + '</td>'
        + '<td style="padding:5px 6px;color:#e0e0e0;font-weight:bold;">' + (r.strategy || '?') + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + before + ' <span style="color:' + arrowColor + ';">→</span> <b style="color:' + arrowColor + ';">' + after + '</b></td>'
        + '<td style="padding:5px 6px;color:#9da8c7;font-size:0.92em;">' + (r.source || '?') + '</td>'
        + '<td style="padding:5px 6px;color:#9da8c7;font-size:0.92em;">' + (r.reason || '').substring(0, 90) + ((r.reason || '').length > 90 ? '…' : '') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="margin-top:6px;font-size:0.7em;color:#7b8ab8;">'
      + 'Source: argus_flow/logs/decision_history.jsonl. Append-only audit trail; every POST /api/allocation_factors logs here automatically.'
      + '</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(()=>{
    const el = document.getElementById('decision-history-panel');
    if (el) el.innerHTML = '';
  });
}
loadDecisionHistory();
setInterval(loadDecisionHistory, 60000);  // 1 min refresh — captures new POSTs quickly

// ─── Decision-engine subset drilldown ──────────────────────────────
// Lazy-loads /api/strategy_drilldown when user expands a row. Surfaces
// salvageable subsets BEFORE a kill — answers the ceremony spec's
// requirement: "you need to know if there's a subset worth keeping."
function toggleDecisionDrilldown(strategy, safeId) {
  const row = document.getElementById('dec-drill-' + safeId);
  const body = document.getElementById('dec-drill-body-' + safeId);
  if (!row || !body) return;
  if (row.style.display === 'none') {
    row.style.display = 'table-row';
    if (!row.dataset.loaded) {
      fetch('/api/strategy_drilldown?strategy=' + encodeURIComponent(strategy) + '&window_days=60').then(r=>r.json()).then(d=>{
        if (d.status !== 'ok') {
          body.innerHTML = '<span style="color:#ff4444;">' + (d.message || d.status) + '</span>';
          return;
        }
        const ov = d.overall || {};
        const sd = (d.groups && d.groups.by_symbol_direction) || [];
        const sv = d.salvageable_subsets || [];
        const kc = d.kill_confirmed_subsets || [];
        // Verdict banner
        const verdictColor = sv.length > 0 ? '#ffaa00' : '#ff4444';
        let html = '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
          + '<div><span style="color:#00d4ff;font-weight:bold;letter-spacing:1px;">SUBSET ANALYSIS — ' + strategy + '</span>'
          + ' <span style="color:#7b8ab8;font-size:0.92em;margin-left:8px;">' + d.n_in_window + ' trades · ' + d.window_days + 'd window</span></div>'
          + '<div style="color:' + verdictColor + ';font-weight:bold;font-size:0.92em;">' + d.verdict_hint + '</div>'
          + '</div>';
        // by_symbol_direction table — most actionable group
        html += '<table style="width:100%;border-collapse:collapse;font-size:0.92em;margin-bottom:8px;">'
          + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
          + '<th style="text-align:left;padding:4px 8px;">Subset (symbol × direction)</th>'
          + '<th style="text-align:right;padding:4px 8px;">N</th>'
          + '<th style="text-align:right;padding:4px 8px;">WR</th>'
          + '<th style="text-align:right;padding:4px 8px;">PF</th>'
          + '<th style="text-align:right;padding:4px 8px;">Expectancy</th>'
          + '<th style="text-align:right;padding:4px 8px;">PnL</th>'
          + '<th style="text-align:center;padding:4px 8px;">Verdict</th>'
          + '</tr></thead><tbody>';
        for (const g of sd) {
          const isSalvage = g.n >= 10 && g.pf != null && g.pf >= 1.5;
          const isKill = g.n >= 5 && g.pf != null && g.pf < 0.5;
          const pfColor = g.pf == null ? '#7b8ab8' : g.pf >= 1.5 ? '#00ff88' : g.pf >= 1.0 ? '#9da8c7' : g.pf >= 0.5 ? '#ffaa00' : '#ff4444';
          const pnlColor = g.pnl_usd > 0 ? '#00ff88' : g.pnl_usd < 0 ? '#ff4444' : '#7b8ab8';
          let verdict = '';
          if (isSalvage) verdict = '<span style="background:#0d1c11;border:1px solid #00ff88;color:#00ff88;padding:1px 6px;border-radius:3px;font-size:0.85em;font-weight:bold;">SALVAGEABLE</span>';
          else if (isKill) verdict = '<span style="background:#3a0a0a;border:1px solid #ff4444;color:#ff4444;padding:1px 6px;border-radius:3px;font-size:0.85em;font-weight:bold;">KILL THIS</span>';
          else verdict = '<span style="color:#7b8ab8;font-size:0.85em;">—</span>';
          html += '<tr style="border-top:1px solid #1e2a42;">'
            + '<td style="padding:4px 8px;color:#e0e0e0;">' + g.key + '</td>'
            + '<td style="padding:4px 8px;text-align:right;color:#9da8c7;">' + g.n + '</td>'
            + '<td style="padding:4px 8px;text-align:right;color:#9da8c7;">' + g.wr_pct.toFixed(1) + '%</td>'
            + '<td style="padding:4px 8px;text-align:right;color:' + pfColor + ';font-weight:bold;">' + (g.pf == null ? '—' : g.pf.toFixed(2)) + '</td>'
            + '<td style="padding:4px 8px;text-align:right;color:' + pnlColor + ';">' + (g.expectancy_usd >= 0 ? '+' : '') + '$' + g.expectancy_usd.toFixed(2) + '</td>'
            + '<td style="padding:4px 8px;text-align:right;color:' + pnlColor + ';font-weight:bold;">' + (g.pnl_usd >= 0 ? '+' : '') + '$' + g.pnl_usd.toFixed(2) + '</td>'
            + '<td style="padding:4px 8px;text-align:center;">' + verdict + '</td>'
            + '</tr>';
        }
        html += '</tbody></table>';
        // Action recommendation
        if (sv.length > 0) {
          html += '<div style="background:#0d1c11;border-left:3px solid #00ff88;padding:6px 10px;font-size:0.92em;color:#9da8c7;">'
            + '<b style="color:#00ff88;">SCOPE_DOWN candidate:</b> ' + sv.length + ' subset(s) have PF≥1.5. '
            + 'Filter strategy to those subsets instead of KILL — preserves the working edge while removing the bleed.'
            + '</div>';
        } else if (kc.length > 0) {
          html += '<div style="background:#3a0a0a;border-left:3px solid #ff4444;padding:6px 10px;font-size:0.92em;color:#9da8c7;">'
            + '<b style="color:#ff4444;">KILL is data-supported:</b> no subsets at PF≥1.5; ' + kc.length + ' subsets are confirmed-bleeders. '
            + 'No salvageable structure inside this strategy — proceed with KILL.'
            + '</div>';
        }
        // Per-axis stats (collapsible) — secondary detail
        html += '<details style="margin-top:8px;color:#7b8ab8;font-size:0.85em;"><summary style="cursor:pointer;color:#9da8c7;">▸ Per-axis breakdown (symbol / direction / exit_reason / hour)</summary>';
        const axes = [['by_symbol','Symbol'],['by_direction','Direction'],['by_exit_reason','Exit Reason'],['by_hour','Entry Hour']];
        html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-top:6px;">';
        for (const [ax, label] of axes) {
          const grps = (d.groups && d.groups[ax]) || [];
          html += '<div><div style="color:#00d4ff;font-size:0.92em;letter-spacing:1px;margin-bottom:3px;">' + label + '</div>';
          for (const g of grps) {
            const pfColor = g.pf == null ? '#7b8ab8' : g.pf >= 1.5 ? '#00ff88' : g.pf >= 1.0 ? '#9da8c7' : g.pf >= 0.5 ? '#ffaa00' : '#ff4444';
            html += '<div style="font-size:0.92em;color:#9da8c7;">' + g.key + ': n=' + g.n + ' · PF <span style="color:' + pfColor + ';font-weight:bold;">' + (g.pf == null ? '—' : g.pf.toFixed(2)) + '</span> · ' + (g.pnl_usd >= 0 ? '+' : '') + '$' + g.pnl_usd.toFixed(2) + '</div>';
          }
          html += '</div>';
        }
        html += '</div></details>';
        body.innerHTML = html;
        row.dataset.loaded = '1';
      }).catch(e => {
        body.innerHTML = '<span style="color:#ff4444;">Error: ' + e + '</span>';
      });
    }
  } else {
    row.style.display = 'none';
  }
}
function loadDecisionEngine() {
  Promise.all([
    fetch('/api/strategy_actions?window_days=30').then(r=>r.json()),
    fetch('/api/allocation_factors').then(r=>r.json()),
  ]).then(([data, allocData])=>{
    const el = document.getElementById('decision-engine-panel');
    if (!el) return;
    const summary = data.summary || {};
    const factors = (allocData && allocData.factors) || {};
    const total = (summary.scale_up||0) + (summary.hold||0) + (summary.reduce||0) + (summary.kill||0) + (summary.observe||0);
    let html = '<div style="background:#141b2d;border:1px solid #00d4ff;border-radius:6px;padding:12px 16px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:1.0em;letter-spacing:2px;">DECISION ENGINE</span>'
      + ' <span style="color:#7b8ab8;font-size:0.78em;margin-left:8px;" title="Sample windows use post-reset cutoff from operational_maturity (currently 2026-04-23 14:00 UTC)">rules ' + (data.rules_version||'') + ' · 30d window · post-reset</span></div>'
      + '<div style="font-size:0.78em;color:#7b8ab8;">'
      + '<span style="color:' + ACTION_COLORS.SCALE_UP.fg + ';">SCALE_UP ' + (summary.scale_up||0) + '</span> · '
      + '<span style="color:' + ACTION_COLORS.HOLD.fg + ';">HOLD ' + (summary.hold||0) + '</span> · '
      + '<span style="color:' + ACTION_COLORS.REDUCE.fg + ';">REDUCE ' + (summary.reduce||0) + '</span> · '
      + (summary.rework ? '<span style="color:#ffaa00;">↻ REWORK ' + summary.rework + '</span> · ' : '')
      + '<span style="color:' + ACTION_COLORS.QUARANTINE.fg + ';">QUARANTINE ' + (summary.quarantine||0) + '</span> · '
      + '<span style="color:' + ACTION_COLORS.KILL.fg + ';">KILL ' + (summary.kill||0) + '</span> · '
      + '<span style="color:' + ACTION_COLORS.OBSERVE.fg + ';">OBSERVE ' + (summary.observe||0) + '</span>'
      + '</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.78em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:center;padding:6px 4px;width:36px;" title="DO NOW: top 3 unapplied REDUCE/KILL/SCALE_UP recommendations, sorted by urgency"></th>'
      + '<th style="text-align:left;padding:6px 8px;">Strategy</th>'
      + '<th style="text-align:center;padding:6px 8px;">Action</th>'
      + '<th style="text-align:right;padding:6px 8px;">Conf</th>'
      + '<th style="text-align:right;padding:6px 8px;">N</th>'
      + '<th style="text-align:right;padding:6px 8px;">PnL 30d</th>'
      + '<th style="text-align:right;padding:6px 8px;">Eff</th>'
      + '<th style="text-align:left;padding:6px 8px;">Drift</th>'
      + '<th style="text-align:center;padding:6px 8px;">Trend</th>'
      + '<th style="text-align:left;padding:6px 8px;">Reason</th>'
      + '<th style="text-align:left;padding:6px 8px;">Suggested</th>'
      + '<th style="text-align:right;padding:6px 8px;">Active ×</th>'
      + '<th style="text-align:center;padding:6px 8px;">Apply</th>'
      + '</tr></thead><tbody>';
    // ─── Urgency re-sort + DO NOW badge ──────────────────────────────
    // Lean-version "Action Queue": don't add a second panel — re-rank the
    // existing rows so unapplied KILL/REDUCE/SCALE_UP float to the top, then
    // badge the top 3 with DO NOW. Bleeding strategies are surfaced first.
    const URGENCY_RANK = {QUARANTINE: 0, KILL: 1, REDUCE: 2, SCALE_UP: 3};
    const recMapForSort = {SCALE_UP:1.5, HOLD:1.0, REDUCE:0.5, QUARANTINE:0.0, KILL:0.0, OBSERVE:1.0};
    function currentFactor(strat) {
      const tries = [strat, strat.replace('forge_',''), 'forge_' + strat];
      for (const k of tries) { if (k in factors) return factors[k]; }
      return 1.0;
    }
    const decoratedRows = (data.strategies || []).map(s => {
      const cur = currentFactor(s.strategy);
      const rec = recMapForSort[s.action];
      const isActionable = (s.action in URGENCY_RANK) && rec != null && Math.abs(cur - rec) >= 0.01;
      return {s, isActionable, urgency: isActionable ? URGENCY_RANK[s.action] : 99};
    });
    decoratedRows.sort((a, b) => {
      // Actionable first, by urgency (KILL < REDUCE < SCALE_UP), then by larger PnL impact
      if (a.urgency !== b.urgency) return a.urgency - b.urgency;
      // Within actionable group, sort by absolute PnL (biggest bleeders first for KILL/REDUCE)
      if (a.urgency < 99) return Math.abs(b.s.pnl_usd) - Math.abs(a.s.pnl_usd);
      // Non-actionable: keep server's original order (SCALE_UP > HOLD > REDUCE > KILL > OBSERVE, by -pnl)
      return 0;
    });
    let doNowAssigned = 0;
    for (const dr of decoratedRows) {
      const s = dr.s;
      const isDoNow = dr.isActionable && doNowAssigned < 3;
      if (isDoNow) doNowAssigned++;
      const c = ACTION_COLORS[s.action] || ACTION_COLORS.HOLD;
      const pnlColor = s.pnl_usd > 0 ? '#00ff88' : (s.pnl_usd < 0 ? '#ff4444' : '#7b8ab8');
      const driftColor = s.drift_verdict === 'RISING' ? '#00ff88' : (s.drift_verdict === 'DECLINING' ? '#ff4444' : '#9da8c7');
      const driftText = s.drift_verdict + (s.drift_delta_pct ? ' ' + (s.drift_delta_pct >= 0 ? '+' : '') + s.drift_delta_pct.toFixed(0) + '%' : '');
      const doNowBadge = isDoNow
        ? '<span style="background:#ff4444;color:#000;padding:1px 5px;border-radius:3px;font-weight:bold;letter-spacing:1px;font-size:0.7em;" title="Top-3 unapplied REDUCE/KILL/SCALE_UP — apply now to stop bleed or capture edge">DO NOW</span>'
        : '';
      const rowBorder = isDoNow ? 'border-top:1px solid #ff4444;border-left:3px solid #ff4444;' : 'border-top:1px solid #1e2a42;';
      const reworkBadge = s.rework_required
        ? ' <span style="background:transparent;color:#ffaa00;border:1px solid #ffaa00;padding:1px 5px;border-radius:3px;font-size:0.7em;letter-spacing:1px;font-weight:bold;margin-left:4px;" title="' + (s.rework_reason || 'Rework required') + '">↻ REWORK</span>'
        : '';
      const safeStratId = s.strategy.replace(/[^a-z0-9]/gi, '_');
      // Drilldown link: only useful for strategies with enough trades to subset
      const drilldownLink = s.n_total >= 10
        ? '<span data-drilldown-strategy="' + s.strategy + '" data-drilldown-id="' + safeStratId + '" onclick="toggleDecisionDrilldown(this.dataset.drilldownStrategy, this.dataset.drilldownId)" style="cursor:pointer;color:#7b8ab8;font-size:0.78em;margin-left:6px;font-weight:normal;" title="Per-symbol/direction subset analysis — surfaces salvageable subsets before KILL">▾ subsets</span>'
        : '';
      html += '<tr style="' + rowBorder + 'background:' + c.bg + ';">'
        + '<td style="padding:6px 4px;text-align:center;">' + doNowBadge + '</td>'
        + '<td style="padding:6px 8px;font-weight:bold;color:#e0e0e0;">' + s.strategy + drilldownLink + '</td>'
        + '<td style="padding:6px 8px;text-align:center;"><span style="background:' + c.border + ';color:#000;padding:2px 8px;border-radius:3px;font-weight:bold;letter-spacing:1px;font-size:0.92em;">' + s.action + '</span>' + reworkBadge + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:#9da8c7;">' + s.confidence_pct + '%</td>'
        + '<td style="padding:6px 8px;text-align:right;color:#9da8c7;">' + s.n_total + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:' + pnlColor + ';font-weight:bold;">' + (s.pnl_usd >= 0 ? '+' : '') + '$' + s.pnl_usd.toFixed(2) + '</td>'
        + '<td style="padding:6px 8px;text-align:right;color:#9da8c7;">' + s.efficiency_score.toFixed(1) + '</td>'
        + '<td style="padding:6px 8px;color:' + driftColor + ';">' + driftText + '</td>'
        + '<td style="padding:6px 8px;text-align:center;">' + renderExpectancySpark(s.expectancy_prior_usd, s.expectancy_recent_usd) + '</td>'
        + '<td style="padding:6px 8px;color:#7b8ab8;font-size:0.92em;">' + s.reason + '</td>'
        + '<td style="padding:6px 8px;color:' + c.fg + ';font-size:0.92em;">' + s.allocation_hint + '</td>'
        + '<td style="padding:6px 8px;text-align:right;">' + (function(){
            const tries = [s.strategy, s.strategy.replace('forge_',''), 'forge_' + s.strategy];
            let f = 1.0;
            let found = false;
            for (const k of tries) { if (k in factors) { f = factors[k]; found = true; break; } }
            const factorColor = !found ? '#7b8ab8' : (f === 0 ? '#ff4444' : (f > 1.2 ? '#00ff88' : (f < 0.8 ? '#ffc107' : '#9da8c7')));
            const fontWeight = found ? 'bold' : 'normal';
            return '<span style="color:' + factorColor + ';font-weight:' + fontWeight + ';">' + f.toFixed(2) + (found ? '' : '<span style="color:#555;font-size:0.85em;"> (default)</span>') + '</span>';
          })()
        + '</td>'
        + '<td style="padding:6px 8px;text-align:center;">' + (function(){
            // Map ACTION -> recommended factor; show Apply button only if different from current
            const recMap = {SCALE_UP:1.5, HOLD:1.0, REDUCE:0.5, KILL:0.0, OBSERVE:1.0};
            const rec = recMap[s.action];
            if (rec == null) return '<span style="color:#555;">-</span>';
            const tries = [s.strategy, s.strategy.replace('forge_',''), 'forge_' + s.strategy];
            let cur = 1.0;
            for (const k of tries) { if (k in factors) { cur = factors[k]; break; } }
            if (Math.abs(cur - rec) < 0.01) return '<span style="color:#00e676;font-size:0.85em;" title="current matches recommendation">applied</span>';
            return '<button data-alloc-strategy="' + s.strategy + '" data-alloc-factor="' + rec + '" onclick="applyAllocFactor(this.dataset.allocStrategy, parseFloat(this.dataset.allocFactor))" style="background:#1e2a42;border:1px solid #00d4ff;color:#00d4ff;padding:3px 10px;border-radius:3px;cursor:pointer;font-size:0.85em;letter-spacing:1px;font-weight:bold;" title="Set ' + s.strategy + ' allocation to ' + rec + 'x">apply ' + rec + 'x</button>';
          })()
        + '</td>'
        + '</tr>';
      // Hidden drilldown row — populated lazily by toggleDecisionDrilldown
      if (s.n_total >= 10) {
        html += '<tr id="dec-drill-' + safeStratId + '" style="display:none;background:#0a1224;">'
          + '<td colspan="13" style="padding:0;">'
          + '<div id="dec-drill-body-' + safeStratId + '" style="padding:10px 18px;color:#9da8c7;font-size:0.82em;">Loading subset analysis…</div>'
          + '</td></tr>';
      }
    }
    html += '</tbody></table>'
      + '<div style="margin-top:8px;font-size:0.72em;color:#7b8ab8;">Rules v2: dirty&gt;5%→QUARANTINE · n&lt;10→OBSERVE · declining+losing+n≥50→KILL · declining+losing+n&lt;50→REDUCE+REWORK · flat+losing+n≥30→REDUCE+REWORK · flat+losing+n&lt;30→REDUCE · rising+eff&gt;5→SCALE_UP · flat+positive→HOLD</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('decision engine error:', e);});
}
loadDecisionEngine();
setInterval(loadDecisionEngine, 60000);

// ─── EFFICIENCY PANEL ──────────────────────────────────────────────
// Backing metric for the decision engine. Sorted by efficiency_score.
function loadEfficiency() {
  fetch('/api/strategy_efficiency?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('efficiency-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) {
      el.innerHTML = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;font-size:0.78em;color:#7b8ab8;">CAPITAL EFFICIENCY (30d): no closed trades in window</div>';
      return;
    }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">CAPITAL EFFICIENCY (30d)'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">PnL per (% time-in-market × avg notional). Higher = more bang per buck of deployed capital.</span></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">PnL</th>'
      + '<th style="text-align:right;padding:5px 6px;">WR</th>'
      + '<th style="text-align:right;padding:5px 6px;">PnL/trade</th>'
      + '<th style="text-align:right;padding:5px 6px;">Time-in-market</th>'
      + '<th style="text-align:right;padding:5px 6px;">PnL/min deployed</th>'
      + '<th style="text-align:right;padding:5px 6px;">Avg notional</th>'
      + '<th style="text-align:right;padding:5px 6px;">Eff score</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const pnlColor = s.total_pnl_usd > 0 ? '#00ff88' : (s.total_pnl_usd < 0 ? '#ff4444' : '#7b8ab8');
      const effColor = s.efficiency_score > 100 ? '#00ff88' : (s.efficiency_score > 0 ? '#ffc107' : (s.efficiency_score < 0 ? '#ff4444' : '#7b8ab8'));
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.trade_count + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + pnlColor + ';font-weight:bold;">' + (s.total_pnl_usd >= 0 ? '+' : '') + '$' + s.total_pnl_usd.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.win_rate_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + pnlColor + ';">' + (s.pnl_per_trade_usd >= 0 ? '+' : '') + '$' + s.pnl_per_trade_usd.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.time_in_market_pct.toFixed(2) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">$' + s.pnl_per_min_in_market_usd.toFixed(4) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">$' + s.avg_notional_usd.toLocaleString(undefined,{maximumFractionDigits:0}) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + effColor + ';font-weight:bold;">' + s.efficiency_score.toFixed(1) + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('efficiency error:', e);});
}
loadEfficiency();
setInterval(loadEfficiency, 60000);

// ─── OPPORTUNITY VS TAKEN ─────────────────────────────────────────
// Are filters helping or killing edge? Strategies that fire 1000 signals
// and take 5 may be too restrictive; strategies that take everything may
// need a filter.
function loadOpportunity() {
  fetch('/api/opportunity_vs_taken?window_days=7').then(r=>r.json()).then(data=>{
    const el = document.getElementById('opportunity-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">OPPORTUNITY vs TAKEN (7d)'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">low take-rate + high block count → filters may be too tight</span></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">Evaluated</th>'
      + '<th style="text-align:right;padding:5px 6px;">Taken</th>'
      + '<th style="text-align:right;padding:5px 6px;">Take rate</th>'
      + '<th style="text-align:left;padding:5px 6px;">Top block reasons</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const takeColor = s.take_rate_pct >= 5 ? '#00ff88' : (s.take_rate_pct >= 1 ? '#ffc107' : '#ff4444');
      const blocks = (s.top_block_reasons || []).map(b => b.reason + '×' + b.count).join(' · ') || '—';
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.signals_evaluated + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#fff;font-weight:bold;">' + s.signals_taken + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + takeColor + ';font-weight:bold;">' + s.take_rate_pct.toFixed(2) + '%</td>'
        + '<td style="padding:5px 6px;color:#7b8ab8;font-size:0.92em;">' + blocks + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('opportunity error:', e);});
}
loadOpportunity();
setInterval(loadOpportunity, 120000);  // signals.csv changes slowly; refresh every 2min

// ─── CAPITAL DEPLOYMENT TIMELINE ───────────────────────────────────
// Inline SVG sparkline. Distinguishes 'flat = no exposure' from 'flat = stable
// performance'. The original equity curve flat sections are mostly the former.
function loadCapitalDeployment() {
  fetch('/api/capital_deployment_timeline?window_days=7&bucket_hours=1').then(r=>r.json()).then(data=>{
    const el = document.getElementById('capital-deployment-panel');
    if (!el) return;
    const samples = data.samples || [];
    if (samples.length === 0) { el.innerHTML = ''; return; }
    const maxPct = Math.max(...samples.map(s=>s.deployed_pct_of_anchor), 5);  // floor at 5% so tiny exposures still show
    const peakPct = Math.max(...samples.map(s=>s.deployed_pct_of_anchor));
    const meanPct = samples.reduce((a,s)=>a+s.deployed_pct_of_anchor,0) / samples.length;
    const flatHours = samples.filter(s => s.deployed_pct_of_anchor < 1).length;
    const flatPct = (flatHours / samples.length * 100);
    // Build inline SVG bars
    const barWidth = Math.max(1, Math.floor(800 / samples.length));
    const svgWidth = barWidth * samples.length;
    const svgHeight = 80;
    let bars = '';
    samples.forEach((s, i) => {
      const h = (s.deployed_pct_of_anchor / maxPct) * svgHeight;
      const x = i * barWidth;
      const y = svgHeight - h;
      const color = s.deployed_pct_of_anchor < 1 ? '#1e2a42' : (s.deployed_pct_of_anchor > 50 ? '#ff4444' : (s.deployed_pct_of_anchor > 20 ? '#ffc107' : '#00d4ff'));
      bars += `<rect x="${x}" y="${y}" width="${barWidth}" height="${h}" fill="${color}"/>`;
    });
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">CAPITAL DEPLOYMENT (7d hourly)</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">distinguishes inactive capital from stable performance</span></div>'
      + '<div style="font-size:0.75em;color:#9da8c7;">'
      + 'peak <b style="color:#fff;">' + peakPct.toFixed(1) + '%</b> · mean <b>' + meanPct.toFixed(1) + '%</b> · flat <b>' + flatPct.toFixed(0) + '%</b> of hours'
      + '</div></div>'
      + '<svg width="' + svgWidth + '" height="' + svgHeight + '" style="display:block;background:#0a1224;border-radius:3px;">' + bars + '</svg>'
      + '<div style="font-size:0.7em;color:#7b8ab8;margin-top:4px;">' + samples[0].ts.slice(0,16) + ' (oldest, left) → ' + samples[samples.length-1].ts.slice(0,16) + ' (now, right). Each bar = 1h. Gray = flat (no exposure).</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('capital deployment error:', e);});
}
loadCapitalDeployment();
setInterval(loadCapitalDeployment, 300000);  // 5min refresh

// ─── TARGET CAPTURE RATIO ──────────────────────────────────────────
// Proxy for MFE/realized capture: ratio of how much of the planned move
// (entry → target) the strategy actually realized. Shows exit quality.
// Strategies with low capture but positive PnL are 'winning small' — leaving
// money on the table. Full MFE with bar lookback is a future build.
function loadTargetCapture() {
  fetch('/api/target_capture?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('target-capture-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;margin-bottom:6px;">TARGET CAPTURE (30d, MFE proxy)'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">capture = realized PnL / planned move (entry→target). 1.0 = full target hit, &lt; 0 = stopped out.</span></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">Mean cap.</th>'
      + '<th style="text-align:right;padding:5px 6px;">Median</th>'
      + '<th style="text-align:right;padding:5px 6px;">Full hit</th>'
      + '<th style="text-align:right;padding:5px 6px;">Partial</th>'
      + '<th style="text-align:right;padding:5px 6px;">Scratch</th>'
      + '<th style="text-align:right;padding:5px 6px;">Stopped</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const meanColor = s.mean_capture > 0.5 ? '#00ff88' : (s.mean_capture > 0 ? '#ffc107' : '#ff4444');
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.n + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + meanColor + ';font-weight:bold;">' + s.mean_capture.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.median_capture.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#00ff88;">' + s.full_capture_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ffc107;">' + s.partial_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#7b8ab8;">' + s.breakeven_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#ff4444;">' + s.adverse_pct.toFixed(0) + '%</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('target capture error:', e);});
}
loadTargetCapture();
setInterval(loadTargetCapture, 120000);

// ─── FLEET CONTRIBUTION ────────────────────────────────────────────
// (Renamed from "Alpha Attribution" 2026-04-28: this measures who is
// carrying fleet PnL, not excess return vs benchmark. True benchmark
// alpha lands post-5/31 as a separate panel.)
// Who's actually carrying the fleet PnL? Sorted by absolute share.
function loadAlphaAttribution() {
  fetch('/api/alpha_attribution?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('alpha-attribution-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    const conc = data.concentration || {};
    const headlineColor = conc.top_3_pct_of_abs > 80 ? '#ffaa00' : '#9da8c7';
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">FLEET CONTRIBUTION (30d)</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">who is carrying the fleet PnL? (not benchmark alpha — that lands post-5/31)</span></div>'
      + '<div style="font-size:0.75em;color:' + headlineColor + ';">'
      + 'Net fleet PnL: <b style="color:' + (data.total_net_pnl_usd >= 0 ? '#00ff88' : '#ff4444') + ';">' + (data.total_net_pnl_usd >= 0 ? '+' : '') + '$' + data.total_net_pnl_usd.toFixed(2) + '</b>'
      + ' · top 1 = ' + conc.top_1_pct_of_abs + '% · top 3 = ' + conc.top_3_pct_of_abs + '% of activity</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">PnL</th>'
      + '<th style="text-align:right;padding:5px 6px;">% of net</th>'
      + '<th style="text-align:right;padding:5px 6px;">% of activity</th>'
      + '<th style="text-align:right;padding:5px 6px;">Cumulative</th>'
      + '<th style="text-align:right;padding:5px 6px;">Wins / Losses</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      const pnlColor = s.pnl_usd > 0 ? '#00ff88' : (s.pnl_usd < 0 ? '#ff4444' : '#7b8ab8');
      const netColor = s.pct_of_net_pnl > 0 ? '#00ff88' : (s.pct_of_net_pnl < 0 ? '#ff4444' : '#7b8ab8');
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.trade_count + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + pnlColor + ';font-weight:bold;">' + (s.pnl_usd >= 0 ? '+' : '') + '$' + s.pnl_usd.toFixed(2) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + netColor + ';">' + (s.pct_of_net_pnl >= 0 ? '+' : '') + s.pct_of_net_pnl.toFixed(1) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.pct_of_abs_pnl.toFixed(1) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#7b8ab8;">' + s.cumulative_abs_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.wins + ' (+$' + s.wins_pnl_usd.toFixed(0) + ') / ' + s.losses + ' ($' + s.losses_pnl_usd.toFixed(0) + ')</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('alpha attribution error:', e);});
}
loadAlphaAttribution();
setInterval(loadAlphaAttribution, 60000);

// ─── BENCHMARK ALPHA ───────────────────────────────────────────────
// Excess return vs a passive buy-and-hold proxy. Answers: "is the strategy
// adding value over just holding the underlying?" — the question the
// renamed Fleet Contribution panel implicitly promised but couldn't yet
// answer with raw PnL. NOT Jensen's alpha, NOT beta-adjusted; just simple
// excess return — honest about what it is.
function loadBenchmarkAlpha() {
  fetch('/api/benchmark_alpha?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('benchmark-alpha-panel');
    if (!el) return;
    const rows = data.strategies || [];
    if (rows.length === 0) { el.innerHTML = ''; return; }
    const inScope = data.n_in_scope || 0;
    const withAlpha = data.n_with_alpha || 0;
    const headlineColor = withAlpha > 0 ? '#00ff88' : '#9da8c7';
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">BENCHMARK ALPHA (30d)</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">excess return vs passive buy-and-hold (NOT beta-adjusted)</span></div>'
      + '<div style="font-size:0.75em;color:' + headlineColor + ';">'
      + '<b>' + withAlpha + '/' + inScope + '</b> strategies beat their benchmark · '
      + (data.n_skipped || 0) + ' skipped (FX/multi-leg/scanner)</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:left;padding:5px 6px;">Benchmark</th>'
      + '<th style="text-align:right;padding:5px 6px;">N</th>'
      + '<th style="text-align:right;padding:5px 6px;">Strat %</th>'
      + '<th style="text-align:right;padding:5px 6px;">Bench %</th>'
      + '<th style="text-align:right;padding:5px 6px;">Excess pp</th>'
      + '<th style="text-align:center;padding:5px 6px;">Verdict</th>'
      + '</tr></thead><tbody>';
    for (const r of rows) {
      const stratColor = r.strategy_pnl_pct > 0 ? '#00ff88' : (r.strategy_pnl_pct < 0 ? '#ff4444' : '#9da8c7');
      const benchColor = r.benchmark_pnl_pct > 0 ? '#9da8c7' : (r.benchmark_pnl_pct < 0 ? '#9da8c7' : '#9da8c7');
      const excessColor = r.excess_pct > 0 ? '#00ff88' : (r.excess_pct < 0 ? '#ff4444' : '#9da8c7');
      const verdict = r.alpha_pass
        ? '<span style="background:#0d1c11;border:1px solid #00ff88;color:#00ff88;padding:1px 6px;border-radius:3px;font-weight:bold;font-size:0.92em;">ALPHA</span>'
        : '<span style="color:#7b8ab8;font-size:0.92em;">no edge vs passive</span>';
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + r.strategy + '</td>'
        + '<td style="padding:5px 6px;color:#9da8c7;" title="' + r.benchmark_label + '">' + r.benchmark_ticker + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + r.trade_count + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + stratColor + ';">' + (r.strategy_pnl_pct >= 0 ? '+' : '') + r.strategy_pnl_pct.toFixed(2) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + benchColor + ';">' + (r.benchmark_pnl_pct >= 0 ? '+' : '') + r.benchmark_pnl_pct.toFixed(2) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + excessColor + ';font-weight:bold;">' + (r.excess_pct >= 0 ? '+' : '') + r.excess_pct.toFixed(2) + 'pp</td>'
        + '<td style="padding:5px 6px;text-align:center;">' + verdict + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>'
      + '<div style="margin-top:6px;font-size:0.7em;color:#7b8ab8;">'
      + 'Excess = (strategy PnL / fleet anchor) - (benchmark buy-and-hold). FX + multi-leg + scanner strategies excluded (no clean benchmark). '
      + 'Conservative bias: strategies using less than full anchor capital are slightly understated.'
      + '</div>'
      + '</div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('benchmark alpha error:', e);});
}
loadBenchmarkAlpha();
setInterval(loadBenchmarkAlpha, 300000);  // 5min — benchmark prices are cached for 1h anyway

// ─── CORRELATION / REDUNDANCY MAP ──────────────────────────────────
// Pairwise correlation + time-overlap. Strategies with high corr + high
// overlap are redundant — same bet by another name.
function loadCorrelationMap() {
  fetch('/api/correlation_map?window_days=30').then(r=>r.json()).then(data=>{
    const el = document.getElementById('correlation-map-panel');
    if (!el) return;
    const pairs = data.pairs || [];
    if (pairs.length === 0) { el.innerHTML = ''; return; }
    const redundantCount = data.n_redundant || 0;
    const headlineColor = redundantCount > 0 ? '#ffaa00' : '#9da8c7';
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">CORRELATION / REDUNDANCY (30d)</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">high corr + high time overlap = same bet by another name</span></div>'
      + '<div style="font-size:0.75em;color:' + headlineColor + ';">'
      + data.n_strategies + ' strategies · ' + data.n_pairs + ' pairs · '
      + '<b>' + redundantCount + ' redundant</b> (corr&gt;0.6 + overlap&gt;30%)</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy A</th>'
      + '<th style="text-align:left;padding:5px 6px;">Strategy B</th>'
      + '<th style="text-align:right;padding:5px 6px;">Correlation</th>'
      + '<th style="text-align:right;padding:5px 6px;">Time overlap</th>'
      + '<th style="text-align:right;padding:5px 6px;" title="Number of co-occurring trade pairs. Low N = correlation is noise.">N overlap</th>'
      + '<th style="text-align:center;padding:5px 6px;">Confidence</th>'
      + '<th style="text-align:center;padding:5px 6px;">Verdict</th>'
      + '</tr></thead><tbody>';
    // Show top 15 most-correlated pairs
    for (const p of pairs.slice(0, 15)) {
      const corr = p.correlation;
      const conf = p.confidence || 'LOW';
      // De-emphasize correlation color when confidence is LOW — a strong-looking
      // correlation off 2 trades is noise, not signal.
      const corrColor = (corr === null || conf === 'LOW') ? '#7b8ab8'
                        : (Math.abs(corr) > 0.6 ? '#ff4444' : (Math.abs(corr) > 0.3 ? '#ffc107' : '#9da8c7'));
      const ovColor = p.time_overlap_pct > 30 ? '#ff4444' : (p.time_overlap_pct > 10 ? '#ffc107' : '#9da8c7');
      const confColor = conf === 'HIGH' ? '#00ff88' : (conf === 'OK' ? '#9da8c7' : '#7b8ab8');
      const confLabel = '<span style="background:' + (conf === 'LOW' ? '#1e2a42' : (conf === 'HIGH' ? '#0d1c11' : '#0d1321')) + ';color:' + confColor + ';padding:1px 6px;border-radius:3px;font-size:0.92em;letter-spacing:1px;" title="N=' + p.n_overlap + ' co-occurring trades">' + conf + '</span>';
      // Verdict: REDUNDANT only fires when confidence isn't LOW (server-side rule),
      // but show "tentative" when confidence is LOW even with high apparent correlation
      // so the user knows there's a pattern but it's not yet trustworthy.
      let verdict;
      if (p.redundant) {
        verdict = '<span style="background:#ff4444;color:#000;padding:1px 6px;border-radius:3px;font-weight:bold;">REDUNDANT</span>';
      } else if (conf === 'LOW' && corr !== null && Math.abs(corr) > 0.6 && p.time_overlap_pct > 30) {
        verdict = '<span style="color:#ffaa00;" title="Pattern would be REDUNDANT but N=' + p.n_overlap + ' is too small">tentative</span>';
      } else {
        verdict = '<span style="color:#7b8ab8;">ok</span>';
      }
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + p.strategy_a + '</td>'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + p.strategy_b + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + corrColor + ';font-weight:bold;">' + (corr === null ? '—' : (corr >= 0 ? '+' : '') + corr.toFixed(2)) + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + ovColor + ';">' + p.time_overlap_pct.toFixed(0) + '%</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + p.n_overlap + '</td>'
        + '<td style="padding:5px 6px;text-align:center;">' + confLabel + '</td>'
        + '<td style="padding:5px 6px;text-align:center;">' + verdict + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>';
    if (pairs.length > 15) {
      html += '<div style="font-size:0.7em;color:#7b8ab8;margin-top:4px;">Showing top 15 of ' + pairs.length + ' pairs (sorted by absolute correlation).</div>';
    }
    html += '</div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('correlation map error:', e);});
}
loadCorrelationMap();
setInterval(loadCorrelationMap, 300000);  // 5min refresh; correlations change slowly

// ─── TRADE VALIDITY COUNTS ─────────────────────────────────────────
// Surfaces strategies with high invalid-trade ratio. Today's incident left
// 6 invalid trades; without this view they'd silently pollute validation.
function loadTradeValidity() {
  fetch('/api/trade_validity_counts').then(r=>r.json()).then(data=>{
    const el = document.getElementById('trade-validity-panel');
    if (!el) return;
    const rows = data.strategies || [];
    // Only show if there are ANY invalid trades anywhere
    if (data.fleet_invalid === 0 || rows.length === 0) {
      el.innerHTML = '<div style="background:#0d1c11;border:1px solid #143021;border-radius:4px;padding:6px 12px;font-size:0.7em;color:#00e676;">'
        + '<span style="letter-spacing:1px;">TRADE VALIDITY</span>: ' + data.fleet_valid + ' valid trades, 0 invalid (clean)</div>';
      return;
    }
    let html = '<div style="background:#141b2d;border:1px solid #1e2a42;border-radius:6px;padding:10px 14px;">'
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">'
      + '<div><span style="color:#00d4ff;font-weight:bold;font-size:0.85em;letter-spacing:2px;">TRADE VALIDITY</span>'
      + ' <span style="color:#7b8ab8;font-size:0.85em;font-weight:normal;letter-spacing:1px;margin-left:8px;">data hygiene surface — invalid trades excluded from edge analysis</span></div>'
      + '<div style="font-size:0.75em;color:#9da8c7;">'
      + 'Fleet: <b>' + data.fleet_valid + '</b> valid · <b style="color:#ffaa00;">' + data.fleet_invalid + '</b> invalid'
      + ' (<b>' + data.fleet_invalid_pct.toFixed(1) + '%</b>)</div></div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:0.74em;">'
      + '<thead><tr style="border-bottom:1px solid #1e2a42;color:#7b8ab8;">'
      + '<th style="text-align:left;padding:5px 6px;">Strategy</th>'
      + '<th style="text-align:right;padding:5px 6px;">Valid</th>'
      + '<th style="text-align:right;padding:5px 6px;">Invalid</th>'
      + '<th style="text-align:right;padding:5px 6px;">Invalid %</th>'
      + '<th style="text-align:left;padding:5px 6px;">Top reasons</th>'
      + '</tr></thead><tbody>';
    for (const s of rows) {
      if (s.invalid === 0) continue;  // only show strategies with invalid trades
      const invColor = s.invalid_pct > 20 ? '#ff4444' : (s.invalid_pct > 5 ? '#ffaa00' : '#ffc107');
      const reasons = (s.top_invalid_reasons || []).map(r => r.reason + '×' + r.count).join(' · ');
      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:5px 6px;color:#e0e0e0;">' + s.strategy + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:#9da8c7;">' + s.valid + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + invColor + ';font-weight:bold;">' + s.invalid + '</td>'
        + '<td style="padding:5px 6px;text-align:right;color:' + invColor + ';">' + s.invalid_pct.toFixed(1) + '%</td>'
        + '<td style="padding:5px 6px;color:#7b8ab8;font-size:0.92em;">' + reasons + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div>';
    el.innerHTML = html;
  }).catch(e=>{console.error('trade validity error:', e);});
}
loadTradeValidity();
setInterval(loadTradeValidity, 120000);
