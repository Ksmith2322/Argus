
function loadStrategyPerformance() {
  // Fetch 4 endpoints in parallel: perf rows, tier/risk, exit-reason dist, promotion progress
  Promise.all([
    fetch('/api/strategy_performance').then(r=>r.json()),
    fetch('/api/strategy_tiers').then(r=>r.json()).catch(()=>({strategies:[]})),
    fetch('/api/exit_reasons').then(r=>r.json()).catch(()=>({strategies:{}})),
    fetch('/api/promotion_ladder').then(r=>r.json()).catch(()=>({strategies:[]}))
  ]).then(([data, tiersData, exitData, promData])=>{
    const el = document.getElementById('strategy-performance');
    if (!el) return;
    const strategies = data.strategies || [];
    const norm = (x) => String(x || '').toLowerCase().replace(/[\s_.\-/]/g, '');

    // Build tier lookup (accept full-name, stripped-prefix, and system synonyms)
    const tierMap = new Map();
    for (const t of (tiersData.strategies || [])) {
      const full = norm(t.strategy);
      tierMap.set(full, t);
      // Strip common prefixes so "spy_mean_rev" matches the "Spy Mean Rev" row
      const stripped = full.replace(/^(forge|argus|apollo|hermes|titan|ares)/, '');
      if (stripped && stripped !== full) tierMap.set(stripped, t);
    }
    const anchorUsd = Number(tiersData.anchor_usd || 0);

    // Build exit-distribution lookup. Keys: full, stripped-prefix, and aggregated
    // parent buckets (e.g. argus_usdjpy + argus_gbpusd + argus_cadjpy → "argus").
    const exitMap = new Map();
    const parentAgg = new Map(); // parent-name -> aggregated stats
    const rawExits = (exitData && exitData.strategies) || {};
    for (const key of Object.keys(rawExits)) {
      const entry = rawExits[key];
      const full = norm(key);
      exitMap.set(full, entry);
      const m = key.match(/^([a-z]+)_(.+)$/i);
      if (m) {
        const parent = m[1].toLowerCase();
        const rest = norm(m[2]);
        if (rest) exitMap.set(rest, entry);
        // Aggregate into parent bucket (for rows like "Argus" that cover 3 pairs)
        if (!parentAgg.has(parent)) {
          parentAgg.set(parent, {total: 0, by_reason: {}});
        }
        const agg = parentAgg.get(parent);
        agg.total += entry.total || 0;
        for (const [r, c] of Object.entries(entry.by_reason || {})) {
          agg.by_reason[r] = (agg.by_reason[r] || 0) + c;
        }
      }
    }
    // Promote aggregated buckets into the lookup (with pct recomputed)
    for (const [parent, agg] of parentAgg.entries()) {
      if (agg.total > 0 && !exitMap.has(parent)) {
        const pct_by_reason = {};
        for (const [r, c] of Object.entries(agg.by_reason)) {
          pct_by_reason[r] = Math.round(c / agg.total * 1000) / 10;
        }
        exitMap.set(parent, {total: agg.total, by_reason: agg.by_reason, pct_by_reason});
      }
    }

    // Build promotion-ladder lookup (same key strategy as tier/exit maps)
    const promMap = new Map();
    for (const p of (promData.strategies || [])) {
      const full = norm(p.strategy);
      promMap.set(full, p);
      const stripped = full.replace(/^(forge|argus|apollo|hermes|titan|ares)/, '');
      if (stripped && stripped !== full) promMap.set(stripped, p);
    }

    const renderExitBar = (entry) => {
      if (!entry || !entry.total) return '<span style="color:#555;">—</span>';
      const p = entry.pct_by_reason || {};
      const stop = p.stop || 0, target = p.target || 0, time = p.time || 0;
      const other = Math.max(0, 100 - stop - target - time);
      const parts = [];
      if (stop > 0)   parts.push('<div title="stop '+stop+'%" style="background:#ff4444;height:100%;width:'+stop+'%;"></div>');
      if (target > 0) parts.push('<div title="target '+target+'%" style="background:#00ff88;height:100%;width:'+target+'%;"></div>');
      if (time > 0)   parts.push('<div title="time '+time+'%" style="background:#ffc107;height:100%;width:'+time+'%;"></div>');
      if (other > 0)  parts.push('<div title="other '+other.toFixed(0)+'%" style="background:#555;height:100%;width:'+other+'%;"></div>');
      const tip = entry.total + ' trades · stop '+stop+'% · target '+target+'%' + (time ? ' · time '+time+'%' : '');
      return '<div title="' + tip + '" style="display:flex;width:100%;min-width:100px;height:10px;border-radius:2px;overflow:hidden;background:#1e2a42;">'
        + parts.join('') + '</div>'
        + '<div style="font-size:0.82em;color:#7b8ab8;margin-top:2px;">' + entry.total + ' trades</div>';
    };

    const fleetConfText = data.fleet_confidence !== null && data.fleet_confidence !== undefined
      ? data.fleet_confidence + '%'
      : '—';
    const fleetConfDetail = data.fleet_confidence_detail || '';
    const fleetSrc = data.fleet_confidence_source || '';
    const fleetBadge = fleetSrc === 'computed'
      ? '<span style="background:#143021;color:#00e676;padding:1px 5px;margin-left:4px;border-radius:3px;font-size:0.8em;" title="Computed from canonical_fills bootstrap">CALC</span>'
      : (fleetSrc === 'insufficient_sample'
        ? '<span style="background:#3a2f10;color:#ffc107;padding:1px 5px;margin-left:4px;border-radius:3px;font-size:0.8em;" title="' + fleetConfDetail + '">INSUFFICIENT SAMPLE</span>'
        : '<span style="background:#3a1010;color:#ff8888;padding:1px 5px;margin-left:4px;border-radius:3px;font-size:0.8em;" title="Manual constant — not data-derived">HARDCODED</span>');
    const expectedBadge = (data.expected_annual_source === 'hardcoded')
      ? '<span style="background:#3a1010;color:#ff8888;padding:1px 5px;margin-left:4px;border-radius:3px;font-size:0.8em;" title="Manual constant — not data-derived">HARDCODED</span>'
      : '';
    const anchorLabel = anchorUsd > 0 ? ' | anchor $' + anchorUsd.toLocaleString(undefined,{maximumFractionDigits:0}) : '';
    let html = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">'
      + '<div style="color:#00d4ff;font-weight:bold;font-size:0.95em;letter-spacing:2px;">STRATEGY PERFORMANCE</div>'
      + '<div style="font-size:0.7em;color:#7b8ab8;" title="' + fleetConfDetail + '">Fleet confidence: ' + fleetConfText + fleetBadge + ' | Expected annual: ' + data.expected_annual + expectedBadge + anchorLabel + '</div>'
      + '</div>';

    html += '<table style="width:100%;border-collapse:collapse;font-size:0.72em;background:#141b2d;border:1px solid #1e2a42;border-radius:6px;overflow:hidden;">';
    html += '<thead><tr style="background:#0d1321;color:#7b8ab8;text-align:left;">'
      + '<th style="padding:8px;">System</th>'
      + '<th style="padding:8px;">Strategy</th>'
      + '<th style="padding:8px;min-width:120px;">Exit Distribution</th>'
      + '<th style="padding:8px;min-width:110px;">Promotion</th>'
      + '<th style="padding:8px;text-align:right;">Risk %</th>'
      + '<th style="padding:8px;text-align:right;">Backtest PF</th>'
      + '<th style="padding:8px;text-align:right;">BT Trades</th>'
      + '<th style="padding:8px;text-align:right;">BT WR</th>'
      + '<th style="padding:8px;text-align:right;">Live Trades</th>'
      + '<th style="padding:8px;text-align:right;">Live WR</th>'
      + '<th style="padding:8px;text-align:right;">Live PnL</th>'
      + '<th style="padding:8px;text-align:right;">Confidence</th>'
      + '<th style="padding:8px;text-align:right;">Status</th>'
      + '</tr></thead><tbody>';

    for (const s of strategies) {
      const confHasNumber = s.confidence !== null && s.confidence !== undefined;
      const confColor = !confHasNumber ? '#7b8ab8'
        : (s.confidence >= 70 ? '#00ff88' : (s.confidence >= 50 ? '#ffc107' : '#ff4444'));
      const liveWrColor = s.live_wr >= 50 ? '#00ff88' : (s.live_wr >= 40 ? '#ffc107' : (s.live_wr > 0 ? '#ff4444' : '#7b8ab8'));
      const livePnlColor = s.live_pnl > 0 ? '#00ff88' : (s.live_pnl < 0 ? '#ff4444' : '#7b8ab8');
      const statusColor = s.status === 'BAKING' || s.status === 'SCANNING' || s.status === 'WAITING_ER' ? '#00d4ff' : '#7b8ab8';

      // Name candidates for cross-endpoint lookups (tier map + exit map)
      const candidates = [
        s.strategy,
        s.system + '_' + s.strategy,
        s.system,
        (s.system || '').toLowerCase() + '_' + (s.strategy || '').toLowerCase()
      ].map(norm);
      let tierInfo = null;
      for (const k of candidates) {
        if (k && tierMap.has(k)) { tierInfo = tierMap.get(k); break; }
      }
      let exitInfo = null;
      for (const k of candidates) {
        if (k && exitMap.has(k)) { exitInfo = exitMap.get(k); break; }
      }
      let promInfo = null;
      for (const k of candidates) {
        if (k && promMap.has(k)) { promInfo = promMap.get(k); break; }
      }
      const riskPctStr = tierInfo ? ((Number(tierInfo.risk_pct || 0) * 100).toFixed(2) + '%') : '—';

      // Render promotion progress as 0-100 bar + % label + verdict color
      let promCell = '<span style="color:#555;">—</span>';
      if (promInfo) {
        const pct = Math.max(0, Math.min(100, Number(promInfo.progress_pct) || 0));
        const verdict = promInfo.verdict || '';
        const barColor = verdict === 'DEGRADED' ? '#ff4444'
                       : pct >= 75 ? '#00ff88'
                       : pct >= 50 ? '#ffc107'
                       : '#7b8ab8';
        const subtitle = promInfo.next_tier
          ? ('→ ' + promInfo.next_tier + ' · need ' + (promInfo.trades_needed || 0) + ' trades'
             + (promInfo.pf_gap > 0 ? ' · PF gap ' + Number(promInfo.pf_gap).toFixed(2) : ''))
          : 'at max tier';
        promCell = '<div style="display:flex;align-items:center;gap:6px;min-width:100px;">'
          + '<div style="flex:1;background:#1e2a42;border-radius:3px;height:6px;overflow:hidden;">'
          + '<div style="background:' + barColor + ';height:100%;width:' + pct + '%;"></div>'
          + '</div>'
          + '<span style="color:' + barColor + ';font-weight:bold;font-size:0.95em;">' + pct.toFixed(0) + '%</span>'
          + '</div>'
          + '<div style="font-size:0.82em;color:#7b8ab8;margin-top:2px;" title="' + subtitle + '">' + subtitle + '</div>';
      }

      // Confidence cell: number (or —) + source badge + sample warning tooltip
      const detail = s.confidence_detail || {};
      const warnText = detail.sample_warning || '';
      const confText = confHasNumber ? (s.confidence + '%') : '—';
      let confBadge = '';
      if (s.confidence_source === 'computed') {
        const bar = detail.evidence_bar || '';
        const nTot = detail.n_total !== undefined ? detail.n_total : '';
        const nLive = detail.n_live !== undefined ? detail.n_live : '';
        const nPaper = detail.n_paper !== undefined ? detail.n_paper : '';
        const tip = 'Computed: n=' + nTot + ' (live=' + nLive + ', paper=' + nPaper + '), bar=' + bar + (warnText ? ' — ' + warnText : '');
        confBadge = '<span title="' + tip + '" style="background:#143021;color:#00e676;padding:1px 4px;margin-left:3px;border-radius:3px;font-size:0.8em;">CALC</span>';
        if (warnText) {
          confBadge += '<span title="' + warnText + '" style="color:#ffc107;margin-left:3px;">⚠</span>';
        }
      } else if (s.confidence_source === 'hardcoded') {
        confBadge = '<span title="Manual constant — not data-derived" style="background:#3a1010;color:#ff8888;padding:1px 4px;margin-left:3px;border-radius:3px;font-size:0.8em;">HARDCODED</span>';
      }

      html += '<tr style="border-top:1px solid #1e2a42;">'
        + '<td style="padding:8px;font-weight:bold;color:#00d4ff;">' + s.system + '</td>'
        + '<td style="padding:8px;color:#e0e0e0;">' + s.strategy + '<br><span style="font-size:0.85em;color:#7b8ab8;">' + s.instruments + '</span></td>'
        + '<td style="padding:8px;vertical-align:middle;">' + renderExitBar(exitInfo) + '</td>'
        + '<td style="padding:8px;vertical-align:middle;">' + promCell + '</td>'
        + '<td style="padding:8px;text-align:right;color:#00d4ff;">' + riskPctStr + '</td>'
        + '<td style="padding:8px;text-align:right;color:#fff;">' + s.backtest_pf + '</td>'
        + '<td style="padding:8px;text-align:right;color:#7b8ab8;">' + s.backtest_trades + '</td>'
        + '<td style="padding:8px;text-align:right;color:#7b8ab8;">' + s.backtest_wr + '</td>'
        + '<td style="padding:8px;text-align:right;color:#fff;">' + s.live_trades + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + liveWrColor + ';">' + (s.live_wr || '-') + '%</td>'
        + '<td style="padding:8px;text-align:right;color:' + livePnlColor + ';">' + (s.live_pnl >= 0 ? '+' : '') + s.live_pnl + ' ' + s.live_unit + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + confColor + ';font-weight:bold;">' + confText + confBadge + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + statusColor + ';font-size:0.85em;">' + s.status + '</td>'
        + '</tr>';
    }

    // ── FLEET TOTALS footer row ─────────────────────────────────
    // Aggregations:
    //   BT Trades: sum numeric values
    //   BT WR:     weighted avg by BT Trades
    //   Live Trades: sum
    //   Live WR:   weighted avg by Live Trades (excludes zero-trade rows)
    //   Live PnL:  sum split by unit (pips vs $) — shown as "+X pips / +$Y" if both present
    //   Confidence: simple avg of numeric values (ignore null/hardcoded misleading 100%)
    //   Risk $:    sum of numeric values
    const parseNum = (v) => { const n = parseFloat(String(v).replace(/[^0-9.\-]/g, '')); return isFinite(n) ? n : null; };
    let btTradesTotal = 0, btWrSum = 0, btWrWt = 0;
    let liveTradesTotal = 0, liveWrSum = 0, liveWrWt = 0;
    const livePnlByUnit = {}; // unit -> sum
    let confSum = 0, confCount = 0;
    let riskUsdTotal = 0;
    for (const s of strategies) {
      const bt = parseNum(s.backtest_trades);
      const btWr = parseNum(s.backtest_wr);
      if (bt != null && bt > 0) {
        btTradesTotal += bt;
        if (btWr != null) { btWrSum += btWr * bt; btWrWt += bt; }
      }
      const lt = parseNum(s.live_trades) || 0;
      liveTradesTotal += lt;
      if (lt > 0 && s.live_wr != null) {
        liveWrSum += Number(s.live_wr) * lt;
        liveWrWt += lt;
      }
      if (s.live_pnl != null && s.live_pnl !== 0) {
        const u = s.live_unit || '$';
        livePnlByUnit[u] = (livePnlByUnit[u] || 0) + Number(s.live_pnl);
      }
      if (s.confidence != null) { confSum += Number(s.confidence); confCount++; }
      // Risk $ is rendered from tier data, not on s directly. Look up via name match logic.
      const candidates = [s.strategy, s.system + '_' + s.strategy, s.system].map(x => String(x || '').toLowerCase().replace(/[\s_.-]/g, ''));
      for (const k of candidates) {
        if (k && tierMap.has(k)) { riskUsdTotal += Number(tierMap.get(k).risk_usd_now || 0); break; }
      }
    }
    const btWrAvg = btWrWt > 0 ? (btWrSum / btWrWt).toFixed(0) + '%' : '—';
    const liveWrAvg = liveWrWt > 0 ? (liveWrSum / liveWrWt).toFixed(1) + '%' : '—';
    const confAvg = confCount > 0 ? (confSum / confCount).toFixed(0) + '%' : '—';
    const pnlParts = [];
    for (const [u, v] of Object.entries(livePnlByUnit)) {
      pnlParts.push((v >= 0 ? '+' : '') + (u === '$' ? '$' + v.toFixed(2) : v.toFixed(1) + ' ' + u));
    }
    const pnlStr = pnlParts.length ? pnlParts.join(' / ') : '—';
    const pnlColorTotal = (livePnlByUnit['$'] || 0) + (livePnlByUnit['pips'] || 0) >= 0 ? '#00ff88' : '#ff4444';
    const riskStr = riskUsdTotal > 0 ? '$' + riskUsdTotal.toLocaleString(undefined,{maximumFractionDigits:0}) : '—';

    // 13 cells must match the data rows above (system, strategy, exitBar, promCell,
    // risk%, BT PF, BT Trades, BT WR, Live Trades, Live WR, Live PnL, Confidence, Status).
    // Was missing the BT PF column → shifted every total one cell left of its header.
    html += '<tr style="border-top:2px solid #00d4ff;background:#0d1321;font-weight:bold;">'
      + '<td style="padding:10px 8px;color:#00d4ff;letter-spacing:1px;">FLEET TOTAL</td>'
      + '<td style="padding:10px 8px;color:#7b8ab8;font-weight:normal;font-size:0.85em;">(' + strategies.length + ' strategies)</td>'
      + '<td style="padding:10px 8px;color:#7b8ab8;">—</td>'
      + '<td style="padding:10px 8px;color:#7b8ab8;">—</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#7b8ab8;">—</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#7b8ab8;">—</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#fff;">' + btTradesTotal.toLocaleString() + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#9da8c7;">' + btWrAvg + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#fff;">' + liveTradesTotal.toLocaleString() + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#9da8c7;">' + liveWrAvg + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:' + pnlColorTotal + ';">' + pnlStr + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#e0e0e0;">' + confAvg + '</td>'
      + '<td style="padding:10px 8px;text-align:right;color:#7b8ab8;">—</td>'
      + '</tr>';
    html += '</tbody></table>';
    el.innerHTML = html;
  }).catch((e)=>{ console.error('strategy perf error:', e); });
}
loadStrategyPerformance();
setInterval(loadStrategyPerformance, 60000);
