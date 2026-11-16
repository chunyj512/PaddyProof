/* PaddyProof MRV console: router and views. Plain JS, no build step. */
(() => {
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const t = (k, v) => window.I18N.t(k, v);
const loc = () => window.I18N.locale();
const num = (n, d = 0) => n == null ? '–' : new Intl.NumberFormat(loc(),
  {minimumFractionDigits: d, maximumFractionDigits: d}).format(n);
const pct = (a, b) => b ? num(a / b * 100, 1) + '%' : '–';
const when = ts => ts ? new Intl.DateTimeFormat(loc(), {month:'short', day:'numeric',
  hour:'2-digit', minute:'2-digit', timeZone:'UTC', hour12:false}).format(new Date(ts)) : '–';
const day = ts => new Intl.DateTimeFormat(loc(), {month:'short', day:'numeric', timeZone:'UTC'})
  .format(new Date(ts));
const country = code => { try { return new Intl.DisplayNames([loc()], {type:'region'}).of(code); }
  catch { return code; } };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

async function api(path, opts) {
  const r = await fetch('/api/console' + path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}
const post = (path, body) => api(path, {method:'POST',
  headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});

let maps = [], charts = [], toastTimer;
function cleanup() {
  maps.forEach(m => { try { m.remove(); } catch {} }); maps = [];
  charts.forEach(c => { try { c.destroy(); } catch {} }); charts = [];
}
function toast(msg, bad) {
  const el = $('#toast'); el.textContent = msg;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.className = 'toast', 4200);
}
const badge = kind => `<span class="badge ${kind}" title="${esc(t('badge.' + kind + '.tip'))}">${esc(t('badge.' + kind))}</span>`;
const tierTag = x => `<span class="tier ${x}">${esc(t('tier.' + x))}</span>`;
const verdictTxt = v => v === 'compliant' ? t('verdict.compliant') : t('verdict.non_compliant');
const tierBar = (T, n) => `<div class="tierbar" title="${T.confirmed} / ${T.provisional} / ${T.review}">${
  ['confirmed','provisional','review'].map(k => `<i class="${k}" style="width:${n ? T[k] / n * 100 : 0}%"></i>`).join('')}</div>`;
const legend = () => `<div class="legend">${['confirmed','provisional','review'].map(k =>
  `<span>${tierTag(k)} <span class="muted">· ${esc(t('tier.' + k + '.hint'))}</span></span>`).join('')}</div>`;
const TIER_COLOR = () => ({confirmed: css('--t-confirmed'), provisional: css('--t-provisional'), review: css('--t-review')});

function baseMap(id, kind) {
  const m = L.map(id, {preferCanvas: true, zoomControl: true, attributionControl: true, worldCopyJump: true});
  if (kind === 'imagery') {
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {maxZoom: 19, attribution: 'Esri World Imagery'}).addTo(m);
  } else {
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}',
      {maxZoom: 16, attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors'}).addTo(m);
  }
  maps.push(m);
  return m;
}
function chart(canvas, cfg) {
  Chart.defaults.color = css('--muted');
  Chart.defaults.borderColor = css('--line');
  Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  Chart.defaults.font.size = 11;
  Chart.defaults.animation = false;
  const c = new Chart(canvas, cfg); charts.push(c); return c;
}
const DAY = 864e5;
const timeAxis = (min, max) => ({type: 'linear', min, max, ticks: {maxTicksLimit: 7, callback: v => day(v)}, grid: {display: false}});

/* ================= Overview ================= */
async function vOverview(el) {
  const d = await api('/overview');
  const T = d.totals, n = T.plots, auto = T.tiers.confirmed + T.tiers.provisional;
  el.innerHTML = `
    <div class="view-head"><div><h2>${esc(t('ov.title'))}</h2>
      <p>${esc(t('ov.sub', {a: T.countries_active, p: T.countries_pipeline}))}</p></div></div>
    <div class="kpis">
      <div class="kpi"><div class="l">${esc(t('kpi.area'))}</div><b>${num(T.area_ha)}<small>ha</small></b>
        <div class="s">${esc(t('kpi.area.s', {n: num(n)}))}</div></div>
      <div class="kpi"><div class="l">${esc(t('kpi.auto'))}</div><b>${pct(auto, n)}</b>
        <div class="s">${esc(t('kpi.auto.s', {c: num(T.tiers.confirmed), p: num(T.tiers.provisional)}))}</div></div>
      <div class="kpi"><div class="l">${esc(t('kpi.review'))}</div><b>${num(T.review_open)}</b>
        <div class="s">${esc(t('kpi.review.s', {p: pct(T.tiers.review, n)}))}</div></div>
      <div class="kpi"><div class="l">${esc(t('kpi.anchors'))}</div><b>${T.anchors_online}<small>/ ${T.anchors}</small></b>
        <div class="s ${T.chain_alerts ? 'alert' : ''}">${esc(t('kpi.anchors.s', {n: T.chain_alerts}))}</div></div>
      <div class="kpi"><div class="l">${esc(t('kpi.er'))}</div><b>${num(T.er_confirmed, 1)}<small>tCO₂e</small></b>
        <div class="s">${esc(t('kpi.er.s', {p: num(T.er_provisional, 1)}))}</div></div>
    </div>
    <div class="cols c21">
      <div class="card"><h3>${esc(t('ov.map'))}</h3><div class="map" id="ovMap"></div>
        <div class="legend"><span>● ${esc(t('ov.active'))}</span><span>○ ${esc(t('ov.pipeline'))}:
          ${d.pipeline.map(p => esc(country(p.country))).join(' · ')}</span></div></div>
      <div class="card dist"><h3>${esc(t('ov.dist'))}</h3>
        ${d.projects.map(p => `<div class="rowx"><div class="h"><b>${esc(p.name)}</b>
            <span class="muted">${num(p.n_plots)} ${esc(t('u.plots'))}</span></div>${tierBar(p.tiers, p.n_plots)}
            <div class="muted" style="font-size:11.5px;margin-top:4px">${['confirmed','provisional','review']
              .map(k => `${esc(t('tier.' + k))} ${pct(p.tiers[k], p.n_plots)}`).join(' · ')}</div></div>`).join('')}
        ${legend()}</div>
    </div>
    <div class="cols c21">
      <div class="card"><h3>${esc(t('ov.projects'))}</h3><div style="overflow-x:auto"><table>
        <tr><th>${esc(t('c.project'))}</th><th>${esc(t('c.season'))}</th><th class="num">${esc(t('c.plots'))}</th>
          <th class="num">${esc(t('c.area'))}</th><th>${esc(t('c.tiers'))}</th><th class="num">${esc(t('c.review'))}</th>
          <th class="num">${esc(t('c.anchors'))}</th><th>${esc(t('c.data'))}</th></tr>
        ${d.projects.map(p => `<tr class="clickable" data-go="#/projects/${p.id}">
          <td><b>${esc(p.name)}</b><div class="muted" style="font-size:12px">${esc(p.region)}, ${esc(country(p.country))}</div></td>
          <td class="muted">${esc(p.season)}</td><td class="num">${num(p.n_plots)}</td>
          <td class="num">${num(p.area_ha)} ha</td><td>${tierBar(p.tiers, p.n_plots)}</td>
          <td class="num">${num(p.review_open)}</td>
          <td class="num">${p.n_anchors ? `${p.anchors_online}/${p.n_anchors}` : '–'}</td>
          <td>${badge(p.data)}</td></tr>`).join('')}
      </table></div></div>
      <div class="card"><h3>${esc(t('ov.activity'))}<span class="sp"></span>
        <a class="muted" style="font-size:11.5px;text-transform:none;letter-spacing:0" href="#/reports">${esc(t('ov.audit'))} →</a></h3>
        <ul class="feed">${d.events.slice(0, 6).map(feedItem).join('')}</ul></div>
    </div>
    <div class="card"><h3>${esc(t('bm.title'))} ${badge('sim')}</h3>
      <div style="overflow-x:auto"><table>
        <tr><th>${esc(t('bm.metric'))}</th><th class="num">${esc(t('bm.visits'))}</th>
          <th class="num">${esc(t('bm.photo'))}</th><th class="num">PaddyProof</th></tr>
        ${d.benchmarks.rows.map(r => { const u = r.key === 'hours' ? ' h' : '%';
          const f = v => v == null ? '–' : num(v, r.key === 'hours' ? 0 : 1) + u;
          return `<tr><td>${esc(t('bm.' + r.key))}</td><td class="num">${f(r.visits)}</td>
            <td class="num">${f(r.photo)}</td><td class="num"><b>${f(r.pp)}</b></td></tr>`; }).join('')}
      </table></div><p class="hint">${esc(t('bm.note'))}</p></div>`;
  bindGo(el);

  const m = baseMap('ovMap');
  const pts = [];
  d.projects.forEach(p => {
    pts.push([p.lat, p.lon]);
    L.circleMarker([p.lat, p.lon], {radius: 9 + Math.sqrt(p.area_ha) / 2.2, color: '#fff', weight: 2,
      fillColor: css('--brand'), fillOpacity: .9}).addTo(m)
      .bindTooltip(`<b>${esc(p.name)}</b><br>${esc(country(p.country))} · ${num(p.n_plots)} ${esc(t('u.plots'))} · ${num(p.area_ha)} ha`,
        {direction: 'top', offset: [0, -8]})
      .on('click', () => location.hash = '#/projects/' + p.id);
  });
  d.pipeline.forEach(p => {
    pts.push([p.lat, p.lon]);
    L.circleMarker([p.lat, p.lon], {radius: 7, color: css('--brand'), weight: 2, dashArray: '3 3',
      fillColor: '#fff', fillOpacity: .7}).addTo(m)
      .bindTooltip(`${esc(country(p.country))} · ${esc(t('ov.pipeline'))}`, {direction: 'top'});
  });
  m.fitBounds(pts, {padding: [40, 40]});
}
const EV_CLASS = {chain_broken:'d-bad', record_edited:'d-bad', anchor_offline:'d-warn', seq_conflict:'d-warn',
  chain_gap:'d-warn', review_resolved:'d-good', chain_verified:'d-good'};
const feedItem = e => `<li><i class="dot ${EV_CLASS[e.action] || ''}"></i>
  <div><b>${esc(t('ev.' + e.action))}</b> <span class="muted">· ${esc(e.target)}</span></div>
  <div class="muted">${esc(e.detail)} <time>· ${when(e.ts)}</time></div></li>`;
function bindGo(el) {
  el.querySelectorAll('[data-go]').forEach(x => x.onclick = () => location.hash = x.dataset.go);
}

/* ================= Project ================= */
async function vProject(el, pid) {
  const d = await api('/projects/' + pid);
  const P = d.project, n = P.n_plots;
  const f = {tier: 'all', q: ''};
  el.innerHTML = `
    <div class="view-head"><div><a class="back" href="#/overview">← ${esc(t('ov.title'))}</a>
      <h2>${esc(P.name)}</h2><p>${esc(P.region)}, ${esc(country(P.country))} · ${esc(P.season)} · ${esc(P.programme)}</p></div>
      <span class="sp"></span>${badge(P.data)}
      <a class="ghost" href="#/reports/${P.id}">${esc(t('pj.report'))}</a></div>
    <div class="kpis">
      <div class="kpi"><div class="l">${esc(t('c.plots'))}</div><b>${num(n)}</b><div class="s">${num(P.area_ha)} ha</div></div>
      <div class="kpi"><div class="l">${esc(t('tier.confirmed'))}</div><b>${pct(P.tiers.confirmed, n)}</b>
        <div class="s">${num(P.tiers.confirmed)} ${esc(t('u.plots'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('tier.review'))}</div><b>${pct(P.tiers.review, n)}</b>
        <div class="s">${num(P.tiers.review)} ${esc(t('u.plots'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('pj.obs'))}</div><b>${num(P.mean_obs, 1)}</b>
        <div class="s">${esc(P.window[0])} → ${esc(P.window[1])}</div></div>
      <div class="kpi"><div class="l">${esc(t('c.anchors'))}</div><b>${P.n_anchors ? `${P.anchors_online}<small>/ ${P.n_anchors}</small>` : '0'}</b>
        <div class="s ${P.chain_alerts ? 'alert' : ''}">${P.n_anchors ? esc(t('kpi.anchors.s', {n: P.chain_alerts})) : esc(t('pj.noanchors'))}</div></div>
    </div>
    <div class="note" style="margin-bottom:14px"><b>${esc(t('pj.rule'))}:</b> ${esc(t('rule.' + P.rule))}
      ${P.data === 'measured' ? `<br>${esc(t('pj.measured.note'))}` : ''}</div>
    <div class="cols c11">
      <div class="card"><h3>${esc(t('pj.map'))}</h3><div class="map tall" id="pjMap"></div>${legend()}</div>
      <div class="card"><h3>${esc(t('pj.plots'))}</h3>
        <div class="toolbar"><div class="seg" id="pjSeg">${['all','confirmed','provisional','review'].map(k =>
          `<button data-k="${k}" class="${k === 'all' ? 'on' : ''}">${esc(k === 'all' ? t('f.all') : t('tier.' + k))}</button>`).join('')}</div>
          <input id="pjQ" placeholder="${esc(t('f.search'))}"></div>
        <div class="scroll" style="max-height:472px"><table id="pjTable"></table></div>
        <p class="hint" id="pjCount"></p></div>
    </div>`;

  const colors = TIER_COLOR();
  const m = baseMap('pjMap', 'imagery');
  const markers = {};
  d.plots.forEach(p => {
    markers[p.id] = L.circleMarker([p.lat, p.lon], {radius: 5, weight: 1, color: '#fff',
      fillColor: colors[p.tier], fillOpacity: .95}).addTo(m)
      .bindTooltip(`${esc(p.name)} · ${esc(t('tier.' + p.tier))}`)
      .on('click', () => openPlot(p.id));
  });
  d.anchors.forEach(a => {
    L.marker([a.lat, a.lon], {icon: L.divIcon({className: '', iconSize: [16, 16],
      html: `<div style="width:16px;height:16px;border-radius:3px;background:#fff;border:3px solid ${
        a.chain.state === 'BROKEN' ? colors.review : '#0f2a1e'};box-shadow:0 0 0 1px #fff"></div>`})}).addTo(m)
      .bindTooltip(`${esc(a.id)} · ${esc(t('an.anchor'))}`)
      .on('click', () => location.hash = '#/anchors/' + a.id);
  });
  m.fitBounds(d.plots.map(p => [p.lat, p.lon]), {padding: [24, 24]});

  const awd = P.rule === 'awd2';
  function rows() {
    const q = f.q.toLowerCase();
    const list = d.plots.filter(p => (f.tier === 'all' || p.tier === f.tier) &&
      (!q || p.name.toLowerCase().includes(q)));
    $('#pjTable').innerHTML = `<tr><th>${esc(t('c.plot'))}</th><th>${esc(t('c.tier'))}</th><th>${esc(t('c.verdict'))}</th>
      <th class="num">${esc(t('c.obs'))}</th><th class="num">${esc(awd ? t('c.cycles') : t('c.longest'))}</th>
      <th>${esc(t('c.why'))}</th></tr>` + list.slice(0, 300).map(p => `<tr class="clickable" data-id="${p.id}">
        <td><b>${esc(p.name)}</b>${p.anchor_id ? ' <span class="chip">⚓</span>' : ''}</td><td>${tierTag(p.tier)}</td>
        <td>${esc(verdictTxt(p.verdict))}</td><td class="num">${p.n_obs}</td>
        <td class="num">${awd ? p.drain_cycles : p.longest_drain_days + ' d'}</td>
        <td>${p.reasons.map(r => `<span class="chip">${esc(t('reason.' + r + '.s'))}</span>`).join('')}</td></tr>`).join('');
    $('#pjCount').textContent = t('f.showing', {a: Math.min(300, list.length), b: list.length});
    $('#pjTable').querySelectorAll('tr[data-id]').forEach(tr => tr.onclick = () => openPlot(tr.dataset.id));
  }
  $('#pjSeg').querySelectorAll('button').forEach(b => b.onclick = () => {
    f.tier = b.dataset.k;
    $('#pjSeg').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
    rows();
  });
  $('#pjQ').oninput = e => { f.q = e.target.value; rows(); };
  rows();
}

/* ---------- plot evidence drawer ---------- */
let drawerCharts = [];
function closeDrawer() {
  $('#drawer').classList.remove('open');
  drawerCharts.forEach(c => { try { c.destroy(); } catch {} }); drawerCharts = [];
}
async function openPlot(id) {
  const d = await api('/plots/' + id);
  const p = d.plot, s = d.series;
  drawerCharts.forEach(c => { try { c.destroy(); } catch {} }); drawerCharts = [];
  $('#drawerTitle').innerHTML = `${esc(p.name)} &nbsp;${tierTag(p.tier)}`;
  const ni = d.nisar;
  $('#drawerBody').innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">${badge(p.data)}
      <span class="muted">${p.lat.toFixed(5)}, ${p.lon.toFixed(5)} · ${p.area_ha} ha</span></div>
    <div class="facts">
      <div>${esc(t('c.verdict'))}<b>${esc(verdictTxt(p.verdict))}${p.field_verified ? ' ✓' : ''}</b></div>
      <div>${esc(t('c.obs'))}<b>${p.n_obs}</b></div>
      <div>${esc(t('c.longest'))}<b>${p.longest_drain_days} d</b></div>
      <div>${esc(t('c.cycles'))}<b>${p.drain_cycles}</b></div>
      <div>${esc(t('dr.margin'))}<b>${num(p.margin_db, 2)} dB</b></div>
      <div>${esc(t('dr.last'))}<b>${esc(p.last_obs || '–')}</b></div>
    </div>
    ${p.field_verified ? `<div class="note good">${esc(t('dr.field'))}</div>` : ''}
    ${p.reasons.length ? `<div class="note warnc"><b>${esc(t('dr.why'))}</b><ul style="margin:6px 0 0;padding-left:18px">${
      p.reasons.map(r => `<li>${esc(t('reason.' + r))}</li>`).join('')}</ul></div>` : ''}
    <h4>${esc(t('dr.sat'))}</h4>
    <div class="chartbox"><canvas id="cSat"></canvas></div>
    <p class="hint">${esc(t('dr.sat.hint', {th: d.threshold_db}))}</p>
    ${ni && ni.rows && ni.rows.length ? `<h4>${esc(t('dr.nisar'))}</h4>
      <div class="note ${ni.verdict === 's1_overestimate' ? 'warnc' : ''}"><b>${esc(t('nisar.' + ni.verdict))}</b>
        ${ni.hh_diff != null ? ` — ${esc(t('dr.nisar.diff', {d: num(ni.hh_drained, 2), p: num(ni.hh_ponded, 2),
          x: (ni.hh_diff > 0 ? '+' : '') + num(ni.hh_diff, 2)}))}` : ''}</div>` : ''}
    ${d.anchor ? `<h4>${esc(t('dr.anchor'))} · ${esc(d.anchor.id)}</h4>
      <div class="chartbox sm"><canvas id="cAnchor"></canvas></div>
      <p class="hint">${esc(t('dr.anchor.hint', {n: d.anchor.sensor_cycles}))}
        <span class="state ${d.anchor.chain.state}">${d.anchor.chain.state}</span>
        <a href="#/anchors/${d.anchor.id}">${esc(t('dr.anchor.open'))} →</a></p>` : ''}
    <h4>${esc(t('dr.hash'))}</h4>
    <div class="mono hashline">${esc(p.evidence_sha256)}</div>
    <div style="display:flex;gap:8px;margin-top:18px;flex-wrap:wrap">
      <button class="ghost" id="drLive">${esc(t('dr.live'))}</button>
      ${p.tier === 'review' ? `<a class="ghost" href="#/review/${p.id}">${esc(t('dr.toreview'))} →</a>` : ''}
    </div>`;
  $('#drawer').classList.add('open');
  $('#drLive').onclick = () => { closeDrawer(); window.openAnalysis(p.lat, p.lon); };

  const cw = css('--water'), cd = css('--drain');
  const ds = [
    {type: 'line', label: t('dr.l.smooth'), data: s.map(r => ({x: +new Date(r.date), y: r.vh_smooth})),
      borderColor: css('--ink'), borderWidth: 1.5, pointRadius: 0, tension: .25},
    {type: 'scatter', label: t('dr.l.flooded'), data: s.filter(r => r.state === 'flooded').map(r => ({x: +new Date(r.date), y: r.vh_db})),
      backgroundColor: cw, pointRadius: 4},
    {type: 'scatter', label: t('dr.l.drained'), data: s.filter(r => r.state === 'drained').map(r => ({x: +new Date(r.date), y: r.vh_db})),
      backgroundColor: cd, pointRadius: 4},
  ];
  let x0 = +new Date(s[0].date), x1 = +new Date(s[s.length - 1].date);
  if (ni && ni.rows && ni.rows.length) {
    ds.push({type: 'scatter', label: 'NISAR HH', data: ni.rows.map(r => ({x: +new Date(r.date), y: r.hh_db})),
      backgroundColor: '#1baf7a', pointStyle: 'triangle', pointRadius: 5});
    x1 = Math.max(x1, +new Date(ni.rows[ni.rows.length - 1].date));
  }
  ds.push({type: 'line', label: t('dr.l.threshold', {th: d.threshold_db}), data: [{x: x0, y: d.threshold_db}, {x: x1, y: d.threshold_db}],
    borderColor: '#c62828', borderDash: [5, 4], borderWidth: 1.2, pointRadius: 0});
  drawerCharts.push(new Chart($('#cSat'), {data: {datasets: ds}, options: {maintainAspectRatio: false, animation: false,
    scales: {x: timeAxis(x0 - 3 * DAY, x1 + 3 * DAY), y: {title: {display: true, text: 'dB'}}},
    plugins: {legend: {labels: {boxWidth: 10, usePointStyle: true}}}}}));
  if (d.anchor) {
    const a = d.anchor_daily;
    const line = y => [{x: +new Date(a[0].date), y}, {x: +new Date(a[a.length - 1].date), y}];
    drawerCharts.push(new Chart($('#cAnchor'), {type: 'line', data: {datasets: [
      {label: t('an.level'), data: a.map(r => ({x: +new Date(r.date), y: r.level_cm})), borderColor: cw,
        backgroundColor: 'rgba(21,101,192,.12)', fill: 'origin', borderWidth: 1.5, pointRadius: 0},
      {label: '−15 cm', data: line(-15), borderColor: cd, borderDash: [4, 4], borderWidth: 1, pointRadius: 0},
    ]}, options: {maintainAspectRatio: false, animation: false, scales: {x: timeAxis(+new Date(a[0].date), +new Date(a[a.length - 1].date)), y: {title: {display: true, text: 'cm'}}},
      plugins: {legend: {labels: {boxWidth: 10}}}}}));
  }
}

/* ================= Anchors ================= */
async function vAnchors(el, sel, opts = {}) {
  const d = await api('/anchors');
  const A = d.anchors;
  if (!sel) sel = (A.find(a => a.chain.state !== 'OK') || A[0]).id;
  const okN = A.filter(a => a.chain.state === 'OK').length;
  el.innerHTML = `
    <div class="view-head"><div><h2>${esc(t('an.title'))}</h2><p>${esc(t('an.sub'))}</p></div>
      <span class="sp"></span>${badge('demo')}<button class="ghost" id="anReset">${esc(t('an.reset'))}</button></div>
    <div class="kpis four">
      <div class="kpi"><div class="l">${esc(t('an.k.total'))}</div><b>${A.length}</b><div class="s">${esc(t('an.k.total.s'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('an.k.online'))}</div><b>${A.filter(a => a.status === 'online').length}<small>/ ${A.length}</small></b>
        <div class="s">${esc(t('an.k.online.s'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('an.k.ok'))}</div><b>${okN}<small>/ ${A.length}</small></b>
        <div class="s">HMAC-SHA256 · seq</div></div>
      <div class="kpi"><div class="l">${esc(t('an.k.records'))}</div><b>${num(A.reduce((s, a) => s + a.chain.records, 0))}</b>
        <div class="s">${esc(t('an.k.records.s'))}</div></div>
    </div>
    <div class="cols c12">
      <div class="card"><h3>${esc(t('an.list'))}</h3><div class="scroll" style="max-height:640px"><table>
        <tr><th>${esc(t('an.anchor'))}</th><th>${esc(t('c.plot'))}</th><th>${esc(t('an.status'))}</th>
          <th class="num">${esc(t('an.batt'))}</th><th>${esc(t('an.chain'))}</th></tr>
        ${A.map(a => `<tr class="clickable ${a.id === sel ? 'sel' : ''}" data-go="#/anchors/${a.id}">
          <td><b>${esc(a.id)}</b><div class="muted" style="font-size:11.5px">${when(a.last_seen)}</div></td>
          <td>${esc(a.plot_name)}</td><td><span class="state ${a.status}">${esc(t('st.' + a.status))}</span></td>
          <td class="num">${a.batt_pct}%</td><td><span class="state ${a.chain.state}">${a.chain.state}</span></td></tr>`).join('')}
      </table></div></div>
      <div class="card" id="anDetail"><div class="skeleton">…</div></div>
    </div>`;
  bindGo(el);
  $('#anReset').onclick = async () => { await post('/demo/reset'); toast(t('an.reset.done')); render(); };
  await anchorDetail(sel, opts.pulse);
}
async function anchorDetail(id, pulse) {
  const d = await api('/anchors/' + id);
  const a = d.anchor, c = a.chain, box = $('#anDetail');
  const cls = {OK: 'good', GAP: 'warnc', SEQ_CONFLICT: 'warnc', BROKEN: 'bad'}[c.state];
  const msg = {OK: t('ch.ok', {n: num(c.records)}), GAP: t('ch.gap', {n: c.missing}),
    SEQ_CONFLICT: t('ch.conflict', {n: c.SEQ_CONFLICT}),
    BROKEN: t('ch.broken', {s: c.first_break_seq, n: num(c.after_break), p: a.plot_name})}[c.state];
  let broken = false;
  const cells = d.days.map(x => { const k = x.status === 'BROKEN' ? 'BROKEN' : broken ? 'after' : x.status;
    if (x.status === 'BROKEN') broken = true;
    return `<i class="${k}" title="${x.date} · ${x.status === 'OK' && k === 'after' ? t('ch.after') : x.status}"></i>`; }).join('');
  const recRow = r => `<tr><td class="num">${r.seq}</td><td>${when(r.ts)}</td><td class="num">${(r.level_mm / 10).toFixed(1)}</td>
    <td class="mono">${r.hash.slice(0, 14)}…</td><td><span class="state ${r.status}">${r.status}</span></td></tr>`;
  box.innerHTML = `
    <h3>${esc(a.id)} · ${esc(a.plot_name)}<span class="sp"></span><span class="state ${a.status}">${esc(t('st.' + a.status))}</span></h3>
    <div class="facts" style="grid-template-columns:repeat(4,1fr)">
      <div>${esc(t('an.level'))}<b>${(a.level_mm / 10).toFixed(1)} cm</b></div>
      <div>${esc(t('an.batt'))}<b>${a.batt_pct}% <small class="muted">${(a.batt_mv / 1000).toFixed(2)} V</small></b></div>
      <div>${esc(t('an.last'))}<b style="font-size:13px">${when(a.last_seen)}</b></div>
      <div>${esc(t('an.fw'))}<b style="font-size:13px">${esc(a.firmware)} · ${esc(a.comms)} · ${a.interval_min} min</b></div>
    </div>
    <div class="chartbox lg"><canvas id="cLevel"></canvas></div>
    <h3 style="margin-top:18px">${esc(t('an.integrity'))}</h3>
    <div class="note ${cls} ${pulse ? 'pulse' : ''}" id="chNote"><b>${c.state}</b> — ${esc(msg)}</div>
    <div class="strip">${cells}</div>
    <p class="hint">${esc(t('ch.strip'))} · ${esc(t('ch.head'))} <span class="mono">${c.head.slice(0, 20)}…</span></p>
    ${d.flagged.length ? `<div style="overflow-x:auto"><table><tr><th class="num">seq</th><th>${esc(t('an.time'))}</th>
      <th class="num">cm</th><th>hash</th><th>${esc(t('an.chain'))}</th></tr>${d.flagged.slice(0, 5).map(recRow).join('')}</table></div>` : ''}
    <details style="margin-top:10px"><summary class="muted" style="cursor:pointer;font-size:12.5px">${esc(t('an.latest'))}</summary>
      <div style="overflow-x:auto"><table><tr><th class="num">seq</th><th>${esc(t('an.time'))}</th><th class="num">cm</th>
        <th>hash</th><th>${esc(t('an.chain'))}</th></tr>${d.tail.slice().reverse().map(recRow).join('')}</table></div></details>
    <div style="display:flex;gap:10px;align-items:center;margin-top:16px;flex-wrap:wrap">
      <button class="danger" id="anTamper" ${a.tampered ? 'disabled' : ''}>${esc(t('an.tamper'))}</button>
      <span class="hint" style="margin:0">${esc(t('an.tamper.hint'))}</span></div>`;
  const bad = d.series.filter(r => r.status !== 'OK');
  chart($('#cLevel'), {data: {datasets: [
    {type: 'line', label: t('an.level') + ' (cm)', data: d.series.map(r => ({x: +new Date(r.ts), y: r.level_mm / 10})),
      borderColor: css('--water'), backgroundColor: 'rgba(21,101,192,.10)', fill: 'origin', borderWidth: 1.2, pointRadius: 0},
    {type: 'scatter', label: t('an.flagged'), data: bad.map(r => ({x: +new Date(r.ts), y: r.level_mm / 10})),
      backgroundColor: css('--t-review'), pointRadius: 6, pointStyle: 'crossRot', borderColor: css('--t-review'), borderWidth: 2.5},
  ]}, options: {maintainAspectRatio: false, scales: {x: timeAxis(+new Date(d.series[0].ts), +new Date(d.series[d.series.length - 1].ts)), y: {title: {display: true, text: 'cm'}}},
    plugins: {legend: {labels: {boxWidth: 10, usePointStyle: true}}}}});
  $('#anTamper').onclick = async () => {
    try {
      const r = await post(`/anchors/${id}/tamper-demo`);
      toast(t('an.tamper.done', {s: r.seq, a: (r.before_mm / 10).toFixed(1), b: (r.after_mm / 10).toFixed(1)}), true);
      await vAnchors($('#view'), id, {pulse: true}); navCounts();
    } catch (e) { toast(e.message, true); }
  };
}

/* ================= Review queue ================= */
const rv = {status: 'open', project: 'all'};
async function vReview(el, focus) {
  const d = await api('/review');
  if (focus) { rv.status = 'all'; rv.project = 'all'; }
  const total = d.items.length, C = d.counts;
  el.innerHTML = `
    <div class="view-head"><div><h2>${esc(t('rv.title'))}</h2><p>${esc(t('rv.sub'))}</p></div></div>
    <div class="kpis four">
      <div class="kpi"><div class="l">${esc(t('st.open'))}</div><b>${num(C.open)}</b><div class="s">${esc(t('rv.k.open'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('st.assigned'))}</div><b>${num(C.assigned)}</b><div class="s">${esc(t('rv.k.assigned'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('st.resolved'))}</div><b>${num(C.resolved)}</b><div class="s">${esc(t('rv.k.resolved'))}</div></div>
      <div class="kpi"><div class="l">${esc(t('rv.k.visit'))}</div><b>${num(total)}</b><div class="s">${esc(t('rv.k.visit.s'))}</div></div>
    </div>
    <div class="card"><div class="toolbar">
      <div class="seg" id="rvSeg">${['open','assigned','resolved','all'].map(k =>
        `<button data-k="${k}" class="${rv.status === k ? 'on' : ''}">${esc(k === 'all' ? t('f.all') : t('st.' + k))}</button>`).join('')}</div>
      <select id="rvProj"><option value="all">${esc(t('f.allprojects'))}</option>${[...new Set(d.items.map(i => i.project_id))].map(p =>
        `<option ${rv.project === p ? 'selected' : ''}>${p}</option>`).join('')}</select></div>
      <div class="scroll" style="max-height:600px"><table id="rvTable"></table></div><p class="hint" id="rvCount"></p></div>`;
  function rows() {
    let list = d.items.filter(i => (rv.status === 'all' || i.review.status === rv.status) &&
      (rv.project === 'all' || i.project_id === rv.project));
    if (focus) list = [...list.filter(i => i.id === focus), ...list.filter(i => i.id !== focus)];
    $('#rvTable').innerHTML = `<tr><th>${esc(t('c.plot'))}</th><th>${esc(t('c.project'))}</th><th>${esc(t('c.why'))}</th>
      <th class="num">${esc(t('dr.margin'))}</th><th>${esc(t('rv.sat'))}</th><th>${esc(t('rv.assignee'))}</th>
      <th>${esc(t('an.status'))}</th><th></th></tr>` + list.slice(0, 150).map(i => `<tr class="${i.id === focus ? 'sel' : ''}">
        <td style="white-space:nowrap"><b>${esc(i.name)}</b> ${i.data === 'measured' ? badge('measured') : ''}</td><td class="muted" style="white-space:nowrap">${esc(i.project_id)}</td>
        <td>${i.reasons.map(r => `<span class="chip">${esc(t('reason.' + r + '.s'))}</span>`).join('') ||
          (i.field_verified ? `<span class="muted">${esc(t('rv.done'))}</span>` : '')}</td>
        <td class="num">${num(i.margin_db, 2)} dB</td><td>${esc(verdictTxt(i.verdict))}${i.field_verified ? ' ✓' : ''}</td>
        <td class="muted">${esc(i.review.assignee || '–')}</td>
        <td><span class="state ${i.review.status}">${esc(t('st.' + i.review.status))}</span></td>
        <td style="white-space:nowrap;text-align:right">
          <button class="ghost" data-ev="${i.id}">${esc(t('rv.evidence'))}</button>
          ${i.review.status === 'open' ? `<button class="ghost" data-assign="${i.id}">${esc(t('rv.assign'))}</button>` : ''}
          ${i.review.status !== 'resolved' ? `<button class="primary" data-res="${i.id}" data-name="${esc(i.name)}">${esc(t('rv.record'))}</button>` : ''}
        </td></tr>`).join('');
    $('#rvCount').textContent = t('f.showing', {a: Math.min(150, list.length), b: list.length});
    $('#rvTable').querySelectorAll('[data-ev]').forEach(b => b.onclick = () => openPlot(b.dataset.ev));
    $('#rvTable').querySelectorAll('[data-assign]').forEach(b => b.onclick = async () => {
      await post('/review/' + b.dataset.assign, {action: 'assign', assignee: 'Field team A'});
      toast(t('rv.assigned.done')); vReview(el); navCounts();
    });
    $('#rvTable').querySelectorAll('[data-res]').forEach(b => b.onclick = () => resolveDialog(b.dataset.res, b.dataset.name, el));
  }
  $('#rvSeg').querySelectorAll('button').forEach(b => b.onclick = () => { rv.status = b.dataset.k; focus = null;
    $('#rvSeg').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b)); rows(); });
  $('#rvProj').onchange = e => { rv.project = e.target.value; rows(); };
  rows();
}
function resolveDialog(id, name, el) {
  $('#dlgTitle').textContent = t('rv.dlg.title', {p: name});
  $('#dlgBody').innerHTML = `<div style="min-width:min(420px,80vw)">
    <label>${esc(t('rv.dlg.outcome'))}</label>
    <select id="rvOutcome"><option value="compliant">${esc(t('verdict.compliant'))}</option>
      <option value="non_compliant">${esc(t('verdict.non_compliant'))}</option></select>
    <label>${esc(t('rv.dlg.note'))}</label><textarea id="rvNote" rows="3" placeholder="${esc(t('rv.dlg.ph'))}"></textarea>
    <p class="hint">${esc(t('rv.dlg.hint'))}</p>
    <button class="go" id="rvSave">${esc(t('rv.dlg.save'))}</button></div>`;
  $('#dlg').showModal();
  $('#rvSave').onclick = async () => {
    await post('/review/' + id, {action: 'resolve', outcome: $('#rvOutcome').value, note: $('#rvNote').value});
    $('#dlg').close(); toast(t('rv.resolved.done', {p: name}));
    rv.status = 'resolved'; vReview(el); navCounts();
  };
}

/* ================= Reports ================= */
async function vReports(el, pid) {
  const ov = await api('/overview');
  pid = pid || ov.projects[1].id;
  const [d, au] = await Promise.all([api('/reports/' + pid), api('/audit')]);
  const P = d.project, T = P.tiers, n = P.n_plots;
  const R = d.reasons;
  el.innerHTML = `
    <div class="view-head"><div><h2>${esc(t('rp.title'))}</h2><p>${esc(t('rp.sub'))}</p></div></div>
    <div class="pick" style="margin-bottom:14px">${ov.projects.map(p => `<button class="${p.id === pid ? 'on' : ''}" data-go="#/reports/${p.id}">
      <b>${esc(p.name)}</b><span>${esc(country(p.country))} · ${num(p.n_plots)} ${esc(t('u.plots'))}</span></button>`).join('')}</div>
    <div class="cols c21">
      <div class="doc">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
          <span class="stamp">${esc(t('rp.stamp'))}</span>${badge(P.data)}</div>
        <h2>${esc(t('rp.doc'))} — ${esc(P.name)}</h2>
        <div class="muted">${esc(P.id)} · ${esc(P.region)}, ${esc(country(P.country))} · ${esc(P.season)} · ${esc(t('top.asof'))} ${day(d.as_of)}</div>
        <h4>1. ${esc(t('rp.scope'))}</h4>
        <div class="facts" style="grid-template-columns:repeat(3,1fr)">
          <div>${esc(t('c.plots'))}<b>${num(n)}</b></div><div>${esc(t('c.area'))}<b>${num(P.area_ha)} ha</b></div>
          <div>${esc(t('pj.obs'))}<b>${num(P.mean_obs, 1)}</b></div></div>
        <div class="note"><b>${esc(t('pj.rule'))}:</b> ${esc(t('rule.' + P.rule))}</div>
        <h4>2. ${esc(t('rp.tiers'))}</h4>
        <table><tr><th>${esc(t('c.tier'))}</th><th class="num">${esc(t('c.plots'))}</th><th class="num">%</th></tr>
          ${['confirmed','provisional','review'].map(k => `<tr><td>${tierTag(k)}</td><td class="num">${num(T[k])}</td>
            <td class="num">${pct(T[k], n)}</td></tr>`).join('')}</table>
        <p class="hint">${esc(t('rp.compliant', {a: num(P.compliant), b: num(n), f: d.field_verified}))}</p>
        <h4>3. ${esc(t('rp.reasons'))}</h4>
        <table>${Object.keys(R).map(k => `<tr><td>${esc(t('reason.' + k))}</td><td class="num">${num(R[k])}</td></tr>`).join('')}</table>
        <h4>4. ${esc(t('rp.er'))}</h4>
        <div class="facts" style="grid-template-columns:repeat(3,1fr)">
          <div>${esc(t('tier.confirmed'))}<b>${num(P.er_confirmed, 1)} tCO₂e</b></div>
          <div>${esc(t('tier.provisional'))}<b>${num(P.er_provisional, 1)} tCO₂e</b></div>
          <div>${esc(t('rp.factor'))}<b>${P.ef} <small class="muted">tCO₂e/ha</small></b></div></div>
        <p class="hint">${esc(t('rp.er.note'))} ${esc(P.ef_source)}.</p>
        <h4>5. ${esc(t('an.integrity'))}</h4>
        ${d.anchors.length ? `<table><tr><th>${esc(t('an.anchor'))}</th><th>${esc(t('c.plot'))}</th><th class="num">${esc(t('an.k.records'))}</th>
          <th>${esc(t('an.chain'))}</th><th>${esc(t('ch.head'))}</th></tr>${d.anchors.map(a => `<tr><td>${esc(a.id)}</td><td>${esc(a.plot)}</td>
          <td class="num">${num(a.records)}</td><td><span class="state ${a.chain}">${a.chain}</span></td>
          <td class="mono">${a.head.slice(0, 16)}…</td></tr>`).join('')}</table>` : `<p class="hint">${esc(t('pj.noanchors'))}</p>`}
        <h4>6. ${esc(t('rp.limits'))}</h4>
        <ul class="muted" style="font-size:12.5px;padding-left:18px;margin:0">
          <li>${esc(t('rp.lim1'))}</li><li>${esc(t('rp.lim2'))}</li><li>${esc(t('rp.lim3'))}</li></ul>
      </div>
      <div>
        <div class="card" style="margin-bottom:14px"><h3>${esc(t('rp.export'))}</h3>
          <div style="display:flex;flex-direction:column;gap:8px">
            <a class="ghost" id="rpMd" href="/api/console/reports/${pid}?format=md" download>⬇ ${esc(t('rp.md'))}</a>
            <a class="ghost" href="/api/console/reports/${pid}?format=csv" download>⬇ ${esc(t('rp.csv'))}</a>
            <a class="ghost" href="/api/console/reports/${pid}?format=json" target="_blank" rel="noopener">{ } ${esc(t('rp.json'))}</a></div>
          <p class="hint">${esc(t('rp.export.note'))}</p></div>
        <div class="card"><h3>${esc(t('rp.audit'))}</h3>
          <ul class="feed">${au.events.slice(0, 9).map(e => `<li><i class="dot ${EV_CLASS[e.action] || ''}"></i>
            <div><b>${esc(t('ev.' + e.action))}</b> <span class="muted">· ${esc(e.target)} · ${esc(e.actor)}</span></div>
            <div class="muted">${esc(e.detail)} <time>· ${when(e.ts)}</time> <span class="mono">· ${e.hash.slice(0, 10)}</span></div></li>`).join('')}</ul>
          <p class="hint">${esc(t('rp.audit.note', {n: au.events.length}))}</p></div>
      </div>
    </div>`;
  bindGo(el);
  el.querySelectorAll('a[download]').forEach(a => a.addEventListener('click', () => {
    toast(t('rp.exported')); setTimeout(() => { if (route().view === 'reports') render(); }, 900); }));
}

/* ================= Router ================= */
const VIEWS = {overview: vOverview, projects: vProject, anchors: vAnchors, review: vReview, reports: vReports};
function route() {
  const parts = (location.hash || '#/overview').replace(/^#\/?/, '').split('/');
  return {view: parts[0] || 'overview', arg: parts[1]};
}
let seq = 0;
async function render() {
  let {view, arg} = route();
  if (view === 'projects' && !arg) { location.hash = '#/overview'; return; }
  if (!VIEWS[view] && view !== 'analysis') view = 'overview';
  const my = ++seq;
  closeDrawer(); cleanup();
  document.querySelectorAll('.nav a[data-view]').forEach(a =>
    a.classList.toggle('on', a.dataset.view === (view === 'projects' ? 'overview' : view)));
  $('#topTitle').textContent = t('nav.' + (view === 'projects' ? 'overview' : view));
  const an = view === 'analysis';
  $('#view').classList.toggle('hidden', an);
  $('#view-analysis').classList.toggle('hidden', !an);
  window.scrollTo(0, 0);
  if (an) { window.showAnalysis && window.showAnalysis(); return; }
  const el = $('#view');
  el.innerHTML = `<div class="skeleton">${esc(t('loading'))}</div>`;
  try { await VIEWS[view](el, arg); }
  catch (e) { if (my === seq) el.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}
async function navCounts() {
  try {
    const d = await api('/overview');
    $('#navReview').textContent = num(d.totals.review_open);
    const al = $('#navAnchors'); al.textContent = d.totals.chain_alerts || '';
    al.classList.toggle('hidden', !d.totals.chain_alerts);
    $('#asof').textContent = `${t('top.asof')} ${day(d.as_of)}`;
  } catch {}
}

window.PPConsole = {
  start() {
    window.addEventListener('hashchange', render);
    window.I18N.onChange(() => { render(); navCounts(); });
    $('#drawerClose').onclick = closeDrawer;
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
    render(); navCounts();
  },
};
})();
