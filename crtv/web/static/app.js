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
  closeChatDrawer();
  resetChat();
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
  const observations = getObservations(card) || [];
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
    const cleanText = cleanObservationText(text);
    html += `
    <div class="observation-card ${isSelected ? 'selected' : ''}"${refAttr} data-triage-idx="${triageIdx}">
      <span class="obs-attention level-${att}">${escapeHtml(label)}</span>
      <p class="obs-text">${escapeHtml(cleanText)}</p>
      <button type="button" class="obs-ask-btn" data-obs-text="${escapeHtml(cleanText)}">Ask about this</button>
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

  container.querySelectorAll('.obs-ask-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      prefillChatWithObservation(btn.dataset.obsText);
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
    plotConfigs.push({ ref: 'adherence', title: 'Adherence', data: adhLine, color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
  }
  if (refs.has('performance') && (metrics.performance || []).length) {
    plotConfigs.push({ ref: 'performance', title: 'Performance', data: metrics.performance, valueKey: 'performance_mean', color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
  }
  if (refs.has('difficulty') && (metrics.difficulty || []).length) {
    plotConfigs.push({ ref: 'difficulty', title: 'Difficulty', data: metrics.difficulty, valueKey: 'difficulty_mean', color: '#198754', yMin: 0, yMax: 1, clampMax: 1 });
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
      plotConfigs.push({ ref: 'protocol_' + protoId + '_adh', title: name + ' (adherence)', data: adh, color: '#6f42c1', yMin: 0, yMax: 1, clampMax: 1 });
    }
    if ((refs.has('protocol_' + protoId + '_perf') || hasLegacy) && perf.length) {
      plotConfigs.push({ ref: 'protocol_' + protoId + '_perf', title: name + ' (performance)', data: perf, color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
    }
    if ((refs.has('protocol_' + protoId + '_diff') || hasLegacy) && diff.length) {
      plotConfigs.push({ ref: 'protocol_' + protoId + '_diff', title: name + ' (difficulty)', data: diff, color: '#198754', yMin: 0, yMax: 1, clampMax: 1 });
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
      if (v == null) return null;
      return cfg.clampMax != null ? Math.min(cfg.clampMax, v) : v;
    });
    if (vals.every(v => v == null)) return;

    const realPointCount = vals.filter(v => v != null).length;
    const pointRadius = realPointCount <= 2 ? 6 : 4;

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
          pointRadius,
          pointHoverRadius: pointRadius + 3,
          pointBackgroundColor: cfg.color,
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

/* ============================================================
   Patient Check-in View
   ============================================================ */

let checkinData = null;
let checkinSelectedPatient = null;
let checkinSelectedWeek = 0;

async function fetchCheckins() {
  const res = await fetch(`${API_BASE}/patient-checkins`);
  if (!res.ok) throw new Error('Failed to load check-ins');
  return res.json();
}

async function fetchPatientCheckins(patientId) {
  const res = await fetch(`${API_BASE}/patient-checkins/${patientId}`);
  if (!res.ok) throw new Error('Failed to load patient check-ins');
  return res.json();
}

function renderCheckinPatientList(checkins) {
  const ul = document.getElementById('checkin-patient-list');
  const byPatient = {};
  checkins.forEach(c => {
    if (!byPatient[c.patient_id]) {
      byPatient[c.patient_id] = { patient_id: c.patient_id, tone: c.overall_tone, preview: c.greeting_preview, count: 0 };
    }
    byPatient[c.patient_id].count++;
  });
  const patients = Object.values(byPatient).sort((a, b) => a.patient_id - b.patient_id);

  ul.innerHTML = patients.map(p => {
    const tone = p.tone || 'steady';
    return `
    <li data-patient-id="${p.patient_id}" class="patient-row">
      <span class="tone-dot ${escapeHtml(tone)}" title="${escapeHtml(tone)}"></span>
      <div>
        <span class="patient-id">Patient ${p.patient_id} <span class="tone-badge ${escapeHtml(tone)}">${escapeHtml(tone)}</span></span>
        <span class="patient-headline">${escapeHtml(p.preview || '')}</span>
      </div>
    </li>`;
  }).join('');

  ul.querySelectorAll('li').forEach(li => {
    li.addEventListener('click', () => selectCheckinPatient(parseInt(li.dataset.patientId)));
  });
}

async function selectCheckinPatient(patientId) {
  document.getElementById('checkin-page-list').classList.add('hidden');
  document.getElementById('checkin-page-detail').classList.remove('hidden');
  checkinSelectedPatient = patientId;
  checkinSelectedWeek = 0;

  try {
    checkinData = await fetchPatientCheckins(patientId);
    renderCheckinWeekTabs();
    renderCheckinCard();
  } catch (e) {
    document.getElementById('checkin-content').innerHTML = `<p class="error">Failed to load: ${e.message}</p>`;
  }
}

function renderCheckinWeekTabs() {
  const container = document.getElementById('checkin-week-tabs');
  if (!checkinData || !checkinData.checkins || !checkinData.checkins.length) {
    container.innerHTML = '';
    return;
  }
  container.innerHTML = checkinData.checkins.map((c, i) => {
    const cpFormatted = formatDateShort(c.checkpoint_date);
    return `<button type="button" class="week-tab ${i === checkinSelectedWeek ? 'active' : ''}" data-week="${i}">Week ${i + 1}${cpFormatted ? ` (${cpFormatted})` : ''}</button>`;
  }).join('');
  container.querySelectorAll('.week-tab').forEach(btn => {
    btn.addEventListener('click', () => {
      checkinSelectedWeek = parseInt(btn.dataset.week, 10);
      container.querySelectorAll('.week-tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      renderCheckinCard();
    });
  });
}

function renderCheckinCard() {
  const container = document.getElementById('checkin-content');
  if (!checkinData || !checkinData.checkins || !checkinData.checkins.length) {
    container.innerHTML = '<p class="muted">No check-ins available for this patient.</p>';
    return;
  }
  const c = checkinData.checkins[checkinSelectedWeek] || checkinData.checkins[0];
  const progress = c.progress || [];
  let html = '';

  // Progress bars
  if (progress.length) {
    html += `<div class="checkin-section">
      <div class="checkin-section-header">Progress</div>
      <div class="progress-bars">`;
    progress.forEach(p => {
      const dir = p.direction || 'flat';
      const current = p.current ?? 0;
      const previous = p.previous;
      const maxVal = p.unit === '%' || p.unit === '% harder' ? 100 : Math.max(current, previous || 0, 1);
      const fillPct = Math.min((current / maxVal) * 100, 100);
      const prevPct = previous != null ? Math.min((previous / maxVal) * 100, 100) : null;
      const valueStr = previous != null
        ? `${previous}${p.unit} → ${current}${p.unit}`
        : `${current}${p.unit}`;

      html += `<div class="progress-item">
        <div class="progress-label-row">
          <span class="progress-name">${escapeHtml(p.label)}</span>
          <span class="progress-value ${dir}">${escapeHtml(valueStr)}</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill ${dir}" style="width:${fillPct}%"></div>
          ${prevPct != null ? `<div class="progress-prev-marker" style="left:${prevPct}%" title="Previous"></div>` : ''}
        </div>
      </div>`;
    });
    html += '</div></div>';
  }

  // Wins
  const winsText = typeof c.wins === 'string' ? c.wins : (Array.isArray(c.wins) ? c.wins.join('. ') : '');
  if (winsText) {
    html += `<div class="checkin-section">
      <div class="checkin-section-header">Wins</div>
      <p class="checkin-prose">${escapeHtml(winsText)}</p>
    </div>`;
  }

  // To improve
  const improveText = typeof c.to_improve === 'string' ? c.to_improve : (Array.isArray(c.to_improve) ? c.to_improve.join('. ') : '');
  if (improveText) {
    html += `<div class="checkin-section">
      <div class="checkin-section-header">To work on</div>
      <p class="checkin-prose muted">${escapeHtml(improveText)}</p>
    </div>`;
  }

  // Check-in (only if present)
  if (c.check_in) {
    html += `<div class="checkin-section">
      <div class="checkin-alert">${escapeHtml(c.check_in)}</div>
    </div>`;
  }

  container.innerHTML = html || '<p class="muted">No updates for this week.</p>';
}

function goBackCheckin() {
  document.getElementById('checkin-page-detail').classList.add('hidden');
  document.getElementById('checkin-page-list').classList.remove('hidden');
}

/* ============================================================
   Chatbot Drawer
   ============================================================ */

let chatMessages = [];
let chatPatientId = null;
let chatCheckpointDate = null;

function getChatContext() {
  if (!patientDetail) return null;
  const triages = patientDetail.triages;
  if (triages && triages.length) {
    const t = triages[selectedWeekTab] || triages[0];
    return { patient_id: patientDetail.patient_id, checkpoint_date: t.checkpoint_date };
  }
  return null;
}

function resetChat() {
  chatMessages = [];
  const ctx = getChatContext();
  chatPatientId = ctx?.patient_id || null;
  chatCheckpointDate = ctx?.checkpoint_date || null;
  const container = document.getElementById('chat-messages');
  if (container) container.innerHTML = '';
}

function openChatDrawer() {
  const ctx = getChatContext();
  if (!ctx) return;
  if (ctx.patient_id !== chatPatientId || ctx.checkpoint_date !== chatCheckpointDate) {
    resetChat();
  }
  document.getElementById('chat-drawer').classList.remove('hidden');
  document.getElementById('chat-input').focus();
}

function closeChatDrawer() {
  document.getElementById('chat-drawer').classList.add('hidden');
}

function renderMarkdown(md) {
  let html = md;
  html = html.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  html = html.replace(/^### (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^## (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/^# (.+)$/gm, '<strong>$1</strong>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>');
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');
  html = html.replace(/\n{2,}/g, '<br><br>');
  html = html.replace(/\n/g, '<br>');
  return html;
}

function appendChatMessage(role, text) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = renderMarkdown(text);
  } else {
    div.textContent = text;
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

const LOADING_STAGES = ['Fetching data...', 'Analysing...', 'Thinking...'];
let loadingInterval = null;

function startLoadingAnimation(el) {
  let stage = 0;
  el.textContent = LOADING_STAGES[0];
  loadingInterval = setInterval(() => {
    stage++;
    if (stage < LOADING_STAGES.length) {
      el.textContent = LOADING_STAGES[stage];
    } else {
      clearInterval(loadingInterval);
      loadingInterval = null;
    }
  }, 1500);
}

function stopLoadingAnimation() {
  if (loadingInterval) {
    clearInterval(loadingInterval);
    loadingInterval = null;
  }
}

async function sendChatMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;

  const ctx = getChatContext();
  if (!ctx) return;

  input.value = '';
  chatMessages.push({ role: 'user', content: text });
  appendChatMessage('user', text);

  const loadingEl = appendChatMessage('loading', '');
  startLoadingAnimation(loadingEl);
  const sendBtn = document.getElementById('chat-send-btn');
  sendBtn.disabled = true;
  input.disabled = true;

  try {
    const res = await fetch(`${API_BASE}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_id: ctx.patient_id,
        checkpoint_date: ctx.checkpoint_date,
        messages: chatMessages,
      }),
    });
    const data = await res.json();
    stopLoadingAnimation();
    loadingEl.remove();

    if (data.error) {
      appendChatMessage('error', `Error: ${data.error}`);
    } else if (data.response) {
      chatMessages.push({ role: 'assistant', content: data.response });
      appendChatMessage('assistant', data.response);
    } else {
      appendChatMessage('error', 'No response received.');
    }
  } catch (e) {
    stopLoadingAnimation();
    loadingEl.remove();
    appendChatMessage('error', `Failed: ${e.message}`);
  } finally {
    sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

function prefillChatWithObservation(obsText) {
  openChatDrawer();
}

/* ============================================================
   Patient-view stroke-care RAG widget
   ============================================================ */

const ptChat = {
  messages: [],
  greeted: false,
  mode: 'standalone',        // 'standalone' | 'integrated'
  boundPatient: null,        // { patient_id, checkpoint_date }
  patientListLoaded: false,
};

function ptChatContext() {
  if (ptChat.mode === 'integrated' && ptChat.boundPatient) {
    return { ...ptChat.boundPatient };
  }
  return null;
}

function ptChatUpdateContextLabel() {
  const el = document.getElementById('pt-chat-context');
  if (!el) return;
  if (ptChat.mode === 'integrated') {
    const ctx = ptChat.boundPatient;
    el.textContent = ctx
      ? `Talking with patient #${ctx.patient_id} (week ${ctx.checkpoint_date})`
      : 'Integrated mode — select a patient';
  } else {
    el.textContent = 'General conversation';
  }
}

async function ptChatLoadPatientList() {
  if (ptChat.patientListLoaded) return;
  const sel = document.getElementById('pt-chat-patient');
  if (!sel) return;
  try {
    const res = await fetch(`${API_BASE}/patient-checkins`);
    if (!res.ok) throw new Error(res.status);
    const checkins = await res.json();
    const byPatient = {};
    checkins.forEach(c => {
      const cur = byPatient[c.patient_id];
      if (!cur || (c.checkpoint_date || '') > (cur.checkpoint_date || '')) {
        byPatient[c.patient_id] = { patient_id: c.patient_id, checkpoint_date: c.checkpoint_date };
      }
    });
    const rows = Object.values(byPatient).sort((a, b) => a.patient_id - b.patient_id);
    sel.innerHTML = '<option value="">Select a patient…</option>' +
      rows.map(p => `<option value="${p.patient_id}|${p.checkpoint_date}">Patient ${p.patient_id} — wk ${p.checkpoint_date}</option>`).join('');
    ptChat.patientListLoaded = true;
  } catch (e) {
    sel.innerHTML = `<option value="">(failed to load: ${e.message})</option>`;
  }
}

function ptChatSetMode(mode) {
  ptChat.mode = mode;
  document.querySelectorAll('.pt-chat-mode').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });
  const sel = document.getElementById('pt-chat-patient');
  if (mode === 'integrated') {
    sel.hidden = false;
    ptChatLoadPatientList();
  } else {
    sel.hidden = true;
    ptChat.boundPatient = null;
  }
  ptChatUpdateContextLabel();
}

function ptChatOnPatientSelect(value) {
  if (!value) { ptChat.boundPatient = null; ptChatUpdateContextLabel(); return; }
  const [pid, cp] = value.split('|');
  ptChat.boundPatient = { patient_id: parseInt(pid, 10), checkpoint_date: cp };
  // Switching bound patient mid-session starts a fresh conversation so the
  // bot's framing aligns with the newly-selected patient.
  ptChatResetMessages();
  ptChatUpdateContextLabel();
  ptChatGreet();
}

function ptChatGreet() {
  if (ptChat.greeted) return;
  ptChat.greeted = true;
  const msg = ptChat.mode === 'integrated' && ptChat.boundPatient
    ? "Hi, I'm your stroke-care companion. I'll keep what we talk about grounded in your weekly summary. What's on your mind today?"
    : "Hi, I'm here to help with questions about stroke recovery and caregiving. What's on your mind today?";
  ptChatAppend('assistant', msg, null);
}

function ptChatResetMessages() {
  ptChat.messages = [];
  ptChat.greeted = false;
  const container = document.getElementById('pt-chat-messages');
  if (container) container.innerHTML = '';
}

function ptChatReset() {
  ptChatResetMessages();
  ptChatGreet();
  const input = document.getElementById('pt-chat-input');
  if (input) input.focus();
}

function ptChatOpen() {
  document.getElementById('pt-chat-drawer').classList.remove('hidden');
  ptChatUpdateContextLabel();
  ptChatGreet();
  const input = document.getElementById('pt-chat-input');
  if (input) input.focus();
}

function ptChatClose() {
  document.getElementById('pt-chat-drawer').classList.add('hidden');
}

function ptChatRenderCitations(citations) {
  if (!citations || !citations.length) return '';
  const seen = new Set();
  const lines = [];
  for (const c of citations) {
    const key = `${c.source_label}|${c.section_title}`;
    if (seen.has(key)) continue;
    seen.add(key);
    lines.push(`<span class="cite">• ${escapeHtml(c.source_label || 'Source')} — ${escapeHtml(c.section_title || '')}</span>`);
    if (lines.length >= 3) break;
  }
  return `<div class="citations">${lines.join('')}</div>`;
}

function ptChatAppend(role, text, citations) {
  const container = document.getElementById('pt-chat-messages');
  const div = document.createElement('div');
  div.className = `pt-msg ${role}`;
  if (role === 'assistant') {
    div.innerHTML = renderMarkdown(text) + ptChatRenderCitations(citations);
  } else if (role === 'loading') {
    div.textContent = 'Thinking…';
  } else {
    div.textContent = text;
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
  return div;
}

async function ptChatSend() {
  const input = document.getElementById('pt-chat-input');
  const text = (input.value || '').trim();
  if (!text) return;
  input.value = '';
  ptChat.messages.push({ role: 'user', content: text });
  ptChatAppend('user', text, null);
  const sendBtn = document.getElementById('pt-chat-send');
  sendBtn.disabled = true;
  input.disabled = true;

  const container = document.getElementById('pt-chat-messages');
  const assistantEl = document.createElement('div');
  assistantEl.className = 'pt-msg assistant streaming';
  assistantEl.textContent = '…';
  container.appendChild(assistantEl);
  container.scrollTop = container.scrollHeight;

  const ctx = ptChatContext();
  let acc = '';
  let citations = null;
  let gotFirstToken = false;
  let errored = false;

  try {
    const res = await fetch(`${API_BASE}/patient-chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: ptChat.messages,
        patient_id: ctx ? ctx.patient_id : null,
        checkpoint_date: ctx ? ctx.checkpoint_date : null,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const line = block.split('\n').find(l => l.startsWith('data: '));
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch { continue; }
        if (evt.type === 'token') {
          if (!gotFirstToken) { assistantEl.textContent = ''; gotFirstToken = true; }
          acc += evt.text;
          assistantEl.innerHTML = renderMarkdown(acc);
          container.scrollTop = container.scrollHeight;
        } else if (evt.type === 'citations') {
          citations = evt.citations;
        } else if (evt.type === 'error') {
          errored = true;
          assistantEl.className = 'pt-msg error';
          assistantEl.textContent = `Error: ${evt.error}`;
        }
      }
    }
  } catch (e) {
    errored = true;
    assistantEl.className = 'pt-msg error';
    assistantEl.textContent = `Failed: ${e.message}`;
  } finally {
    if (!errored && gotFirstToken) {
      assistantEl.className = 'pt-msg assistant';
      assistantEl.innerHTML = renderMarkdown(acc) + ptChatRenderCitations(citations);
      ptChat.messages.push({ role: 'assistant', content: acc });
    } else if (!errored && !gotFirstToken) {
      assistantEl.className = 'pt-msg error';
      assistantEl.textContent = 'No response received.';
    }
    sendBtn.disabled = false;
    input.disabled = false;
    input.focus();
  }
}

function switchView(view) {
  const clinicianMain = document.querySelector('.main:not(#patient-view)');
  const patientMain = document.getElementById('patient-view');
  const btnClinician = document.getElementById('view-clinician');
  const btnPatient = document.getElementById('view-patient');
  const ptFab = document.getElementById('pt-chat-fab');
  const ptDrawer = document.getElementById('pt-chat-drawer');

  if (view === 'patient') {
    clinicianMain.classList.add('hidden');
    patientMain.classList.remove('hidden');
    btnClinician.classList.remove('active');
    btnPatient.classList.add('active');
    if (ptFab) ptFab.style.display = '';
    // Load check-ins on first switch
    if (!document.getElementById('checkin-patient-list').children.length) {
      fetchCheckins()
        .then(renderCheckinPatientList)
        .catch(e => {
          document.getElementById('checkin-patient-list').innerHTML = `<p class="error">Failed to load: ${e.message}</p>`;
        });
    }
  } else {
    patientMain.classList.add('hidden');
    clinicianMain.classList.remove('hidden');
    btnPatient.classList.remove('active');
    btnClinician.classList.add('active');
    if (ptFab) ptFab.style.display = 'none';
    if (ptDrawer) ptDrawer.classList.add('hidden');
  }
}

async function init() {
  document.getElementById('back-btn').addEventListener('click', goBack);
  document.getElementById('checkin-back-btn').addEventListener('click', goBackCheckin);
  document.getElementById('view-clinician').addEventListener('click', () => switchView('clinician'));
  document.getElementById('view-patient').addEventListener('click', () => switchView('patient'));

  document.getElementById('chat-close-btn').addEventListener('click', closeChatDrawer);
  document.getElementById('chat-send-btn').addEventListener('click', sendChatMessage);
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
  });

  const ptFab = document.getElementById('pt-chat-fab');
  if (ptFab) {
    ptFab.style.display = 'none'; // shown when entering patient view
    ptFab.addEventListener('click', ptChatOpen);
    document.getElementById('pt-chat-close').addEventListener('click', ptChatClose);
    document.getElementById('pt-chat-reset').addEventListener('click', ptChatReset);
    document.getElementById('pt-chat-send').addEventListener('click', ptChatSend);
    document.getElementById('pt-chat-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ptChatSend(); }
    });
    document.querySelectorAll('.pt-chat-mode').forEach(b => {
      b.addEventListener('click', () => ptChatSetMode(b.dataset.mode));
    });
    document.getElementById('pt-chat-patient').addEventListener('change', (e) => {
      ptChatOnPatientSelect(e.target.value);
    });
  }

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
