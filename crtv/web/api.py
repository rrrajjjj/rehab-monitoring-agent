"""FastAPI app for clinician frontend."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root before reading env vars
load_dotenv(Path(__file__).parent.parent.parent / ".env")

# Logging: ensure crtv.* always output (uvicorn may override root logger)
import logging
import sys
_log_level = getattr(logging, (os.environ.get("CRTV_LOG_LEVEL") or "INFO").upper(), logging.INFO)
_log_fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
for _name in ("crtv.llm", "crtv.triage"):
    _lg = logging.getLogger(_name)
    _lg.setLevel(_log_level)
    if not _lg.handlers:
        _h = logging.StreamHandler(sys.stderr)
        _h.setFormatter(logging.Formatter(_log_fmt))
        _lg.addHandler(_h)

from datetime import datetime

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse

from pydantic import BaseModel

from crtv.adapters.mock_adapter import MockAdapter
from crtv.web.service import ClinicianViewService
from crtv.web.historical_service import HistoricalTriageService
from crtv.web.patient_checkin_service import PatientCheckInService
from crtv.web.all_service import AllPatientsService
from chatbot.chat_service import ChatService
from chatbot.patient_chat_service import PatientChatService
from chatbot import config_store as chat_config_store
from chatbot import patient_config_store
from chatbot import triage_prompt_store
from chatbot import patient_tests_store
from chatbot import patient_tests_runner
from chatbot import clinician_tests_store
from chatbot import clinician_tests_runner
from chatbot import ingest as chatbot_ingest

app = FastAPI(title="Eodyne Systems — Rehabilitation Monitoring", version="0.1.0")

# Backend selection: explicit env > mysql (DB_HOST set) > csv (data dir exists) > mock.
DATA_DIR = Path(__file__).parent.parent.parent / "data"
if os.environ.get("CRTV_DATA_DIR"):
    DATA_DIR = Path(os.environ["CRTV_DATA_DIR"])
_BACKEND_ENV = (os.environ.get("CRTV_DATA_BACKEND") or "auto").lower()
_MYSQL_SET = bool(os.environ.get("DB_HOST"))
USE_MYSQL = _BACKEND_ENV == "mysql" or (_BACKEND_ENV == "auto" and _MYSQL_SET)
USE_REAL_DATA = USE_MYSQL or (_BACKEND_ENV in ("csv", "auto") and DATA_DIR.exists())
USE_MEDGEMMA = os.environ.get("CRTV_USE_MEDGEMMA", "").lower() in ("1", "true", "yes")

if USE_REAL_DATA:
    from crtv.adapters import get_adapter
    data_adapter = get_adapter(DATA_DIR)
    historical_service = HistoricalTriageService(DATA_DIR, use_medgemma=USE_MEDGEMMA)
    service = ClinicianViewService(data_adapter)
    checkin_service = PatientCheckInService(DATA_DIR, use_medgemma=USE_MEDGEMMA)
    all_service: AllPatientsService | None = AllPatientsService(
        DATA_DIR, use_medgemma=USE_MEDGEMMA
    )
else:
    historical_service = None
    adapter = MockAdapter()
    service = ClinicianViewService(adapter)
    checkin_service = PatientCheckInService(adapter=adapter, use_medgemma=USE_MEDGEMMA)
    all_service = None

DEFAULT_RUN_DATE = datetime(2024, 1, 25)

chat_service = ChatService()
patient_chat_service = PatientChatService()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/mode")
def get_mode():
    """Whether using real NEST data, mock, and which LLM provider is active."""
    from crtv.reasoning.llm_providers import get_provider
    provider = get_provider(use_medgemma=USE_MEDGEMMA)
    return {
        "use_real_data": USE_REAL_DATA,
        "use_medgemma": USE_MEDGEMMA,
        "llm_provider": type(provider).__name__.replace("Provider", "").lower(),
    }


@app.get("/api/llm-verify")
def llm_verify():
    """
    Make one direct OpenAI API call (bypasses cache).
    Use to verify API key works and requests show up on platform.openai.com.
    """
    from crtv.reasoning.llm_providers import OpenAICompatibleProvider
    provider = OpenAICompatibleProvider()
    if not provider.api_key:
        return {"ok": False, "error": "CRTV_OPENAI_API_KEY not set", "response": None}
    try:
        raw = provider.generate("Reply with exactly: OK")
        ok = bool(raw and "ok" in raw.lower())
        return {
            "ok": ok,
            "error": None if ok else "Empty or unexpected response",
            "response": raw[:200] if raw else None,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "response": None}


@app.get("/api/triage-cards")
def list_triage_cards():
    """All triage cards from historical run (real data mode)."""
    if not historical_service:
        return []
    return historical_service.list_triage_cards()


@app.get("/api/patients")
def list_patients(run_date: str | None = None):
    """List patients. In real data mode: grouped by patient, max attention across triages."""
    if USE_REAL_DATA and historical_service:
        return historical_service.list_patients_grouped()
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    return service.list_patients(rd)


@app.get("/api/triage-cards/{patient_id}/{checkpoint_date}")
def get_triage_card_detail(patient_id: int, checkpoint_date: str):
    """Full detail for a triage card."""
    if not historical_service:
        raise HTTPException(status_code=404, detail="Real data mode required")
    detail = historical_service.get_card_detail(patient_id, checkpoint_date)
    if detail is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return detail


@app.get("/api/patients/{patient_id}")
def get_patient_detail(patient_id: int, run_date: str | None = None):
    """Full triage card + observations. In real data mode, returns primary checkpoint (highest attention)."""
    if USE_REAL_DATA and historical_service:
        detail = historical_service.get_patient_detail(patient_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        return detail
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail


@app.get("/api/patients/{patient_id}/adherence")
def get_patient_adherence(patient_id: int, run_date: str | None = None):
    """Adherence calendar data (mock mode)."""
    if not service:
        raise HTTPException(status_code=404)
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail.get("adherence", {})


@app.get("/api/patients/{patient_id}/metrics")
def get_patient_metrics(patient_id: int, run_date: str | None = None):
    """Metric time-series for charts (mock mode)."""
    if not service:
        raise HTTPException(status_code=404)
    rd = datetime.fromisoformat(run_date) if run_date else DEFAULT_RUN_DATE
    detail = service.get_patient_detail(patient_id, rd)
    if detail is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return detail.get("metrics", {})


# --- Patient check-in endpoints ---


@app.get("/api/patient-checkins")
def list_patient_checkins():
    """All patient check-in messages (one per patient per checkpoint)."""
    return checkin_service.list_checkins()


@app.get("/api/patient-checkins/{patient_id}")
def get_patient_checkin(patient_id: int):
    """All weekly check-in messages for a patient, oldest first."""
    result = checkin_service.get_patient_checkins(patient_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    return result


@app.get("/api/patient-checkins/{patient_id}/{checkpoint_date}")
def get_patient_checkin_detail(patient_id: int, checkpoint_date: str):
    """Single check-in message for a specific patient + week."""
    result = checkin_service.get_checkin_detail(patient_id, checkpoint_date)
    if result is None:
        raise HTTPException(status_code=404, detail="Check-in not found")
    return result


# --- /all: on-demand card generation ---


def _require_all_service() -> AllPatientsService:
    if all_service is None:
        raise HTTPException(status_code=503, detail="Real data backend required for /all")
    return all_service


@app.get("/api/all/search")
def search_patients(q: str):
    """Resolve a free-text query (patient id or PATIENT_USER substring)."""
    svc = _require_all_service()
    return svc.search_patients(q)


@app.get("/api/all/weeks/{patient_id}")
def get_patient_weeks(patient_id: int):
    """Active weeks (ISO date of the Sunday ending each week with ≥1 session)."""
    svc = _require_all_service()
    weeks = svc.patient_weeks(patient_id)
    return {"patient_id": patient_id, "weeks": weeks}


class AllRunRequest(BaseModel):
    patient_id: int
    checkpoint_date: str


@app.post("/api/all/run")
def run_all(req: AllRunRequest):
    svc = _require_all_service()
    result = svc.generate(req.patient_id, req.checkpoint_date)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No data for patient {req.patient_id} at {req.checkpoint_date}",
        )
    return result


@app.get("/api/all/cards")
def list_all_cards():
    svc = _require_all_service()
    return svc.list_cards()


@app.get("/api/all/cards/{patient_id}/{checkpoint_date}")
def get_all_card_detail(patient_id: int, checkpoint_date: str):
    svc = _require_all_service()
    detail = svc.get_card_detail(patient_id, checkpoint_date)
    if detail is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return detail


@app.delete("/api/all/cards/{patient_id}/{checkpoint_date}")
def delete_all_card(patient_id: int, checkpoint_date: str):
    svc = _require_all_service()
    if not svc.delete_card(patient_id, checkpoint_date):
        raise HTTPException(status_code=404, detail="Card not found")
    return {"ok": True}


# --- Chatbot endpoints ---


class ChatRequest(BaseModel):
    patient_id: int
    checkpoint_date: str
    messages: list[dict]


class ChatConfigRequest(BaseModel):
    system_prompt: str
    kb: str
    model: str
    label: str = ""


@app.post("/api/chat")
def chat_endpoint(req: ChatRequest):
    return chat_service.chat(req.patient_id, req.checkpoint_date, req.messages)


@app.get("/api/chat-config")
def get_chat_config():
    return chat_config_store.get_active()


@app.get("/api/chat-config/versions")
def list_chat_versions():
    return chat_config_store.list_versions()


@app.get("/api/chat-config/versions/{vid}")
def get_chat_version(vid: int):
    v = chat_config_store.get_version(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@app.post("/api/chat-config")
def save_chat_config(req: ChatConfigRequest):
    if req.model not in chat_config_store.ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model must be one of {chat_config_store.ALLOWED_MODELS}",
        )
    return chat_config_store.save_new(req.system_prompt, req.kb, req.model, req.label)


@app.post("/api/chat-config/versions/{vid}/activate")
def activate_chat_version(vid: int):
    v = chat_config_store.activate(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


# --- Patient-facing RAG chat ---


class PatientChatRequest(BaseModel):
    messages: list[dict]
    patient_id: int | None = None
    checkpoint_date: str | None = None


@app.post("/api/patient-chat")
def patient_chat_endpoint(req: PatientChatRequest):
    return patient_chat_service.chat(
        req.messages,
        patient_id=req.patient_id,
        checkpoint_date=req.checkpoint_date,
    )


@app.post("/api/patient-chat/stream")
def patient_chat_stream_endpoint(req: PatientChatRequest):
    def gen():
        for evt in patient_chat_service.chat_stream(
            req.messages,
            patient_id=req.patient_id,
            checkpoint_date=req.checkpoint_date,
        ):
            yield f"data: {json.dumps(evt)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Patient chatbot config (no KB field — uses vector retriever) ---


class PatientChatConfigRequest(BaseModel):
    system_prompt: str
    model: str
    label: str = ""


@app.get("/api/patient-chat-config")
def get_patient_chat_config():
    return patient_config_store.get_active()


@app.get("/api/patient-chat-config/versions")
def list_patient_chat_versions():
    return patient_config_store.list_versions()


@app.get("/api/patient-chat-config/versions/{vid}")
def get_patient_chat_version(vid: int):
    v = patient_config_store.get_version(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@app.post("/api/patient-chat-config")
def save_patient_chat_config(req: PatientChatConfigRequest):
    if req.model not in patient_config_store.ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"model must be one of {patient_config_store.ALLOWED_MODELS}",
        )
    return patient_config_store.save_new(req.system_prompt, req.model, req.label)


@app.post("/api/patient-chat-config/versions/{vid}/activate")
def activate_patient_chat_version(vid: int):
    v = patient_config_store.activate(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


# --- Patient chat tests (Q/GT + LLM-judge runs) ---


class PatientTestCaseRequest(BaseModel):
    question: str
    gt_answer: str


class PatientTestRatingRequest(BaseModel):
    score: int


_TESTS_STATE: dict = {"running": False, "version_id": None, "error": None}


@app.get("/api/patient-chat-tests")
def list_patient_tests():
    return {
        "cases": patient_tests_store.list_cases(),
        "runs": patient_tests_store.all_run_summaries(),
    }


@app.post("/api/patient-chat-tests")
def add_patient_test(req: PatientTestCaseRequest):
    if not req.question.strip() or not req.gt_answer.strip():
        raise HTTPException(status_code=400, detail="question and gt_answer required")
    return patient_tests_store.add_case(req.question, req.gt_answer)


@app.put("/api/patient-chat-tests/{case_id}")
def update_patient_test(case_id: int, req: PatientTestCaseRequest):
    c = patient_tests_store.update_case(case_id, req.question, req.gt_answer)
    if c is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return c


@app.delete("/api/patient-chat-tests/{case_id}")
def delete_patient_test(case_id: int):
    if not patient_tests_store.delete_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return {"ok": True}


def _run_patient_tests(version_id: int):
    _TESTS_STATE["running"] = True
    _TESTS_STATE["version_id"] = version_id
    _TESTS_STATE["error"] = None
    try:
        patient_tests_runner.run_tests(version_id)
    except Exception as e:
        _TESTS_STATE["error"] = str(e)
    finally:
        _TESTS_STATE["running"] = False


@app.post("/api/patient-chat-tests/run/{version_id}")
def start_patient_test_run(version_id: int, background_tasks: BackgroundTasks):
    if _TESTS_STATE["running"]:
        raise HTTPException(status_code=409, detail="A test run is already in progress")
    if patient_config_store.get_version(version_id) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if not patient_tests_store.list_cases():
        raise HTTPException(status_code=400, detail="No test cases defined")
    background_tasks.add_task(_run_patient_tests, version_id)
    return {"started": True, "version_id": version_id}


@app.get("/api/patient-chat-tests/run/status")
def patient_test_run_status():
    return dict(_TESTS_STATE)


@app.get("/api/patient-chat-tests/runs/{version_id}")
def get_patient_test_run(version_id: int):
    run = patient_tests_store.get_run(version_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No run for this version")
    return run


@app.put("/api/patient-chat-tests/runs/{version_id}/results/{case_id}")
def override_patient_test_rating(version_id: int, case_id: int, req: PatientTestRatingRequest):
    if not (1 <= req.score <= 5):
        raise HTTPException(status_code=400, detail="score must be 1..5")
    run = patient_tests_store.override_rating(version_id, case_id, req.score)
    if run is None:
        raise HTTPException(status_code=404, detail="Run or case not found")
    return run


# --- Clinician chat tests (card-scoped Q/GT + LLM-judge runs) ---


class ClinicianTestCaseRequest(BaseModel):
    patient_id: int
    checkpoint_date: str
    question: str
    gt_answer: str


_CL_TESTS_STATE: dict = {"running": False, "version_id": None, "error": None}


@app.get("/api/clinician-chat-tests")
def list_clinician_tests():
    return {
        "cases": clinician_tests_store.list_cases(),
        "runs": clinician_tests_store.all_run_summaries(),
    }


@app.post("/api/clinician-chat-tests")
def add_clinician_test(req: ClinicianTestCaseRequest):
    if not req.question.strip() or not req.gt_answer.strip():
        raise HTTPException(status_code=400, detail="question and gt_answer required")
    return clinician_tests_store.add_case(
        req.patient_id, req.checkpoint_date, req.question, req.gt_answer
    )


@app.put("/api/clinician-chat-tests/{case_id}")
def update_clinician_test(case_id: int, req: ClinicianTestCaseRequest):
    c = clinician_tests_store.update_case(
        case_id, req.patient_id, req.checkpoint_date, req.question, req.gt_answer
    )
    if c is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return c


@app.delete("/api/clinician-chat-tests/{case_id}")
def delete_clinician_test(case_id: int):
    if not clinician_tests_store.delete_case(case_id):
        raise HTTPException(status_code=404, detail="Case not found")
    return {"ok": True}


def _run_clinician_tests(version_id: int):
    _CL_TESTS_STATE["running"] = True
    _CL_TESTS_STATE["version_id"] = version_id
    _CL_TESTS_STATE["error"] = None
    try:
        clinician_tests_runner.run_tests(version_id)
    except Exception as e:
        _CL_TESTS_STATE["error"] = str(e)
    finally:
        _CL_TESTS_STATE["running"] = False


@app.post("/api/clinician-chat-tests/run/{version_id}")
def start_clinician_test_run(version_id: int, background_tasks: BackgroundTasks):
    if _CL_TESTS_STATE["running"]:
        raise HTTPException(status_code=409, detail="A test run is already in progress")
    if chat_config_store.get_version(version_id) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if not clinician_tests_store.list_cases():
        raise HTTPException(status_code=400, detail="No test cases defined")
    background_tasks.add_task(_run_clinician_tests, version_id)
    return {"started": True, "version_id": version_id}


@app.get("/api/clinician-chat-tests/run/status")
def clinician_test_run_status():
    return dict(_CL_TESTS_STATE)


@app.get("/api/clinician-chat-tests/runs/{version_id}")
def get_clinician_test_run(version_id: int):
    run = clinician_tests_store.get_run(version_id)
    if run is None:
        raise HTTPException(status_code=404, detail="No run for this version")
    return run


@app.put("/api/clinician-chat-tests/runs/{version_id}/results/{case_id}")
def override_clinician_test_rating(version_id: int, case_id: int, req: PatientTestRatingRequest):
    if not (1 <= req.score <= 5):
        raise HTTPException(status_code=400, detail="score must be 1..5")
    run = clinician_tests_store.override_rating(version_id, case_id, req.score)
    if run is None:
        raise HTTPException(status_code=404, detail="Run or case not found")
    return run


# --- Triage prompt config (card generation) ---


class TriagePromptConfigRequest(BaseModel):
    system_prompt: str
    label: str = ""


@app.get("/api/triage-prompt-config")
def get_triage_prompt_config():
    return triage_prompt_store.get_active()


@app.get("/api/triage-prompt-config/versions")
def list_triage_prompt_versions():
    return triage_prompt_store.list_versions()


@app.get("/api/triage-prompt-config/versions/{vid}")
def get_triage_prompt_version(vid: int):
    v = triage_prompt_store.get_version(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


@app.post("/api/triage-prompt-config")
def save_triage_prompt_config(req: TriagePromptConfigRequest):
    return triage_prompt_store.save_new(req.system_prompt, req.label)


@app.post("/api/triage-prompt-config/versions/{vid}/activate")
def activate_triage_prompt_version(vid: int):
    v = triage_prompt_store.activate(vid)
    if v is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return v


# --- Corpus management (PDFs + reindex) ---


CORPUS_DIR = Path(__file__).parent.parent.parent / "chatbot" / "corpus"
_REINDEX_STATE: dict = {"running": False, "log": [], "summary": None, "error": None}


@app.get("/api/ops/corpus")
def list_corpus():
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = chatbot_ingest._load_manifest()
    files_meta = manifest.get("files", {})
    out = []
    for p in sorted(CORPUS_DIR.glob("*.pdf")):
        stat = p.stat()
        indexed = p.name in files_meta
        out.append({
            "filename": p.name,
            "size_bytes": stat.st_size,
            "modified_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
            "source_label": chatbot_ingest._source_label(p.name),
            "indexed": indexed,
        })
    return {
        "files": out,
        "index": {
            "chunk_count": manifest.get("chunk_count", 0),
            "embed_model": manifest.get("embed_model", ""),
        },
    }


@app.post("/api/ops/corpus/upload")
async def upload_corpus(files: list[UploadFile] = File(...)):
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = Path(f.filename or "").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"Only PDFs accepted ({name})")
        dest = CORPUS_DIR / name
        dest.write_bytes(await f.read())
        saved.append(name)
    return {"saved": saved}


@app.delete("/api/ops/corpus/{filename}")
def delete_corpus(filename: str):
    name = Path(filename).name
    p = CORPUS_DIR / name
    if not p.exists():
        raise HTTPException(status_code=404, detail="File not found")
    p.unlink()
    return {"ok": True, "filename": name}


def _run_reindex(force: bool) -> None:
    _REINDEX_STATE["running"] = True
    _REINDEX_STATE["log"] = []
    _REINDEX_STATE["summary"] = None
    _REINDEX_STATE["error"] = None
    try:
        summary = chatbot_ingest.reindex(
            force=force,
            progress_cb=lambda msg: _REINDEX_STATE["log"].append(msg),
        )
        _REINDEX_STATE["summary"] = summary
    except Exception as e:
        _REINDEX_STATE["error"] = str(e)
    finally:
        _REINDEX_STATE["running"] = False


@app.post("/api/ops/corpus/reindex")
def start_reindex(force: bool = False, background_tasks: BackgroundTasks = None):
    if _REINDEX_STATE["running"]:
        raise HTTPException(status_code=409, detail="Reindex already in progress")
    background_tasks.add_task(_run_reindex, force)
    return {"started": True, "force": force}


@app.get("/api/ops/corpus/reindex/status")
def reindex_status():
    return {
        "running": _REINDEX_STATE["running"],
        "log": _REINDEX_STATE["log"][-50:],
        "summary": _REINDEX_STATE["summary"],
        "error": _REINDEX_STATE["error"],
    }


# Serve static frontend
FRONTEND_DIR = Path(__file__).parent / "static"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/demo")
    def demo_page():
        return FileResponse(FRONTEND_DIR / "demo.html")

    @app.get("/ops")
    def ops_page():
        return FileResponse(FRONTEND_DIR / "ops.html")

    @app.get("/all")
    def all_page():
        return FileResponse(FRONTEND_DIR / "all.html")
