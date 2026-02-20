/* CRTV Clinician View - Frontend */

const API_BASE = '/api';
const RUN_DATE = '2024-01-25';

let patientDetail = null;
let chartInstances = [];
let useRealData = false;
let selectedObservationRefs = null;
let selectedTriageIdx = null;  // which triage's metrics to use for plots
let selectedWeekTab = 0;  // which week tab is active (0-indexed)

function attentionLabel(att) {
  if (att === 3) return 'May need intervention';
  if (att === 2) return 'Needs review';
  return 'On track';
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];

function formatDateShort(iso) {
  const str = (iso || '').slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(str)) return '';
  const [y, m, d] = str.split('-').map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}

function cleanObservationText(text) {
  if (!text || typeof text !== 'string') return '';
  let s = text.trim();
  s = s.replace(/^[A-Z][A-Z0-9_]+:\s*/, '');
  s = s.replace(/^\d{4}-\d{2}-\d{2}(?:[–-]\d{2,4}(?:-\d{2})?)?(?:\s*[—\-]\s*)?/, '');
  s = s.replace(/^\d{4}-\d{2}-\d{2}[–-]\d{4}-\d{2}-\d{2}(?:\s*[—\-]\s*)?/, '');
  s = s.replace(/\s*\(\d{4}-\d{2}-\d{2}(?:[–-]\d{2,4}(?:-\d{2})?)?\)\s*/g, '');
  s = s.replace(/\s*\(\d{4}-\d{2}-\d{2}[–-]\d{4}-\d{2}-\d{2}\)\s*/g, '');
  s = s.replace(/\s*\d{4}-\d{2}-\d{2}(?:[–-]\d{2,4}(?:-\d{2})?)?\s*/g, ' ');
  s = s.replace(/\s*\d{4}-\d{2}-\d{2}[–-]\d{4}-\d{2}-\d{2}\s*/g, ' ');
  s = s.replace(/\s+/g, ' ').replace(/\s*—\s*—\s*/g, ' — ').trim();
  s = s.replace(/^[\s—\-–]+/, '').trim();
  if (s && s[0] === s[0].toLowerCase()) s = s[0].toUpperCase() + s.slice(1);
  return s;
}

function normalizeRef(r) {
  const s = String(r).trim();
  const idx = s.indexOf(' (');
  return idx >= 0 ? s.slice(0, idx) : s;
}

async function fetchMode() {
  const res = await fetch(`${API_BASE}/mode`);
  if (!res.ok) return { use_real_data: false };
  return res.json();
}

async function fetchPatients() {
  const res = await fetch(`${API_BASE}/patients?run_date=${RUN_DATE}`);
  if (!res.ok) throw new Error('Failed to load patients');
  return res.json();
}

async function fetchPatientDetail(patientId) {
  const res = await fetch(`${API_BASE}/patients/${patientId}?run_date=${RUN_DATE}`);
  if (!res.ok) throw new Error('Failed to load patient');
  return res.json();
}

function getObservations(card) {
  const obs = card?.observations;
  if (Array.isArray(obs) && obs.length) return obs;
  const ev = card?.evidence;
  const items = Array.isArray(ev) ? ev : (ev?.items || []);
  if (items.length) return items;
  const reasons = card?.reasons;
  if (Array.isArray(reasons) && reasons.length) {
    return reasons.map(r => ({ text: r, attention: 2, refs: [] }));
  }
  return [];
}

function renderPatientList(patients) {
  const ul = document.getElementById('patient-list');
  ul.innerHTML = patients.map(p => {
    const pid = p.patient_id;
    const att = p.attention_level || 0;
    const label = attentionLabel(att);
    return `
    <li data-patient-id="${pid}" class="patient-row ${att >= 2 ? 'needs-attention' : ''}">
      <span class="attention-badge level-${att}" title="${escapeHtml(label)}"></span>
      <div>
        <span class="patient-id">Patient ${pid} <span class="attention-label">${escapeHtml(label)}</span></span>
        <span class="patient-headline">${escapeHtml(p.headline || 'No action needed')}</span>
      </div>
    </li>
  `}).join('');

  ul.querySelectorAll('li').forEach(li => {
    li.addEventListener('click', () => selectPatient(parseInt(li.dataset.patientId)));
  });
}

async function selectPatient(patientId) {
  document.getElementById('page-list').classList.add('hidden');
  document.getElementById('page-detail').classList.remove('hidden');
  document.querySelectorAll('.patient-list-items li').forEach(li => {
    li.classList.toggle('selected', parseInt(li.dataset.patientId) === patientId);
  });

  try {
    patientDetail = await fetchPatientDetail(patientId);
    selectedObservationRefs = null;
    selectedTriageIdx = null;
    selectedWeekTab = 0;
    renderObservationCards(patientDetail);
    renderPlots(patientDetail);
  } catch (e) {
    console.error(e);
    document.getElementById('metrics-plots').innerHTML = `<p class="error">Failed to load: ${e.message}</p>`;
  }
}

function goBack() {
  document.getElementById('page-detail').classList.add('hidden');
  document.getElementById('page-list').classList.remove('hidden');
}

function renderObservationCards(detail) {
  const container = document.getElementById('observation-cards');
  let triages = detail?.triages;
  if (!triages || !triages.length) {
    triages = detail?.card ? [{ ...detail, checkpoint_date: '' }] : [];
  }
  if (!triages.length) {
    container.innerHTML = '<p class="muted">No observations</p>';
    return;
  }

  // Week tabs
  const tabContainer = document.getElementById('week-tabs-bar');
  if (tabContainer) {
    selectedWeekTab = Math.min(selectedWeekTab, triages.length - 1);
    tabContainer.innerHTML = triages.map((t, i) => {
      const cpFormatted = formatDateShort(t.checkpoint_date);
      return `<button type="button" class="week-tab ${i === selectedWeekTab ? 'active' : ''}" data-week="${i}">Week ${i + 1}${cpFormatted ? ` (${cpFormatted})` : ''}</button>`;
    }).join('');
    tabContainer.querySelectorAll('.week-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        selectedWeekTab = parseInt(btn.dataset.week, 10);
        document.querySelectorAll('.week-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderObservationCards(detail);
        renderPlots(detail);
      });
    });
  }

  // Show only selected week's triage
  const triage = triages[selectedWeekTab] || triages[0];
  const triageIdx = selectedWeekTab;
  const card = triage.card;
  let observations = getObservations(card) || [];
  const hasHigher = observations.some(o => (o.attention || 1) >= 2);
  if (hasHigher) observations = observations.filter(o => (o.attention || 1) >= 2);
  let html = `<div class="triage-section" data-triage-idx="${triageIdx}">
    <h4 class="triage-date">${escapeHtml(card?.headline || '')}</h4>
    <div class="triage-observations">`;
  observations.forEach((obs) => {
    const text = String(obs.text || '');
    const att = obs.attention || 1;
    const label = attentionLabel(att);
    const refs = Array.isArray(obs.refs) ? obs.refs : (obs.refs ? [String(obs.refs)] : []);
    const refAttr = refs.length ? ` data-refs='${JSON.stringify(refs)}'` : '';
    const isSelected = selectedTriageIdx === triageIdx && selectedObservationRefs &&
      refs.length === selectedObservationRefs.length &&
      refs.map(normalizeRef).every((r, i) => r === selectedObservationRefs[i]);
    html += `
    <div class="observation-card ${isSelected ? 'selected' : ''}"${refAttr} data-triage-idx="${triageIdx}">
      <span class="obs-attention level-${att}">${escapeHtml(label)}</span>
      <p class="obs-text">${escapeHtml(cleanObservationText(text))}</p>
    </div>`;
  });
  html += '</div></div>';
  container.innerHTML = html || '<p class="muted">No observations</p>';

  container.querySelectorAll('.observation-card[data-refs]').forEach(el => {
    el.addEventListener('click', () => {
      const refsJson = el.dataset.refs;
      const triageIdx = parseInt(el.dataset.triageIdx, 10);
      if (!refsJson || isNaN(triageIdx)) return;
      try {
        const rawRefs = JSON.parse(refsJson);
        if (!rawRefs || !rawRefs.length) return;
        const refs = rawRefs.map(normalizeRef);
        const same = selectedTriageIdx === triageIdx && selectedObservationRefs &&
          refs.length === selectedObservationRefs.length &&
          refs.every((r, i) => r === selectedObservationRefs[i]);
        selectedObservationRefs = same ? null : refs;
        selectedTriageIdx = same ? null : triageIdx;
        renderPlots(patientDetail);
        container.querySelectorAll('.observation-card[data-refs]').forEach(c => {
          const r = (JSON.parse(c.dataset.refs || '[]') || []).map(normalizeRef);
          const tidx = parseInt(c.dataset.triageIdx, 10);
          const sel = selectedTriageIdx === tidx && selectedObservationRefs &&
            r.length === selectedObservationRefs.length &&
            r.every((x, i) => x === selectedObservationRefs[i]);
          c.classList.toggle('selected', !!sel);
        });
      } catch (_) {}
    });
  });
}


function getActiveTriage(detail) {
  if (!detail) return null;
  const triages = detail.triages;
  if (triages && triages.length) {
    const idx = selectedWeekTab >= 0 && selectedWeekTab < triages.length ? selectedWeekTab : 0;
    return triages[idx];
  }
  return detail;
}

function collectPlotRefs(detail) {
  const refs = new Set();
  if (selectedObservationRefs && selectedObservationRefs.length) {
    selectedObservationRefs.forEach(r => refs.add(normalizeRef(r)));
    return refs;
  }
  const triages = detail?.triages;
  const cards = triages?.length ? triages.flatMap(t => getObservations(t?.card) || []) : getObservations(detail?.card);
  cards.forEach(o => {
    const r = Array.isArray(o?.refs) ? o.refs : (o?.refs ? [String(o.refs)] : []);
    r.forEach(x => refs.add(normalizeRef(x)));
  });
  return refs;
}

function getAllDates(detail) {
  const dates = new Set();
  const metrics = detail?.metrics || {};
  const adhLine = metrics.adherence_line || (detail?.adherence?.days || []).map(d => ({ date: d.date, value: d.ratio }));
  adhLine.forEach(d => dates.add((d.date || '').slice(0, 10)));
  (metrics.performance || []).forEach(d => dates.add(d.date));
  (metrics.difficulty || []).forEach(d => dates.add(d.date));
  Object.values(metrics.self_reports || {}).forEach(arr => arr.forEach(d => dates.add(d.date)));
  Object.values(metrics.protocol_wise || {}).forEach(data => {
    (data.performance || []).forEach(d => dates.add(d.date));
    (data.difficulty || []).forEach(d => dates.add(d.date));
    (data.adherence || []).forEach(d => dates.add(d.date));
  });
  return [...dates].sort();
}

function valueAtDate(series, date, valueKey) {
  const found = (series || []).find(d => (d.date || '').slice(0, 10) === date);
  if (!found) return null;
  if (valueKey) return found[valueKey];
  return found.value ?? found.performance_mean ?? found.difficulty_mean ?? null;
}

function renderPlots(detail) {
  if (!detail) return;
  const triage = getActiveTriage(detail);
  const refs = collectPlotRefs(detail);
  const allDates = getAllDates(triage);
  const metrics = triage?.metrics || {};
  const pw = metrics.protocol_wise || {};
  const selfReports = metrics.self_reports || {};

  chartInstances.forEach(c => c.destroy());
  chartInstances = [];

  const container = document.getElementById('metrics-plots');
  if (!container) return;

  if (!selectedObservationRefs || selectedObservationRefs.length === 0) {
    container.innerHTML = '<p class="muted">Select an observation to view its evidence plots.</p>';
    return;
  }

  container.innerHTML = '';
  const plotConfigs = [];

  const adhLine = metrics.adherence_line || (triage?.adherence?.days || []).map(d => ({ date: (d.date || '').slice(0, 10), value: d.ratio ?? d.value }));
  if (refs.has('adherence') && adhLine.length) {
    plotConfigs.push({ ref: 'adherence', title: 'Adherence', data: adhLine, color: '#0d6efd', yMin: 0, yMax: 1.2 });
  }
  if (refs.has('performance') && (metrics.performance || []).length) {
    plotConfigs.push({ ref: 'performance', title: 'Performance', data: metrics.performance, valueKey: 'performance_mean', color: '#0d6efd', yMin: 0, yMax: 1.2 });
  }
  if (refs.has('difficulty') && (metrics.difficulty || []).length) {
    plotConfigs.push({ ref: 'difficulty', title: 'Difficulty', data: metrics.difficulty, valueKey: 'difficulty_mean', color: '#198754', yMin: 0, yMax: null });
  }
  Object.keys(selfReports || {}).forEach(key => {
    const showAll = refs.has('self_reports');
    const showKey = refs.has(key);
    if ((!showAll && !showKey) || !selfReports[key]?.length) return;
    plotConfigs.push({
      ref: key,
      title: 'Self-report: ' + key,
      data: selfReports[key],
      color: '#fd7e14',
      yMin: 0,
      yMax: null,
    });
  });
  Object.entries(pw).forEach(([protoId, data]) => {
    const name = data.name || 'Protocol ' + protoId;
    const perf = data.performance || [];
    const diff = data.difficulty || [];
    const adh = data.adherence || [];
    const hasLegacy = refs.has('protocol_' + protoId);
    if (refs.has('protocol_' + protoId + '_adh') && adh.length) {
      plotConfigs.push({ ref: 'protocol_' + protoId + '_adh', title: name + ' (adherence)', data: adh, color: '#6f42c1', yMin: 0, yMax: 1.2 });
    }
    if ((refs.has('protocol_' + protoId + '_perf') || hasLegacy) && perf.length) {
      plotConfigs.push({ ref: 'protocol_' + protoId + '_perf', title: name + ' (performance)', data: perf, color: '#0d6efd', yMin: 0, yMax: 1.2 });
    }
    if ((refs.has('protocol_' + protoId + '_diff') || hasLegacy) && diff.length) {
      plotConfigs.push({ ref: 'protocol_' + protoId + '_diff', title: name + ' (difficulty)', data: diff, color: '#198754', yMin: 0, yMax: null });
    }
  });
  if (refs.has('sessions') && (triage?.sessions || []).length) {
    const sessByDate = {};
    (triage?.sessions || []).forEach(s => {
      const d = (s.start_time || '').slice(0, 10);
      if (!sessByDate[d]) sessByDate[d] = 0;
      sessByDate[d] += (s.duration_sec || 0) / 60;
    });
    const sessionsLine = Object.entries(sessByDate).map(([date, mins]) => ({ date, value: mins })).sort((a, b) => a.date.localeCompare(b.date));
    plotConfigs.push({ ref: 'sessions', title: 'Sessions (min/day)', data: sessionsLine, color: '#6f42c1', yMin: 0, yMax: null });
  }

  const labels = allDates.length ? allDates : [...new Set(plotConfigs.flatMap(p => (p.data || []).map(d => d.date)))].sort();

  plotConfigs.forEach(cfg => {
    const block = document.createElement('div');
    block.className = 'metric-plot-block';
    block.innerHTML = `<h5>${escapeHtml(cfg.title)}</h5><canvas></canvas>`;
    container.appendChild(block);

    const vals = labels.map(d => {
      const v = valueAtDate(cfg.data, d, cfg.valueKey);
      return v != null ? v : null;
    });
    if (vals.every(v => v == null)) return;

    const ctx = block.querySelector('canvas').getContext('2d');
    const chart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: cfg.title,
          data: vals,
          borderColor: cfg.color,
          backgroundColor: cfg.color + '20',
          fill: false,
          tension: 0.2,
          spanGaps: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { display: true },
          y: { min: cfg.yMin ?? 0, max: cfg.yMax ?? undefined },
        },
      },
    });
    chartInstances.push(chart);
  });

  if (plotConfigs.length === 0) {
    container.innerHTML = '<p class="muted">No matching plots for this observation.</p>';
  }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

async function init() {
  document.getElementById('back-btn').addEventListener('click', goBack);

  try {
    const mode = await fetchMode();
    useRealData = mode.use_real_data || false;
    const items = await fetchPatients();
    renderPatientList(items);
  } catch (e) {
    document.getElementById('patient-list').innerHTML = `<p class="error">Failed to load: ${e.message}</p>`;
  }
}
init();
