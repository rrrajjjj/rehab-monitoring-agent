/* Ops page: two tabs (clinician, patient) + knowledge base management. */

const $ = (id) => document.getElementById(id);

function fmtDate(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleString();
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

// --- tabs ---------------------------------------------------------------

function initTabs() {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
      document.querySelectorAll('.tab-panel').forEach(p => {
        p.classList.toggle('active', p.id === `tab-${tab}`);
      });
      if (tab === 'patient') refreshCorpus();
    });
  });
}

// --- generic config controller ------------------------------------------

function makeConfigController({ api, hasKb, hasModel = true, prefix, testsApi = null }) {
  let activeId = null;
  let runSummaries = {}; // {version_id: {avg_score, ran_at, n}}
  let runningVid = null;
  let runPollTimer = null;

  const systemEl = $(`${prefix}-system-prompt`);
  const kbEl = hasKb ? $(`${prefix}-kb-editor`) : null;
  const modelEl = hasModel ? $(`${prefix}-model-select`) : null;
  const labelEl = $(`${prefix}-version-label`);
  const saveBtn = $(`${prefix}-save-btn`);
  const statusEl = $(`${prefix}-save-status`);
  const listEl = $(`${prefix}-version-list`);

  function setStatus(msg, err = false) {
    statusEl.textContent = msg;
    statusEl.className = err ? 'save-status err' : 'save-status';
  }

  function apply(cfg) {
    systemEl.value = cfg.system_prompt || '';
    if (kbEl) kbEl.value = cfg.kb || '';
    if (modelEl) modelEl.value = cfg.model || 'gpt-5.4-nano';
  }

  async function loadActive() {
    const res = await fetch(api);
    if (!res.ok) return;
    const cfg = await res.json();
    activeId = cfg.id;
    apply(cfg);
  }

  async function loadVersions() {
    const res = await fetch(`${api}/versions`);
    if (!res.ok) return;
    const versions = await res.json();
    listEl.innerHTML = versions.map(v => {
      const isActive = v.id === activeId;
      const summary = runSummaries[v.id];
      let scoreHtml = '';
      let runLabel = 'Run tests';
      if (testsApi) {
        if (summary && typeof summary.avg_score === 'number') {
          const s = summary.avg_score;
          const cls = s >= 4 ? 'good' : s >= 3 ? 'med' : 'bad';
          scoreHtml = `<span class="version-score ${cls}" title="${summary.n} case(s), ran ${fmtDate(summary.ran_at)}">★ ${s.toFixed(2)}</span>`;
          runLabel = 'Re-run';
        } else {
          scoreHtml = '<span class="version-score none">— no run —</span>';
        }
      }
      const isRunning = runningVid === v.id;
      return `
        <li class="version-item ${isActive ? 'active-version' : ''}">
          <span class="version-id">v${v.id}</span>
          ${v.model ? `<span class="version-model">${escapeHtml(v.model)}</span>` : ''}
          <span class="version-label">${escapeHtml(v.label || '')}</span>
          <span class="version-date">${escapeHtml(fmtDate(v.created_at))}</span>
          ${scoreHtml}
          ${isActive ? '<span class="version-badge">active</span>' : ''}
          <span class="version-actions">
            <button type="button" data-act="preview" data-vid="${v.id}">Preview</button>
            ${!isActive ? `<button type="button" class="revert-btn" data-act="revert" data-vid="${v.id}">Revert</button>` : ''}
            ${testsApi ? `<button type="button" class="run-btn" data-act="run" data-vid="${v.id}" ${isRunning || runningVid ? 'disabled' : ''}>${isRunning ? 'Running…' : runLabel}</button>` : ''}
            ${testsApi && summary ? `<button type="button" class="inspect-btn" data-act="inspect" data-vid="${v.id}">Inspect</button>` : ''}
          </span>
        </li>`;
    }).join('');
    listEl.querySelectorAll('button[data-act]').forEach(b => {
      b.addEventListener('click', () => {
        const vid = Number(b.dataset.vid);
        const act = b.dataset.act;
        if (act === 'preview') preview(vid);
        else if (act === 'revert') revert(vid);
        else if (act === 'run') runTests(vid);
        else if (act === 'inspect') inspectRun(vid);
      });
    });
  }

  async function loadRunSummaries() {
    if (!testsApi) return;
    try {
      const res = await fetch(testsApi);
      if (!res.ok) return;
      const data = await res.json();
      runSummaries = data.runs || {};
    } catch (e) {}
  }

  async function runTests(vid) {
    const statusEl = $(`${prefix}-run-status`);
    try {
      const res = await fetch(`${testsApi}/run/${vid}`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        if (statusEl) statusEl.textContent = `Run failed: ${err.detail || res.status}`;
        return;
      }
      runningVid = vid;
      if (statusEl) statusEl.textContent = `Running tests against v${vid}…`;
      loadVersions();
      pollRun();
    } catch (e) {
      if (statusEl) statusEl.textContent = `Error: ${e.message}`;
    }
  }

  function pollRun() {
    if (runPollTimer) clearInterval(runPollTimer);
    runPollTimer = setInterval(async () => {
      try {
        const res = await fetch(`${testsApi}/run/status`);
        const s = await res.json();
        if (!s.running) {
          clearInterval(runPollTimer);
          runPollTimer = null;
          const vid = runningVid;
          runningVid = null;
          const statusEl = $(`${prefix}-run-status`);
          if (s.error) {
            if (statusEl) statusEl.textContent = `Run failed: ${s.error}`;
          } else {
            if (statusEl) statusEl.textContent = `Run complete for v${vid}.`;
          }
          await loadRunSummaries();
          loadVersions();
        }
      } catch (e) {}
    }, 1500);
  }

  async function inspectRun(vid) {
    const res = await fetch(`${testsApi}/runs/${vid}`);
    if (!res.ok) return;
    const run = await res.json();
    openInspectModal(vid, run, async (caseId, score) => {
      const r = await fetch(`${testsApi}/runs/${vid}/results/${caseId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ score }),
      });
      if (r.ok) {
        const updated = await r.json();
        runSummaries[vid] = {
          avg_score: updated.avg_score,
          ran_at: updated.ran_at,
          n: (updated.results || []).length,
        };
        loadVersions();
        return updated;
      }
      return null;
    });
  }

  async function preview(vid) {
    const res = await fetch(`${api}/versions/${vid}`);
    if (!res.ok) return;
    apply(await res.json());
    setStatus(`Previewing v${vid}. Save to create a new version, or revert to activate.`, false);
  }

  async function revert(vid) {
    const res = await fetch(`${api}/versions/${vid}/activate`, { method: 'POST' });
    if (!res.ok) { setStatus('Failed to revert.', true); return; }
    const v = await res.json();
    activeId = v.id;
    apply(v);
    setStatus(`Reverted to v${vid}.`);
    loadVersions();
  }

  async function saveNew() {
    saveBtn.disabled = true;
    setStatus('');
    const body = {
      system_prompt: systemEl.value,
      label: labelEl.value.trim(),
    };
    if (hasModel) body.model = modelEl.value;
    if (hasKb) body.kb = kbEl.value;
    try {
      const res = await fetch(api, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        setStatus(err.detail || 'Save failed.', true);
        return;
      }
      const v = await res.json();
      activeId = v.id;
      labelEl.value = '';
      setStatus(`Saved as v${v.id}.`);
      loadVersions();
    } catch (e) {
      setStatus(`Error: ${e.message}`, true);
    } finally {
      saveBtn.disabled = false;
    }
  }

  saveBtn.addEventListener('click', saveNew);
  (async () => {
    await loadActive();
    await loadRunSummaries();
    loadVersions();
  })();

  return { refreshVersions: async () => { await loadRunSummaries(); loadVersions(); } };
}

// --- inspect modal ------------------------------------------------------

function openInspectModal(vid, run, onRate) {
  const modal = $('inspect-modal');
  const title = $('inspect-title');
  const body = $('inspect-body');
  title.textContent = `Test run — v${vid} · avg ${run.avg_score == null ? '—' : run.avg_score.toFixed(2)} (${(run.results || []).length} cases)`;
  function render() {
    body.innerHTML = (run.results || []).map(r => {
      const scoreOpts = [1, 2, 3, 4, 5].map(n =>
        `<option value="${n}" ${r.score === n ? 'selected' : ''}>${n}</option>`
      ).join('');
      return `
        <div class="result-item" data-case-id="${r.case_id}">
          <div class="result-head">
            <span class="case-id">#${r.case_id}</span>
            ${r.manual_override ? '<span class="manual-tag">manual</span>' : ''}
            <label style="font-size:0.78rem;color:var(--text-muted);margin-left:auto;">Rating
              <select class="score-select" data-case-id="${r.case_id}">${scoreOpts}</select>
            </label>
          </div>
          ${r.patient_id != null ? `<div class="result-field"><div class="lbl">Card</div><div class="val">p${r.patient_id} · ${escapeHtml(r.checkpoint_date || '')}</div></div>` : ''}
          <div class="result-field"><div class="lbl">Question</div><div class="val">${escapeHtml(r.question)}</div></div>
          <div class="result-field"><div class="lbl">Ground truth</div><div class="val">${escapeHtml(r.gt_answer)}</div></div>
          <div class="result-field"><div class="lbl">LLM answer</div><div class="val">${escapeHtml(r.answer || '(empty)')}</div></div>
        </div>`;
    }).join('');
    body.querySelectorAll('.score-select').forEach(sel => {
      sel.addEventListener('change', async () => {
        const caseId = Number(sel.dataset.caseId);
        const newScore = Number(sel.value);
        const updated = await onRate(caseId, newScore);
        if (updated) {
          run.results = updated.results;
          run.avg_score = updated.avg_score;
          title.textContent = `Test run — v${vid} · avg ${updated.avg_score == null ? '—' : updated.avg_score.toFixed(2)} (${updated.results.length} cases)`;
          render();
        }
      });
    });
  }
  render();
  modal.classList.remove('hidden');
}

function closeInspectModal() {
  $('inspect-modal').classList.add('hidden');
}

// --- tests cases CRUD ---------------------------------------------------

async function loadCases(api, listId, renderRow) {
  const res = await fetch(api);
  if (!res.ok) return;
  const data = await res.json();
  const list = $(listId);
  list.innerHTML = (data.cases || []).map(renderRow).join('')
    || '<li style="color:var(--text-muted);font-size:0.85rem;">No cases yet. Add one above.</li>';
  list.querySelectorAll('.del').forEach(b => {
    b.addEventListener('click', async () => {
      const id = Number(b.dataset.id);
      if (!confirm(`Delete case #${id}?`)) return;
      const r = await fetch(`${api}/${id}`, { method: 'DELETE' });
      if (r.ok) loadCases(api, listId, renderRow);
    });
  });
}

function renderPatientCase(c) {
  return `
    <li class="case-item" data-id="${c.id}">
      <span class="id">#${c.id}</span>
      <div class="q">${escapeHtml(c.question)}</div>
      <div class="a">${escapeHtml(c.gt_answer)}</div>
      <button type="button" class="del" data-id="${c.id}">Delete</button>
    </li>`;
}

function renderClinicianCase(c) {
  return `
    <li class="case-item" style="grid-template-columns: 2rem 180px 1fr 1fr auto;" data-id="${c.id}">
      <span class="id">#${c.id}</span>
      <div style="font-size:0.78rem;color:var(--text-muted);">p${c.patient_id}<br>${escapeHtml(c.checkpoint_date)}</div>
      <div class="q">${escapeHtml(c.question)}</div>
      <div class="a">${escapeHtml(c.gt_answer)}</div>
      <button type="button" class="del" data-id="${c.id}">Delete</button>
    </li>`;
}

async function addPatientCase() {
  const q = $('pt-new-question').value.trim();
  const gt = $('pt-new-gt').value.trim();
  if (!q || !gt) return;
  const btn = $('pt-add-case-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/patient-chat-tests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: q, gt_answer: gt }),
    });
    if (res.ok) {
      $('pt-new-question').value = '';
      $('pt-new-gt').value = '';
      loadCases('/api/patient-chat-tests', 'pt-cases-list', renderPatientCase);
    }
  } finally {
    btn.disabled = false;
  }
}

async function addClinicianCase() {
  const pid = Number($('cl-new-patient').value);
  const week = $('cl-new-week').value.trim();
  const q = $('cl-new-question').value.trim();
  const gt = $('cl-new-gt').value.trim();
  if (!pid || !week || !q || !gt) { alert('patient id, week, question and GT answer all required'); return; }
  const btn = $('cl-add-case-btn');
  btn.disabled = true;
  try {
    const res = await fetch('/api/clinician-chat-tests', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        patient_id: pid,
        checkpoint_date: week,
        question: q,
        gt_answer: gt,
      }),
    });
    if (res.ok) {
      $('cl-new-patient').value = '';
      $('cl-new-week').value = '';
      $('cl-new-question').value = '';
      $('cl-new-gt').value = '';
      loadCases('/api/clinician-chat-tests', 'cl-cases-list', renderClinicianCase);
    } else {
      const err = await res.json().catch(() => ({}));
      alert(err.detail || `Save failed (${res.status})`);
    }
  } finally {
    btn.disabled = false;
  }
}

// --- corpus / reindex ---------------------------------------------------

let reindexPollTimer = null;

async function refreshCorpus() {
  const meta = $('kb-meta');
  const files = $('kb-files');
  try {
    const res = await fetch('/api/ops/corpus');
    if (!res.ok) throw new Error(`${res.status}`);
    const data = await res.json();
    meta.textContent = `${data.files.length} file(s) · ${data.index.chunk_count} chunks indexed · ${data.index.embed_model || 'no model'}`;
    files.innerHTML = data.files.map(f => `
      <li class="kb-file">
        <span class="name">${escapeHtml(f.filename)}</span>
        <span class="label">${escapeHtml(f.source_label)}</span>
        <span class="size">${fmtBytes(f.size_bytes)}</span>
        <span class="badge ${f.indexed ? '' : 'new'}">${f.indexed ? 'indexed' : 'pending'}</span>
        <button type="button" class="del" data-name="${escapeHtml(f.filename)}">Delete</button>
      </li>
    `).join('');
    files.querySelectorAll('.del').forEach(b => {
      b.addEventListener('click', () => deleteCorpusFile(b.dataset.name));
    });
  } catch (e) {
    meta.textContent = `Error loading corpus: ${e.message}`;
  }
}

async function deleteCorpusFile(name) {
  if (!confirm(`Delete ${name}? (chunks stay in index until next reindex)`)) return;
  const res = await fetch(`/api/ops/corpus/${encodeURIComponent(name)}`, { method: 'DELETE' });
  if (!res.ok) { setKbStatus(`Delete failed: ${res.status}`, 'err'); return; }
  setKbStatus(`Deleted ${name}. Run reindex to remove its chunks.`);
  refreshCorpus();
}

async function uploadCorpus() {
  const input = $('kb-upload-input');
  if (!input.files || !input.files.length) { setKbStatus('Select PDFs first.', 'err'); return; }
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  const btn = $('kb-upload-btn');
  btn.disabled = true;
  setKbStatus('Uploading…', 'run');
  try {
    const res = await fetch('/api/ops/corpus/upload', { method: 'POST', body: fd });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${res.status}`);
    }
    const data = await res.json();
    setKbStatus(`Uploaded ${data.saved.length} file(s). Run reindex to embed them.`);
    input.value = '';
    refreshCorpus();
  } catch (e) {
    setKbStatus(`Upload failed: ${e.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function startReindex() {
  const btn = $('kb-reindex-btn');
  btn.disabled = true;
  const force = $('kb-force').checked;
  setKbStatus('Starting reindex…', 'run');
  $('kb-log').hidden = false;
  $('kb-log').textContent = '';
  try {
    const res = await fetch(`/api/ops/corpus/reindex?force=${force}`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `${res.status}`);
    }
    pollReindex();
  } catch (e) {
    setKbStatus(`Reindex failed to start: ${e.message}`, 'err');
    btn.disabled = false;
  }
}

function pollReindex() {
  if (reindexPollTimer) clearInterval(reindexPollTimer);
  reindexPollTimer = setInterval(async () => {
    try {
      const res = await fetch('/api/ops/corpus/reindex/status');
      const s = await res.json();
      $('kb-log').textContent = (s.log || []).join('\n');
      $('kb-log').scrollTop = $('kb-log').scrollHeight;
      if (!s.running) {
        clearInterval(reindexPollTimer);
        reindexPollTimer = null;
        $('kb-reindex-btn').disabled = false;
        if (s.error) {
          setKbStatus(`Reindex failed: ${s.error}`, 'err');
        } else if (s.summary) {
          setKbStatus(
            `Done. ${s.summary.new_chunks} new chunks across ${s.summary.files_indexed.length} file(s). Total: ${s.summary.total_chunks}.`,
            'ok'
          );
        }
        refreshCorpus();
      } else {
        setKbStatus('Reindexing…', 'run');
      }
    } catch (e) {
      // transient fetch error — keep polling
    }
  }, 1500);
}

function setKbStatus(msg, kind = 'ok') {
  const el = $('kb-status');
  el.textContent = msg;
  el.className = `kb-status ${kind}`;
}

// --- init ---------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  makeConfigController({
    api: '/api/chat-config', hasKb: true, prefix: 'cl',
    testsApi: '/api/clinician-chat-tests',
  });
  makeConfigController({
    api: '/api/patient-chat-config', hasKb: false, prefix: 'pt',
    testsApi: '/api/patient-chat-tests',
  });
  makeConfigController({ api: '/api/triage-prompt-config', hasKb: false, hasModel: false, prefix: 'tr' });
  $('kb-upload-btn').addEventListener('click', uploadCorpus);
  $('kb-reindex-btn').addEventListener('click', startReindex);
  $('pt-add-case-btn').addEventListener('click', addPatientCase);
  $('cl-add-case-btn').addEventListener('click', addClinicianCase);
  $('inspect-close').addEventListener('click', closeInspectModal);
  $('inspect-modal').addEventListener('click', (e) => {
    if (e.target.id === 'inspect-modal') closeInspectModal();
  });
  loadCases('/api/patient-chat-tests', 'pt-cases-list', renderPatientCase);
  loadCases('/api/clinician-chat-tests', 'cl-cases-list', renderClinicianCase);
  // initial load of corpus so meta shows something once patient tab is opened
  refreshCorpus();
});
