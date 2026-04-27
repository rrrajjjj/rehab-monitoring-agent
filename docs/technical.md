# CRTV — Technical Documentation

Companion to `CLAUDE.md`. Focus here: *where things live, how they connect, where to make changes*.

## 1. High-level picture

Two products glued together in one FastAPI app:

1. **Triage pipeline** — turns a patient's weekly session data into a clinician-facing triage card.
2. **Chatbots** — a clinician Q&A bot scoped to a single card, and a patient-facing RAG companion grounded in stroke-care PDFs.

One web server serves four pages (`/`, `/demo`, `/all`, `/ops`). All state is on disk — JSON card files, JSONL/NPY vector index, config version files. No database in the app itself (the **source** DB is MySQL, but no schema is written back).

## 2. Directory layout

```
medgemma agent/
├── crtv/                        # Triage pipeline + web API
│   ├── adapters/                # Data-source abstraction
│   │   ├── database.py          # DatabaseAdapter ABC (the interface)
│   │   ├── csv_adapter.py       # CSVDataAdapter (local NEST dump)
│   │   ├── mysql_adapter.py     # MySQLDataAdapter (live DB, SQLAlchemy)
│   │   ├── mock_adapter.py      # MockAdapter (fixtures)
│   │   └── __init__.py          # get_adapter() — selects by env
│   ├── repositories/            # Bundle rows into a time window
│   ├── builders/                # Raw rows → domain objects (sessions, ticks...)
│   ├── features/                # Adherence, signals, PPF, learning rate
│   ├── drift/                   # DriftDetector + PatientStateBuilder
│   ├── checkin/                 # Self-report parsing (rule-based)
│   ├── reasoning/
│   │   ├── medgemma_triage.py   # Triage LLM orchestrator
│   │   ├── patient_checkin.py   # PatientCheckInEngine (weekly motivational msg)
│   │   └── llm_providers.py     # Rule / MedGemma / OpenAI + CachingProvider
│   ├── recommendations/engine.py
│   ├── cards/                   # Triage card renderers
│   ├── pipeline/
│   │   ├── runner.py            # Batch triage for a given date
│   │   ├── historical_runner.py # Weekly-checkpoint mode (incl. run_single_checkpoint)
│   │   └── metrics_builder.py   # Combines features → metrics dict
│   ├── domain/models.py         # Pydantic v2 models (~47 classes)
│   └── web/
│       ├── api.py               # FastAPI app, ALL routes declared here
│       ├── service.py           # ClinicianViewService (mock)
│       ├── historical_service.py# HistoricalTriageService (real data)
│       ├── patient_checkin_service.py
│       ├── all_service.py       # AllPatientsService (on-demand /all)
│       └── static/              # HTML/CSS/JS frontend
│           ├── index.html       # Landing
│           ├── demo.html        # Clinician + patient views + chat drawers
│           ├── app.js           # ~850 lines, everything demo.html needs
│           ├── all.html / all.js
│           └── ops.html / ops.js
├── chatbot/                     # Chatbot subsystem
│   ├── corpus/                  # Source PDFs (uploaded via Ops)
│   ├── index/                   # Built vector index
│   │   ├── chunks.jsonl         # One chunk per line w/ enrichment metadata
│   │   ├── vectors.npy          # (N, 1536) float32 embeddings
│   │   └── manifest.json        # sha256 per PDF, chunk_count, embed_model
│   ├── ingest.py                # PDF → LLM chunk + enrich → embed (parallel)
│   ├── retriever.py             # Cosine-similarity search (singleton, lazy load)
│   ├── chat_service.py          # Clinician chat (card-scoped, no retrieval)
│   ├── patient_chat_service.py  # Patient RAG chat (retrieval + optional card ctx)
│   ├── config_store.py          # Clinician chat config (prompt + KB + model)
│   ├── patient_config_store.py  # Patient chat config (prompt + model only)
│   ├── triage_prompt_store.py   # Triage card generation prompt (versioned)
│   ├── patient_tests_store.py   # Q/GT cases + per-version judge runs
│   ├── patient_tests_runner.py  # LLM-as-judge test executor
│   ├── config/versions/v*.json  # Clinician versions (active pointer: active.json)
│   ├── patient_config/…         # Patient versions
│   ├── triage_prompts/…         # Triage versions
│   └── patient_tests/           # cases.json + runs/v*.json (latest run per version)
├── demo_mining/
│   ├── card_store.py            # CardStore — JSON-per-card persistence
│   └── card_store/              # The curated /demo cohort (read-only in app)
├── card_store_all/              # On-demand cards created from /all (separate dir)
├── data/                        # Optional NEST CSV dump (CSV backend)
├── prompts/                     # System prompts for triage LLM
│   ├── triage_system.txt
│   └── patient_checkin.txt
├── tests/
├── .env                         # Config (secrets live here)
├── run_web.py                   # Server launcher
└── requirements.txt
```

## 3. Request flow by page

### `/demo` (curated cohort, read-only)
```
demo.html + app.js
  ↓ GET /api/patients
HistoricalTriageService.list_patients_grouped()
  ↓ reads
demo_mining/card_store/*.json
```
No pipeline runs here — cards are pre-built by `python -m demo_mining.build_cards` and committed. `shortlist.json` filters which patients appear.

### `/all` (on-demand generation)
```
all.html + all.js
  ↓ GET /api/all/search?q=…
AllPatientsService.search_patients → DatabaseAdapter.resolve_patient

  ↓ GET /api/all/weeks/{pid}
AllPatientsService.patient_weeks → DatabaseAdapter.patient_active_weeks

  ↓ POST /api/all/run {patient_id, checkpoint_date}
AllPatientsService.generate
  → HistoricalTriageRunner.run_single_checkpoint  (full pipeline for 1 week)
  → PatientCheckInEngine.generate                 (patient-facing message)
  → CardStore.save                                (writes card_store_all/p{pid}_w{date}.json)

  ↓ GET /api/all/cards               → list
  ↓ GET /api/all/cards/{pid}/{cp}    → detail
  ↓ DELETE /api/all/cards/{pid}/{cp} → remove
```

### `/ops`
Two tabs, two config stores, plus corpus management. See §6.

### `/demo` patient-view chatbot (RAG)
```
demo.html floating FAB
  ↓ POST /api/patient-chat {messages, patient_id?, checkpoint_date?}
PatientChatService.chat
  1. Retriever.search(last_user_turn, k=5)      → chatbot/index/
  2. _load_card(...) if patient context given   → card_store_all or demo_mining/card_store
  3. _summarize_card(...)                       → compact stats (no time series)
  4. Compose: patient_config prompt + passages + patient summary
  5. OpenAI chat completion
  6. Return {response, citations[], patient_grounded}
```

Frontend state (`app.js::ptChat`):
- `mode`: `'standalone' | 'integrated'` — toggle in the drawer header. In standalone mode no `patient_id`/`checkpoint_date` is sent; in integrated mode `ptChat.boundPatient` is forwarded and the service weaves the card summary in.
- `boundPatient`: `{patient_id, checkpoint_date}` chosen from a dropdown populated by `/api/patient-checkins` (most-recent checkpoint per patient).
- `greeted`: greeting is emitted **once** per session (on first open or after Reset). Minimize (×) preserves the transcript; Reset clears it and re-greets. Selecting a different bound patient also resets so the bot's framing re-anchors.

## 4. Data backend selection

Set in `.env` via `CRTV_DATA_BACKEND`:

| Value | Effect |
|---|---|
| `mysql` | force MySQL (requires `DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME`, `DB_PORT`) |
| `csv` | force CSV under `CRTV_DATA_DIR` (default `./data`) |
| `auto` (default) | MySQL if `DB_HOST` set, else CSV if `data/` exists, else Mock |

`crtv/adapters/__init__.py::get_adapter(data_dir)` applies this. All services (`ClinicianViewService`, `HistoricalTriageService`, `AllPatientsService`, `MetricsBuilder`) go through it — **don't instantiate adapters directly** elsewhere.

## 5. Adding things

### Add a new API endpoint
Edit `crtv/web/api.py` only. It's a flat file — add near the related block.

### Add a new page
1. New `crtv/web/static/<page>.html` + `<page>.js`
2. Route in `api.py`:
   ```python
   @app.get("/<page>")
   def <page>_page():
       return FileResponse(FRONTEND_DIR / "<page>.html")
   ```
3. Link from `index.html` landing grid.

### Add a new data source field (e.g. new column from DB)
1. Extend `DatabaseAdapter` ABC (`crtv/adapters/database.py`) with the new method.
2. Implement in `csv_adapter.py` and `mysql_adapter.py`.
3. Wire into the builder / feature that needs it.

### Change the triage LLM prompt
Use Ops → **Triage prompt** tab (versioned, active-pointer, same pattern as the chat configs). Stored in `chatbot/triage_config/versions/v*.json` with `chatbot/triage_config/active.json` as the pointer. `medgemma_triage.py::_load_triage_prompt()` reads the store first and falls back to `prompts/triage_system.txt` (which still serves as the seed for v1 on first boot).

### Change chatbot prompt/model
Use the Ops UI — it creates a new version in `chatbot/config/versions/` or `chatbot/patient_config/versions/`. Default prompts live in code (`config_store.py` / `patient_config_store.py`) and only seed the first version.

### Change retrieval strategy
`chatbot/retriever.py` — currently dense cosine over `text-embedding-3-small`. Hybrid/rerank would plug in here.

### Change the chunking prompt
`chatbot/ingest.py::CHUNK_SYSTEM_PROMPT`. After editing, run a `--force` reindex.

### Swap the embedding model
Set `CRTV_EMBED_MODEL` and reindex with `--force`. The manifest stores the model used, but the retriever assumes any saved vectors share the current model — changing it without rebuilding yields silent garbage.

### Add a PDF to the KB
Drop it in `chatbot/corpus/` (or upload via Ops → Patient tab → Knowledge base), then click Reindex. Unchanged PDFs are skipped (sha256). Human-friendly source label goes in `SOURCE_LABELS` in `ingest.py`; otherwise the filename stem is used.

### Change where on-demand cards are stored
`AllPatientsService(card_store_path=...)` in `api.py`. Two stores are kept separate on purpose: `demo_mining/card_store/` is the frozen curated demo; `card_store_all/` is scratch space for /all.

## 6. Configuration stores

Both chatbots and the triage-pipeline prompt use an append-only versioned JSON store with a pointer to the active version.

| | Clinician chat | Patient chat | Triage prompt |
|---|---|---|---|
| Module | `chatbot/config_store.py` | `chatbot/patient_config_store.py` | `chatbot/triage_prompt_store.py` |
| Versions dir | `chatbot/config/versions/` | `chatbot/patient_config/versions/` | `chatbot/triage_config/versions/` |
| Active pointer | `chatbot/config/active.json` | `chatbot/patient_config/active.json` | `chatbot/triage_config/active.json` |
| Fields | system_prompt, **kb** (appended to prompt), model | system_prompt, model | system_prompt |
| API | `/api/chat-config*` | `/api/patient-chat-config*` | `/api/triage-prompt-config*` |

The patient store has no `kb` because retrieval handles grounding. The triage store has neither (the triage model is controlled by `CRTV_OPENAI_MODEL`; retrieval is not used).

## 7. Env vars

Key ones (see `.env.example` for the full list):

| Var | Purpose |
|---|---|
| `CRTV_LLM_PROVIDER` | `rule` \| `medgemma` \| `openai` |
| `CRTV_OPENAI_API_KEY` | OpenAI key (used for triage LLM, chunking, embedding, chats) |
| `CRTV_OPENAI_MODEL` | Triage-pipeline model |
| `CRTV_CHUNK_MODEL` | Chunking model (default `gpt-5.4-mini`) |
| `CRTV_CHUNK_CONCURRENCY` | Parallel LLM calls during ingest (default 8) |
| `CRTV_EMBED_MODEL` | Embedding model (default `text-embedding-3-small`) |
| `CRTV_LLM_USE_CACHE` | `1` enables deterministic `(pid, model, week)` cache in `.llm_cache/` |
| `CRTV_DATA_BACKEND` | `mysql` \| `csv` \| `auto` |
| `DB_HOST`, `DB_USER`, `DB_PASS`, `DB_NAME`, `DB_PORT` | MySQL credentials |
| `CRTV_DATA_DIR` | CSV backend root (default `./data`) |
| `CRTV_USE_MEDGEMMA` | Route triage through MedGemma |
| `CRTV_TRIAL_MODE` | 1 = 1 patient × 3 weeks (dev speed-up) |

## 8. Gotchas

- **`SESSION_DURATION` is always seconds.** Divide by 60 to get minutes. Both adapters do this unconditionally; don't re-introduce a "looks like it's already minutes" heuristic.
- **Route ordering matters in FastAPI.** `/api/patients/{pid}` will greedily match `/api/patients/search`. Namespace searches under their own path (we use `/api/all/search`).
- **`HistoricalTriageService._cache_done`** is a list cache. `AllPatientsService` flips it to `False` after every write so new cards show up. If you add another writer, do the same.
- **`session_plus` lacks `PATIENT_ID`** in the live DB. Sessions are joined via `prescription_plus.PRESCRIPTION_ID`. The MySQL adapter encodes this; don't try to query `session_plus` by patient directly.
- **The reindex runs as a FastAPI background task** (in-process). Restarting the server mid-reindex aborts it. Partial state is safe: `chunks.jsonl` and `vectors.npy` are only rewritten at the end.
- **Prompt edits in Ops only activate the newly saved version.** Old cached clinician-chat or triage LLM responses still reflect the old prompt. Bump the cache key or clear `.llm_cache/` if you need fresh output.
- **`patient_config_store._ensure_init()`** seeds v1 from the hard-coded default prompt on first access. Deleting `chatbot/patient_config/` resets to defaults.
- **Patient-chat test runs are standalone only.** `patient_tests_runner.run_tests()` calls the chat flow with no patient card attached and invokes the judge with `CRTV_TEST_JUDGE_MODEL` (defaults to `gpt-5.4-mini`). Runs are keyed by version; re-running a version overwrites `chatbot/patient_tests/runs/v{vid}.json`. Only one run at a time (guarded by `_TESTS_STATE` in `api.py`). Manual rating overrides set `manual_override: true` on the result and recompute `avg_score`.
