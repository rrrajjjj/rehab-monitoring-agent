// /all page: patient search -> week picker -> generate -> list / detail / delete.

const state = {
  selectedPatient: null,   // {patient_id, patient_user}
  selectedWeek: null,
  weeks: [],
  cards: [],
};

const $ = (id) => document.getElementById(id);

function setStatus(msg, isErr = false) {
  const el = $("gen-status");
  el.textContent = msg;
  el.classList.toggle("err", isErr);
}

async function jsonFetch(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text || res.statusText}`);
  }
  return res.json();
}

// --- search --------------------------------------------------------------

async function doSearch() {
  const q = $("patient-query").value.trim();
  if (!q) return;
  const list = $("candidates");
  list.innerHTML = `<li style="cursor:default;color:var(--text-muted);">Searching…</li>`;
  list.hidden = false;
  try {
    const results = await jsonFetch(`/api/all/search?q=${encodeURIComponent(q)}`);
    renderCandidates(results);
  } catch (e) {
    list.innerHTML = `<li style="cursor:default;color:var(--attention-high);">${e.message}</li>`;
  }
}

function renderCandidates(results) {
  const list = $("candidates");
  if (!results.length) {
    list.innerHTML = `<li style="cursor:default;color:var(--text-muted);">No matches.</li>`;
    return;
  }
  list.innerHTML = "";
  for (const p of results) {
    const li = document.createElement("li");
    const who = p.patient_user ? ` — ${p.patient_user}` : "";
    li.textContent = `#${p.patient_id}${who}`;
    li.onclick = () => selectPatient(p);
    list.appendChild(li);
  }
}

// --- weeks ---------------------------------------------------------------

async function selectPatient(p) {
  state.selectedPatient = p;
  state.selectedWeek = null;
  for (const li of $("candidates").children) {
    li.classList.toggle("active", li.textContent.startsWith(`#${p.patient_id}`));
  }
  $("weeks-wrap").hidden = false;
  $("generate-btn").disabled = true;
  setStatus("");
  const grid = $("week-grid");
  grid.innerHTML = `<div style="color:var(--text-muted);font-size:0.85rem;">Loading weeks…</div>`;
  try {
    const resp = await jsonFetch(`/api/all/weeks/${p.patient_id}`);
    state.weeks = resp.weeks || [];
    renderWeeks();
  } catch (e) {
    grid.innerHTML = `<div style="color:var(--attention-high);font-size:0.85rem;">${e.message}</div>`;
  }
}

function renderWeeks() {
  const grid = $("week-grid");
  if (!state.weeks.length) {
    grid.innerHTML = `<div style="color:var(--text-muted);font-size:0.85rem;">No active weeks for this patient.</div>`;
    return;
  }
  grid.innerHTML = "";
  // newest first
  for (const w of [...state.weeks].reverse()) {
    const chip = document.createElement("div");
    chip.className = "week-chip";
    chip.textContent = w;
    chip.onclick = () => selectWeek(w, chip);
    grid.appendChild(chip);
  }
}

function selectWeek(w, chipEl) {
  state.selectedWeek = w;
  for (const c of $("week-grid").children) c.classList.remove("active");
  chipEl.classList.add("active");
  $("generate-btn").disabled = false;
  setStatus("");
}

// --- generate ------------------------------------------------------------

async function generate() {
  if (!state.selectedPatient || !state.selectedWeek) return;
  $("generate-btn").disabled = true;
  setStatus("Running pipeline… this may take 10–30s.");
  try {
    const body = JSON.stringify({
      patient_id: state.selectedPatient.patient_id,
      checkpoint_date: state.selectedWeek,
    });
    await jsonFetch("/api/all/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    setStatus("Card generated.");
    await loadCards();
  } catch (e) {
    setStatus(e.message, true);
  } finally {
    $("generate-btn").disabled = false;
  }
}

// --- card list / detail / delete ----------------------------------------

async function loadCards() {
  try {
    state.cards = await jsonFetch("/api/all/cards");
    renderCards();
  } catch (e) {
    $("cards-list").innerHTML = `<div style="color:var(--attention-high);">${e.message}</div>`;
  }
}

function renderCards() {
  const el = $("cards-list");
  if (!state.cards.length) {
    el.innerHTML = `<div style="color:var(--text-muted);font-size:0.88rem;">No generated cards yet.</div>`;
    return;
  }
  el.innerHTML = "";
  // newest first by checkpoint_date
  const sorted = [...state.cards].sort((a, b) =>
    b.checkpoint_date.localeCompare(a.checkpoint_date)
  );
  for (const c of sorted) {
    const row = document.createElement("div");
    row.className = "card-row";
    row.onclick = () => openDetail(c);
    row.innerHTML = `
      <span class="pid">#${c.patient_id}</span>
      <span class="cp">${c.checkpoint_date}</span>
      <span class="headline">${escapeHtml(c.headline || "—")}</span>
      <span class="disp ${c.disposition || ""}">${c.disposition || ""}</span>
      <button type="button" class="del-btn">Delete</button>
    `;
    row.querySelector(".del-btn").onclick = (e) => {
      e.stopPropagation();
      deleteCard(c);
    };
    el.appendChild(row);
  }
}

async function deleteCard(c) {
  if (!confirm(`Delete card for patient ${c.patient_id} / ${c.checkpoint_date}?`)) return;
  try {
    await jsonFetch(`/api/all/cards/${c.patient_id}/${c.checkpoint_date}`, {
      method: "DELETE",
    });
    $("detail-pane").classList.remove("open");
    await loadCards();
  } catch (e) {
    alert(e.message);
  }
}

let detailView = null;

function showList() {
  document.querySelector(".all-app").style.display = "";
  const host = $("all-detail");
  host.classList.add("hidden");
  if (detailView) { detailView.destroy(); detailView = null; }
}

function showDetailHost() {
  document.querySelector(".all-app").style.display = "none";
  const host = $("all-detail");
  host.classList.remove("hidden");
  return host;
}

async function openDetail(c) {
  const host = showDetailHost();
  host.innerHTML = `<div style="padding:2rem;color:var(--text-muted);">Loading…</div>`;
  try {
    const d = await jsonFetch(`/api/all/cards/${c.patient_id}/${c.checkpoint_date}`);
    host.innerHTML = "";
    if (detailView) detailView.destroy();
    detailView = window.DetailView.create(host, {
      chatEndpoint: "/api/chat",
      onBack: showList,
    });
    detailView.render(d);
  } catch (e) {
    host.innerHTML = `<div style="padding:2rem;color:var(--attention-high);">${e.message}</div>
      <div style="padding:0 2rem;"><button type="button" onclick="showList()">← Back</button></div>`;
  }
}

function renderDetail(d) {
  const card = d.card || {};
  const obs = (card.observations || card.evidence || []).map((o, i) => {
    const att = o.attention || 1;
    return `<li class="obs-item attention-${att}">${escapeHtml(o.text || "")}</li>`;
  }).join("");
  const adh = d.adherence || {};
  const drift = (d.drift_events || []).map(e => e.type).join(", ") || "—";
  const sessN = (d.sessions || []).length;
  const pct = (v) => v == null ? "—" : `${Math.round(v * 100)}%`;
  return `
    <h2>${escapeHtml(card.headline || "Triage card")}</h2>
    <div class="meta">Patient #${d.patient_id} · Week ending ${d.checkpoint_date} · ${card.disposition || ""}</div>
    ${obs ? `<ul class="obs-list">${obs}</ul>` : `<div class="meta">No observations.</div>`}
    <div class="stats-grid">
      <div class="stat"><div class="k">Sessions</div><div class="v">${sessN}</div></div>
      <div class="stat"><div class="k">Adherence</div><div class="v">${pct(adh.adherence_minutes)}</div></div>
      <div class="stat"><div class="k">Done / Planned</div><div class="v">${Math.round(adh.done_total || 0)} / ${Math.round(adh.planned_total || 0)} min</div></div>
      <div class="stat"><div class="k">Drift</div><div class="v" style="font-size:0.85rem;">${drift}</div></div>
    </div>
  `;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// --- init ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  $("search-btn").onclick = doSearch;
  $("patient-query").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  $("generate-btn").onclick = generate;
  loadCards();
});
