
const VERDICT_OPTIONS = ["BLOCKED","OBSERVE","KEEP-PAPER","WINNER-CANDIDATE","REAL-CANDIDATE","REWORK","QUARANTINE","KILL"];
let DATA = null;

function setStatus(msg, kind) {
  const el = document.getElementById("status");
  el.textContent = msg;
  el.className = "status " + (kind || "");
}

// In-memory map of strategy → recommended action (populated alongside skeleton load).
// Inline display on each card eliminates context-switching to /api/recommended_actions.
let RECOMMENDATIONS = {};

function loadData() {
  const date = document.getElementById("date-input").value.trim();
  setStatus("Loading...");
  Promise.all([
    fetch("/api/verdict_filled?date=" + encodeURIComponent(date)).then(r=>r.json()),
    fetch("/api/recommended_actions?window_days=30").then(r=>r.json()).catch(()=>({actions:[]})),
  ]).then(([d, recs])=>{
    if (d.status === "missing") {
      setStatus("No skeleton found for date " + date + ". Run ops.generate_verdict_skeleton first.", "err");
      return;
    }
    DATA = d;
    DATA.review_date_compact = date;
    // Index recommendations by strategy for O(1) lookup during render
    RECOMMENDATIONS = {};
    for (const a of (recs.actions || [])) {
      RECOMMENDATIONS[a.strategy] = a;
    }
    render();
    setStatus("Loaded " + (d._source || "skeleton") + " — " + (d.n_strategies || 0) + " strategies, " + (recs.n_actions || 0) + " recommended actions", "ok");
  }).catch(e => setStatus("Load error: " + e, "err"));
}

function render() {
  if (!DATA) return;
  const sb = document.getElementById("summary-bar");
  const counts = {};
  for (const s of (DATA.strategies || [])) {
    const v = s.verdict || "(unfilled)";
    counts[v] = (counts[v] || 0) + 1;
  }
  sb.innerHTML = '<span>Source: <b>' + (DATA._source || "?") + '</b></span>'
    + '<span>Total: <b>' + (DATA.n_strategies || 0) + '</b></span>'
    + '<span>Auto-filled: <b>' + (DATA.n_auto_filled || 0) + '</b></span>'
    + '<span>Manual needed: <b>' + (DATA.n_requiring_manual_judgment || 0) + '</b></span>'
    + '<span>|</span>'
    + Object.entries(counts).map(([k,v]) =>
        '<span>' + k + ': <b class="v-color-' + k + '">' + v + '</b></span>'
      ).join('');

  const root = document.getElementById("strategies");
  // Manual-judgment cards first (the actual work), then auto-filled.
  const sorted = (DATA.strategies || []).slice().sort((a,b)=>{
    const aAuto = a.auto_filled ? 1 : 0;
    const bAuto = b.auto_filled ? 1 : 0;
    if (aAuto !== bAuto) return aAuto - bAuto;
    return -(a.live_trades_post_clamp || 0) - -(b.live_trades_post_clamp || 0);
  });
  root.innerHTML = sorted.map((s, idx) => renderCard(s, DATA.strategies.indexOf(s))).join('');
}

function renderCard(s, originalIdx) {
  const cls = s.auto_filled ? "auto-filled" : "manual";
  const badge = s.auto_filled
    ? '<span class="badge auto">AUTO</span>'
    : '<span class="badge manual">MANUAL</span>';
  const elig = s.criteria_eligibility || {};
  const refs = s.references || {};

  // Inline recommendation pill — pulls from /api/recommended_actions cached
  // at load time. Eliminates context-switching to look up "what does the
  // engine think we should do" while filling each card.
  const rec = RECOMMENDATIONS[s.strategy];
  let recPill = '';
  if (rec) {
    const recColor = rec.priority === 1 ? '#ff4444' : rec.priority === 2 ? '#ffaa00' : '#9da8c7';
    const actionColor = {
      KILL: '#ff4444', KILL_CANDIDATE: '#ff4444', QUARANTINE: '#c084fc',
      SCOPE_DOWN: '#ffaa00', PROMOTE_REVIEW: '#00ff88', ALPHA_NEGATIVE: '#ffc107',
      REVIEW: '#9da8c7',
    }[rec.action] || '#9da8c7';
    const tooltip = (rec.reason || '').replace(/"/g, '&quot;');
    recPill = '<div style="margin:6px 0 8px;padding:6px 10px;background:#0a1224;border:1px solid ' + recColor + ';border-radius:4px;font-size:0.85em;" title="' + tooltip + '">'
      + '<span style="color:' + recColor + ';font-weight:bold;letter-spacing:1px;">P' + rec.priority + ' ENGINE SUGGESTS:</span> '
      + '<span style="background:' + actionColor + ';color:#000;padding:1px 6px;border-radius:3px;font-weight:bold;letter-spacing:1px;font-size:0.92em;">' + rec.action + '</span> '
      + '<span style="color:#9da8c7;">' + (rec.reason || '') + '</span>'
      + '</div>';
  }

  const metrics = [
    ['n', s.live_trades_post_clamp ?? '—'],
    ['PF', s.live_pf ?? '—'],
    ['PnL', s.live_pnl_usd != null ? '$' + s.live_pnl_usd.toFixed(2) : '—'],
    ['expectancy', s.live_expectancy_usd != null ? '$' + s.live_expectancy_usd.toFixed(2) : '—'],
    ['win rate', s.live_win_rate_pct != null ? s.live_win_rate_pct + '%' : '—'],
    ['op maturity', s.operational_maturity || '—'],
    ['op vetting', s.operational_vetting_auto || '—'],
  ];
  const eligLabels = [
    ['REAL-CAND quant', elig.real_candidate_quant_thresholds_met],
    ['WINNER-CAND quant', elig.winner_candidate_quant_thresholds_met],
    ['KEEP-PAPER quant', elig.keep_paper_quant_thresholds_met],
    ['KILL quant', elig['kill_quant_threshold_met (n>=30 + PF<1.0)']],
    ['OBSERVE quant', elig.observe_quant_threshold_met],
  ];
  const eligDisplay = eligLabels
    .filter(([_, v]) => v === true)
    .map(([l, _]) => '<span style="color:#00ff88;">' + l + '</span>')
    .join(' · ') || '<span style="color:#7b8ab8;">none triggered</span>';

  const verdictOpts = ['<option value="">(unfilled)</option>'].concat(
    VERDICT_OPTIONS.map(v =>
      '<option value="' + v + '"' + (s.verdict === v ? ' selected' : '') + '>' + v + '</option>'
    )
  ).join('');

  return '<div class="strategy-card ' + cls + '">'
    + '<div class="card-header">'
    + '<span class="strategy-name">' + s.strategy + '</span>'
    + badge
    + '</div>'
    + '<div class="metric-grid">'
    + metrics.map(([k, v]) => '<span class="metric">' + k + ': <b>' + v + '</b></span>').join('')
    + '</div>'
    + '<div style="font-size:0.78em;color:#7b8ab8;margin-bottom:8px;">Eligibility: ' + eligDisplay + '</div>'
    + recPill
    + '<div class="form-row">'
    + '<label>Verdict:</label>'
    + '<select onchange="updateField(' + originalIdx + ', \'verdict\', this.value)">' + verdictOpts + '</select>'
    + '<input type="date" value="' + (s.next_review_date || '') + '" onchange="updateField(' + originalIdx + ', \'next_review_date\', this.value)">'
    + '<span style="color:#7b8ab8;font-size:0.78em;align-self:center;">next review</span>'
    + '</div>'
    + '<div class="form-row">'
    + '<label>Reasoning:</label>'
    + '<textarea class="grow" onchange="updateField(' + originalIdx + ', \'reasoning\', this.value)" placeholder="1-3 sentences citing the metrics above">' + (s.reasoning || '') + '</textarea>'
    + '</div>'
    + '<div class="form-row">'
    + '<label>Action:</label>'
    + '<input class="grow" value="' + (s.action || '').replace(/"/g, '&quot;') + '" onchange="updateField(' + originalIdx + ', \'action\', this.value)" placeholder="concrete next step">'
    + '</div>'
    + '<div class="form-row">'
    + '<label>Rework fix:</label>'
    + '<input class="grow" value="' + (s.named_rework_fix || '').replace(/"/g, '&quot;') + '" onchange="updateField(' + originalIdx + ', \'named_rework_fix\', this.value)" placeholder="ONE specific fix (only if verdict=REWORK)">'
    + '</div>'
    + '<div class="ref-links">'
    + (refs.drilldown ? '<a href="' + refs.drilldown + '" target="_blank">↗ subset drilldown</a>' : '')
    + '<a href="/api/benchmark_alpha" target="_blank">↗ benchmark alpha</a>'
    + '<a href="/api/recommended_actions" target="_blank">↗ recommended actions</a>'
    + '<a href="/" target="_blank">↗ main dashboard</a>'
    + '</div>'
    + '</div>';
}

function updateField(idx, field, value) {
  if (!DATA || !DATA.strategies[idx]) return;
  DATA.strategies[idx][field] = value || null;
  // If verdict changed, the auto_filled badge becomes meaningful — recompute summary
  if (field === 'verdict') {
    DATA.strategies[idx].auto_filled = false;  // user touched it
    render();
  }
}

function saveData() {
  if (!DATA) { setStatus("Nothing loaded", "err"); return; }
  setStatus("Saving...");
  fetch("/api/save_verdict", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(DATA),
  }).then(r=>r.json()).then(d=>{
    if (d.status === "ok") {
      setStatus("Saved to " + d.path + " at " + d.saved_at_utc, "ok");
    } else {
      setStatus("Save failed: " + (d.message || JSON.stringify(d)), "err");
    }
  }).catch(e => setStatus("Save error: " + e, "err"));
}

// Auto-load on first paint
loadData();
