/*
 * DetailView — reusable clinician-style detail pane.
 *
 * Renders into a caller-provided root element:
 *   - week tabs (one per triage in detail.triages, or one synthetic tab if
 *     detail carries a single card directly)
 *   - observations pane (click to filter plots)
 *   - evidence/plots pane (Chart.js line charts)
 *   - chat drawer backed by a caller-specified endpoint
 *
 * Usage:
 *   const view = DetailView.create(rootEl, {
 *     chatEndpoint: '/api/chat',
 *     chatTitle: 'Ask about this card',
 *   });
 *   view.render(detail);     // detail = { triages: [...] } OR { card, metrics, ... }
 *   view.destroy();          // tear down charts + listeners
 */

window.DetailView = (function () {
  const MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December'];

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function attentionLabel(att) {
    if (att === 3) return 'May need intervention';
    if (att === 2) return 'Needs review';
    return 'On track';
  }

  function formatDateShort(iso) {
    const str = (iso || '').slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(str)) return '';
    const [y, m, d] = str.split('-').map(Number);
    return `${MONTHS[m - 1]} ${d}`;
  }

  function normalizeRef(r) {
    const s = String(r).trim();
    const idx = s.indexOf(' (');
    return idx >= 0 ? s.slice(0, idx) : s;
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
    s = s.replace(/\s+/g, ' ').replace(/\s*—\s*—\s*/g, ' — ').trim();
    s = s.replace(/^[\s—\-–]+/, '').trim();
    if (s && s[0] === s[0].toLowerCase()) s = s[0].toUpperCase() + s.slice(1);
    return s;
  }

  function getObservations(card) {
    const obs = card && card.observations;
    if (Array.isArray(obs) && obs.length) return obs;
    const ev = card && card.evidence;
    const items = Array.isArray(ev) ? ev : (ev && ev.items) || [];
    if (items.length) return items;
    const reasons = card && card.reasons;
    if (Array.isArray(reasons) && reasons.length) return reasons.map(r => ({ text: r, attention: 2, refs: [] }));
    return [];
  }

  function valueAtDate(series, date, valueKey) {
    const found = (series || []).find(d => (d.date || '').slice(0, 10) === date);
    if (!found) return null;
    if (valueKey) return found[valueKey];
    return found.value != null ? found.value : (found.performance_mean != null ? found.performance_mean : (found.difficulty_mean != null ? found.difficulty_mean : null));
  }

  function markup() {
    return `
      <div class="detail-topbar">
        <button type="button" class="dv-back back-btn">← Back</button>
      </div>
      <div class="dv-week-tabs week-tabs-bar"></div>
      <div class="two-pane">
        <aside class="observations-pane">
          <h3>Observations</h3>
          <p class="hint">Click an observation to view its evidence plots.</p>
          <div class="dv-observations observation-cards"></div>
        </aside>
        <section class="plots-pane">
          <h3>Evidence</h3>
          <div class="dv-plots metrics-plots"></div>
        </section>
      </div>
      <div class="dv-chat chat-drawer hidden">
        <div class="chat-drawer-header">
          <span class="dv-chat-title">Chat — follow-up questions</span>
          <button type="button" class="dv-chat-close chat-close-btn">Close</button>
        </div>
        <div class="dv-chat-messages chat-messages"></div>
        <div class="chat-input-bar">
          <input type="text" class="dv-chat-input" placeholder="Ask a question about this card..." autocomplete="off" />
          <button type="button" class="dv-chat-send chat-send-btn">Send</button>
        </div>
      </div>
    `;
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

  function create(rootEl, opts) {
    opts = opts || {};
    const chatEndpoint = opts.chatEndpoint || '/api/chat';
    const onBack = opts.onBack || null;

    rootEl.innerHTML = markup();
    const $ = (sel) => rootEl.querySelector(sel);
    const el = {
      back: $('.dv-back'),
      weekTabs: $('.dv-week-tabs'),
      observations: $('.dv-observations'),
      plots: $('.dv-plots'),
      chat: $('.dv-chat'),
      chatClose: $('.dv-chat-close'),
      chatMessages: $('.dv-chat-messages'),
      chatInput: $('.dv-chat-input'),
      chatSend: $('.dv-chat-send'),
    };

    const state = {
      detail: null,
      selectedWeekTab: 0,
      selectedRefs: null,
      selectedTriageIdx: null,
      chartInstances: [],
      chatMessages: [],
    };

    function destroyCharts() {
      state.chartInstances.forEach(c => { try { c.destroy(); } catch (_) {} });
      state.chartInstances = [];
    }

    function destroy() {
      destroyCharts();
      rootEl.innerHTML = '';
    }

    function getTriages(detail) {
      if (detail && Array.isArray(detail.triages) && detail.triages.length) return detail.triages;
      if (detail && detail.card) return [detail];
      return [];
    }

    function getActiveTriage() {
      const triages = getTriages(state.detail);
      if (!triages.length) return null;
      const idx = Math.min(Math.max(0, state.selectedWeekTab), triages.length - 1);
      return triages[idx];
    }

    function renderWeekTabs() {
      const triages = getTriages(state.detail);
      if (triages.length <= 1) {
        el.weekTabs.innerHTML = '';
        return;
      }
      el.weekTabs.innerHTML = triages.map((t, i) => {
        const cp = formatDateShort(t.checkpoint_date);
        return `<button type="button" class="week-tab ${i === state.selectedWeekTab ? 'active' : ''}" data-week="${i}">Week ${i + 1}${cp ? ` (${cp})` : ''}</button>`;
      }).join('');
      el.weekTabs.querySelectorAll('.week-tab').forEach(btn => {
        btn.addEventListener('click', () => {
          state.selectedWeekTab = parseInt(btn.dataset.week, 10) || 0;
          state.selectedRefs = null;
          state.selectedTriageIdx = null;
          renderWeekTabs();
          renderObservations();
          renderPlots();
        });
      });
    }

    function renderObservations() {
      const triage = getActiveTriage();
      if (!triage) {
        el.observations.innerHTML = '<p class="muted">No observations</p>';
        return;
      }
      const card = triage.card || triage;
      const tIdx = state.selectedWeekTab;
      const observations = getObservations(card) || [];
      let html = `<div class="triage-section">
        <h4 class="triage-date">${esc(card && card.headline || '')}</h4>
        <div class="triage-observations">`;
      observations.forEach(obs => {
        const att = obs.attention || 1;
        const refs = Array.isArray(obs.refs) ? obs.refs : (obs.refs ? [String(obs.refs)] : []);
        const refAttr = refs.length ? ` data-refs='${JSON.stringify(refs)}'` : '';
        const cleaned = cleanObservationText(String(obs.text || ''));
        const sel = state.selectedTriageIdx === tIdx && state.selectedRefs &&
          refs.length === state.selectedRefs.length &&
          refs.map(normalizeRef).every((r, i) => r === state.selectedRefs[i]);
        html += `
          <div class="observation-card ${sel ? 'selected' : ''}"${refAttr} data-triage-idx="${tIdx}">
            <span class="obs-attention level-${att}">${esc(attentionLabel(att))}</span>
            <p class="obs-text">${esc(cleaned)}</p>
            <button type="button" class="obs-ask-btn" data-obs-text="${esc(cleaned)}">Ask about this</button>
          </div>`;
      });
      html += '</div></div>';
      el.observations.innerHTML = observations.length ? html : '<p class="muted">No observations</p>';

      el.observations.querySelectorAll('.obs-ask-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          openChat();
        });
      });

      el.observations.querySelectorAll('.observation-card[data-refs]').forEach(card => {
        card.addEventListener('click', () => {
          try {
            const raw = JSON.parse(card.dataset.refs || '[]');
            if (!raw || !raw.length) return;
            const refs = raw.map(normalizeRef);
            const tidx = parseInt(card.dataset.triageIdx, 10);
            const same = state.selectedTriageIdx === tidx && state.selectedRefs &&
              refs.length === state.selectedRefs.length &&
              refs.every((r, i) => r === state.selectedRefs[i]);
            state.selectedRefs = same ? null : refs;
            state.selectedTriageIdx = same ? null : tidx;
            renderObservations();
            renderPlots();
          } catch (_) {}
        });
      });
    }

    function collectPlotRefs() {
      const refs = new Set();
      if (state.selectedRefs && state.selectedRefs.length) {
        state.selectedRefs.forEach(r => refs.add(normalizeRef(r)));
      }
      return refs;
    }

    function getAllDates(triage) {
      const dates = new Set();
      const metrics = (triage && triage.metrics) || {};
      const adhLine = metrics.adherence_line || ((triage && triage.adherence && triage.adherence.days) || []).map(d => ({ date: d.date, value: d.ratio }));
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

    function renderPlots() {
      destroyCharts();
      const triage = getActiveTriage();
      if (!triage) { el.plots.innerHTML = ''; return; }
      if (!state.selectedRefs || !state.selectedRefs.length) {
        el.plots.innerHTML = '<p class="muted">Select an observation to view its evidence plots.</p>';
        return;
      }
      const refs = collectPlotRefs();
      const metrics = triage.metrics || {};
      const pw = metrics.protocol_wise || {};
      const selfReports = metrics.self_reports || {};
      const plotConfigs = [];

      const adhLine = metrics.adherence_line || (triage.adherence && triage.adherence.days || []).map(d => ({ date: (d.date || '').slice(0, 10), value: d.ratio != null ? d.ratio : d.value }));
      if (refs.has('adherence') && adhLine.length) plotConfigs.push({ ref: 'adherence', title: 'Adherence', data: adhLine, color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
      if (refs.has('performance') && (metrics.performance || []).length) plotConfigs.push({ ref: 'performance', title: 'Performance', data: metrics.performance, valueKey: 'performance_mean', color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
      if (refs.has('difficulty') && (metrics.difficulty || []).length) plotConfigs.push({ ref: 'difficulty', title: 'Difficulty', data: metrics.difficulty, valueKey: 'difficulty_mean', color: '#198754', yMin: 0, yMax: 1, clampMax: 1 });
      Object.keys(selfReports).forEach(key => {
        if (!(refs.has('self_reports') || refs.has(key)) || !(selfReports[key] || []).length) return;
        plotConfigs.push({ ref: key, title: 'Self-report: ' + key, data: selfReports[key], color: '#fd7e14', yMin: 0, yMax: null });
      });
      Object.entries(pw).forEach(([protoId, data]) => {
        const name = data.name || 'Protocol ' + protoId;
        const hasLegacy = refs.has('protocol_' + protoId);
        if (refs.has('protocol_' + protoId + '_adh') && (data.adherence || []).length) plotConfigs.push({ ref: 'protocol_' + protoId + '_adh', title: name + ' (adherence)', data: data.adherence, color: '#6f42c1', yMin: 0, yMax: 1, clampMax: 1 });
        if ((refs.has('protocol_' + protoId + '_perf') || hasLegacy) && (data.performance || []).length) plotConfigs.push({ ref: 'protocol_' + protoId + '_perf', title: name + ' (performance)', data: data.performance, color: '#0d6efd', yMin: 0, yMax: 1, clampMax: 1 });
        if ((refs.has('protocol_' + protoId + '_diff') || hasLegacy) && (data.difficulty || []).length) plotConfigs.push({ ref: 'protocol_' + protoId + '_diff', title: name + ' (difficulty)', data: data.difficulty, color: '#198754', yMin: 0, yMax: 1, clampMax: 1 });
      });
      if (refs.has('sessions') && (triage.sessions || []).length) {
        const byDate = {};
        (triage.sessions || []).forEach(s => {
          const d = (s.start_time || '').slice(0, 10);
          byDate[d] = (byDate[d] || 0) + (s.duration_sec || 0) / 60;
        });
        const sessionsLine = Object.entries(byDate).map(([date, mins]) => ({ date, value: mins })).sort((a, b) => a.date.localeCompare(b.date));
        plotConfigs.push({ ref: 'sessions', title: 'Sessions (min/day)', data: sessionsLine, color: '#6f42c1', yMin: 0, yMax: null });
      }

      if (!plotConfigs.length) { el.plots.innerHTML = '<p class="muted">No matching plots for this observation.</p>'; return; }

      const allDates = getAllDates(triage);
      const labels = allDates.length ? allDates : [...new Set(plotConfigs.flatMap(p => (p.data || []).map(d => d.date)))].sort();

      el.plots.innerHTML = '';
      plotConfigs.forEach(cfg => {
        const block = document.createElement('div');
        block.className = 'metric-plot-block';
        block.innerHTML = `<h5>${esc(cfg.title)}</h5><canvas></canvas>`;
        el.plots.appendChild(block);
        const vals = labels.map(d => {
          const v = valueAtDate(cfg.data, d, cfg.valueKey);
          if (v == null) return null;
          return cfg.clampMax != null ? Math.min(cfg.clampMax, v) : v;
        });
        if (vals.every(v => v == null)) return;
        const realN = vals.filter(v => v != null).length;
        const radius = realN <= 2 ? 6 : 4;
        const ctx = block.querySelector('canvas').getContext('2d');
        const chart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{
            label: cfg.title, data: vals, borderColor: cfg.color,
            backgroundColor: cfg.color + '20', fill: false, tension: 0.2,
            spanGaps: true, pointRadius: radius, pointHoverRadius: radius + 3, pointBackgroundColor: cfg.color,
          }] },
          options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } }, scales: { x: { display: true }, y: { min: cfg.yMin != null ? cfg.yMin : 0, max: cfg.yMax != null ? cfg.yMax : undefined } } },
        });
        state.chartInstances.push(chart);
      });
    }

    // --- chat -------------------------------------------------------------

    function chatContext() {
      const triage = getActiveTriage();
      if (!triage) return null;
      const pid = state.detail && state.detail.patient_id;
      const cp = triage.checkpoint_date || (state.detail && state.detail.checkpoint_date);
      if (pid == null || !cp) return null;
      return { patient_id: pid, checkpoint_date: cp };
    }

    function appendChatMsg(role, text) {
      const div = document.createElement('div');
      div.className = `chat-msg ${role}`;
      if (role === 'assistant') div.innerHTML = renderMarkdown(text);
      else div.textContent = text;
      el.chatMessages.appendChild(div);
      el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
      return div;
    }

    function resetChat() {
      state.chatMessages = [];
      el.chatMessages.innerHTML = '';
    }

    function openChat() {
      const ctx = chatContext();
      if (!ctx) return;
      el.chat.classList.remove('hidden');
      el.chatInput.focus();
    }

    function closeChat() {
      el.chat.classList.add('hidden');
    }

    async function sendChat() {
      const text = el.chatInput.value.trim();
      if (!text) return;
      const ctx = chatContext();
      if (!ctx) return;
      el.chatInput.value = '';
      state.chatMessages.push({ role: 'user', content: text });
      appendChatMsg('user', text);
      const loading = appendChatMsg('loading', 'Thinking...');
      el.chatSend.disabled = true;
      el.chatInput.disabled = true;
      try {
        const res = await fetch(chatEndpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ...ctx, messages: state.chatMessages }),
        });
        const data = await res.json();
        loading.remove();
        if (data.error) appendChatMsg('error', `Error: ${data.error}`);
        else if (data.response) { state.chatMessages.push({ role: 'assistant', content: data.response }); appendChatMsg('assistant', data.response); }
        else appendChatMsg('error', 'No response received.');
      } catch (e) {
        loading.remove();
        appendChatMsg('error', `Failed: ${e.message}`);
      } finally {
        el.chatSend.disabled = false;
        el.chatInput.disabled = false;
        el.chatInput.focus();
      }
    }

    // --- wiring -----------------------------------------------------------

    el.back.addEventListener('click', () => {
      closeChat();
      if (onBack) onBack();
    });
    el.chatClose.addEventListener('click', closeChat);
    el.chatSend.addEventListener('click', sendChat);
    el.chatInput.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });

    function render(detail) {
      state.detail = detail;
      state.selectedWeekTab = 0;
      state.selectedRefs = null;
      state.selectedTriageIdx = null;
      resetChat();
      closeChat();
      renderWeekTabs();
      renderObservations();
      renderPlots();
    }

    return { render, destroy };
  }

  return { create };
})();
