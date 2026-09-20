/* Blood bank dashboard front end.
 * Every number on screen comes from the Flask API (/api/...), which reads the Neon analytics views.
 * The only constants here are labels, colours and illustrative management targets. */
(function () {
  'use strict';

  // ------------------------------------------------------------------ constants
  const BLOOD_GROUPS = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'];
  const COMP_LABEL = { RBC: 'Red cells', PLATELETS: 'Platelets', PLASMA: 'Plasma' };
  const COMP_COLOR = { RBC: '#a4161a', PLATELETS: '#d9902f', PLASMA: '#6b7f95' };
  const C = { teal: '#0d6e6a', risk: '#a4161a', amber: '#d9902f', navy: '#14243a', slate: '#6b7f95', light: '#c9d2dc', blue: '#2b5fa8' };
  // Illustrative management targets (assumptions, shown on the KPI cards)
  const TARGETS = { wastageMax: 10, fulfilmentMin: 95, emergencyMinutesMax: 45, atRiskWarnPct: 8, atRiskBadPct: 15 };
  const AUTO_REFRESH_MS = 10000;
  const FEED_REFRESH_MS = 4000;
  const STALE_HOURS = 6;

  const state = {
    tab: 'overview',
    loaded: {},
    charts: {},
    tables: {},
    sort: {},
    branches: [],
    branchName: {},
    busy: false,
    auto: true,
    timers: [],
    prev: {},          // last rendered KPI text, used to flash values that change
    feedSeen: null,    // event keys already shown in the live feed
    lastActivity: null,
    filters: { branch: '', blood_group: '', component: '', days: '30' },
  };

  // ------------------------------------------------------------------ formatting helpers
  const nfIN = new Intl.NumberFormat('en-IN');
  const fInt = (v) => (v == null ? '\u2014' : nfIN.format(Math.round(v)));
  const fPct = (v) => (v == null ? '\u2014' : v.toFixed(1) + '%');
  const fMin = (v) => (v == null ? '\u2014' : v >= 120 ? (v / 60).toFixed(1) + ' h' : Math.round(v) + ' min');
  const fHours = (h) => {
    if (h == null) return '\u2014';
    if (h < 1) return Math.round(h * 60) + ' min';
    if (h < 48) return Math.round(h) + ' h';
    return (h / 24).toFixed(1) + ' days';
  };
  const fDateTime = (iso) => (iso ? new Date(iso).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false }) : '\u2014');
  const fClock = (iso) => (iso ? new Date(iso).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '');
  const fDay = (s) => { const [y, m, d] = s.slice(0, 10).split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); };
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = (id) => document.getElementById(id);
  const nUnits = (n) => `${fInt(n)} ${n === 1 ? 'unit' : 'units'}`;
  const bName = (id) => state.branchName[id] || id || '\u2014';

  // Briefly highlight a figure when its value changes between refreshes (makes live updates visible)
  function flashIfChanged(key, el, text) {
    const before = state.prev[key];
    state.prev[key] = text;
    if (before !== undefined && before !== text) {
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    }
  }

  const pill = (text, cls) => `<span class="pill ${cls}">${esc(text)}</span>`;
  const compTag = (c) => `<span class="comp"><i style="background:${COMP_COLOR[c] || '#999'}"></i>${esc(COMP_LABEL[c] || c)}</span>`;
  const urgencyPill = (u) => pill({ EMERGENCY: 'Emergency', URGENT: 'Urgent', ROUTINE: 'Routine' }[u] || u, { EMERGENCY: 'crit', URGENT: 'high', ROUTINE: 'neutral' }[u] || 'neutral');
  const statusPill = (s) => pill({ FULFILLED: 'Met in full', PARTIAL: 'Partly met', UNFULFILLED: 'Unmet' }[s] || s, { FULFILLED: 'ok', PARTIAL: 'high', UNFULFILLED: 'crit' }[s] || 'neutral');
  const expiryPill = (h) => (h == null ? '' : h < 24 ? pill('Critical', 'crit') : h < 72 ? pill('High', 'high') : pill('Watch', 'watch'));

  // ------------------------------------------------------------------ API layer
  async function api(path, params, refresh) {
    const qs = new URLSearchParams();
    Object.entries(params || {}).forEach(([k, v]) => { if (v !== '' && v != null) qs.set(k, v); });
    if (refresh) qs.set('refresh', '1');
    let res;
    try {
      res = await fetch(`/api/${path}?${qs}`, { headers: { Accept: 'application/json' } });
    } catch (e) {
      throw new Error('Cannot reach the dashboard server. Check that app.py is still running in the terminal.');
    }
    let body = null;
    try { body = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((body && body.message) || `Request failed (${res.status})`);
    return body;
  }

  async function settle(promises) {
    const res = await Promise.allSettled(promises);
    return {
      values: res.map((r) => (r.status === 'fulfilled' ? r.value : null)),
      errors: res.filter((r) => r.status === 'rejected').map((r) => r.reason.message),
    };
  }

  function showErrors(errors) {
    const el = $('error-banner');
    if (!errors.length) { el.hidden = true; return; }
    el.textContent = [...new Set(errors)][0];
    el.hidden = false;
  }

  const F = () => ({ branch: state.filters.branch, blood_group: state.filters.blood_group, component: state.filters.component, days: state.filters.days });

  // ------------------------------------------------------------------ charts
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 12;
  Chart.defaults.color = '#44546a';
  Chart.defaults.borderColor = '#e4e8ec';
  Chart.defaults.animation = false;
  Chart.defaults.scales.linear.ticks.precision = 0;   // counts of units/requests are whole numbers   // crisp updates on every refresh, and no half-drawn charts

  const valueLabels = {
    id: 'valueLabels',
    afterDatasetsDraw(chart, _args, opts) {
      if (!opts || !opts.enabled) return;
      const { ctx } = chart;
      ctx.save();
      ctx.font = `600 12px ${Chart.defaults.font.family}`;
      ctx.fillStyle = '#16222e';
      ctx.textAlign = 'center';
      chart.data.datasets.forEach((ds, i) => {
        chart.getDatasetMeta(i).data.forEach((bar, j) => {
          const v = ds.data[j];
          if (v != null) ctx.fillText(v, bar.x, bar.y - 5);
        });
      });
      ctx.restore();
    },
  };

  function upsertChart(key, canvasId, config) {
    const existing = state.charts[key];
    if (existing) {
      existing.data = config.data;
      existing.options = config.options;
      existing.update('none');
      return;
    }
    config.plugins = (config.plugins || []).concat([valueLabels]);
    state.charts[key] = new Chart($(canvasId), config);
  }

  const baseOptions = (extra) => Object.assign({
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, boxHeight: 12 } } },
  }, extra || {});

  // ------------------------------------------------------------------ generic sortable table
  function drawTable(id) {
    const t = state.tables[id];
    if (!t) return;
    const el = $(id);
    const sort = state.sort[id];
    let rows = t.rows.slice();
    if (sort) {
      const col = t.cols.find((c) => c.key === sort.key);
      if (col) {
        const val = col.sort || ((r) => r[col.key]);
        rows.sort((a, b) => {
          const x = val(a), y = val(b);
          if (x === y) return 0;
          if (x == null) return 1;
          if (y == null) return -1;
          return (x > y ? 1 : -1) * (sort.dir === 'asc' ? 1 : -1);
        });
      }
    }
    if (t.opts.max) rows = rows.slice(0, t.opts.max);
    const head = t.cols.map((c) => {
      const active = sort && sort.key === c.key;
      const aria = active ? ` aria-sort="${sort.dir === 'asc' ? 'ascending' : 'descending'}"` : '';
      return `<th class="${c.align || ''}${c.sortable === false ? '' : ' sortable'}" data-key="${c.key}"${aria}>${esc(c.label)}</th>`;
    }).join('');
    const body = rows.length
      ? rows.map((r) => `<tr${t.opts.rowClass ? ` class="${t.opts.rowClass(r)}"` : ''}>${t.cols.map((c) => `<td class="${c.align || ''}">${c.html ? c.html(r) : esc(r[c.key] == null ? '\u2014' : r[c.key])}</td>`).join('')}</tr>`).join('')
      : `<tr><td class="empty" colspan="${t.cols.length}">${esc(t.opts.empty || 'No rows for these filters.')}</td></tr>`;
    el.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
  }

  function renderTable(id, cols, rows, opts) {
    state.tables[id] = { cols, rows, opts: opts || {} };
    drawTable(id);
  }

  document.addEventListener('click', (e) => {
    const th = e.target.closest('th.sortable');
    if (!th) return;
    const table = th.closest('table');
    if (!table || !state.tables[table.id]) return;
    const cur = state.sort[table.id];
    const key = th.dataset.key;
    state.sort[table.id] = cur && cur.key === key ? { key, dir: cur.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: 'desc' };
    drawTable(table.id);
  });

  // ------------------------------------------------------------------ KPI band
  const KPI_DEFS = [
    { id: 'units_in_stock', label: 'Units in stock', tip: 'Units collected and not yet issued, expired or discarded.',
      value: (k) => fInt(k.units_in_stock), sub: (k) => `${fInt(k.collected)} collected in the last ${k.period_days} days`, tone: () => '' },
    { id: 'units_at_risk', label: 'Units at expiry risk', accent: true, tip: 'In-stock units expiring inside their risk window: red cells 7 days, platelets 48 hours, plasma 30 days.',
      value: (k) => fInt(k.units_at_risk), sub: (k) => `${fPct(k.at_risk_share_pct)} of units in stock`,
      tone: (k) => (k.at_risk_share_pct == null ? '' : k.at_risk_share_pct >= TARGETS.atRiskBadPct ? 'bad' : k.at_risk_share_pct >= TARGETS.atRiskWarnPct ? 'warn' : 'good') },
    { id: 'wastage', label: 'Wastage rate', tip: 'Expired units divided by units collected, over the selected period.',
      value: (k) => fPct(k.wastage_rate_pct), sub: (k) => `${fInt(k.expired)} expired of ${fInt(k.collected)} collected. Target ${TARGETS.wastageMax}% or less`,
      tone: (k) => (k.wastage_rate_pct == null ? '' : k.wastage_rate_pct > TARGETS.wastageMax * 1.5 ? 'bad' : k.wastage_rate_pct > TARGETS.wastageMax ? 'warn' : 'good') },
    { id: 'fulfilment', label: 'Fulfilment rate', tip: 'Requests met in full divided by all requests, over the selected period.',
      value: (k) => fPct(k.fulfilment_rate_pct), sub: (k) => `${fInt(k.fulfilled)} of ${fInt(k.requests)} requests met in full. Target ${TARGETS.fulfilmentMin}% or more`,
      tone: (k) => (k.fulfilment_rate_pct == null ? '' : k.fulfilment_rate_pct >= TARGETS.fulfilmentMin ? 'good' : k.fulfilment_rate_pct >= TARGETS.fulfilmentMin - 5 ? 'warn' : 'bad') },
    { id: 'emergency', label: 'Emergency requests', tip: 'Requests with emergency urgency in the selected period.',
      value: (k) => fInt(k.emergency_requests), sub: (k) => `${fPct(k.emergency_share_pct)} of ${fInt(k.requests)} requests`, tone: () => '' },
    { id: 'emergency_resp', label: 'Emergency response time', tip: 'Average minutes from request to fulfilment for emergency requests.',
      value: (k) => fMin(k.avg_emergency_response_min), sub: () => `Request to fulfilment. Target ${TARGETS.emergencyMinutesMax} min or less`,
      tone: (k) => (k.avg_emergency_response_min == null ? '' : k.avg_emergency_response_min <= TARGETS.emergencyMinutesMax ? 'good' : k.avg_emergency_response_min <= TARGETS.emergencyMinutesMax * 1.5 ? 'warn' : 'bad') },
    { id: 'unmet', label: 'Unmet requests', tip: 'Requests only partly met or not met at all. A proxy for stockout incidents.',
      value: (k) => fInt(k.unmet_requests), sub: (k) => `${fPct(k.requests ? 100 * k.unmet_requests / k.requests : null)} of requests, partly or fully unfulfilled`, tone: () => '' },
    { id: 'repeat', label: 'Repeat donor rate', tip: 'Donors (duplicate records merged) who donated more than once, divided by all donors.',
      value: (k) => fPct(k.repeat_donor_rate_pct), sub: (k) => `${fInt(k.repeat_donors)} of ${fInt(k.unique_donors)} donors gave more than once`, tone: () => '' },
  ];

  function renderKpis(k) {
    const band = $('kpi-band');
    if (!band.children.length) {
      band.innerHTML = KPI_DEFS.map((d) => `<div class="kpi${d.accent ? ' accent' : ''}" id="kpi-${d.id}" title="${esc(d.tip)}"><div class="kpi-label">${esc(d.label)}</div><div class="kpi-value">\u2014</div><div class="kpi-sub">&nbsp;</div></div>`).join('');
    }
    KPI_DEFS.forEach((d) => {
      const el = $('kpi-' + d.id);
      el.className = `kpi${d.accent ? ' accent' : ''} ${d.tone(k)}`.trim();
      const text = d.value(k);
      flashIfChanged('kpi-' + d.id, el, text);
      el.querySelector('.kpi-value').textContent = text;
      el.querySelector('.kpi-sub').textContent = d.sub(k);
    });
    const scope = [];
    if (state.filters.branch) scope.push(bName(state.filters.branch));
    if (state.filters.blood_group) scope.push(state.filters.blood_group);
    if (state.filters.component) scope.push(COMP_LABEL[state.filters.component]);
    $('kpi-title').textContent = `Network performance, last ${k.period_days} days` + (scope.length ? ` (${scope.join(', ')})` : '');
  }

  // ------------------------------------------------------------------ OVERVIEW
  function overviewInsight(k, inv) {
    if (!k.units_in_stock) return 'There are no units in stock for the selected filters.';
    const top = state.filters.branch ? null : (inv ? inv.by_branch : []).slice().sort((a, b) => b.at_risk - a.at_risk)[0];
    let s = `<strong>${fInt(k.units_at_risk)} of ${fInt(k.units_in_stock)} units in stock (${fPct(k.at_risk_share_pct)}) are inside their expiry-risk window.</strong>`;
    if (top && top.at_risk > 0) s += ` ${esc(top.branch_name)} holds the most (${fInt(top.at_risk)}).`;
    if (k.wastage_rate_pct != null) {
      const above = k.wastage_rate_pct > TARGETS.wastageMax;
      s += ` Wastage of ${fPct(k.wastage_rate_pct)} over the last ${k.period_days} days is ${above ? 'above' : 'within'} the ${TARGETS.wastageMax}% target.`;
    }
    return s;
  }

  function renderStockByBloodGroup(inv) {
    const rows = inv.by_blood_group;
    upsertChart('bg', 'c-bg', {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.blood_group),
        datasets: ['RBC', 'PLATELETS', 'PLASMA'].map((c) => ({ label: COMP_LABEL[c], data: rows.map((r) => r[c]), backgroundColor: COMP_COLOR[c], borderWidth: 0 })),
      },
      options: baseOptions({
        scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Units in stock' } } },
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, boxHeight: 12 } },
          tooltip: { callbacks: { footer: (items) => { const r = rows[items[0].dataIndex]; return `Total ${fInt(r.total)}, at expiry risk ${fInt(r.at_risk)}`; } } } },
      }),
    });
  }

  function renderStockByBranch(inv) {
    const rows = inv.by_branch.slice().sort((a, b) => b.total - a.total);
    upsertChart('branch', 'c-branch', {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.branch_name),
        datasets: [
          { label: 'Not at risk', data: rows.map((r) => r.total - r.at_risk), backgroundColor: C.teal, borderWidth: 0 },
          { label: 'At expiry risk', data: rows.map((r) => r.at_risk), backgroundColor: C.risk, borderWidth: 0 },
        ],
      },
      options: baseOptions({
        indexAxis: 'y',
        scales: { x: { stacked: true, beginAtZero: true, title: { display: true, text: 'Units in stock' } }, y: { stacked: true, grid: { display: false } } },
      }),
    });
  }

  function renderTrend(tr) {
    const d = tr.daily;
    upsertChart('trend', 'c-trend', {
      type: 'line',
      data: {
        labels: d.map((r) => fDay(r.day)),
        datasets: [
          { label: 'Collected', data: d.map((r) => r.collected), borderColor: C.navy, backgroundColor: C.navy, tension: 0.25, pointRadius: 0, borderWidth: 2 },
          { label: 'Issued to hospitals', data: d.map((r) => r.issued), borderColor: C.teal, backgroundColor: C.teal, tension: 0.25, pointRadius: 0, borderWidth: 2 },
          { label: 'Expired', data: d.map((r) => r.expired), borderColor: C.risk, backgroundColor: C.risk, tension: 0.25, pointRadius: 0, borderWidth: 2 },
        ],
      },
      options: baseOptions({
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 10, maxRotation: 0 } }, y: { beginAtZero: true, title: { display: true, text: 'Units per day' } } },
      }),
    });
  }

  function renderBranchScorecard(br) {
    state.branches = br.branches;
    br.branches.forEach((b) => { state.branchName[b.branch_id] = b.branch_name; });
    renderTable('t-branches', [
      { key: 'branch_name', label: 'Branch', html: (r) => `<span class="strong">${esc(r.branch_name)}</span><span class="sub">${esc(r.city)}, ${r.branch_type === 'HUB' ? 'hub' : 'spoke'}</span>` },
      { key: 'units_in_stock', label: 'In stock', align: 'num', html: (r) => fInt(r.units_in_stock) },
      { key: 'units_at_risk', label: 'At risk', align: 'num', html: (r) => `${fInt(r.units_at_risk)}<span class="sub">${fPct(r.at_risk_share_pct)}</span>` },
      { key: 'requests', label: 'Requests', align: 'num', html: (r) => fInt(r.requests) },
      { key: 'unmet_requests', label: 'Unmet', align: 'num', html: (r) => (r.unmet_requests ? pill(fInt(r.unmet_requests), r.unmet_requests > 0.05 * r.requests ? 'crit' : 'high') : pill('0', 'ok')) },
      { key: 'fulfilment_rate_pct', label: 'Fulfilled', align: 'num', html: (r) => fPct(r.fulfilment_rate_pct) },
      { key: 'avg_response_min', label: 'Response', align: 'num', html: (r) => fMin(r.avg_response_min) },
    ], br.branches);
  }

  async function loadOverview(refresh) {
    const f = F();
    const { values: [k, inv, br, tr], errors } = await settle([
      api('kpis', f, refresh), api('inventory', f, refresh), api('branches', f, refresh), api('trends', f, refresh),
    ]);
    if (br) renderBranchScorecard(br);
    if (k) renderKpis(k);
    if (inv) { renderStockByBloodGroup(inv); renderStockByBranch(inv); }
    if (tr) renderTrend(tr);
    if (k) $('insight-overview').innerHTML = overviewInsight(k, inv);
    return errors;
  }

  // ------------------------------------------------------------------ INVENTORY & EXPIRY
  function renderBuckets(ex) {
    const far = ex.buckets.find((r) => r.order === 5);
    const rows = ex.buckets.filter((r) => r.order <= 4);      // focus on units expiring within 14 days
    $('bucket-note').textContent = far && far.total
      ? `Units by time to expiry. ${fInt(far.total)} units with more than 14 days left are not shown`
      : 'Units by time to expiry';
    upsertChart('buckets', 'c-buckets', {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.bucket),
        datasets: ['RBC', 'PLATELETS', 'PLASMA'].map((c) => ({ label: COMP_LABEL[c], data: rows.map((r) => r[c]), backgroundColor: COMP_COLOR[c], borderWidth: 0 })),
      },
      options: baseOptions({
        scales: { x: { stacked: true, grid: { display: false }, title: { display: true, text: 'Time left before expiry' } }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Units in stock' } } },
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, boxHeight: 12 } },
          tooltip: { callbacks: { footer: (items) => `Total ${fInt(rows[items[0].dataIndex].total)} units` } } },
      }),
    });
  }

  function renderHeatmap(inv) {
    const cover = inv.cover;
    $('heat-title').textContent = `Stock cover: ${COMP_LABEL[inv.cover_component].toLowerCase()}`;
    $('heat-note').textContent = state.filters.component ? `Days of stock, ${inv.cover_window_days}-day average demand` : `Days of stock, ${inv.cover_window_days}-day average demand. Choose a component to change.`;
    const branches = [];
    const seen = new Set();
    cover.forEach((r) => { if (!seen.has(r.branch_id)) { seen.add(r.branch_id); branches.push({ id: r.branch_id, name: r.branch_name }); } });
    const groups = state.filters.blood_group ? [state.filters.blood_group] : BLOOD_GROUPS;
    const cell = {};
    cover.forEach((r) => { cell[r.branch_id + '|' + r.blood_group] = r; });
    const head = `<thead><tr><th>Branch</th>${groups.map((g) => `<th>${esc(g)}</th>`).join('')}</tr></thead>`;
    const body = branches.map((b) => `<tr><td>${esc(b.name)}</td>${groups.map((g) => {
      const r = cell[b.id + '|' + g];
      if (!r) return '<td><span class="heat-none">\u2014</span></td>';
      let cls, txt;
      if (r.stock_status === 'STOCKOUT') { cls = 'heat-out'; txt = 'Out'; }
      else if (r.cover_days == null) { cls = 'heat-none'; txt = '\u2014'; }
      else { cls = r.stock_status === 'LOW' ? 'heat-low' : r.stock_status === 'SURPLUS' ? 'heat-surplus' : 'heat-ok'; txt = r.cover_days >= 100 ? '99+' : r.cover_days.toFixed(1); }
      const tip = `${b.name}, ${g}: ${fInt(r.units_in_stock)} units in stock, demand ${r.avg_daily_demand} per day, target cover ${r.target_cover_days} days`;
      return `<td title="${esc(tip)}"><span class="${cls}">${txt}</span></td>`;
    }).join('')}</tr>`).join('');
    $('t-heat').innerHTML = head + `<tbody>${body || '<tr><td colspan="9" class="empty">No data for these filters.</td></tr>'}</tbody>`;
  }

  function renderRiskTable(ex) {
    $('risk-summary').textContent = `${fInt(ex.summary.units_at_risk)} units across ${fInt(ex.summary.risk_lines)} stock lines, ${fInt(ex.summary.expiring_within_24h)} expiring within 24 hours`;
    renderTable('t-risk', [
      { key: 'branch_name', label: 'Branch', html: (r) => esc(r.branch_name) },
      { key: 'blood_group', label: 'Group', html: (r) => `<span class="strong">${esc(r.blood_group)}</span>` },
      { key: 'component', label: 'Component', html: (r) => compTag(r.component) },
      { key: 'units_in_stock', label: 'In stock', align: 'num', html: (r) => fInt(r.units_in_stock) },
      { key: 'units_at_risk', label: 'At risk', align: 'num', html: (r) => `<span class="strong">${fInt(r.units_at_risk)}</span>` },
      { key: 'min_hours_to_expiry', label: 'First expiry in', align: 'num', html: (r) => `<span title="${esc(fDateTime(r.next_expiry))}">${fHours(r.min_hours_to_expiry)}</span>` },
      { key: 'urgency', label: 'Urgency', sort: (r) => -r.min_hours_to_expiry, html: (r) => expiryPill(r.min_hours_to_expiry) },
    ], ex.at_risk, { empty: 'No units are inside their expiry-risk window for these filters.' });
  }

  function renderInventoryTable(inv) {
    renderTable('t-inventory', [
      { key: 'branch_name', label: 'Branch', html: (r) => esc(r.branch_name) },
      { key: 'blood_group', label: 'Group', html: (r) => `<span class="strong">${esc(r.blood_group)}</span>` },
      { key: 'component', label: 'Component', html: (r) => compTag(r.component) },
      { key: 'units_in_stock', label: 'In stock', align: 'num', html: (r) => fInt(r.units_in_stock) },
      { key: 'units_at_risk', label: 'At risk', align: 'num', html: (r) => (r.units_at_risk ? `<span class="strong" style="color:var(--risk)">${fInt(r.units_at_risk)}</span>` : '0') },
      { key: 'min_hours_to_expiry', label: 'First expiry in', align: 'num', html: (r) => fHours(r.min_hours_to_expiry) },
    ], inv.rows, { max: 300, empty: 'No stock for these filters.' });
  }

  function renderTransfers(tr) {
    const showAlt = $('show-alt').checked;
    const s = tr.summary;
    $('transfer-summary').textContent = s.plan_lines
      ? `Plan: ${fInt(s.plan_units)} units in ${fInt(s.plan_lines)} moves, ordered by how soon the units expire`
      : 'No transfers needed for these filters';
    let rows = tr.recommendations;
    if (!showAlt) rows = rows.filter((r) => r.plan_units > 0);
    renderTable('t-transfers', [
      { key: 'rank', label: 'Priority', align: 'num', sortable: false, html: (r) => (r.plan_units > 0 ? r.rank : pill('Option', 'neutral')) },
      { key: 'blood_group', label: 'Blood', html: (r) => `<span class="strong">${esc(r.blood_group)}</span> ${compTag(r.component)}` },
      { key: 'from_branch', label: 'Move from', html: (r) => esc(r.from_branch) },
      { key: 'to_branch', label: 'Move to', html: (r) => esc(r.to_branch) },
      { key: 'plan_units', label: 'Units to move', align: 'num', html: (r) => (r.plan_units > 0 ? `<span class="strong">${fInt(r.plan_units)}</span>` : `<span title="Already covered by a higher-priority move">0 of ${fInt(r.suggested_units)}</span>`) },
      { key: 'hours_to_expiry', label: 'Sender units expire in', align: 'num', html: (r) => `${fHours(r.hours_to_expiry)} ${expiryPill(r.hours_to_expiry)}` },
      { key: 'receiver_stock', label: 'Receiver stock', align: 'num', html: (r) => `${fInt(r.receiver_stock)} units<span class="sub">${r.receiver_cover_days == null ? 'no recent demand' : r.receiver_cover_days + ' days of cover'}</span>` },
    ], rows, { empty: 'No transfers recommended for these filters.', rowClass: (r) => (r.plan_units > 0 ? '' : 'muted-row') });
  }

  function inventoryInsight(ex, tr) {
    const plan = tr.recommendations.filter((r) => r.plan_units > 0);
    const bid = state.filters.branch;
    const moveText = (r, dir) => `${fInt(r.plan_units)} ${r.blood_group} ${COMP_LABEL[r.component].toLowerCase()} ${dir === 'to' ? 'to ' + esc(r.to_branch) : 'from ' + esc(r.from_branch)}`;
    const sum = (rows) => rows.reduce((t, r) => t + r.plan_units, 0);

    if (bid) {                                   // one branch selected: describe its own position
      const name = esc(bName(bid));
      const out = plan.filter((r) => r.from_branch_id === bid);
      const inn = plan.filter((r) => r.to_branch_id === bid);
      const risk = ex.summary.units_at_risk;
      let s = risk > 0
        ? `<strong>${name} holds ${nUnits(risk)} inside their expiry-risk window${ex.summary.expiring_within_24h ? `, ${fInt(ex.summary.expiring_within_24h)} of them expiring within 24 hours` : ''}.</strong> `
        : `<strong>${name} has no units inside their expiry-risk window.</strong> `;
      if (out.length) s += `It should send ${nUnits(sum(out))} to other branches, starting with ${moveText(out[0], 'to')}. `;
      if (inn.length) s += `It is due to receive ${nUnits(sum(inn))}, starting with ${moveText(inn[0], 'from')}.`;
      if (!out.length && !inn.length) s += 'No transfers involve this branch.';
      return s;
    }

    const first = plan[0];
    let s = ex.summary.expiring_within_24h > 0
      ? `<strong>${nUnits(ex.summary.expiring_within_24h)} ${ex.summary.expiring_within_24h === 1 ? 'expires' : 'expire'} within 24 hours.</strong> `
      : `${nUnits(ex.summary.units_at_risk)} ${ex.summary.units_at_risk === 1 ? 'is' : 'are'} inside their expiry-risk window. `;
    if (first) {
      s += `The most urgent move: send ${fInt(first.plan_units)} ${esc(first.blood_group)} ${esc(COMP_LABEL[first.component].toLowerCase())} from ${esc(first.from_branch)} to ${esc(first.to_branch)}. ` +
        `The sender's units expire in ${fHours(first.hours_to_expiry)}, and the receiver holds ${nUnits(first.receiver_stock)}.`;
    } else {
      s += 'No inter-branch transfer is recommended for the selected filters.';
    }
    return s;
  }

  async function loadInventory(refresh) {
    const f = F();
    const { values: [inv, ex, tr], errors } = await settle([
      api('inventory', f, refresh), api('expiry-risk', f, refresh), api('transfer-recommendations', Object.assign({}, f, { limit: 100 }), refresh),
    ]);
    if (tr) tr.recommendations.forEach((r, i, a) => { r.rank = a.slice(0, i + 1).filter((x) => x.plan_units > 0).length; });
    if (tr) { state.lastTransfers = tr; renderTransfers(tr); }
    if (inv) { renderHeatmap(inv); renderInventoryTable(inv); }
    if (ex) { renderBuckets(ex); renderRiskTable(ex); }
    if (ex && tr) $('insight-inventory').innerHTML = inventoryInsight(ex, tr);
    return errors;
  }

  // ------------------------------------------------------------------ EMERGENCY & DEMAND
  function renderRequestKpis(rq) {
    const s = rq.summary;
    const defs = [
      { label: 'Requests', v: fInt(s.requests), sub: `last ${rq.period_days} days`, tone: '' },
      { label: 'Emergency', v: fInt(s.emergency), sub: `${fPct(s.requests ? 100 * s.emergency / s.requests : null)} of requests`, tone: '' },
      { label: 'Fulfilment rate', v: fPct(s.fulfilment_rate_pct), sub: `Target ${TARGETS.fulfilmentMin}% or more`, tone: s.fulfilment_rate_pct == null ? '' : s.fulfilment_rate_pct >= TARGETS.fulfilmentMin ? 'good' : s.fulfilment_rate_pct >= TARGETS.fulfilmentMin - 5 ? 'warn' : 'bad' },
      { label: 'Unmet requests', v: fInt(s.unmet), sub: `${fInt(s.unfulfilled)} unmet, ${fInt(s.partial)} partly met`, tone: s.unmet ? 'bad' : 'good' },
      { label: 'Emergency response', v: fMin(s.avg_emergency_response_min), sub: `Target ${TARGETS.emergencyMinutesMax} min or less`, tone: s.avg_emergency_response_min == null ? '' : s.avg_emergency_response_min <= TARGETS.emergencyMinutesMax ? 'good' : 'warn' },
      { label: 'Supplied by another branch', v: fPct(s.cross_branch_pct), sub: 'Slower than local supply', tone: '' },
    ];
    const band = $('req-kpis');
    if (band.children.length !== defs.length) {
      band.innerHTML = defs.map((d, i) => `<div class="kpi" id="rk-${i}"><div class="kpi-label"></div><div class="kpi-value">\u2014</div><div class="kpi-sub">&nbsp;</div></div>`).join('');
    }
    defs.forEach((d, i) => {
      const el = $('rk-' + i);
      el.className = `kpi ${d.tone}`.trim();
      flashIfChanged('rk-' + i, el, d.v);
      el.querySelector('.kpi-label').textContent = d.label;
      el.querySelector('.kpi-value').textContent = d.v;
      el.querySelector('.kpi-sub').textContent = d.sub;
    });
    const scope = [];
    if (state.filters.branch) scope.push(bName(state.filters.branch));
    if (state.filters.blood_group) scope.push(state.filters.blood_group);
    if (state.filters.component) scope.push(COMP_LABEL[state.filters.component]);
    $('req-title').textContent = 'Hospital requests' + (scope.length ? ` (${scope.join(', ')})` : '');
  }

  function renderRequestCharts(rq) {
    const u = rq.by_urgency;
    upsertChart('resp', 'c-resp', {
      type: 'bar',
      data: {
        labels: u.map((r) => ({ EMERGENCY: 'Emergency', URGENT: 'Urgent', ROUTINE: 'Routine' }[r.urgency] || r.urgency)),
        datasets: [{ label: 'Average minutes', data: u.map((r) => (r.avg_response_min == null ? null : Math.round(r.avg_response_min))), backgroundColor: u.map((r) => ({ EMERGENCY: C.risk, URGENT: C.amber, ROUTINE: C.slate }[r.urgency] || C.slate)), borderWidth: 0, maxBarThickness: 64 }],
      },
      options: baseOptions({
        layout: { padding: { top: 18 } },
        scales: { x: { grid: { display: false } }, y: { beginAtZero: true, title: { display: true, text: 'Minutes' } } },
        plugins: { legend: { display: false }, valueLabels: { enabled: true },
          tooltip: { callbacks: { footer: (items) => { const r = u[items[0].dataIndex]; return `${fInt(r.requests)} requests, ${fPct(r.fulfilment_rate_pct)} met in full`; } } } },
      }),
    });

    const g = rq.by_blood_group;
    upsertChart('reqbg', 'c-reqbg', {
      type: 'bar',
      data: {
        labels: g.map((r) => r.blood_group),
        datasets: [
          { label: 'Met in full', data: g.map((r) => r.fulfilled), backgroundColor: C.teal, borderWidth: 0 },
          { label: 'Partly met', data: g.map((r) => r.partial), backgroundColor: C.amber, borderWidth: 0 },
          { label: 'Unmet', data: g.map((r) => r.unfulfilled), backgroundColor: C.risk, borderWidth: 0 },
        ],
      },
      options: baseOptions({
        scales: { x: { stacked: true, grid: { display: false } }, y: { stacked: true, beginAtZero: true, title: { display: true, text: 'Requests' } } },
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 12, boxHeight: 12 } },
          tooltip: { callbacks: { footer: (items) => { const r = g[items[0].dataIndex]; return `${fPct(r.requests ? 100 * (r.partial + r.unfulfilled) / r.requests : null)} not met in full`; } } } },
      }),
    });

    const d = rq.daily;
    upsertChart('reqdaily', 'c-reqdaily', {
      type: 'bar',
      data: {
        labels: d.map((r) => fDay(r.day)),
        datasets: [
          { type: 'bar', label: 'All requests', data: d.map((r) => r.requests), backgroundColor: C.light, borderWidth: 0, order: 3 },
          { type: 'line', label: 'Emergency', data: d.map((r) => r.emergency), borderColor: C.navy, backgroundColor: C.navy, pointRadius: 0, borderWidth: 2, tension: 0.25, order: 2 },
          { type: 'line', label: 'Unmet', data: d.map((r) => r.unmet), borderColor: C.risk, backgroundColor: C.risk, pointRadius: 0, borderWidth: 2, tension: 0.25, order: 1 },
        ],
      },
      options: baseOptions({
        scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8, maxRotation: 0 } }, y: { beginAtZero: true, title: { display: true, text: 'Requests per day' } } },
      }),
    });
  }

  function renderRequestTable(rq) {
    renderTable('t-requests', [
      { key: 'requested_at', label: 'Received', html: (r) => fDateTime(r.requested_at) },
      { key: 'hospital_name', label: 'Hospital', html: (r) => `${esc(r.hospital_name)}<span class="sub">Received by ${esc(r.branch_name)}</span>` },
      { key: 'blood_group', label: 'Blood', html: (r) => `<span class="strong">${esc(r.blood_group)}</span> ${compTag(r.component)}` },
      { key: 'units_requested', label: 'Units', align: 'num', html: (r) => `${fInt(r.units_fulfilled)} of ${fInt(r.units_requested)}` },
      { key: 'urgency', label: 'Urgency', html: (r) => urgencyPill(r.urgency) },
      { key: 'status', label: 'Outcome', html: (r) => statusPill(r.status) },
      { key: 'response_minutes', label: 'Response', align: 'num', html: (r) => `${fMin(r.response_minutes)}<span class="sub">${r.status === 'UNFULFILLED' ? 'No stock found' : r.cross_branch ? 'From ' + esc(r.fulfilled_by_branch) : 'Local stock'}</span>` },
    ], rq.recent, { empty: 'No requests match these filters.' });
  }

  const FEED_TYPES = {
    DONATION: { label: 'Donation', color: C.teal },
    REQUEST: { label: 'Hospital request', color: C.amber },
    ISSUED: { label: 'Units issued', color: C.blue },
    EXPIRED: { label: 'Units expired', color: C.risk },
  };

  function renderFeed(feed) {
    const list = $('feed-list');
    if (!feed.events.length) { list.innerHTML = '<li><span></span><span class="empty">No activity for this branch yet.</span></li>'; return; }
    const key = (e) => e.event_time + '|' + e.detail;
    const seen = state.feedSeen;
    list.innerHTML = feed.events.map((e) => {
      const t = FEED_TYPES[e.event_type] || { label: e.event_type, color: C.slate };
      return `<li${seen && !seen.has(key(e)) ? ' class="new"' : ''}><i class="fdot" style="background:${t.color}"></i><div><div class="ftitle">${esc(t.label)} at ${esc(e.branch_name || e.branch_id)}` +
        `${e.blood_group ? ' <span class="pill neutral">' + esc(e.blood_group) + ' ' + esc(COMP_LABEL[e.component] || e.component || '') + '</span>' : ''}</div>` +
        `<div class="fdetail">${esc(e.detail)}</div></div><time datetime="${esc(e.event_time)}" title="${esc(fDateTime(e.event_time))}">${fClock(e.event_time)}</time></li>`;
    }).join('');
    state.feedSeen = new Set(feed.events.map(key));
    $('feed-note').textContent = `Latest ${feed.events.length} events`;
  }

  function emergencyInsight(rq) {
    const s = rq.summary;
    if (!s.requests) return 'There are no hospital requests for the selected filters.';
    const worst = rq.by_blood_group.slice().sort((a, b) => (b.partial + b.unfulfilled) - (a.partial + a.unfulfilled))[0];
    let text = `<strong>${fInt(s.unmet)} of ${fInt(s.requests)} requests (${fPct(100 * s.unmet / s.requests)}) were not met in full.</strong>`;
    if (worst && worst.partial + worst.unfulfilled > 0) text += ` ${esc(worst.blood_group)} is the most affected group (${fInt(worst.partial + worst.unfulfilled)}).`;
    if (s.avg_emergency_response_min != null) text += ` Emergency requests wait ${fMin(s.avg_emergency_response_min)} on average, against a ${TARGETS.emergencyMinutesMax} minute target.`;
    return text;
  }

  async function loadEmergency(refresh) {
    const f = F();
    const { values: [rq, feed], errors } = await settle([
      api('requests', Object.assign({}, f, { urgency: $('f-urgency').value, status: $('f-status').value, limit: 40 }), refresh),
      api('live-feed', { branch: f.branch, limit: 40 }, refresh),
    ]);
    if (rq) { renderRequestKpis(rq); renderRequestCharts(rq); renderRequestTable(rq); $('insight-emergency').innerHTML = emergencyInsight(rq); }
    if (feed) renderFeed(feed);
    return errors;
  }

  async function refreshFeedOnly() {
    try { renderFeed(await api('live-feed', { branch: state.filters.branch, limit: 40 })); } catch (e) { /* the next full refresh reports errors */ }
  }

  // ------------------------------------------------------------------ health / status
  function paintAsof() {
    const el = $('data-asof');
    const t = state.lastActivity;
    if (!t) { el.textContent = ''; return; }
    const secs = (Date.now() - t) / 1000;
    const stale = secs > STALE_HOURS * 3600;
    el.classList.toggle('live', secs < 120);
    el.classList.toggle('stale', stale);
    if (secs < 120) {
      el.textContent = 'Latest activity ' + (secs < 5 ? 'just now' : Math.round(secs) + ' seconds ago');
    } else {
      const age = secs >= 172800 ? Math.round(secs / 86400) + ' days' : Math.round(secs / 3600) + ' hours';
      el.textContent = 'Latest record ' + fDateTime(new Date(t).toISOString()) + (stale ? ', ' + age + ' old' : '');
    }
  }

  async function loadHealth(refresh) {
    const pillEl = $('status-pill');
    try {
      const h = await api('health', {}, refresh);
      pillEl.dataset.state = 'ok';
      $('status-text').textContent = 'Connected to Neon';
      const latest = [h.last_collection, h.last_request, h.last_activity].filter(Boolean).map((x) => new Date(x).getTime()).sort((a, b) => a - b).pop();
      state.lastActivity = latest || null;
      paintAsof();
      $('foot-source').textContent = `Source: Neon PostgreSQL. ${fInt(h.units)} blood units, ${fInt(h.requests)} hospital requests and ${fInt(h.donor_records)} donor records. Last refreshed ${fClock(h.generated_at)}.`;
    } catch (e) {
      pillEl.dataset.state = 'error';
      $('status-text').textContent = 'Database unreachable';
    }
  }

  // ------------------------------------------------------------------ orchestration
  const LOADERS = { overview: loadOverview, inventory: loadInventory, emergency: loadEmergency };

  async function loadActive(refresh) {
    if (state.busy) return;
    state.busy = true;
    const btn = $('btn-refresh');
    btn.disabled = true;
    btn.textContent = 'Refreshing';
    try {
      const [errors] = await Promise.all([LOADERS[state.tab](refresh), loadHealth(refresh)]);
      showErrors(errors);
      if (!errors.length) state.loaded[state.tab] = true;
    } finally {
      state.busy = false;
      btn.disabled = false;
      btn.textContent = 'Refresh data';
    }
  }

  function selectTab(name) {
    state.tab = name;
    state.prev = {};
    state.feedSeen = null;
    document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
    document.querySelectorAll('.page').forEach((p) => { p.hidden = p.id !== 'page-' + name; });
    if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
    // charts drawn while hidden have zero size: resize after the page becomes visible
    requestAnimationFrame(() => Object.values(state.charts).forEach((c) => c.resize()));
    if (!state.loaded[name]) loadActive(false);
  }

  function startTimers() {
    state.timers.forEach(clearInterval);
    state.timers = [];
    if (!state.auto) return;
    state.timers.push(setInterval(() => { if (!document.hidden) loadActive(false); }, AUTO_REFRESH_MS));
    state.timers.push(setInterval(() => { if (!document.hidden && state.tab === 'emergency' && !state.busy) refreshFeedOnly(); }, FEED_REFRESH_MS));
  }

  function fillSelect(el, items) {
    items.forEach(([value, label]) => { const o = document.createElement('option'); o.value = value; o.textContent = label; el.appendChild(o); });
  }

  function onFilterChange() {
    state.filters = { branch: $('f-branch').value, blood_group: $('f-bg').value, component: $('f-comp').value, days: $('f-days').value };
    state.loaded = {};
    state.prev = {};
    state.feedSeen = null;
    loadActive(false);
  }

  async function init() {
    if (typeof Chart === 'undefined') {
      showErrors(['The chart library did not load. Check that static/vendor/chart.umd.js exists.']);
      return;
    }
    fillSelect($('f-bg'), BLOOD_GROUPS.map((g) => [g, g]));
    fillSelect($('f-comp'), Object.entries(COMP_LABEL));

    try {   // branch list for the filter drop-down and name look-ups
      const b = await api('branches', { days: 30 });
      state.branches = b.branches;
      b.branches.forEach((x) => { state.branchName[x.branch_id] = x.branch_name; });
      fillSelect($('f-branch'), b.branches.map((x) => [x.branch_id, x.branch_name]));
    } catch (e) { showErrors([e.message]); }

    ['f-branch', 'f-bg', 'f-comp', 'f-days'].forEach((id) => $(id).addEventListener('change', onFilterChange));
    ['f-urgency', 'f-status'].forEach((id) => $(id).addEventListener('change', () => loadActive(false)));
    $('btn-reset').addEventListener('click', () => {
      ['f-branch', 'f-bg', 'f-comp'].forEach((id) => { $(id).value = ''; });
      $('f-days').value = '30';
      onFilterChange();
    });
    $('btn-refresh').addEventListener('click', () => loadActive(true));
    $('auto-refresh').addEventListener('change', (e) => { state.auto = e.target.checked; startTimers(); });
    $('show-alt').addEventListener('change', () => { if (state.lastTransfers) renderTransfers(state.lastTransfers); });
    document.querySelectorAll('.tab').forEach((t) => t.addEventListener('click', () => selectTab(t.dataset.tab)));
    window.addEventListener('hashchange', () => { const n = location.hash.slice(1); if (LOADERS[n] && n !== state.tab) selectTab(n); });

    startTimers();
    setInterval(paintAsof, 1000);
    const start = LOADERS[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
    selectTab(start);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
